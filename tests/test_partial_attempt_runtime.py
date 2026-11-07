import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
)
from spectra_core.models import IntentKind
from spectra_core.partial_recycle import PartialRecycleStore, plan_partial_recycle
from spectra_core.vcap_runtime import VCAPRuntimePolicy, VCAPRuntimeStore
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)
from tests.test_partial_recovery_recycling import (
    add_recovery,
    governor,
    ledger,
    prepare_execution,
    rule,
)
from tests.test_vcap_lifecycle_controller import healthy_accounting


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    g, book = governor(tmp_path), ledger(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-executor",
        intent=IntentKind.ENTRY,
        amount="100",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry",
        proposal_id="entry",
        base_amount=D(100),
        cost_basis_quote=D(100),
        occurred_at=1000,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery",
        executor_id="recovery-executor",
        lot_id="recovery",
        base="10",
        basis="4",
        occurred_at=1010,
    )
    rules = rule(min_quote="4")
    planned = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rules,
        current_price=D("0.6"),
        expected_exit_fee_bps=D(40),
        slippage_allowance_bps=D(5),
        now=1500,
    )
    lifecycle = VCAPStateStore(tmp_path / "lifecycle.db")
    state = VCAPLifecycleState(
        envelope_proposal_id="envelope",
        pair="XRP-USD",
        next_sequence=3,
        remaining_authorized_quote=D(50),
        recovery_proposal_ids=("recovery",),
        position_base=D(110),
        cost_basis_quote=D(104),
        last_known_price=D("0.3"),
        profit_state=None,
        status=VCAPLifecycleStatus.ACTIVE,
        updated_at=1500,
    )
    lifecycle.save(state)
    calls = []
    observations = {
        "price": D("0.3"),
        "pending": False,
        "reads": 0,
        "appear_on_second": False,
        "unavailable": False,
    }

    async def accounting(*args, **kwargs):
        return healthy_accounting("xrp-position-1", kwargs["now"])

    async def market(*args, **kwargs):
        return (
            AssetFingerprint.cold_start(
                AssetObservation(
                    "XRP-USD", kwargs["now"], float(observations["price"]), 1000000
                )
            ),
            rules,
            observations["price"],
        )

    async def get_executor(executor_id):
        if observations["unavailable"]:
            raise RuntimeError("unavailable")
        return None

    async def orders(client, *, executor_id):
        observations["reads"] += 1
        pending = observations["pending"] or (
            observations["appear_on_second"] and observations["reads"] > 2
        )
        rows = ()
        if pending and executor_id == "recovery-executor":
            rows = (
                ExecutorOrderEvidence(
                    executor_id,
                    "pending-order",
                    "LIMIT",
                    "BUY",
                    D(10),
                    D("0.3"),
                    "OPEN",
                    D(0),
                    None,
                    1600,
                    1600,
                ),
            )
        return ExecutorOrderSnapshot(executor_id, rows)

    async def dispatch(*args, **kwargs):
        calls.append(kwargs["decision"])
        return {"executed": False, "reasons": ["test_dispatch_observed"]}

    monkeypatch.setattr(runtime, "_refresh_accounting", accounting)
    monkeypatch.setattr(runtime, "_refresh_market", market)
    monkeypatch.setattr(runtime, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(runtime, "execute_vcap_lifecycle_decision", dispatch)
    params = dict(
        client=SimpleNamespace(executors=SimpleNamespace(get_executor=get_executor)),
        position_id="xrp-position-1",
        envelope_proposal_id="envelope",
        governor=g,
        ledger=book,
        lifecycle_store=lifecycle,
        runtime_store=VCAPRuntimeStore(tmp_path / "runtime.db"),
        accounting_supervisor=AccountingHealthSupervisor(book.path),
        seed_quote=D(40),
        expected_exit_fee_bps=D(40),
        slippage_allowance_bps=D(5),
        now=2000,
        runtime_policy=VCAPRuntimePolicy(partial_attempt_deferral_enabled=True),
    )
    return params, observations, calls, state, planned.manifest


def test_enabled_runtime_defers_then_reevaluates_same_sale_without_buy(integrated):
    params, observations, calls, state, manifest = integrated
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert result["phase"] == "HOLD"
    assert "partial_attempt_defer_price" in result["reasons"]
    assert calls == []
    assert params["lifecycle_store"].load("envelope") == state
    assert params["governor"].active_reserved_quote() == 0
    observations["price"] = D("0.6")
    asyncio.run(runtime.run_vcap_runtime_cycle(**{**params, "now": 2001}))
    assert len(calls) == 1
    assert calls[0].action.value == "PARTIAL_RECYCLE"
    assert calls[0].partial_recycle_decision.manifest == manifest
    assert calls[0].execution_decision is None
    assert (
        PartialRecycleStore(params["ledger"].path).open_for_position("xrp-position-1")
        == manifest
    )


@pytest.mark.parametrize("flag", ["pending", "unavailable"])
def test_enabled_runtime_blocks_pending_or_unknown_exact_evidence(integrated, flag):
    params, observations, calls, _, _ = integrated
    observations.update(price=D("0.6"), **{flag: True})
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert result["phase"] == "HOLD"
    assert calls == []
    assert params["governor"].active_reserved_quote() == 0


def test_pending_order_appearing_after_plan_blocks_actual_dispatch(integrated):
    params, observations, calls, _, manifest = integrated
    observations.update(price=D("0.6"), appear_on_second=True)
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert result["phase"] == "HOLD"
    assert "partial_attempt_revalidation_blocked" in result["reasons"]
    assert calls == []
    assert (
        PartialRecycleStore(params["ledger"].path).open_for_position("xrp-position-1")
        == manifest
    )


def test_disabled_runtime_does_not_fetch_new_pending_surfaces(integrated):
    params, observations, calls, _, _ = integrated
    params["runtime_policy"] = VCAPRuntimePolicy()
    observations["unavailable"] = True
    asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert len(calls) == 1
    assert observations["reads"] == 0


def test_ambiguous_committed_same_asset_without_lots_blocks_partial_dispatch(
    integrated,
):
    from spectra_core.models import CapitalProposal, CapitalSnapshot

    params, observations, calls, _, _ = integrated
    observations["price"] = D("0.6")
    g = params["governor"]
    g.reserve(
        CapitalProposal("unresolved", "XRP-USD", IntentKind.RECOVERY, D(5)),
        CapitalSnapshot(free_quote=D(1000)),
    )
    assert g.commit("unresolved")
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert result["phase"] == "HOLD"
    assert calls == []
    assert "existing_position_order_pending" in result["reasons"]


def real_dispatch_setup(
    integrated,
    monkeypatch,
    *,
    consume_during_source=False,
    pending_during_source=False,
    uncertain_create=False,
):
    import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
    from mcp_servers.hummingbot_api.tools.vcap_lifecycle import (
        execute_vcap_lifecycle_decision,
    )
    from spectra_core.executor_provenance_bridge import ExecutorTradeSnapshot
    from spectra_core.fill_reconciliation import TradeFillEvidence
    from spectra_core.models import BreakerMode

    params, observations, _, _, manifest = integrated
    observations["price"] = D("0.6")
    created = []
    sold = {"done": False}

    async def get_executor(executor_id):
        if executor_id == "recovery-executor":
            if consume_during_source and not sold["done"]:
                params["ledger"].record_partial_exit(
                    event_id="concurrent-sale",
                    position_id="xrp-position-1",
                    base_sold=D(10),
                    gross_proceeds_quote=D(6),
                    occurred_at=1900,
                    eligible_lot_ids=("recovery",),
                )
                sold["done"] = True
            if pending_during_source:
                observations["pending"] = True
            return {
                "id": executor_id,
                "executor_type": "dca_executor",
                "status": "TERMINATED",
                "close_type": "POSITION_HOLD",
                "filled_amount_quote": "4",
                "cum_fees_quote": "0",
                "position_size_quote": "4",
                "net_pnl_quote": "0",
                "custom_info": {"realized_pnl_quote": "0"},
            }
        if executor_id == manifest.sell_executor_id:
            return {"id": executor_id} if created and not uncertain_create else None
        return None

    async def create_executor(**kwargs):
        created.append(kwargs)
        if uncertain_create:
            raise RuntimeError("transport outcome unknown")
        return {"executor_id": manifest.sell_executor_id}

    order = ExecutorOrderEvidence(
        manifest.sell_executor_id,
        "actual-sell",
        "MARKET",
        "SELL",
        D(10),
        None,
        "FILLED",
        D(10),
        D("0.6"),
        2000,
        2000,
    )
    fill = TradeFillEvidence(
        "actual-fill",
        "actual-sell",
        "master_account",
        "kraken",
        "XRP-USD",
        "SELL",
        D(10),
        D("0.6"),
        D("0.024"),
        "USD",
        2000,
    )

    async def orders(client, *, executor_id):
        return ExecutorOrderSnapshot(
            executor_id,
            (order,) if created and executor_id == manifest.sell_executor_id else (),
        )

    async def trades(client, *, executor_id):
        return ExecutorTradeSnapshot(
            executor_id, (fill,) if created and not uncertain_create else ()
        )

    params["client"].executors = SimpleNamespace(
        get_executor=get_executor, create_executor=create_executor
    )
    monkeypatch.setattr(
        runtime, "execute_vcap_lifecycle_decision", execute_vcap_lifecycle_decision
    )
    monkeypatch.setattr(partial_tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(partial_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(partial_tool, "fetch_executor_trade_snapshot", trades)
    return created


def test_real_partial_adapter_rechecks_consumed_inventory_after_its_own_await(
    integrated, monkeypatch
):
    created = real_dispatch_setup(integrated, monkeypatch, consume_during_source=True)
    params, _, _, _, _ = integrated
    asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert created == []
    assert params["ledger"].get_lot("entry").base_remaining == 100
    assert params["ledger"].get_lot("recovery").base_remaining == 0


def test_real_partial_adapter_rechecks_pending_order_after_its_own_await(
    integrated, monkeypatch
):
    created = real_dispatch_setup(integrated, monkeypatch, pending_during_source=True)
    asyncio.run(runtime.run_vcap_runtime_cycle(**integrated[0]))
    assert created == []


def test_enabled_real_partial_dispatch_fill_and_owner_reconciliation(
    integrated, monkeypatch
):
    created = real_dispatch_setup(integrated, monkeypatch)
    params, _, _, _, manifest = integrated
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert len(created) == 1
    assert result["phase"] == "COMPLETED"
    assert created[0]["executor_config"]["amount"] == "10"
    assert params["ledger"].get_lot("entry").base_remaining == 100
    assert params["ledger"].get_lot("recovery").base_remaining == 0
    assert params["governor"].current_recovery_exposure_quote() == 0
    assert params["ledger"].selected_recovery_lock_owner("xrp-position-1") is None
    assert (
        PartialRecycleStore(params["ledger"].path)
        .load(manifest.manifest_id)
        .status.value
        == "COMPLETED"
    )


def test_uncertain_partial_create_retains_durable_claim_and_position_lock(
    integrated, monkeypatch
):
    created = real_dispatch_setup(integrated, monkeypatch, uncertain_create=True)
    params, _, _, _, manifest = integrated
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert len(created) == 1
    assert result["phase"] == "AWAITING_RECONCILIATION"
    assert PartialRecycleStore(params["ledger"].path).dispatch_claimed(
        manifest.manifest_id
    )
    assert (
        params["ledger"].selected_recovery_lock_owner("xrp-position-1")
        == "partial:" + manifest.manifest_id
    )
    # A legacy call cannot omit the guard to bypass a prior ambiguous attempt.
    import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
    from spectra_core.partial_recycle import (
        PartialRecycleAction,
        PartialRecycleDecision,
    )

    blocked = asyncio.run(
        partial_tool.execute_partial_recycle(
            params["client"],
            decision=PartialRecycleDecision(
                PartialRecycleAction.RECYCLE, manifest, (), ("resume",)
            ),
            governor=params["governor"],
            ledger=params["ledger"],
            trading_rule=rule(min_quote="4"),
            current_price=D("0.6"),
            now=2001,
        )
    )
    assert blocked["executed"] is False
    assert blocked["reasons"] == ["guarded_partial_mutation_requires_reconciliation"]
    assert len(created) == 1


@pytest.mark.parametrize(
    "case", ["missing_trade", "overfill", "duplicate_order", "duplicate_trade"]
)
def test_partial_reconciliation_does_not_release_lock_on_incomplete_terminal_evidence(
    integrated, monkeypatch, case
):
    from dataclasses import replace

    import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
    from spectra_core.executor_provenance_bridge import ExecutorTradeSnapshot
    from spectra_core.fill_reconciliation import TradeFillEvidence

    params, _, _, _, manifest = integrated
    owner = "partial:" + manifest.manifest_id
    params["ledger"].claim_selected_recovery_lock("xrp-position-1", owner)
    order = ExecutorOrderEvidence(
        manifest.sell_executor_id,
        "sell",
        "MARKET",
        "SELL",
        D(10),
        None,
        "FILLED",
        D(10),
        D("0.6"),
        2000,
        2000,
    )
    fill = TradeFillEvidence(
        "fill",
        "sell",
        "master_account",
        "kraken",
        "XRP-USD",
        "SELL",
        D(10),
        D("0.6"),
        D("0.024"),
        "USD",
        2000,
    )
    order_rows, trade_rows = (order,), (fill,)
    if case == "missing_trade":
        trade_rows = ()
    elif case == "overfill":
        order_rows = (replace(order, filled_amount=D(11)),)
    elif case == "duplicate_order":
        order_rows = (order, order)
    else:
        trade_rows = (fill, fill)

    async def orders(*args, **kwargs):
        return ExecutorOrderSnapshot(manifest.sell_executor_id, order_rows)

    async def trades(*args, **kwargs):
        return ExecutorTradeSnapshot(manifest.sell_executor_id, trade_rows)

    monkeypatch.setattr(partial_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(partial_tool, "fetch_executor_trade_snapshot", trades)
    with pytest.raises(RuntimeError):
        asyncio.run(
            partial_tool.synchronize_partial_recycle_executor(
                params["client"],
                manifest=manifest,
                governor=params["governor"],
                ledger=params["ledger"],
                store=PartialRecycleStore(params["ledger"].path),
                now=2000,
            )
        )
    assert params["ledger"].selected_recovery_lock_owner("xrp-position-1") == owner
    assert params["ledger"].get_lot("recovery").base_remaining == 10


def test_final_partial_send_clock_expires_during_actual_adapter_await(
    integrated, monkeypatch
):
    created = real_dispatch_setup(integrated, monkeypatch)
    params = integrated[0]
    clock = {"now": 0.0}
    params["monotonic_clock"] = lambda: clock["now"]
    original = params["client"].executors.get_executor

    async def delayed(executor_id):
        if executor_id == "recovery-executor":
            clock["now"] = 31.0
        return await original(executor_id)

    params["client"].executors.get_executor = delayed
    result = asyncio.run(runtime.run_vcap_runtime_cycle(**params))
    assert created == []
    assert result["executed"] is False
    assert params["ledger"].get_lot("recovery").base_remaining == 10


def test_partial_owner_release_uses_actual_lots_after_asymmetric_consumption(
    tmp_path, monkeypatch
):
    import sqlite3
    from contextlib import closing

    import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
    from spectra_core.executor_provenance_bridge import ExecutorTradeSnapshot
    from spectra_core.fill_reconciliation import TradeFillEvidence

    g, book = governor(tmp_path), ledger(tmp_path)
    for owner, basis in (("a", "4"), ("b", "8")):
        add_recovery(
            g,
            book,
            proposal_id=owner,
            executor_id=owner + "-exec",
            lot_id=owner,
            base="10",
            basis=basis,
            occurred_at=1010,
        )
    manifest = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(min_quote="4"),
        current_price=D(1),
        expected_exit_fee_bps=D(40),
        slippage_allowance_bps=D(5),
        now=1500,
    ).manifest
    book.record_partial_exit(
        event_id="earlier-a-only",
        position_id=manifest.position_id,
        base_sold=D(5),
        gross_proceeds_quote=D(5),
        eligible_lot_ids=("a",),
        occurred_at=1600,
    )
    order = ExecutorOrderEvidence(
        manifest.sell_executor_id,
        "sale",
        "MARKET",
        "SELL",
        D(15),
        None,
        "FILLED",
        D(15),
        D(1),
        2000,
        2000,
    )
    fill = TradeFillEvidence(
        "final-fill",
        "sale",
        "master_account",
        "kraken",
        "XRP-USD",
        "SELL",
        D(15),
        D(1),
        D(0),
        "USD",
        2000,
    )

    async def orders(*args, **kwargs):
        return ExecutorOrderSnapshot(manifest.sell_executor_id, (order,))

    async def trades(*args, **kwargs):
        return ExecutorTradeSnapshot(manifest.sell_executor_id, (fill,))

    monkeypatch.setattr(partial_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(partial_tool, "fetch_executor_trade_snapshot", trades)
    kwargs = dict(
        client=SimpleNamespace(),
        manifest=manifest,
        governor=g,
        ledger=book,
        store=PartialRecycleStore(book.path),
        now=2000,
    )
    asyncio.run(partial_tool.synchronize_partial_recycle_executor(**kwargs))
    # Exact receipt replay must not redistribute original manifest weights.
    asyncio.run(partial_tool.synchronize_partial_recycle_executor(**kwargs))
    with closing(sqlite3.connect(g.db_path)) as conn:
        rows = conn.execute(
            "SELECT proposal_id, amount_quote FROM recovery_recycle_releases ORDER BY proposal_id"
        ).fetchall()
    assert [(owner, D(basis)) for owner, basis in rows] == [("a", D(2)), ("b", D(8))]
    assert book.get_lot("a").base_remaining == book.get_lot("b").base_remaining == 0
