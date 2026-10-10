"""Offline safety checks for new partial dispatch and interrupted retries."""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D

import pytest
from aiohttp import ClientResponseError
from tests.test_partial_recovery_recycling import (
    FakeClient,
    governor,
    ledger,
    prepare_execution,
    rule,
)

import mcp_servers.hummingbot_api.tools.partial_recycle as tool
import spectra_core.partial_recycle as partial_core
from spectra_core.economic_exit import create_or_load_position_exit_manifest
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.fill_reconciliation import TradeFillEvidence
from spectra_core.models import BreakerMode, IntentKind
from spectra_core.partial_recycle import (
    PartialRecycleAction,
    PartialRecyclePolicy,
    PartialRecycleStore,
    assess_partial_recycle_dispatch,
    partial_dispatch_terminal_certificate,
    plan_partial_recycle,
)


def make_inventory(root, *, base="10", entry_fees="0"):
    g, book = governor(root), ledger(root)
    prepare_execution(
        g,
        proposal_id="recovery-profit",
        executor_id="recovery-profit-exec",
        intent=IntentKind.RECOVERY,
        amount="4",
    )
    book.record_vcap_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="recovery-profit-lot",
        proposal_id="recovery-profit",
        base_amount=D(base),
        cost_basis_quote=D("4"),
        entry_fees_quote=D(entry_fees),
        occurred_at=1020,
    )
    return g, book


def make_case(root, *, base="10", entry_fees="0", costs="0", target="174"):
    g, book = make_inventory(root, base=base, entry_fees=entry_fees)
    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D(costs),
        slippage_allowance_bps=D(0),
        now=2000,
        policy=PartialRecyclePolicy(target_net_profit_bps=D(target)),
    )
    assert decision.action == PartialRecycleAction.RECYCLE
    return g, book, decision


@pytest.fixture
def case(tmp_path):
    return make_case(tmp_path)


