"""Corrected primary executor methods with inert transport and real tracker state.

These do not import compiled Hummingbot, start a connector, or place venue orders.
Persistence fixtures exercise ordering/failures; a real SQLite helper restart
case runs separately when that helper is present in the build overlay.
"""

import asyncio
import importlib.util
import json
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCHED = ROOT / "deploy/hummingbot_core/patches/order_executor.py"
SPEC = importlib.util.spec_from_file_location(
    "corrected_executor_primary_probe", ROOT / "scripts/probe_limit_lifecycle.py"
)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class Reservation:
    def __init__(self, fail=None):
        self.order_ids, self.snapshots = [], []
        self.uncertain, self.fail = False, fail

    def record_order(self, order_id):
        if self.fail == "record":
            raise OSError("fixture persistence failure")
        self.order_ids.append(order_id)

    def record_snapshot(self, snapshot):
        if self.fail == "snapshot":
            raise OSError("fixture persistence failure")
        self.snapshots.append(snapshot)

    def mark_uncertain(self):
        if self.fail == "uncertain":
            raise OSError("fixture persistence failure")
        self.uncertain = True

    def describe(self):
        if self.fail == "describe":
            raise OSError("fixture persistence failure")
        return {
            "order_ids": self.order_ids[:],
            "uncertain": self.uncertain,
            "last_snapshot": self.snapshots[-1] if self.snapshots else None,
        }


class FixtureAlreadyClaimed(RuntimeError):
    pass


class FixtureFeeFactory:
    @staticmethod
    def from_json(data):
        if data != {"fixture_fee": "zero"}:
            raise ValueError("Unknown inert fee encoding")
        return PROBE.ZeroFeeFixture()


class Claims:
    def __init__(self, fail=None):
        self.claims, self.reservations, self.fail = [], {}, fail

    def __call__(self, **scope):
        self.claims.append(scope)
        if scope["executor_id"] in self.reservations:
            raise FixtureAlreadyClaimed("fixture replay claim denied")
        if self.fail == "claim":
            raise RuntimeError("fixture claim denied")
        reservation = Reservation(self.fail)
        self.reservations[scope["executor_id"]] = reservation
        return reservation

    def find_executor_claim(self, executor_id):
        if executor_id not in self.reservations:
            return None
        return self.read_claim(namespace="executor", attempt_id=executor_id)

    def open_executor_evidence(self, executor_id):
        return self.reservations[executor_id]

    def read_claim(self, *, namespace, attempt_id):
        reservation = self.reservations[attempt_id]
        scope = next(
            scope for scope in self.claims if scope["executor_id"] == attempt_id
        )
        return {
            "scope": {
                "connector_name": scope["connector_name"],
                "trading_pair": scope["trading_pair"],
                "side": scope["side"].name,
                "amount": str(scope["amount"]),
                "price": str(scope["price"]),
            },
            "client_order_ids": reservation.order_ids,
            "snapshot": reservation.snapshots[-1] if reservation.snapshots else None,
            "venue_finality_established": False,
        }


@pytest.fixture
def runtime():
    _, sources = PROBE.verify_source_integrity()
    env = PROBE.primary_environment(sources)
    claims = Claims()
    env["claim_executor"] = claims
    env["AttemptAlreadyClaimed"] = FixtureAlreadyClaimed
    env["read_claim"] = claims.read_claim
    env["find_executor_claim"] = claims.find_executor_claim
    env["open_executor_evidence"] = claims.open_executor_evidence
    env["TradeFeeBase"] = FixtureFeeFactory
    env["json"] = json
    PROBE.compile_primary_classes(
        (ROOT / "deploy/hummingbot_core/patches/client_order_tracker.py").read_text(),
        {"ClientOrderTracker"},
        env,
        "<corrected-client-order-tracker>",
    )
    PROBE.compile_primary_classes(
        PATCHED.read_text(), {"OrderExecutor"}, env, "<corrected-order-executor>"
    )
    # asyncio uses a local AF_UNIX socketpair to wake its event loop. Block
    # external transport operations while allowing that inert framework detail.
    with ExitStack() as trap:
        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.socket.sendto",
            "socket.create_connection",
            "socket.getaddrinfo",
            "subprocess.Popen",
            "urllib.request.urlopen",
        ):
            trap.enter_context(patch(target, PROBE.deny_external_io))
        yield env, claims


def new_executor(runtime, kind="LIMIT", side="BUY", executor_id="corrected-fixture"):
    env, claims = runtime
    strategy = PROBE.InertStrategy(env)
    config = NS(
        id=executor_id,
        connector_name="kraken",
        trading_pair="ALT-USD",
        side=env["TradeType"][side],
        amount=Decimal("10"),
        price=Decimal("1.0174"),
        execution_strategy=env["ExecutionStrategy"][kind],
        position_action=env["PositionAction"].OPEN,
        slippage_pct=Decimal("0.05"),
        max_slippage_pct=Decimal("5"),
        slippage_multiplier=Decimal("5"),
        level_id=None,
        chaser_config=NS(distance=Decimal("0.01"), refresh_threshold=Decimal("0.01")),
    )
    executor = env["OrderExecutor"](strategy, config)
    executor.get_price = lambda *args, **kwargs: Decimal("1.0174")
    strategy.executor = executor
    executor._sleep = PROBE.inert_sleep
    return executor, strategy


def ticks(executor, count=20):
    for _ in range(count):
        asyncio.run(executor.control_task())


