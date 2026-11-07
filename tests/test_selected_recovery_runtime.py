"""Enabled helper integration against real core ledgers and fake exchange I/O."""

import asyncio
import copy
import sqlite3
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction as F
from types import SimpleNamespace

import pytest

from spectra_core.accounting_health import (
    AccountingHealthDecision,
    AccountingHealthState,
)
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    BreakerMode,
    CapitalProposal,
    CapitalSnapshot,
    IntentKind,
)
from spectra_core.selected_recovery_runtime import (
    SelectedRecoveryRuntime,
    SelectedRecoveryRuntimePolicy,
    run_selected_recovery_cycle,
)
from tests.test_selected_recovery import LADDER, Harness, decimal

D = Decimal


def health():
    return AccountingHealthDecision(
        "parent",
        AccountingHealthState.HEALTHY,
        1000,
        1000,
        1000,
        30,
        1,
        1,
        1,
        1,
        1,
        1,
        (),
        (),
        ("confirmed",),
    )


def rule():
    return KrakenTradingRule(
        "XRP-USD", D(".001"), D(1), D(0), D(".0001"), D(".001"), D(".0001"), True, True
    )


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(self.value)


class FakeExchange:
    def __init__(self, h):
        self.executors = self.session = self
        self.base_url = "https://fake.invalid"
        self.created = []
        self.orders, self.trades = {}, {}
        self.fee = D(".08")
        self.sell_fraction = F(1)
        self.buy_fraction = F(1)
        self.timeout_after_create = False
        self.source_hook = None
        self.seed(
            "exec:entry",
            "order:entry",
            "original",
            "BUY",
            D(10),
            D("9.96"),
            D(".4"),
            created=2,
            filled_at=3,
        )

    @staticmethod
    def iso(value):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    def seed(
        self,
        executor,
        order,
        trade,
        side,
        amount,
        price,
        fee,
        *,
        created=999,
        filled_at=1000,
        fraction=F(1),
    ):
        filled = decimal(F(amount) * fraction)
        self.orders[executor] = [
            {
                "executor_id": executor,
                "client_order_id": order,
                "order_type": "LIMIT",
                "trade_type": side,
                "amount": str(amount),
                "price": str(price),
                "status": "FILLED" if fraction == 1 else "CANCELED",
                "filled_amount": str(filled),
                "average_fill_price": str(price),
                "created_at": self.iso(created),
                "updated_at": self.iso(filled_at),
            }
        ]
        self.trades[executor] = [
            {
                "executor_id": executor,
                "trade_id": trade,
                "order_id": order,
                "account_name": "account",
                "connector_name": "kraken",
                "trading_pair": "XRP-USD",
                "trade_type": side,
                "amount": str(filled),
                "price": str(price),
                "fee_paid": str(fee),
                "fee_currency": "USD",
                "timestamp": self.iso(filled_at),
            }
        ]

    async def get_executor(self, executor):
        if self.source_hook:
            hook, self.source_hook = self.source_hook, None
            hook()
        if executor not in self.orders:
            raise TimeoutError("lookup unavailable")
        rows = self.trades[executor]
        gross = sum((D(r["amount"]) * D(r["price"]) for r in rows), D(0))
        fees = sum((D(r["fee_paid"]) for r in rows), D(0))
        return {
            "id": executor,
            "status": "TERMINATED",
            "close_type": "POSITION_HOLD",
            "executor_type": "dca_executor",
            "filled_amount_quote": str(gross),
            "cum_fees_quote": str(fees),
            "custom_info": {"position_size_quote": str(gross)},
        }

    async def get(self, url):
        executor, surface = url.split("/executors/")[1].rsplit("/", 1)
        rows = (self.orders if surface == "orders" else self.trades).get(executor)
        if rows is None:
            raise TimeoutError("unknown execution is not absence proof")
        return Response({"executor_id": executor, "count": len(rows), surface: rows})

    async def create_executor(self, *, executor_config, account_name, controller_id):
        assert account_name == "account" and controller_id == "controller"
        cfg = copy.deepcopy(executor_config)
        self.created.append(cfg)
        executor = cfg["id"]
        if cfg["side"] == 1:
            price = D(cfg["prices"][0])
            amount = D(cfg["amounts_quote"][0]) / price
            side, fee, fraction = (
                "BUY",
                self.fee if self.buy_fraction else D(0),
                self.buy_fraction,
            )
        else:
            price, amount = D(cfg["price"]), D(cfg["amount"])
            side, fraction = "SELL", self.sell_fraction
            fee = decimal(F(amount) * fraction * F(price) * F(".004"))
        self.seed(
            executor,
            "order:" + executor,
            "trade:" + executor,
            side,
            amount,
            price,
            fee,
            fraction=fraction,
        )
        if fraction == 0:
            self.trades[executor] = []
        if self.timeout_after_create:
            raise TimeoutError("response lost after create")
        return {"executor_id": executor}


