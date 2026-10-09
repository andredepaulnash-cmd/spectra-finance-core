"""Activated runtime must reconcile an existing sale through a cash hold."""

import asyncio
from decimal import Decimal

import pytest

import mcp_servers.hummingbot_api.tools.accounting_health as health_tool
import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime_tool
from mcp_servers.hummingbot_api.tools.cash_accounting import prepare_sale_cash_fill
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.partial_recycle import (
    PartialRecycleManifestStatus,
    PartialRecycleStore,
)
from spectra_core.vcap_lifecycle import VCAPLifecycleAction
from spectra_core.vcap_runtime import VCAPRuntimePhase, VCAPRuntimeStore
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)
from tests import test_protected_recovery_exit as evidence
from tests.test_protected_recovery_exit import protected_case

D = Decimal


@pytest.mark.parametrize(
    "crash_stage", ["before_inventory", "after_manifest_completed"]
)
def test_pending_sale_cash_survives_crash_and_runtime_finishes_same_sale(
    protected_case, tmp_path, monkeypatch, crash_stage
):
    governor, ledger, decision = protected_case
    manifest = decision.manifest
    assert governor.activate_cash_authority(
        account_name="master_account",
        connector_name="kraken",
        quote_asset="USD",
        opening_productive_quote=D("100"),
        activation_id="reconciled-runtime-opening",
        activated_at=2_000,
    )
    trade = evidence.fill(manifest, "cash-runtime-trade-1", str(manifest.target_base))
    client = evidence.EvidenceClient(manifest)
    client.executors.known = True
    evidence.install_evidence(monkeypatch, manifest, (trade,), status="FILLED")

    # Source evidence is fetched by the real health supervisor and bridge.
    # Its already-reconciled historical source inventory is distinct from the
    # pending synthetic sale and must not inherit that sale's cash HOLD.
    async def source_orders(client, *, executor_id):
        assert executor_id in {"entry-exec", "recovery-profit-exec"}
        return ExecutorOrderSnapshot(executor_id=executor_id, orders=())

    async def source_trades(client, *, executor_id):
        assert executor_id in {"entry-exec", "recovery-profit-exec"}
        return ExecutorTradeSnapshot(executor_id=executor_id, trades=())

    monkeypatch.setattr(health_tool, "fetch_executor_order_snapshot", source_orders)
    monkeypatch.setattr(health_tool, "fetch_executor_trade_snapshot", source_trades)

    lifecycle = VCAPStateStore(tmp_path / "cash-resume-lifecycle.sqlite3")
    lifecycle.save(
        VCAPLifecycleState(
            envelope_proposal_id="cash-runtime-envelope",
            pair=manifest.pair,
            next_sequence=1,
            remaining_authorized_quote=D("0"),
            recovery_proposal_ids=manifest.proposal_ids,
            position_base=ledger.snapshot(manifest.position_id).active_base,
            cost_basis_quote=ledger.snapshot(
                manifest.position_id
            ).active_cost_basis_quote,
            last_known_price=D("0.50"),
            profit_state=None,
            status=VCAPLifecycleStatus.ACTIVE,
            updated_at=2_000,
        )
    )
    runtime = VCAPRuntimeStore(tmp_path / "cash-resume-runtime.sqlite3")
    cycle = runtime.begin_or_resume(
        position_id=manifest.position_id,
        envelope_proposal_id="cash-runtime-envelope",
        now=2_000,
    )
    cycle = runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.PLANNED,
        now=2_001,
        expected_phases=(VCAPRuntimePhase.STARTED,),
        planned_action=VCAPLifecycleAction.PARTIAL_RECYCLE.value,
        mutation_key="PARTIAL_RECYCLE:" + manifest.manifest_id,
    )
    cycle = runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MUTATION_DISPATCHING,
        now=2_002,
        expected_phases=(VCAPRuntimePhase.PLANNED,),
    )

    if crash_stage == "before_inventory":
        cash_event_id = prepare_sale_cash_fill(
            governor=governor,
            manifest=manifest,
            fill=trade,
            sale_purpose="RECOVERY_PARTIAL",
            geometry=partial_tool._sell_geometry(trade),
        )
        assert cash_event_id is not None
    else:
        # The venue fill is fully journaled, but the process dies before cash
        # settlement and before dispatch returns its manifest ID to runtime.
        original_finalize = partial_tool.finalize_sale_cash_fill

        def crash_before_cash_settlement(**kwargs):
            raise RuntimeError("simulated crash before cash settlement")

        monkeypatch.setattr(
            partial_tool, "finalize_sale_cash_fill", crash_before_cash_settlement
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            evidence.synchronize(protected_case, client)
        monkeypatch.setattr(partial_tool, "finalize_sale_cash_fill", original_finalize)
        assert PartialRecycleStore(ledger.path).load(manifest.manifest_id).status == (
            PartialRecycleManifestStatus.COMPLETED
        )
        assert (
            PartialRecycleStore(ledger.path).open_for_position(manifest.position_id)
            is None
        )

    assert governor.cash_authority_snapshot().blocked
    assert (
        governor.cash_authority_snapshot().held_quote
        == trade.quote_amount - trade.fee_paid
    )
    assert runtime.load(cycle.cycle_id).execution_result is None

    # Fresh durable objects model process restart. No source or SELL executor
    # creation is permitted: the exact existing sale must settle and complete.
    restarted_governor = CapitalGovernor(governor.db_path, governor.policy)
    restarted_ledger = EconomicPositionLedger(ledger.path)
    restarted_runtime = VCAPRuntimeStore(runtime.path)
    restarted_lifecycle = VCAPStateStore(lifecycle.path)
    resumed = asyncio.run(
        runtime_tool._resume_reconciliation(
            client,
            cycle=restarted_runtime.load(cycle.cycle_id),
            governor=restarted_governor,
            ledger=restarted_ledger,
            lifecycle_store=restarted_lifecycle,
            runtime_store=restarted_runtime,
            accounting_supervisor=AccountingHealthSupervisor(restarted_ledger.path),
            now=2_020,
        )
    )
    assert resumed["phase"] == VCAPRuntimePhase.COMPLETED.value
    assert resumed["cycle_id"] == cycle.cycle_id
    assert client.executors.created == []
    cash = restarted_governor.cash_authority_snapshot()
    assert not cash.blocked
    assert cash.held_quote == D("0")
    assert cash.parked_quote == trade.quote_amount - trade.fee_paid
    assert cash.productive_quote == D("100")
    assert restarted_ledger.snapshot(manifest.position_id).active_base == D("20")