@pytest.mark.parametrize("kind", ["LIMIT", "LIMIT_MAKER"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("state", ["CANCELED", "FAILED"])
def test_terminal_zero_fill_keeps_late_fills_and_never_replaces(
    runtime, kind, side, state
):
    ex, st = new_executor(runtime, kind, side)
    ex.place_open_order()
    asyncio.run(PROBE.terminal_update(st, state))
    assert ex._order is None
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    PROBE.fill(st, "fixture-1", "3")
    ticks(ex)
    # Explicit calls and repeated ticks cannot bypass a spent attempt.
    for _ in range(3):
        ex.place_open_order()
        ex.control_order()
    info = ex.get_custom_info()
    assert len(st.submissions) == 1
    assert ex.executed_amount_base == Decimal("3")
    assert ex.filled_amount_quote == Decimal("3.0522")
    assert info["held_position_orders"][0]["executed_amount_base"] == "3"
    assert info["spectra_attempt_order_ids"] == ["fixture-1"]
    assert info["spectra_reconciliation_required"] is True
    assert info["spectra_venue_finality_established"] is False
    assert (
        info["spectra_attempt_reservation"]["last_snapshot"]["executed_amount_base"]
        == "3"
    )


@pytest.mark.parametrize("state", ["CANCELED", "FAILED"])
def test_known_partial_and_delayed_trade_do_not_authorize_remaining(runtime, state):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    PROBE.fill(st, "fixture-1", "3")
    asyncio.run(PROBE.terminal_update(st, state))
    PROBE.fill(st, "fixture-1", "2", "second-trade")
    # Replayed event/trade is not a second accounting entry.
    PROBE.fill(st, "fixture-1", "2", "second-trade")
    ex.process_order_filled_event(None, st, NS(order_id="fixture-1"))
    ticks(ex)
    assert ex.executed_amount_base == Decimal("5")
    assert len(st.submissions) == 1
    assert len(ex._collect_held_position_orders()) == 1
    assert ex._collect_held_position_orders()[0]["executed_amount_base"] == "5"


def test_retained_object_survives_tracker_cache_expiry(runtime):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    asyncio.run(PROBE.terminal_update(st, "CANCELED"))
    retained = ex._spectra_orders["fixture-1"].order
    st._order_tracker._cached_orders.clear()
    ex.update_tracked_order_with_order_id("fixture-1")
    assert ex._spectra_orders["fixture-1"].order is retained
    ticks(ex)
    assert len(st.submissions) == 1
    # This proves retention, not tracker delivery after TTL or venue coverage.
    assert ex.get_custom_info()["spectra_venue_finality_established"] is False


def test_known_full_trade_preserves_normal_position_hold(runtime):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    PROBE.fill(st, "fixture-1", "10")
    ex.process_order_completed_event(None, st, NS(order_id="fixture-1"))
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert ex.close_type == runtime[0]["CloseType"].POSITION_HOLD
    assert ex._held_position_orders[0]["executed_amount_base"] == "10"
    assert ex.get_custom_info()["spectra_venue_finality_established"] is False
    ticks(ex)
    ex.place_open_order()
    assert len(st.submissions) == 1


@pytest.mark.parametrize("filled", ["0", "3", "11"])
def test_completed_event_without_exact_authorized_trade_quantity_quarantines(
    runtime, filled
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    if filled != "0":
        PROBE.fill(st, "fixture-1", filled)
    ex.process_order_completed_event(None, st, NS(order_id="fixture-1"))
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert ex.close_type is None
    ticks(ex)
    assert len(st.submissions) == 1


@pytest.mark.parametrize(
    "failure", ["claim", "record", "snapshot", "describe", "uncertain"]
)
def test_all_persistence_failures_leave_attempt_spent(runtime, failure):
    env, _ = runtime
    claims = Claims(failure)
    env["claim_executor"] = claims
    ex, st = new_executor((env, claims))
    ex.place_open_order()
    if failure == "describe":
        assert ex.get_custom_info()["spectra_attempt_reservation"] is None
    elif failure == "uncertain":
        asyncio.run(PROBE.terminal_update(st, "CANCELED"))
    ticks(ex)
    for _ in range(3):
        ex.place_open_order()
    assert len(st.submissions) == (0 if failure == "claim" else 1)
    assert len(claims.claims) == 1
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN


def test_latch_and_durable_claim_precede_transport_and_reentrant_submission(runtime):
    ex, st = new_executor(runtime)
    native = st.place_order

    def checked_submission(**kwargs):
        assert ex._spectra_attempt_claimed is True
        assert runtime[1].reservations[ex.config.id] is ex._spectra_reservation
        ex.place_open_order()
        return native(**kwargs)

    st.place_order = checked_submission
    ex.place_open_order()
    assert len(st.submissions) == 1
    assert len(runtime[1].claims) == 1


def test_transport_accepted_then_raised_is_quarantined_without_retry(runtime):
    ex, st = new_executor(runtime)
    native = st.place_order

    def accepted_then_raised(**kwargs):
        native(**kwargs)
        raise ConnectionError("fixture ambiguous transport response")

    st.place_order = accepted_then_raised
    ex.place_open_order()
    ticks(ex)
    assert len(st.submissions) == 1
    assert ex._order is None
    assert (
        ex.get_custom_info()["spectra_quarantine_reason"] == "submission_indeterminate"
    )
    assert ex._spectra_reservation.uncertain is True


@pytest.mark.parametrize("order_id", [None, "", 123])
def test_missing_or_invalid_returned_order_id_does_not_unlock(runtime, order_id):
    ex, st = new_executor(runtime)
    calls = []
    st.place_order = lambda **kwargs: calls.append(kwargs) or order_id
    ex.place_open_order()
    ticks(ex)
    assert len(calls) == 1
    assert (
        ex.get_custom_info()["spectra_quarantine_reason"]
        == "submission_missing_order_id"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "replacement-id"),
        ("connector_name", "other"),
        ("trading_pair", "OTHER-USD"),
        ("amount", Decimal("11")),
        ("price", Decimal("2")),
        ("side", "changed-side"),
        ("execution_strategy", "MARKET"),
        ("position_action", "changed-action"),
    ],
)
def test_mutated_authorization_cannot_rebind_original_fixed_limit(
    runtime, field, value
):
    ex, st = new_executor(runtime)
    setattr(ex.config, field, value)
    ex.place_open_order()
    assert len(st.submissions) == 0
    assert runtime[1].claims == []
    assert ex._spectra_attempt_claimed is True
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN


def test_restart_new_executor_with_same_identity_cannot_repeat(runtime):
    original, first_strategy = new_executor(runtime)
    original.place_open_order()
    restarted, second_strategy = new_executor(runtime)
    restarted.place_open_order()
    ticks(restarted)
    assert len(first_strategy.submissions) == 1
    assert second_strategy.submissions == []
    assert (
        restarted.get_custom_info()["spectra_quarantine_reason"]
        == "claim_already_spent_requires_reconciliation"
    )


def test_stop_before_first_attempt_never_submits(runtime):
    ex, st = new_executor(runtime)
    ex.early_stop()
    ticks(ex)
    ex.place_open_order()
    assert st.submissions == []
    assert runtime[1].claims == []
    assert ex.close_type == runtime[0]["CloseType"].EARLY_STOP


def test_indeterminate_shutdown_never_claims_venue_finality_or_resubmits(runtime):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    ex._order.order = None
    ex.early_stop()
    ticks(ex, ex.MAX_SHUTDOWN_TICKS + 5)
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert ex.close_type is None
    assert len(st.submissions) == 1
    assert ex.get_custom_info()["spectra_venue_finality_established"] is False


@pytest.mark.parametrize("kind", ["MARKET", "LIMIT_CHASER"])
def test_non_protected_execution_keeps_original_cancel_replacement_policy(
    runtime, kind
):
    ex, st = new_executor(runtime, kind=kind)
    ex.place_open_order()
    asyncio.run(PROBE.terminal_update(st, "CANCELED"))
    asyncio.run(ex.control_task())
    assert len(st.submissions) == 2
    assert runtime[1].claims == []
    assert ex.get_custom_info()["spectra_single_attempt"] is False


def test_zero_fill_terminal_late_fee_and_quantity_aggregate_once(runtime):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    asyncio.run(PROBE.terminal_update(st, "FAILED"))
    PROBE.fill(st, "fixture-1", "3")
    order = ex._spectra_orders["fixture-1"].order
    fee = next(iter(order.order_fills.values())).fee
    fee.fee_amount_in_token = lambda **kwargs: Decimal("0.25")
    ex.process_order_filled_event(None, st, NS(order_id="fixture-1"))
    info = ex.get_custom_info()
    assert ex.get_cum_fees_quote() == Decimal("0.25")
    assert ex.executed_amount_base == Decimal("3")
    assert (
        info["spectra_attempt_reservation"]["last_snapshot"]["cum_fees_quote"] == "0.25"
    )
    assert len(info["held_position_orders"]) == 1


def test_source_contract_marker_is_explicit_on_module_and_class(runtime):
    import ast

    module = ast.parse(PATCHED.read_text())
    marker = next(
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(name, ast.Name) and name.id == "SPECTRA_EXECUTION_CONTRACT"
            for name in node.targets
        )
    )
    assert marker == "spectra-single-attempt-v1"
    assert runtime[0]["OrderExecutor"].SPECTRA_EXECUTION_CONTRACT == marker