def setup(tmp_path, *, enabled=True, count=5):
    h = Harness(tmp_path, count=count)
    now = [1000.0]
    runtime = SelectedRecoveryRuntime(
        book=h.b,
        ledger=h.ledger,
        governor=h.g,
        policy=SelectedRecoveryRuntimePolicy(enabled=enabled),
        clock=lambda: now[0],
        breaker=lambda: BreakerMode.NORMAL,
    )
    return h, runtime, FakeExchange(h), now


def reserve(h, name, amount):
    proposal = CapitalProposal(name, "XRP-USD", IntentKind.RECOVERY, D(amount))
    assert h.g.reserve(proposal, CapitalSnapshot(D(10000))).approved


def tick(h, runtime, client, *, price=8, child="a", proposal=None, fee=".08", **kwargs):
    return asyncio.run(
        run_selected_recovery_cycle(
            client,
            runtime=runtime,
            child_id=child,
            ladder=LADDER,
            current_price=D(price),
            market_observed_at=1000,
            rule=kwargs.pop("rule", rule()),
            health=kwargs.pop("health", health()),
            remaining_authorized_quote=D(1000),
            proposal_id=proposal,
            buy_fee_reserve_quote=D(fee),
            expected_exit_cost_bps=D(40),
            evidence_allowed=True,
            **kwargs,
        )
    )


def test_enabled_planner_dispatch_reconcile_native_exit_then_recycle_next_child(
    tmp_path,
):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    assert not h.b.snapshot()["split"]
    result = tick(h, runtime, client, proposal="patch1")
    assert result["completed"] and result["action"] == "BUY"
    assert h.b.snapshot()["split"]
    assert h.ledger.snapshot("parent").active_base == D("12.49")
    assert len(client.created) == 1
    assert client.created[0]["amounts_quote"] == ["19.92"]
    result = tick(h, runtime, client, price=10)
    assert result["completed"] and result["action"] == "SELL"
    original = h.ledger.get_lot("trade-fill:original")
    assert (
        original.base_remaining,
        original.cost_basis_remaining_quote,
        original.entry_fees_remaining_quote,
    ) == (D(8), D("79.68"), D(".32"))
    assert h.b.snapshot()["selected"] is None
    assert F(h.b.snapshot()["cash"]["PRODUCTIVE"]) == F("19.92")
    assert F(h.b.snapshot()["cash"]["VCAP"]) == F("144.8004")
    with sqlite3.connect(h.g.db_path) as conn:
        assert (
            conn.execute(
                "SELECT amount_quote FROM recovery_recycle_releases WHERE proposal_id='patch1'"
            ).fetchone()[0]
            == "19.92"
        )
    reserve(h, "patch2", 20)
    assert tick(h, runtime, client, child="b", proposal="patch2")["completed"]
    assert h.b.snapshot()["selected"] == "b"
    assert h.b.snapshot()["initial_cash"]["VCAP"] == "140"