def empty_evidence(monkeypatch):
    async def empty_orders(client, *, executor_id):
        return ExecutorOrderSnapshot(executor_id=executor_id, orders=())

    async def empty_trades(client, *, executor_id):
        return ExecutorTradeSnapshot(executor_id=executor_id, trades=())

    monkeypatch.setattr(tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(tool, "fetch_executor_order_snapshot", empty_orders)
    monkeypatch.setattr(tool, "fetch_executor_trade_snapshot", empty_trades)


@pytest.fixture
def offline(monkeypatch):
    empty_evidence(monkeypatch)


CURRENT_OBSERVATION = object()


def execute(
    case,
    client,
    *,
    price="0.50",
    trading_rule=None,
    costs=None,
    observed_at=CURRENT_OBSERVATION
):
    g, book, decision = case
    return asyncio.run(
        tool.execute_partial_recycle(
            client,
            decision=decision,
            governor=g,
            ledger=book,
            trading_rule=trading_rule or rule(),
            current_price=D(price),
            expected_exit_cost_bps=costs,
            market_observed_at=(
                time.time() if observed_at is CURRENT_OBSERVATION else observed_at
            ),
            now=2010,
        )
    )


@pytest.mark.parametrize("price", ["0.39", "0.405"])
def test_stale_manifest_cannot_newly_dispatch_loss_or_subfloor(case, offline, price):
    client = FakeClient()
    result = execute(case, client, price=price)
    assert result["executed"] is False
    assert "current_partial_recycle_profit_floor_not_met" in result["reasons"]
    assert client.executors.created == []
    assert not PartialRecycleStore(case[1].path).dispatch_claimed(
        case[2].manifest.manifest_id
    )


def test_profitable_new_dispatch_uses_venue_step_and_keeps_residue(tmp_path, offline):
    case = make_case(tmp_path, base="10.04")
    client = FakeClient()
    result = execute(case, client)
    assert result["dispatch_state"] == "NEW_DISPATCH_CONFIRMED"
    assert len(client.executors.created) == 1
    assert D(client.executors.created[0][0]["amount"]) == D("10.0")
    # Empty fake evidence cannot erase owned residue or realize imaginary P&L.
    assert case[1].get_lot("recovery-profit-lot").base_remaining == D("10.04")


def test_reprices_exact_remaining_basis_and_entry_fees(tmp_path):
    g, book, decision = make_case(tmp_path, entry_fees="0.20")
    book.record_partial_exit(
        event_id="prior-partial",
        position_id="xrp-position-1",
        base_sold=D(4),
        gross_proceeds_quote=D(2),
        occurred_at=2001,
        eligible_lot_ids=decision.manifest.lot_ids,
    )
    check = assess_partial_recycle_dispatch(
        decision.manifest,
        ledger=book,
        governor=g,
        trading_rule=rule(),
        current_price=D("0.45"),
        expected_exit_cost_bps=D(50),
    )
    assert check.sell_base == D(6)
    assert check.cost_basis_quote == D("2.4")
    assert check.entry_fees_quote == D("0.12")
    assert check.estimated_net_pnl_quote == D("0.1665")
    assert check.allowed is True


def test_higher_current_cost_can_block_but_lower_cost_cannot_relax_manifest(tmp_path):
    g, book, decision = make_case(tmp_path, costs="500")
    kwargs = dict(ledger=book, governor=g, trading_rule=rule(), current_price=D("0.43"))
    lower = assess_partial_recycle_dispatch(
        decision.manifest, expected_exit_cost_bps=D(0), **kwargs
    )
    higher = assess_partial_recycle_dispatch(
        decision.manifest, expected_exit_cost_bps=D(1000), **kwargs
    )
    assert lower.expected_exit_cost_bps == D(500)
    assert lower.allowed is True
    assert higher.allowed is False


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity"])
def test_invalid_current_price_fails_before_any_create(case, offline, price):
    client = FakeClient()
    with pytest.raises(ValueError):
        execute(case, client, price=price)
    assert client.executors.created == []


@pytest.mark.parametrize("costs", [D(-1), D("NaN"), D("Infinity"), D(10000)])
def test_invalid_current_cost_fails_before_any_create(case, offline, costs):
    client = FakeClient()
    with pytest.raises(ValueError):
        execute(case, client, costs=costs)
    assert client.executors.created == []


@pytest.mark.parametrize(
    "changed",
    [
        dict(min_base_amount_increment=D(0)),
        dict(min_order_size=D("NaN")),
    ],
)
def test_invalid_venue_geometry_fails_closed(case, offline, changed):
    client = FakeClient()
    with pytest.raises(ValueError):
        execute(case, client, trading_rule=replace(rule(), **changed))
    assert client.executors.created == []


@pytest.mark.parametrize(
    "changed,reason",
    [
        (dict(min_order_size=D(11)), "current_exchange_minimum_blocks_recycle"),
        (dict(min_notional_size=D(6)), "current_exchange_minimum_blocks_recycle"),
        (dict(supports_market_orders=False), "current_market_order_not_supported"),
    ],
)
def test_current_venue_minimum_and_support_gate_new_dispatch(
    case, offline, changed, reason
):
    client = FakeClient()
    result = execute(case, client, trading_rule=replace(rule(), **changed))
    assert result["executed"] is False
    assert reason in result["reasons"]
    assert client.executors.created == []


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("not found"),
        TimeoutError("lookup timed out"),
        ClientResponseError(None, (), status=503, message="unavailable"),
    ],
)
def test_lookup_failure_never_means_executor_absent(case, offline, failure):
    client = FakeClient()

    async def unknown(executor_id):
        raise failure

    client.executors.get_executor = unknown
    result = execute(case, client)
    assert result["dispatch_state"] == "LOOKUP_UNKNOWN_REQUIRES_RECONCILIATION"
    assert client.executors.created == []


@pytest.mark.parametrize("response", [None, {}, {"id": "wrong-id"}])
def test_ambiguous_lookup_payload_does_not_create(case, offline, response):
    client = FakeClient()

    async def ambiguous(executor_id):
        return response

    client.executors.get_executor = ambiguous
    result = execute(case, client)
    assert result["dispatch_state"] == "LOOKUP_UNKNOWN_REQUIRES_RECONCILIATION"
    assert client.executors.created == []


def test_existing_executor_reconciles_when_red_halted_below_minima(
    case, offline, monkeypatch
):
    client = FakeClient()
    client.executors.recycle_executor_id = case[2].manifest.sell_executor_id
    monkeypatch.setattr(tool, "get_breaker_mode", lambda: BreakerMode.HALTED)
    result = execute(
        case,
        client,
        price="0.01",
        trading_rule=replace(
            rule(), min_order_size=D(99), supports_market_orders=False
        ),
        observed_at=None,
    )
    assert result["dispatch_state"] == "EXISTING_RECONCILED"
    assert result["result"]["remaining_base"] == "10"
    assert client.executors.created == []