def test_market_config_cannot_be_mutated_into_unguarded_fixed_limit(runtime):
    ex, st = new_executor(runtime, kind="MARKET")
    ex.config.execution_strategy = runtime[0]["ExecutionStrategy"].LIMIT
    ex.place_open_order()
    assert st.submissions == []
    assert runtime[1].claims == []
    assert ex._spectra_attempt_claimed is True
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN


def test_restored_tracked_order_without_claim_requires_reconciliation(runtime):
    ex, st = new_executor(runtime)
    order_id = st.place_order(
        connector_name="kraken",
        trading_pair="ALT-USD",
        order_type=runtime[0]["OrderType"].LIMIT,
        amount=Decimal("10"),
        price=Decimal("1.0174"),
        side=runtime[0]["TradeType"].BUY,
        position_action=runtime[0]["PositionAction"].OPEN,
    )
    ex._order = runtime[0]["TrackedOrder"](order_id=order_id)
    ex.place_open_order()
    assert len(st.submissions) == 1
    assert runtime[1].claims == []
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", Decimal("0")),
        ("amount", Decimal("NaN")),
        ("price", Decimal("-1")),
        ("price", Decimal("Infinity")),
    ],
)
def test_invalid_original_authorization_fails_before_claim(runtime, field, value):
    ex, st = new_executor(runtime)
    setattr(ex.config, field, value)
    ex._spectra_authorization = ex._spectra_config_scope()
    ex.place_open_order()
    assert st.submissions == []
    assert runtime[1].claims == []
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN


def load_real_guard(name):
    import sys

    spec = importlib.util.spec_from_file_location(
        name, ROOT / "deploy/hummingbot_core/patches/spectra_attempt_guard.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_real_journal_restart_preserves_known_late_fill_and_denies_replay(
    runtime, tmp_path, monkeypatch
):
    first_guard = load_real_guard("executor_first_process_journal")
    tmp_path.chmod(0o700)
    journal = tmp_path / "attempts.sqlite3"
    identity = first_guard.initialize_journal(str(journal))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", str(journal))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", identity)
    runtime[0]["claim_executor"] = first_guard.claim_executor
    runtime[0]["AttemptAlreadyClaimed"] = first_guard.AttemptAlreadyClaimed
    runtime[0]["read_claim"] = first_guard.read_claim
    runtime[0]["find_executor_claim"] = first_guard.find_executor_claim
    runtime[0]["open_executor_evidence"] = first_guard.open_executor_evidence
    original, first_strategy = new_executor(runtime)
    original.place_open_order()
    asyncio.run(PROBE.terminal_update(first_strategy, "CANCELED"))
    PROBE.fill(first_strategy, "fixture-1", "3")
    original.get_custom_info()

    # A fresh helper module and fresh executor have no process-memory latch.
    restarted_guard = load_real_guard("executor_restarted_process_journal")
    runtime[0]["claim_executor"] = restarted_guard.claim_executor
    runtime[0]["AttemptAlreadyClaimed"] = restarted_guard.AttemptAlreadyClaimed
    runtime[0]["read_claim"] = restarted_guard.read_claim
    runtime[0]["find_executor_claim"] = restarted_guard.find_executor_claim
    runtime[0]["open_executor_evidence"] = restarted_guard.open_executor_evidence
    restarted, restarted_strategy = new_executor(runtime)
    restarted.place_open_order()
    ticks(restarted)
    persisted = restarted_guard.read_claim(
        namespace="executor", attempt_id=original.config.id
    )
    assert len(first_strategy.submissions) == 1
    assert restarted_strategy.submissions == []
    assert persisted["client_order_ids"] == ["fixture-1"]
    assert persisted["snapshot"]["executed_amount_base"] == "3"
    assert (
        persisted["snapshot"]["held_position_orders"][0]["executed_amount_base"] == "3"
    )
    assert len(persisted["snapshot"]["tracked_orders"][0]["order_fills"]) == 1
    assert persisted["uncertain"] is True
    assert persisted["venue_finality_established"] is False
    info = restarted.get_custom_info()
    assert restarted._order is None
    assert restarted.executed_amount_base == Decimal("3")
    assert restarted.filled_amount_quote == Decimal("3.0522")
    assert info["executed_amount_base"] == Decimal("3")
    assert info["filled_amount_quote"] == Decimal("3.0522")
    assert info["spectra_attempt_order_ids"] == ["fixture-1"]
    assert info["spectra_accounting_available"] is True
    assert info["spectra_accounting_source"] == "durable_snapshot"
    assert info["held_position_orders"][0]["executed_amount_base"] == "3"
    assert list(
        info["spectra_recovered_attempt_evidence"]["snapshot"]["tracked_orders"][0][
            "order_fills"
        ]
    ) == ["trade-1"]
    assert info["spectra_reconciliation_required"] is True
    assert info["spectra_venue_finality_established"] is False