def test_default_off_has_no_dispatch_claim_or_economic_mutation(tmp_path):
    h, runtime, client, _ = setup(tmp_path, enabled=False)
    reserve(h, "patch1", 20)
    before = h.b.snapshot()
    assert tick(h, runtime, client, proposal="patch1")["executed"] is False
    assert not client.created and runtime.active() is None
    assert h.b.snapshot() == before
    assert h.ledger.selected_recovery_lock_owner("parent") is None


def test_missing_or_mismatched_claim_never_dispatches(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="RESERVED"):
        tick(h, runtime, client, proposal="missing")
    reserve(h, "too-small", 19)
    with pytest.raises(ValueError, match="RESERVED"):
        tick(h, runtime, client, proposal="too-small")
    assert not client.created


def test_market_or_accounting_stale_after_await_blocks_dispatch(tmp_path):
    h, runtime, client, now = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.source_hook = lambda: now.__setitem__(0, 1031)
    with pytest.raises(ValueError, match="fresh"):
        tick(h, runtime, client, proposal="patch1")
    assert not client.created
    assert h.g.get_decision("patch1").status.value == "RESERVED"


def test_competing_native_exit_and_other_proposals_block_under_lock(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    with pytest.raises(ValueError, match="owns exact"):
        h.ledger.record_partial_exit(
            event_id="competing",
            position_id="parent",
            base_sold=D(1),
            gross_proceeds_quote=D(8),
            occurred_at=1001,
        )
    assert h.ledger.selected_recovery_proposal_allowed("XRP-USD", "patch1")
    assert not h.ledger.selected_recovery_proposal_allowed("XRP-USD", "unrelated")


def test_timeout_after_create_resumes_exact_executor_without_duplicate_send(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.timeout_after_create = True
    with pytest.raises(TimeoutError):
        tick(h, runtime, client, proposal="patch1")
    assert runtime.active()["phase"] == "UNCERTAIN"
    assert not h.b.snapshot()["split"]
    client.timeout_after_create = False
    restarted = SelectedRecoveryRuntime(
        book=h.b,
        ledger=h.ledger,
        governor=h.g,
        policy=SelectedRecoveryRuntimePolicy(enabled=True),
        clock=lambda: 1000,
        breaker=lambda: BreakerMode.NORMAL,
    )
    assert tick(h, restarted, client, proposal="patch1")["completed"]
    assert len(client.created) == 1
    assert h.b.snapshot()["selected"] == "a"


def test_unknown_dispatch_remains_pending_without_blind_retry(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.timeout_after_create = True
    with pytest.raises(TimeoutError):
        tick(h, runtime, client, proposal="patch1")
    client.orders.clear()
    client.trades.clear()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="unavailable"):
            tick(h, runtime, client, proposal="patch1")
    assert len(client.created) == 1
    assert runtime.active()["phase"] == "UNCERTAIN"


def test_partial_sale_native_book_remainders_and_restart_then_full_exit(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    client.sell_fraction = F(1, 2)
    assert tick(h, runtime, client, price=10)["completed"]
    assert h.b.snapshot()["selected"] == "a"
    assert h.ledger.get_lot("trade-fill:original").base_remaining == D(9)
    assert h.ledger.snapshot("parent").active_base == D("10.245")
    client.sell_fraction = F(1)
    assert tick(h, runtime, client, price=10)["completed"]
    assert h.b.snapshot()["selected"] is None
    assert h.ledger.get_lot("trade-fill:original").base_remaining == D(8)


def test_crash_after_native_sale_recovers_owner_settlement_once(tmp_path, monkeypatch):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    original_ingest = h.b.ingest_sale_fill
    monkeypatch.setattr(
        h.b,
        "ingest_sale_fill",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash after native sale")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        tick(h, runtime, client, price=10)
    assert h.ledger.get_lot("trade-fill:original").base_remaining == D(8)
    monkeypatch.setattr(h.b, "ingest_sale_fill", original_ingest)
    assert tick(h, runtime, client, price=10)["completed"]
    assert len(client.created) == 2
    assert F(h.b.snapshot()["cash"]["PRODUCTIVE"]) == F("19.92")
    with sqlite3.connect(h.g.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM recovery_recycle_releases").fetchone()[0]
            == 1
        )


def test_same_asset_committed_without_any_lot_blocks_selected_dispatch(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    reserve(h, "unresolved_other_buy", 10)
    h.g.commit("unresolved_other_buy")
    with pytest.raises(ValueError, match="unresolved"):
        tick(h, runtime, client, proposal="patch1")
    assert not client.created
    assert not h.b.snapshot()["split"]


def test_post_commit_late_native_inventory_change_blocks_send(tmp_path, monkeypatch):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    original = h.g.commit_if_matches

    def late_fill(proposal):
        accepted = original(proposal)
        h.ledger.record_base_fill(
            position_id="parent",
            lot_id="late",
            pair="XRP-USD",
            base_amount=D(1),
            cost_basis_quote=D(10),
            occurred_at=1000,
        )
        return accepted

    monkeypatch.setattr(h.g, "commit_if_matches", late_fill)
    with pytest.raises(ValueError, match="membership"):
        tick(h, runtime, client, proposal="patch1")
    assert not client.created
    assert runtime.active() is None
    assert h.ledger.selected_recovery_lock_owner("parent") is None


def test_forged_quantity_plan_is_rejected_before_dispatch(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "small", 1)
    plan = replace(h.plan(), amount_quote=F(1))
    with pytest.raises(ValueError, match="altered"):
        asyncio.run(
            runtime.dispatch(
                client,
                plan=plan,
                rule=rule(),
                health=health(),
                market_observed_at=1000,
                proposal_id="small",
            )
        )
    assert not client.created


def test_all_children_closed_releases_lock_and_allows_next_parent(tmp_path):
    h, runtime, client, _ = setup(tmp_path, count=2)
    client.fee = D(".2")
    for index, child in enumerate("ab"):
        reserve(h, f"patch{index}", 50)
        assert tick(
            h, runtime, client, child=child, proposal=f"patch{index}", fee=".2"
        )["completed"]
        assert tick(h, runtime, client, child=child, price=10)["completed"]
    assert h.ledger.snapshot("parent").status.value == "CLOSED"
    assert h.ledger.selected_recovery_lock_owner("parent") is None
    assert h.ledger.selected_recovery_proposal_allowed("XRP-USD", "new-entry")
    h.ledger.open_position(position_id="next-parent", pair="XRP-USD", created_at=1001)


def test_native_partial_decimal_allocations_preserve_every_original_remainder(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    # An arbitrary partial fill needs non-terminating pro-rata ratios. The native
    # receipt retains finite allocations and every unconsumed source residual.
    client.sell_fraction = F(1, 449)
    assert tick(h, runtime, client, price=10)["completed"]
    state = h.b.snapshot()
    original = h.ledger.get_lot("trade-fill:original")
    children_original_base = sum(
        (
            F(lot["base"])
            for lots in state["children"].values()
            for lot in lots
            if lot["id"] == "trade-fill:original"
        ),
        F(0),
    )
    assert F(original.base_remaining) == children_original_base
    assert all(F(state["children"][child][0]["base"]) == 2 for child in "bcde")
    assert state["selected"] == "a"
    client.sell_fraction = F(1)
    assert tick(h, runtime, client, price=10)["completed"]
    assert h.ledger.get_lot("trade-fill:original").base_remaining == D(8)


def test_disable_blocks_new_dispatch_but_reconciles_authorized_inflight(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.timeout_after_create = True
    with pytest.raises(TimeoutError):
        tick(h, runtime, client, proposal="patch1")
    runtime.policy = SelectedRecoveryRuntimePolicy(enabled=False)
    h.b.enabled = False
    assert tick(h, runtime, client, proposal="patch1")["completed"]
    assert len(client.created) == 1
    assert tick(h, runtime, client, price=10)["executed"] is False
    assert len(client.created) == 1


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_trade",
        "unknown_order",
        "filled_mismatch",
        "false_terminal",
        "duplicate_order",
    ],
)
def test_malformed_executor_evidence_does_not_split_or_mutate_inventory(
    tmp_path, corruption
):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.timeout_after_create = True
    with pytest.raises(TimeoutError):
        tick(h, runtime, client, proposal="patch1")
    executor = runtime.active()["executor_id"]
    if corruption == "duplicate_trade":
        client.trades[executor].append(copy.deepcopy(client.trades[executor][0]))
    elif corruption == "unknown_order":
        client.trades[executor][0]["order_id"] = "unknown"
    elif corruption == "filled_mismatch":
        client.orders[executor][0]["filled_amount"] = "1"
    elif corruption == "false_terminal":
        client.orders[executor][0]["filled_amount"] = "1"
        client.trades[executor][0]["amount"] = "1"
    else:
        client.orders[executor].append(copy.deepcopy(client.orders[executor][0]))
    with pytest.raises((ValueError, RuntimeError)):
        tick(h, runtime, client, proposal="patch1")
    assert not h.b.snapshot()["split"]
    assert h.ledger.snapshot("parent").active_base == 10
    assert len(client.created) == 1


def test_nonactive_parent_cannot_acquire_selected_dispatch_lock(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    with sqlite3.connect(h.ledger.path) as conn:
        conn.execute(
            "UPDATE economic_positions SET status='DUST' WHERE position_id='parent'"
        )
    with pytest.raises(ValueError, match="ACTIVE"):
        tick(h, runtime, client, proposal="patch1")
    assert not client.created


def test_sealed_gross_basis_avoids_fee_capacity_leak_and_tracks_partial_exit(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    assert h.g.current_recovery_exposure_quote() == D("19.92")
    client.sell_fraction = F(1, 2)
    tick(h, runtime, client, price=10)
    assert h.g.current_recovery_exposure_quote() == D("9.96")
    client.sell_fraction = F(1)
    tick(h, runtime, client, price=10)
    assert h.g.current_recovery_exposure_quote() == 0
    with sqlite3.connect(h.g.db_path) as conn:
        assert conn.execute(
            "SELECT gross_basis_quote,entry_fee_quote FROM selected_recovery_sealed_basis WHERE proposal_id='patch1'"
        ).fetchone() == ("19.92", "0.08")
        assert sum(
            (
                D(row[0])
                for row in conn.execute(
                    "SELECT amount_quote FROM recovery_recycle_releases WHERE proposal_id='patch1'"
                )
            ),
            D(0),
        ) == D("19.92")


def test_no_sealed_receipt_until_source_executor_is_passive_terminal(
    tmp_path, monkeypatch
):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    original = client.get_executor

    async def running_patch(executor):
        result = await original(executor)
        if executor != "exec:entry":
            result.update(status="RUNNING", close_type=None)
        return result

    monkeypatch.setattr(client, "get_executor", running_patch)
    assert tick(h, runtime, client, proposal="patch1")["pending"]
    with sqlite3.connect(h.g.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM selected_recovery_sealed_basis"
            ).fetchone()[0]
            == 0
        )
    assert h.b.snapshot()["pending"] is not None
    monkeypatch.setattr(client, "get_executor", original)
    assert tick(h, runtime, client, proposal="patch1")["completed"]
    assert h.g.current_recovery_exposure_quote() == D("19.92")
    assert len(client.created) == 1


def test_sealed_basis_scope_or_counter_drift_restores_conservative_fallback(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    assert h.g.current_recovery_exposure_quote() == D("19.92")
    with sqlite3.connect(h.g.db_path) as conn:
        conn.execute(
            "UPDATE selected_recovery_sealed_basis SET account_name='wrong' WHERE proposal_id='patch1'"
        )
    assert h.g.current_recovery_exposure_quote() == D(20)


def test_sealed_receipt_replay_and_conflicting_totals(tmp_path):
    from mcp_servers.hummingbot_api.tools.executor_provenance import (
        fetch_executor_order_snapshot,
        fetch_executor_trade_snapshot,
    )
    from spectra_core.executor_accounting import (
        executor_accounting_observation_from_raw,
    )

    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    tick(h, runtime, client, proposal="patch1")
    executor = client.created[0]["id"]
    orders = asyncio.run(fetch_executor_order_snapshot(client, executor_id=executor))
    trades = asyncio.run(fetch_executor_trade_snapshot(client, executor_id=executor))
    observation = executor_accounting_observation_from_raw(
        asyncio.run(client.get_executor(executor)), expected_executor_id=executor
    )
    kwargs = dict(
        ledger=h.ledger,
        book=h.b,
        owner_id=runtime.owner_id,
        proposal_id="patch1",
        order=replace(orders.orders[0], average_fill_price=None, updated_at=None),
        fills=trades.trades,
        observation=observation,
    )
    h.g.seal_selected_recovery_opening_basis(**kwargs)
    with pytest.raises(ValueError, match="inconsistent"):
        h.g.seal_selected_recovery_opening_basis(
            **{**kwargs, "observation": replace(observation, cum_fees_quote=D(".09"))}
        )
    assert h.g.current_recovery_exposure_quote() == D("19.92")


def test_first_zero_fill_cancellation_releases_unsplit_parent_lock(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.buy_fraction = F(0)
    assert tick(h, runtime, client, proposal="patch1")["completed"]
    assert not h.b.snapshot()["split"]
    assert h.b.snapshot()["pending"] is None
    assert h.ledger.selected_recovery_lock_owner("parent") is None
    assert h.g.current_recovery_exposure_quote() == 0
    h.ledger.record_partial_exit(
        event_id="ordinary-resumes",
        position_id="parent",
        base_sold=D(1),
        gross_proceeds_quote=D(10),
        occurred_at=1001,
    )
    assert h.ledger.get_lot("trade-fill:original").base_remaining == D(9)


def test_stale_initial_admission_releases_transient_lock_without_dispatch(tmp_path):
    h, runtime, client, now = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.source_hook = lambda: now.__setitem__(0, 1040)
    with pytest.raises(ValueError, match="fresh"):
        tick(h, runtime, client, proposal="patch1")
    assert not client.created and runtime.active() is None
    assert h.ledger.selected_recovery_lock_owner("parent") is None
    assert not h.b.snapshot()["split"]


def test_failed_initial_commit_releases_transient_lock(tmp_path, monkeypatch):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    monkeypatch.setattr(h.g, "commit_if_matches", lambda proposal: False)
    assert tick(h, runtime, client, proposal="patch1")["executed"] is False
    assert not client.created and runtime.active() is None
    assert h.ledger.selected_recovery_lock_owner("parent") is None


def test_uncertain_initial_send_does_not_release_transient_lock(tmp_path):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    client.timeout_after_create = True
    with pytest.raises(TimeoutError):
        tick(h, runtime, client, proposal="patch1")
    assert not h.b.snapshot()["split"]
    assert h.ledger.selected_recovery_lock_owner("parent") == runtime.owner_id
    runtime.release_unactivated_lock()
    assert h.ledger.selected_recovery_lock_owner("parent") == runtime.owner_id


@pytest.mark.parametrize(
    "bad", ["duplicate_order", "duplicate_trade", "false_terminal"]
)
def test_invalid_passive_source_evidence_prevents_selected_send(tmp_path, bad):
    h, runtime, client, _ = setup(tmp_path)
    reserve(h, "patch1", 20)
    if bad == "duplicate_order":
        client.orders["exec:entry"].append(
            copy.deepcopy(client.orders["exec:entry"][0])
        )
    elif bad == "duplicate_trade":
        client.trades["exec:entry"].append(
            copy.deepcopy(client.trades["exec:entry"][0])
        )
    else:
        client.orders["exec:entry"][0]["amount"] = "11"
    with pytest.raises((ValueError, RuntimeError)):
        tick(h, runtime, client, proposal="patch1")
    assert not client.created
    assert h.ledger.selected_recovery_lock_owner("parent") is None