def test_halted_breaker_blocks_only_new_dispatch(case, offline, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(tool, "get_breaker_mode", lambda: BreakerMode.HALTED)
    result = execute(case, client)
    assert result["executed"] is False
    assert result["reasons"] == ["breaker_halted_new_recycle_dispatch"]
    assert client.executors.created == []


def test_create_timeout_claim_survives_restart_and_even_later_404(case, offline):
    client = FakeClient()
    attempts = []

    async def timeout_create(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("ambiguous create")

    client.executors.create_executor = timeout_create
    first = execute(case, client)
    assert first["dispatch_state"] == "AMBIGUOUS_REQUIRES_RECONCILIATION"
    assert len(attempts) == 1
    restarted = FakeClient()
    second = execute(case, restarted)
    assert second["dispatch_state"] == "CLAIMED_REQUIRES_RECONCILIATION"
    assert restarted.executors.created == []
    store = PartialRecycleStore(case[1].path)
    assert store.dispatch_claimed(case[2].manifest.manifest_id)


def test_ambiguous_returned_identity_cannot_retry_create(case, offline):
    client = FakeClient()

    async def wrong_identity(**kwargs):
        client.executors.created.append(kwargs)
        return {"executor_id": "unexpected"}

    client.executors.create_executor = wrong_identity
    assert (
        execute(case, client)["dispatch_state"] == "AMBIGUOUS_REQUIRES_RECONCILIATION"
    )
    assert len(client.executors.created) == 1
    assert (
        execute(case, FakeClient())["dispatch_state"]
        == "CLAIMED_REQUIRES_RECONCILIATION"
    )


def test_dispatch_claim_is_atomic_across_stores(case):
    path, mid = case[1].path, case[2].manifest.manifest_id
    first, second = PartialRecycleStore(path), PartialRecycleStore(path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda store: store.claim_dispatch(
                    case[2].manifest,
                    now=2010,
                    ledger=case[1],
                    governor=case[0],
                    trading_rule=rule(),
                    current_price=D("0.50"),
                    market_observed_at=time.time(),
                ),
                [first, second],
            )
        )
    assert sum(result is not None and result.allowed for result in results) == 1
    assert sum(result is None for result in results) == 1


def test_explicit_custom_target_is_persisted_and_immutable(tmp_path, offline):
    case = make_case(tmp_path, target="100")
    store = PartialRecycleStore(case[1].path)
    assert store.load(case[2].manifest.manifest_id).target_net_profit_bps == D(100)
    assert execute(case, FakeClient(), price="0.405")["executed"] is True
    altered = replace(
        case[2], manifest=replace(case[2].manifest, target_net_profit_bps=D(0))
    )
    with pytest.raises(RuntimeError, match="durable manifest changed"):
        execute((case[0], case[1], altered), FakeClient())


def test_legacy_schema_migration_preserves_manifest_with_default174(case):
    path, mid = case[1].path, case[2].manifest.manifest_id
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE partial_recycle_manifests DROP COLUMN target_net_profit_bps"
        )
    reopened = PartialRecycleStore(path).load(mid)
    assert reopened.target_net_profit_bps == D(174)
    assert reopened.lot_ids == ("recovery-profit-lot",)
    assert reopened.target_cost_basis_quote == D(4)