def test_real_missing_journal_never_submits_or_silently_creates_storage(
    runtime, tmp_path, monkeypatch
):
    guard = load_real_guard("executor_missing_process_journal")
    tmp_path.chmod(0o700)
    missing = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", str(missing))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", "1" * 32)
    runtime[0]["claim_executor"] = guard.claim_executor
    runtime[0]["AttemptAlreadyClaimed"] = guard.AttemptAlreadyClaimed
    runtime[0]["read_claim"] = guard.read_claim
    runtime[0]["find_executor_claim"] = guard.find_executor_claim
    runtime[0]["open_executor_evidence"] = guard.open_executor_evidence
    ex, st = new_executor(runtime)
    ex.place_open_order()
    ticks(ex)
    assert st.submissions == []
    assert not missing.exists()
    assert ex.get_custom_info()["spectra_quarantine_reason"] == "claim_denied_or_failed"
    assert ex.get_custom_info()["executed_amount_base"] is None
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        _ = ex.executed_amount_base


def test_serialization_failure_keeps_claim_spent_and_does_not_force_stop(runtime):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    PROBE.fill(st, "fixture-1", "3")

    def unavailable_snapshot():
        raise ValueError("fixture malformed serialized fill")

    ex._order.order.to_json = unavailable_snapshot
    ex.early_stop()
    ticks(ex)
    info = ex.get_custom_info()
    assert len(st.submissions) == 1
    assert ex.executed_amount_base == Decimal("3")
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert info["spectra_venue_finality_established"] is False
    assert info["spectra_quarantine_reason"] == "exposure_snapshot_unavailable"


def test_completed_trade_snapshot_persistence_failure_cannot_force_terminal_hold(
    runtime,
):
    env, _ = runtime
    claims = Claims("snapshot")
    env["claim_executor"] = claims
    ex, st = new_executor((env, claims))
    ex.place_open_order()
    PROBE.fill(st, "fixture-1", "10")
    ex.process_order_completed_event(None, st, NS(order_id="fixture-1"))
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert ex.close_type is None
    ticks(ex)
    assert len(st.submissions) == 1


def test_retained_corrected_tracker_delivers_actual_trade_after_cache_ttl(runtime):
    ex, st = new_executor(runtime)
    clock = [0.0]
    st._order_tracker._cached_orders = PROBE.TTLCache(
        maxsize=1000, ttl=30, timer=lambda: clock[0]
    )
    ex.place_open_order()
    original = st._order_tracker.retain_spectra_order("fixture-1")
    asyncio.run(PROBE.terminal_update(st, "CANCELED"))
    clock[0] = 61.0
    assert st._order_tracker.fetch_cached_order("fixture-1") is None
    assert "fixture-1" not in st._order_tracker.active_orders
    assert "fixture-1" not in st._order_tracker.all_updatable_orders
    assert st._order_tracker.fetch_order(client_order_id="fixture-1") is original
    assert (
        st._order_tracker.all_fillable_orders_by_exchange_order_id["exchange-fixture-1"]
        is original
    )
    PROBE.fill(st, "fixture-1", "3", "post-ttl-trade")
    PROBE.fill(st, "fixture-1", "3", "post-ttl-trade")
    ticks(ex)
    assert ex.executed_amount_base == Decimal("3")
    assert (
        ex.get_custom_info()["held_position_orders"][0]["executed_amount_base"] == "3"
    )
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "3"
    assert len(original.order_fills) == 1
    assert len(st.submissions) == 1


def test_unregistered_orders_still_use_original_ttl_policy(runtime):
    ex, st = new_executor(runtime, kind="MARKET")
    clock = [0.0]
    st._order_tracker._cached_orders = PROBE.TTLCache(
        maxsize=1000, ttl=30, timer=lambda: clock[0]
    )
    ex.place_open_order()
    asyncio.run(PROBE.terminal_update(st, "CANCELED"))
    clock[0] = 61.0
    assert st._order_tracker.fetch_order(client_order_id="fixture-1") is None
    assert "fixture-1" not in st._order_tracker.all_fillable_orders


def test_retention_requires_real_tracked_order_and_cannot_rebind(runtime):
    ex, st = new_executor(runtime)
    tracker = st._order_tracker
    with pytest.raises(ValueError, match="existing tracked"):
        tracker.retain_spectra_order("absent")
    ex.place_open_order()
    original = tracker.retain_spectra_order("fixture-1")
    assert tracker.retain_spectra_order("fixture-1") is original
    duplicate = runtime[0]["InFlightOrder"].from_json(original.to_json())
    with pytest.raises(ValueError, match="cannot be rebound"):
        tracker.start_tracking_order(duplicate)
    tracker.MAX_RETAINED_SPECTRA_ORDERS = 1
    ex2, st2 = new_executor(runtime, executor_id="other-executor")
    ex2.place_open_order()
    other = st2._order_tracker.fetch_order(client_order_id="fixture-1")
    other.client_order_id = "fixture-2"
    tracker.start_tracking_order(other)
    with pytest.raises(ValueError, match="capacity"):
        tracker.retain_spectra_order("fixture-2")


@pytest.mark.parametrize(
    "corruption", ["missing", "wrong_quantity", "wrong_pair", "oversize", "no_trade"]
)
def test_replay_missing_or_corrupt_snapshot_returns_unknown_metrics(
    runtime, corruption
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    PROBE.fill(st, "fixture-1", "3")
    ex.get_custom_info()
    reservation = runtime[1].reservations[ex.config.id]
    if corruption == "missing":
        reservation.snapshots = []
    elif corruption == "wrong_quantity":
        reservation.snapshots[-1]["executed_amount_base"] = "4"
    elif corruption == "wrong_pair":
        reservation.snapshots[-1]["tracked_orders"][0]["trading_pair"] = "OTHER-USD"
    elif corruption == "oversize":
        reservation.snapshots[-1]["padding"] = "x" * 70000
    elif corruption == "no_trade":
        reservation.snapshots[-1]["tracked_orders"][0]["order_fills"] = {}
    restarted, restarted_strategy = new_executor(runtime)
    restarted.place_open_order()
    ticks(restarted)
    info = restarted.get_custom_info()
    assert restarted_strategy.submissions == []
    assert info["spectra_accounting_available"] is False
    assert info["spectra_accounting_source"] == "unavailable"
    assert info["executed_amount_base"] is None
    assert info["spectra_venue_finality_established"] is False
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        _ = restarted.executed_amount_base


@pytest.mark.parametrize("corruption", ["missing", "quantity", "oversize"])
def test_real_sqlite_snapshot_corruption_cannot_report_zero_exposure(
    runtime, tmp_path, monkeypatch, corruption
):
    import sqlite3

    guard = load_real_guard("executor_corrupted_snapshot_journal")
    tmp_path.chmod(0o700)
    journal = tmp_path / "attempts.sqlite3"
    identity = guard.initialize_journal(str(journal))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", str(journal))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", identity)
    runtime[0]["claim_executor"] = guard.claim_executor
    runtime[0]["AttemptAlreadyClaimed"] = guard.AttemptAlreadyClaimed
    runtime[0]["read_claim"] = guard.read_claim
    runtime[0]["find_executor_claim"] = guard.find_executor_claim
    runtime[0]["open_executor_evidence"] = guard.open_executor_evidence
    original, st = new_executor(runtime)
    original.place_open_order()
    PROBE.fill(st, "fixture-1", "3")
    original.get_custom_info()
    with sqlite3.connect(journal) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM snapshots ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0]
        )
        if corruption == "missing":
            db.execute("DELETE FROM snapshots")
        else:
            payload["executed_amount_base"] = (
                "4" if corruption == "quantity" else "x" * 70000
            )
            db.execute("UPDATE snapshots SET payload=?", (json.dumps(payload),))
        db.commit()
    restarted, restarted_strategy = new_executor(runtime)
    restarted.place_open_order()
    ticks(restarted)
    info = restarted.get_custom_info()
    assert restarted_strategy.submissions == []
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert info["filled_amount_quote"] is None
    assert info["spectra_venue_finality_established"] is False
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        _ = restarted.filled_amount_quote


def test_unresolved_initial_order_id_exposes_unavailable_until_matching_reference_links(
    runtime,
):
    ex, st = new_executor(runtime)
    real_lookup = ex.get_in_flight_order
    ex.get_in_flight_order = lambda *_args: None
    ex.place_open_order()
    PROBE.fill(st, "fixture-1", "3")
    ticks(ex, 3)
    info = ex.get_custom_info()
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert info["filled_amount_quote"] is None
    assert ex._spectra_reservation.snapshots == []
    assert len(st.submissions) == 1
    assert st.cancels == []  # Normal asynchronous registration is not canceled.
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        _ = ex.executed_amount_base
    ex.get_in_flight_order = real_lookup
    ex.process_order_created_event(None, st, NS(order_id="fixture-1"))
    assert ex.status == PROBE.RunnableStatus.RUNNING
    assert ex.executed_amount_base == Decimal("3")
    assert ex.get_custom_info()["spectra_accounting_available"] is True
    ticks(ex, 3)
    assert len(st.submissions) == 1
    assert st.cancels == []


def test_lookup_failure_for_unresolved_initial_id_cannot_advertise_zero(runtime):
    ex, st = new_executor(runtime)

    def broken_lookup(*_args):
        raise RuntimeError("fixture missing tracker")

    ex.get_in_flight_order = broken_lookup
    ex.place_open_order()
    info = ex.get_custom_info()
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    ticks(ex, 3)
    assert len(st.submissions) == 1


def test_wrong_initial_reference_scope_cannot_become_accounting_evidence(runtime):
    ex, st = new_executor(runtime)
    original = st.place_order

    def corrupt_reference(**kwargs):
        order_id = original(**kwargs)
        st._order_tracker.fetch_order(client_order_id=order_id).trading_pair = (
            "OTHER-USD"
        )
        return order_id

    st.place_order = corrupt_reference
    ex.place_open_order()
    info = ex.get_custom_info()
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert info["spectra_quarantine_reason"] == "order_reference_scope_mismatch"
    ticks(ex, 3)
    assert len(st.submissions) == 1


def test_protected_full_hold_keeps_callbacks_and_journals_late_fill_without_polling(
    runtime,
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "10")
    ex.process_order_completed_event(None, st, NS(order_id="fixture-1"))
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert ex.close_type == runtime[0]["CloseType"].POSITION_HOLD
    assert st.executor_callbacks_attached is True
    PROBE.fill(st, "fixture-1", "1", "late-fill-after-full")
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "11"
    assert ex._held_position_orders[0]["executed_amount_base"] == "11"
    ticks(ex, 3)
    assert len(st.submissions) == 1
    assert st.cancels == []


@pytest.mark.parametrize("kind", ["MARKET", "LIMIT_CHASER"])
def test_spent_protected_identity_cannot_switch_to_unprotected_strategy(runtime, kind):
    original, st = new_executor(runtime)
    original.place_open_order()
    PROBE.fill(st, "fixture-1", "3")
    replayed, replayed_strategy = new_executor(runtime, kind=kind)
    replayed.place_open_order()
    ticks(replayed, 3)
    assert replayed_strategy.submissions == []
    assert replayed.executed_amount_base == Decimal("3")
    assert replayed.get_custom_info()["spectra_accounting_source"] == "durable_snapshot"
    assert replayed.get_custom_info()["spectra_reconciliation_required"] is True


@pytest.mark.parametrize("kind", ["MARKET", "LIMIT_CHASER"])
def test_unprotected_job_with_unavailable_prior_identity_history_cannot_submit(
    runtime, kind
):
    def unavailable_history(_executor_id):
        raise RuntimeError("fixture unreadable journal history")

    runtime[0]["find_executor_claim"] = unavailable_history
    ex, st = new_executor(runtime, kind=kind)
    ex.place_open_order()
    ticks(ex, 3)
    assert st.submissions == []
    assert ex.get_custom_info()["executed_amount_base"] is None
    assert (
        ex.get_custom_info()["spectra_quarantine_reason"]
        == "prior_claim_lookup_unavailable"
    )