def terminal_client(
    case, monkeypatch, *, filled="10", status="FILLED", amount="10", volume=None
):
    manifest = case[2].manifest
    client = FakeClient()
    original_get = client.executors.get_executor

    async def get_executor(executor_id):
        raw = await original_get(executor_id)
        if executor_id == client.executors.recycle_executor_id:
            raw = dict(
                raw,
                status="TERMINATED",
                close_type="COMPLETED",
                filled_amount_quote=str(
                    D(filled) * D("0.50") if volume is None else D(volume)
                ),
            )
        return raw

    client.executors.get_executor = get_executor
    order = ExecutorOrderEvidence(
        executor_id=manifest.sell_executor_id,
        order_id="terminal-sell-order",
        order_type="MARKET",
        side="SELL",
        amount=D(amount),
        price=None,
        status=status,
        filled_amount=D(filled),
        average_fill_price=D("0.50"),
        created_at=2010,
        updated_at=2011,
    )
    trade = TradeFillEvidence(
        trade_id="terminal-trade",
        order_id="terminal-sell-order",
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        side="SELL",
        amount_base=D(filled),
        price=D("0.50"),
        fee_paid=D(0),
        fee_currency="USD",
        occurred_at=2011,
    )

    async def orders(client, *, executor_id):
        return ExecutorOrderSnapshot(executor_id=executor_id, orders=(order,))

    async def trades(client, *, executor_id):
        return ExecutorTradeSnapshot(
            executor_id=executor_id, trades=(() if D(filled) == 0 else (trade,))
        )

    monkeypatch.setattr(tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(tool, "fetch_executor_trade_snapshot", trades)
    return client


def test_quantized_terminal_fill_keeps_residue_and_cannot_redispatch(
    tmp_path, offline, monkeypatch
):
    case = make_case(tmp_path, base="10.04")
    client = terminal_client(case, monkeypatch)
    first = execute(case, client)
    assert first["result"]["completed"] is False
    assert first["result"]["terminal"] is True
    store = PartialRecycleStore(case[1].path)
    assert store.load(case[2].manifest.manifest_id).status.value == "TERMINAL"
    assert store.terminal_proof(case[2].manifest.manifest_id) is not None
    lot = case[1].get_lot("recovery-profit-lot")
    assert lot.base_remaining == D("0.04")
    assert lot.cost_basis_remaining_quote > D(0)
    second = execute(case, client, price="0.01")
    assert second["dispatch_state"] == "EXISTING_RECONCILED"
    assert len(client.executors.created) == 1
    assert case[1].get_lot("recovery-profit-lot").base_remaining == D("0.04")


def test_certified_terminal_partial_allows_later_whole_exit(case, offline, monkeypatch):
    client = terminal_client(case, monkeypatch, filled="4", status="CANCELED")
    result = execute(case, client)
    assert result["result"]["terminal"] is True
    assert result["result"]["remaining_base"] == "6"
    whole = create_or_load_position_exit_manifest(
        ledger=case[1],
        governor=case[0],
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        now=2020,
    )
    assert whole.target_active_base == D(6)
    assert whole.target_cost_basis_quote == D("2.4")


@pytest.mark.parametrize(
    "changes",
    [
        dict(filled="4", status="FILLED"),
        dict(filled="4", status="CANCELED", volume="3"),
    ],
)
def test_incomplete_terminal_evidence_never_releases_whole_exit_interlock(
    case, offline, monkeypatch, changes
):
    client = terminal_client(case, monkeypatch, **changes)
    result = execute(case, client)
    assert result["result"]["terminal"] is False
    assert (
        PartialRecycleStore(case[1].path).terminal_proof(case[2].manifest.manifest_id)
        is None
    )
    with pytest.raises(ValueError, match="partial"):
        create_or_load_position_exit_manifest(
            ledger=case[1],
            governor=case[0],
            position_id="xrp-position-1",
            trading_rule=rule(),
            current_price=D("0.50"),
            now=2020,
        )


def test_terminal_certificate_explicit_attached_schema_ignores_main_collision(
    case, offline, monkeypatch, tmp_path
):
    execute(case, terminal_client(case, monkeypatch, filled="4", status="CANCELED"))
    with sqlite3.connect(tmp_path / "unrelated-main.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE partial_recycle_dispatch_claims(manifest_id TEXT, terminal_reconciled_at REAL, terminal_proof_json TEXT)"
        )
        connection.execute("ATTACH DATABASE ? AS economic_ledger", (str(case[1].path),))
        proof = partial_dispatch_terminal_certificate(
            connection, case[2].manifest.manifest_id, schema="economic_ledger"
        )
        assert proof["executor_status"] == "TERMINATED"
        assert (
            partial_dispatch_terminal_certificate(
                connection, case[2].manifest.manifest_id, schema="main"
            )
            is None
        )


def test_tampered_terminal_proof_fails_closed(case, offline, monkeypatch):
    execute(case, terminal_client(case, monkeypatch, filled="4", status="CANCELED"))
    with sqlite3.connect(case[1].path) as connection:
        connection.execute(
            "UPDATE partial_recycle_dispatch_claims SET terminal_proof_json = '{}' WHERE manifest_id = ?",
            (case[2].manifest.manifest_id,),
        )
    with pytest.raises(RuntimeError, match="terminal proof"):
        PartialRecycleStore(case[1].path).terminal_proof(case[2].manifest.manifest_id)


@pytest.mark.parametrize(
    "observed_at,reason",
    [
        (None, "market_observation_missing"),
        (969.9, "market_observation_stale_or_future"),
        (1000.1, "market_observation_stale_or_future"),
    ],
)
def test_missing_stale_or_future_market_observation_blocks_claim(
    case, offline, monkeypatch, observed_at, reason
):
    monkeypatch.setattr(partial_core, "wall_time", lambda: 1000.0)
    client = FakeClient()
    result = execute(case, client, observed_at=observed_at)
    assert result["executed"] is False
    assert reason in result["reasons"]
    assert client.executors.created == []
    assert not PartialRecycleStore(case[1].path).dispatch_claimed(
        case[2].manifest.manifest_id
    )


@pytest.mark.parametrize("observed_at", [float("nan"), float("inf"), 0, True])
def test_invalid_market_timestamp_never_claims(case, offline, observed_at):
    client = FakeClient()
    with pytest.raises(ValueError, match="market_observed_at"):
        execute(case, client, observed_at=observed_at)
    assert client.executors.created == []
    assert not PartialRecycleStore(case[1].path).dispatch_claimed(
        case[2].manifest.manifest_id
    )


def test_passive_source_await_cannot_use_market_older_than30_seconds(
    case, offline, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(partial_core, "wall_time", lambda: clock[0])

    async def slow_sources(client, manifest):
        clock[0] = 1030.1
        return True, ()

    monkeypatch.setattr(tool, "_sources_are_passive", slow_sources)
    client = FakeClient()
    result = execute(case, client, observed_at=1000.0)
    assert result["executed"] is False
    assert "market_observation_stale_or_future" in result["reasons"]
    assert client.executors.created == []


def test_final_atomic_guard_recalculates_ownership_changed_during_await(
    case, offline, monkeypatch
):
    async def source_changes_inventory(client, manifest):
        case[1].record_partial_exit(
            event_id="concurrent-confirmed-sale",
            position_id=manifest.position_id,
            base_sold=D(4),
            gross_proceeds_quote=D(2),
            occurred_at=2005,
            eligible_lot_ids=manifest.lot_ids,
        )
        return True, ()

    monkeypatch.setattr(tool, "_sources_are_passive", source_changes_inventory)
    client = FakeClient()
    result = execute(case, client)
    assert result["dispatch_state"] == "NEW_DISPATCH_CONFIRMED"
    assert D(client.executors.created[0][0]["amount"]) == D(6)
    assert case[1].get_lot("recovery-profit-lot").cost_basis_remaining_quote == D("2.4")


@pytest.mark.parametrize("filled", ["0", "4"])
def test_certified_terminal_sale_creates_distinct_stable_partial_generation(
    case, offline, monkeypatch, filled
):
    first_client = terminal_client(case, monkeypatch, filled=filled, status="CANCELED")
    first = execute(case, first_client)
    assert first["result"]["terminal"] is True
    prior_id = case[2].manifest.manifest_id
    next_decision = plan_partial_recycle(
        ledger=case[1],
        governor=case[0],
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D(0),
        slippage_allowance_bps=D(0),
        now=2020,
    )
    assert next_decision.action == PartialRecycleAction.RECYCLE
    assert next_decision.manifest.manifest_id != prior_id
    assert next_decision.manifest.predecessor_manifest_id == prior_id
    assert next_decision.manifest.target_base == D(10) - D(filled)
    replay = plan_partial_recycle(
        ledger=case[1],
        governor=case[0],
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.60"),
        expected_exit_fee_bps=D(0),
        slippage_allowance_bps=D(0),
        now=2021,
    )
    assert replay.manifest == next_decision.manifest
    empty_evidence(monkeypatch)
    next_client = FakeClient()
    next_case = case[0], case[1], next_decision
    assert execute(next_case, next_client)["dispatch_state"] == "NEW_DISPATCH_CONFIRMED"
    assert execute(next_case, next_client)["dispatch_state"] == "EXISTING_RECONCILED"
    assert len(first_client.executors.created) == 1
    assert len(next_client.executors.created) == 1


def test_open_whole_exit_prevents_installing_partial_manifest(tmp_path):
    g, book = make_inventory(tmp_path)
    whole = create_or_load_position_exit_manifest(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        now=2000,
    )
    partial = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D(0),
        slippage_allowance_bps=D(0),
        now=2001,
    )
    assert whole.status.value == "OPEN"
    assert partial.action == PartialRecycleAction.NONE
    assert partial.reasons == ("open_whole_exit_blocks_partial_manifest_creation",)
    assert PartialRecycleStore(book.path).open_for_position("xrp-position-1") is None


def test_uncertain_dispatch_cannot_install_new_partial_generation(case, offline):
    store = PartialRecycleStore(case[1].path)
    store.claim_dispatch(
        case[2].manifest,
        now=2010,
        ledger=case[1],
        governor=case[0],
        trading_rule=rule(),
        current_price=D("0.50"),
        market_observed_at=time.time(),
    )
    successor = replace(
        case[2].manifest,
        manifest_id="partial-recycle:unsafe-successor",
        sell_executor_id="unsafe-successor:sell",
        predecessor_manifest_id=case[2].manifest.manifest_id,
        created_at=2020,
    )
    with pytest.raises(ValueError, match="terminal_proof_required"):
        store.create(successor)
    assert store.load(successor.manifest_id) is None