def ambiguous_trade(
    env,
    order_id="fixture-1",
    exchange_id="exchange-fixture-1",
    pair="ALT-USD",
    trade_id="ambiguous-fill",
):
    return env["TradeUpdate"](
        trade_id=trade_id,
        client_order_id=order_id,
        exchange_order_id=exchange_id,
        trading_pair=pair,
        fill_timestamp=9,
        fill_price=Decimal("1.0174"),
        fill_base_amount=Decimal("3"),
        fill_quote_amount=Decimal("3.0522"),
        fee=PROBE.ZeroFeeFixture(),
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        "exchange",
        "pair",
        "client",
        "missing_known_exchange",
        "missing_trade_exchange",
        "missing_trade_id",
    ],
)
def test_retained_foreign_or_incomplete_signature_never_mutates_financial_truth(
    runtime, mismatch
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    values = dict(
        exchange_id="exchange-fixture-1",
        pair="ALT-USD",
        order_id="fixture-1",
        trade_id="foreign-fill",
    )
    if mismatch == "exchange":
        values["exchange_id"] = "foreign-exchange-order"
    elif mismatch == "pair":
        values["pair"] = "OTHER-USD"
    elif mismatch == "client":
        values["order_id"] = "foreign-client-order"
    elif mismatch == "missing_known_exchange":
        order.exchange_order_id = None
    elif mismatch == "missing_trade_exchange":
        values["exchange_id"] = None
    elif mismatch == "missing_trade_id":
        values["trade_id"] = ""
    st._order_tracker.process_trade_update(ambiguous_trade(runtime[0], **values))
    assert order.executed_amount_base == Decimal("0")
    assert order.order_fills == {}
    ticks(ex, 3)
    info = ex.get_custom_info()
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert info["spectra_accounting_uncertainty_reason"]
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is False
    assert len(st.submissions) == 1
    assert st.cancels == []


def configure_real_journal(runtime, tmp_path, monkeypatch, name):
    guard = load_real_guard(name)
    tmp_path.chmod(0o700)
    journal = tmp_path / "attempts.sqlite3"
    identity = guard.initialize_journal(str(journal))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", str(journal))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", identity)
    runtime[0].update(
        claim_executor=guard.claim_executor,
        AttemptAlreadyClaimed=guard.AttemptAlreadyClaimed,
        read_claim=guard.read_claim,
        find_executor_claim=guard.find_executor_claim,
        open_executor_evidence=guard.open_executor_evidence,
    )
    return guard


def test_real_ambiguous_signature_keeps_valid_late_fills_and_unknown_flag_after_restart(
    runtime, tmp_path, monkeypatch
):
    guard = configure_real_journal(
        runtime, tmp_path, monkeypatch, "executor_ambiguous_signature_journal"
    )
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "3")
    st._order_tracker.process_trade_update(
        ambiguous_trade(runtime[0], exchange_id="foreign-order")
    )
    ticks(ex, 3)
    assert ex.get_custom_info()["executed_amount_base"] is None
    PROBE.fill(st, "fixture-1", "1", "valid-late-fill-during-quarantine")
    # A valid matching trade remains financial evidence while ambiguity cannot
    # regain budget authority or lose its durable unavailable flag.
    assert order.executed_amount_base == Decimal("4")
    persisted = guard.read_claim(namespace="executor", attempt_id=ex.config.id)
    assert persisted["snapshot"]["executed_amount_base"] == "4"
    assert persisted["snapshot"]["accounting_available"] is False
    assert len(persisted["snapshot"]["tracked_orders"][0]["order_fills"]) == 2
    fresh = load_real_guard("executor_ambiguous_signature_journal_restarted")
    runtime[0].update(
        claim_executor=fresh.claim_executor,
        AttemptAlreadyClaimed=fresh.AttemptAlreadyClaimed,
        read_claim=fresh.read_claim,
        find_executor_claim=fresh.find_executor_claim,
        open_executor_evidence=fresh.open_executor_evidence,
    )
    restarted, restarted_strategy = new_executor(runtime)
    restarted.place_open_order()
    ticks(restarted, 3)
    info = restarted.get_custom_info()
    assert restarted_strategy.submissions == []
    assert info["executed_amount_base"] is None
    assert info["spectra_accounting_available"] is False
    assert info["held_position_orders"][0]["executed_amount_base"] == "4"
    assert (
        info["spectra_recovered_attempt_evidence"]["snapshot"]["accounting_available"]
        is False
    )
    assert st.cancels == []


@pytest.mark.parametrize("kind", ["MARKET", "LIMIT_CHASER"])
def test_real_journal_spent_fixed_limit_identity_blocks_strategy_switch(
    runtime, tmp_path, monkeypatch, kind
):
    configure_real_journal(
        runtime, tmp_path, monkeypatch, "executor_strategy_switch_journal"
    )
    first, first_strategy = new_executor(runtime)
    first.place_open_order()
    PROBE.fill(first_strategy, "fixture-1", "3")
    restarted, restarted_strategy = new_executor(runtime, kind=kind)
    restarted.place_open_order()
    ticks(restarted, 3)
    assert restarted_strategy.submissions == []
    assert restarted.executed_amount_base == Decimal("3")
    assert restarted.get_custom_info()["spectra_reconciliation_required"] is True


@pytest.mark.parametrize("mismatch", ["exchange", "pair", "client", "empty_exchange"])
def test_retained_foreign_status_cannot_mutate_state_or_hide_known_fills(
    runtime, mismatch
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "3")
    previous_state = order.current_state
    values = dict(
        client_order_id="fixture-1",
        exchange_order_id="exchange-fixture-1",
        trading_pair="ALT-USD",
        update_timestamp=99,
        new_state=runtime[0]["OrderState"].CANCELED,
    )
    if mismatch == "exchange":
        values["exchange_order_id"] = "FOREIGN-ORDER"
    elif mismatch == "pair":
        values["trading_pair"] = "OTHER-USD"
    elif mismatch == "client":
        values["client_order_id"] = "foreign-client"
    else:
        values["exchange_order_id"] = ""
    asyncio.run(
        st._order_tracker._process_order_update(runtime[0]["OrderUpdate"](**values))
    )
    assert order.current_state == previous_state
    assert order.executed_amount_base == Decimal("3")
    ticks(ex, 3)
    info = ex.get_custom_info()
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "3"
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is False
    PROBE.fill(st, "fixture-1", "1", "valid-after-foreign-status")
    assert order.executed_amount_base == Decimal("4")
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "4"
    assert len(st.submissions) == 1
    assert st.cancels == []


def test_retained_local_missing_venue_id_update_preserves_native_failure_lifecycle(
    runtime,
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    update = runtime[0]["OrderUpdate"](
        client_order_id="fixture-1",
        trading_pair="ALT-USD",
        update_timestamp=99,
        new_state=runtime[0]["OrderState"].FAILED,
    )
    asyncio.run(st._order_tracker._process_order_update(update))
    assert order.current_state == runtime[0]["OrderState"].FAILED
    assert st._order_tracker.spectra_order_ambiguity("fixture-1") is None
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    PROBE.fill(st, "fixture-1", "3")
    assert ex.executed_amount_base == Decimal("3")
    assert ex.get_custom_info()["spectra_venue_finality_established"] is False
    assert len(st.submissions) == 1


def test_retained_validated_creation_can_bind_initial_unknown_venue_id(runtime):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    order.exchange_order_id = None
    update = runtime[0]["OrderUpdate"](
        client_order_id="fixture-1",
        exchange_order_id="exchange-fixture-1",
        trading_pair="ALT-USD",
        update_timestamp=99,
        new_state=runtime[0]["OrderState"].OPEN,
    )
    asyncio.run(st._order_tracker._process_order_update(update))
    assert order.exchange_order_id == "exchange-fixture-1"
    assert st._order_tracker.spectra_order_ambiguity("fixture-1") is None
    PROBE.fill(st, "fixture-1", "3")
    assert ex.executed_amount_base == Decimal("3")


@pytest.mark.parametrize(
    "field,value",
    [
        ("fill_base_amount", Decimal("-3")),
        ("fill_base_amount", Decimal("NaN")),
        ("fill_quote_amount", Decimal("NaN")),
        ("fill_quote_amount", Decimal("Infinity")),
        ("fill_quote_amount", Decimal("3")),
        ("fill_price", Decimal("NaN")),
        ("fill_price", Decimal("-1")),
        ("fill_price", "1.0174"),
    ],
)
def test_protected_trade_geometry_is_rejected_before_financial_mutation(
    runtime, field, value
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "3")
    malformed = ambiguous_trade(runtime[0])._replace(**{field: value})
    st._order_tracker.process_trade_update(malformed)
    assert order.executed_amount_base == Decimal("3")
    assert order.executed_amount_quote == Decimal("3.0522")
    assert len(order.order_fills) == 1
    ticks(ex, 3)
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "3"
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is False
    assert len(st.submissions) == 1
    assert st.cancels == []


@pytest.mark.parametrize("fee_value", [Decimal("NaN"), Decimal("-1"), "0", None])
def test_protected_malformed_or_unsupported_fee_cannot_become_known_zero(
    runtime, fee_value
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "3")
    fee = NS(fee_amount_in_token=lambda **_kwargs: fee_value)
    st._order_tracker.process_trade_update(
        ambiguous_trade(runtime[0])._replace(fee=fee)
    )
    assert order.executed_amount_base == Decimal("3")
    assert len(order.order_fills) == 1
    ticks(ex, 3)
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "3"
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is False


@pytest.mark.parametrize("kind", ["LIMIT", "MARKET", "LIMIT_CHASER"])
def test_startup_recovers_spent_identity_before_insufficient_budget_can_stop_it(
    runtime, kind
):
    first, st = new_executor(runtime)
    first.place_open_order()
    PROBE.fill(st, "fixture-1", "3")
    restarted, restarted_strategy = new_executor(runtime, kind=kind)
    budget_calls = []
    runtime[0]["OrderCandidate"] = NS
    restarted.is_perpetual_connector = lambda *_args: False
    restarted.adjust_order_candidates = lambda *_args: (
        budget_calls.append("called") or [NS(amount=Decimal("0"))]
    )
    asyncio.run(restarted.validate_sufficient_balance())
    assert budget_calls == []
    assert restarted.executed_amount_base == Decimal("3")
    assert restarted.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert (
        restarted.get_custom_info()["spectra_accounting_source"] == "durable_snapshot"
    )
    ticks(restarted, 3)
    assert restarted_strategy.submissions == []


@pytest.mark.parametrize("kind", ["LIMIT", "MARKET"])
def test_startup_fresh_identity_keeps_native_budget_validation_without_spending_claim(
    runtime, kind
):
    ex, st = new_executor(runtime, kind=kind)
    runtime[0]["OrderCandidate"] = NS
    ex.is_perpetual_connector = lambda *_args: False
    ex.adjust_order_candidates = lambda *_args: [NS(amount=Decimal("0"))]
    asyncio.run(ex.validate_sufficient_balance())
    assert ex.close_type == runtime[0]["CloseType"].INSUFFICIENT_BALANCE
    assert ex.status == PROBE.RunnableStatus.TERMINATED
    assert runtime[1].claims == []
    assert st.submissions == []


def test_startup_unavailable_claim_history_blocks_before_balance_validation(runtime):
    ex, st = new_executor(runtime, kind="MARKET")

    def unavailable_history(_executor_id):
        raise RuntimeError("fixture unreadable startup journal")

    runtime[0]["find_executor_claim"] = unavailable_history
    asyncio.run(ex.validate_sufficient_balance())
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    assert ex.get_custom_info()["executed_amount_base"] is None
    assert st.submissions == []


def test_administrative_stop_checkpoints_known_evidence_before_detaching_callbacks(
    runtime,
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "3")
    reservation = ex._spectra_reservation
    assert reservation.snapshots[-1]["executed_amount_base"] == "3"
    # Simulate an actual tracked update just before a shutdown callback; stop
    # must checkpoint the current reference instead of relying on API polling.
    st.executor_callbacks_attached = False
    PROBE.fill(st, "fixture-1", "2", "known-before-service-shutdown")
    assert reservation.snapshots[-1]["executed_amount_base"] == "3"
    ex.stop()
    assert reservation.snapshots[-1]["executed_amount_base"] == "5"
    assert reservation.snapshots[-1]["venue_finality_established"] is False
    assert reservation.uncertain is True
    assert st.executor_callbacks_attached is False
    assert ex.status == PROBE.RunnableStatus.TERMINATED
    ex.place_open_order()
    assert len(st.submissions) == 1
    restarted, restarted_strategy = new_executor(runtime)
    restarted.place_open_order()
    assert restarted.executed_amount_base == Decimal("5")
    assert restarted_strategy.submissions == []


def status_with_watermark(runtime, strategy, state, amount):
    strategy._order_tracker.TRADE_FILLS_WAIT_TIMEOUT = 0.001
    runtime[0]["BuyOrderCompletedEvent"] = lambda *args: NS(order_id=args[1])
    runtime[0]["SellOrderCompletedEvent"] = lambda *args: NS(order_id=args[1])
    update = runtime[0]["OrderUpdate"](
        client_order_id="fixture-1",
        exchange_order_id="exchange-fixture-1",
        trading_pair="ALT-USD",
        update_timestamp=99,
        new_state=runtime[0]["OrderState"][state],
        misc_updates={"spectra_executed_amount_base": amount},
    )
    asyncio.run(strategy._order_tracker._process_order_update(update))


@pytest.mark.parametrize(
    "state,amount", [("OPEN", "3"), ("CANCELED", "3"), ("FILLED", "10")]
)
def test_trusted_cumulative_status_fill_lag_is_reversible_and_retains_maximum(
    runtime, state, amount
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    st._order_tracker.retain_spectra_order("fixture-1")
    status_with_watermark(runtime, st, state, Decimal(amount))
    info = ex.get_custom_info()
    assert st._order_tracker.spectra_order_fill_lag("fixture-1") is True
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert (
        info["spectra_accounting_uncertainty_reason"] == "venue_trade_details_pending"
    )
    assert st._order_tracker.spectra_order_ambiguity("fixture-1") is None
    assert ex._spectra_reservation.snapshots[-1]["venue_fill_watermarks"] == {
        "fixture-1": amount
    }
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is False
    status_with_watermark(runtime, st, state, Decimal("0"))
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    assert st._order_tracker.spectra_order_fill_watermark("fixture-1") == Decimal(
        amount
    )
    PROBE.fill(st, "fixture-1", "1", "first-catchup-trade")
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    PROBE.fill(st, "fixture-1", str(Decimal(amount) - 1), "remaining-catchup-trade")
    later = ex.get_custom_info()
    assert st._order_tracker.spectra_order_fill_lag("fixture-1") is False
    assert later["spectra_accounting_available"] is True
    assert later["executed_amount_base"] == Decimal(amount)
    assert later["spectra_accounting_uncertainty_reason"] is None
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is True
    assert ex.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert later["spectra_venue_finality_established"] is False
    ex.place_open_order()
    assert len(st.submissions) == 1
    assert st.cancels == []


@pytest.mark.parametrize(
    "value", [Decimal("NaN"), Decimal("-1"), Decimal("Infinity"), [], True]
)
def test_malformed_cumulative_status_watermark_keeps_prior_truth_and_sticky_unknown(
    runtime, value
):
    ex, st = new_executor(runtime)
    ex.place_open_order()
    order = st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "3")
    previous_state = order.current_state
    status_with_watermark(runtime, st, "CANCELED", value)
    assert order.current_state == previous_state
    assert order.executed_amount_base == Decimal("3")
    assert st._order_tracker.spectra_order_fill_watermark("fixture-1") is None
    ticks(ex, 3)
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    assert ex._spectra_reservation.snapshots[-1]["executed_amount_base"] == "3"
    assert ex._spectra_reservation.snapshots[-1]["accounting_available"] is False
    PROBE.fill(st, "fixture-1", "1", "valid-after-malformed-watermark")
    assert order.executed_amount_base == Decimal("4")
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    assert len(st.submissions) == 1


def test_real_sqlite_pending_cumulative_status_lag_survives_restart_without_zero_claim(
    runtime, tmp_path, monkeypatch
):
    guard = configure_real_journal(
        runtime, tmp_path, monkeypatch, "executor_status_lag_journal"
    )
    ex, st = new_executor(runtime)
    ex.place_open_order()
    st._order_tracker.retain_spectra_order("fixture-1")
    PROBE.fill(st, "fixture-1", "1")
    status_with_watermark(runtime, st, "CANCELED", Decimal("3"))
    assert ex.get_custom_info()["spectra_accounting_available"] is False
    saved = guard.read_claim(namespace="executor", attempt_id=ex.config.id)["snapshot"]
    assert saved["executed_amount_base"] == "1"
    assert saved["venue_fill_watermarks"] == {"fixture-1": "3"}
    assert saved["accounting_uncertainty_reason"] == "venue_trade_details_pending"
    fresh = load_real_guard("executor_status_lag_journal_restarted")
    runtime[0].update(
        claim_executor=fresh.claim_executor,
        AttemptAlreadyClaimed=fresh.AttemptAlreadyClaimed,
        read_claim=fresh.read_claim,
        find_executor_claim=fresh.find_executor_claim,
        open_executor_evidence=fresh.open_executor_evidence,
    )
    restarted, restarted_strategy = new_executor(runtime)
    asyncio.run(restarted.validate_sufficient_balance())
    info = restarted.get_custom_info()
    assert restarted_strategy.submissions == []
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert info["held_position_orders"][0]["executed_amount_base"] == "1"
    assert info["spectra_venue_fill_watermarks"] == {"fixture-1": "3"}
    assert restarted._spectra_tracker_ambiguity is None
    # Only actual same-ID trade evidence can catch up; restore remains an
    # accounting-only reference and never registers an active backend order.
    recovered = restarted._spectra_orders["fixture-1"].order
    actual_trade = ambiguous_trade(
        runtime[0], trade_id="restart-reconciled-trade"
    )._replace(fill_base_amount=Decimal("2"), fill_quote_amount=Decimal("2.0348"))
    assert recovered.update_with_trade_update(actual_trade) is True
    later = restarted.get_custom_info()
    assert later["spectra_accounting_available"] is True
    assert later["executed_amount_base"] == Decimal("3")
    assert restarted._order is None
    assert restarted_strategy._order_tracker.active_orders == {}
    assert restarted.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert later["spectra_venue_finality_established"] is False
    checkpoint = fresh.read_claim(namespace="executor", attempt_id=restarted.config.id)[
        "snapshot"
    ]
    assert checkpoint["executed_amount_base"] == "3"
    assert checkpoint["venue_fill_watermarks"] == {"fixture-1": "3"}
    assert checkpoint["accounting_available"] is True
    assert checkpoint["accounting_uncertainty_reason"] == ""
    assert len(checkpoint["tracked_orders"][0]["order_fills"]) == 2
    assert not hasattr(restarted._spectra_reservation, "record_order")
    assert not hasattr(restarted._spectra_reservation, "record_exchange_order")
    restarted.place_open_order()
    assert restarted_strategy.submissions == []


def test_existing_accounting_handle_failure_preserves_known_refs_as_unavailable(
    runtime,
):
    first, strategy = new_executor(runtime)
    first.place_open_order()
    PROBE.fill(strategy, "fixture-1", "3")

    def unavailable_evidence(_executor_id):
        raise RuntimeError("fixture corrupt existing-claim evidence handle")

    runtime[0]["open_executor_evidence"] = unavailable_evidence
    restarted, restarted_strategy = new_executor(runtime)
    asyncio.run(restarted.validate_sufficient_balance())
    info = restarted.get_custom_info()
    assert info["spectra_accounting_available"] is False
    assert info["executed_amount_base"] is None
    assert info["held_position_orders"][0]["executed_amount_base"] == "3"
    assert info["spectra_venue_finality_established"] is False
    assert restarted.status == PROBE.RunnableStatus.SHUTTING_DOWN
    assert restarted._order is None
    assert restarted._spectra_reservation is None
    restarted.place_open_order()
    assert restarted_strategy.submissions == []
