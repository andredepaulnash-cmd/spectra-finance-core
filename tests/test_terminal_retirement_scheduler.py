import asyncio
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.runtime_scheduler import (
    run_vcap_scheduler_tick,
)
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
)
from spectra_core.models import (
    BreakerMode,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.runtime_scheduler import (
    RuntimeSchedulerPolicy,
    RuntimeSchedulerStore,
)
from spectra_core.vcap_runtime import VCAPRuntimePhase, VCAPRuntimeStore
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)

D = Decimal


def fleet(tmp_path, count=1):
    governor = CapitalGovernor(
        tmp_path / "capital.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("10000"),
            max_pair_quote=D("10000"),
            max_position_quote=D("10000"),
            max_recovery_quote=D("5000"),
        ),
    )
    ledger = EconomicPositionLedger(governor.db_path)
    lifecycle = VCAPStateStore(tmp_path / "lifecycle.sqlite3")
    runtime = VCAPRuntimeStore(tmp_path / "runtime.sqlite3")
    scheduler = RuntimeSchedulerStore(tmp_path / "scheduler.sqlite3")
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    for index in range(count):
        position_id = f"position-{index}"
        pair = f"ASSET{index}-USD"
        proposal_id = f"entry-{index}"
        ledger.open_position(
            position_id=position_id,
            pair=pair,
            created_at=1000 + index,
        )
        decision = governor.reserve(
            CapitalProposal(proposal_id, pair, IntentKind.ENTRY, D("100")),
            CapitalSnapshot(free_quote=D("10000")),
        )
        assert decision.status == DecisionStatus.RESERVED
        assert governor.commit(proposal_id)
        assert governor.reconcile_execution(
            proposal_id=proposal_id,
            executor_id=f"executor-{index}",
            account_name="master_account",
            connector_name="kraken",
            controller_id="spectra",
        )
        ledger.record_entry_fill(
            governor=governor,
            position_id=position_id,
            lot_id=f"lot-{index}",
            proposal_id=proposal_id,
            base_amount=D("10"),
            cost_basis_quote=D("100"),
            occurred_at=1100 + index,
        )
        reconciler.bind_executor_order(
            order_id=f"order-{index}",
            position_id=position_id,
            role=EconomicFillRole.BASE_ENTRY,
            proposal_id=proposal_id,
            account_name="master_account",
            connector_name="kraken",
            pair=pair,
            created_at=1100 + index,
        )
        lifecycle.save(
            VCAPLifecycleState(
                envelope_proposal_id=f"envelope-{index}",
                pair=pair,
                next_sequence=1,
                remaining_authorized_quote=D("100"),
                recovery_proposal_ids=(),
                position_base=D("10"),
                cost_basis_quote=D("100"),
                last_known_price=D("10"),
                profit_state=None,
                status=VCAPLifecycleStatus.ACTIVE,
                updated_at=1200 + index,
            )
        )
    return dict(
        governor=governor,
        ledger=ledger,
        lifecycle_store=lifecycle,
        runtime_store=runtime,
        scheduler_store=scheduler,
        accounting_supervisor=AccountingHealthSupervisor(ledger.path),
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
    )


def close_position(stores, index=0):
    stores["ledger"].record_partial_exit(
        event_id=f"close-{index}",
        position_id=f"position-{index}",
        base_sold=D("10"),
        gross_proceeds_quote=D("110"),
        exit_fee_quote=D("0"),
        occurred_at=1500 + index,
    )


def cycle(stores, phase, index=0, envelope=None):
    runtime = stores["runtime_store"]
    started = runtime.begin_or_resume(
        position_id=f"position-{index}",
        envelope_proposal_id=envelope or f"envelope-{index}",
        now=1600,
    )
    return runtime.transition(started.cycle_id, phase=phase, now=1601)


def tick(stores, runner, *, breaker=BreakerMode.NORMAL, limit=2, now=2000):
    return asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **stores,
            now=now,
            breaker_mode=breaker,
            scheduler_policy=RuntimeSchedulerPolicy(max_positions_per_tick=limit),
            cycle_runner=runner,
        )
    )


@pytest.mark.parametrize("breaker", [BreakerMode.NORMAL, BreakerMode.HALTED])
@pytest.mark.parametrize(
    "phase",
    [
        VCAPRuntimePhase.MUTATION_DISPATCHING,
        VCAPRuntimePhase.MUTATION_DISPATCHED,
        VCAPRuntimePhase.AWAITING_RECONCILIATION,
    ],
)
def test_closed_owner_keeps_only_existing_reconciliation_in_service_pool(
    tmp_path, breaker, phase
):
    stores = fleet(tmp_path)
    existing = cycle(stores, phase)
    close_position(stores)
    calls = []

    async def runner(client, **kwargs):
        calls.append((kwargs["position_id"], kwargs["envelope_proposal_id"]))
        return {"phase": phase.value, "cycle_id": existing.cycle_id}

    result = tick(stores, runner, breaker=breaker)

    assert result["active_position_count"] == 0
    assert result["service_position_count"] == 1
    assert result["retirement_position_ids"] == ["position-0"]
    assert result["selected_position_ids"] == ["position-0"]
    assert calls == [("position-0", "envelope-0")]
    assert stores["scheduler_store"].binding_for_position("position-0") is not None
    assert result["fleet"][0]["runtime_cycle_id"] == existing.cycle_id


@pytest.mark.parametrize(
    "phase",
    [
        None,
        VCAPRuntimePhase.STARTED,
        VCAPRuntimePhase.PLANNED,
        VCAPRuntimePhase.COMPLETED,
        VCAPRuntimePhase.HOLD,
        VCAPRuntimePhase.CONFLICT,
    ],
)
def test_closed_owner_without_pending_mutation_cannot_start_new_cycle(tmp_path, phase):
    stores = fleet(tmp_path)
    if phase is not None:
        cycle(stores, phase)
    close_position(stores)

    async def runner(client, **kwargs):
        pytest.fail("closed position started a fresh runtime path")

    result = tick(stores, runner)
    assert result["active_position_count"] == 0
    assert result["service_position_count"] == 0
    assert result["selected_position_ids"] == []
    assert result["fleet"] == []


def test_closed_reconciliation_remains_bounded_with_active_fleet(tmp_path):
    stores = fleet(tmp_path, count=4)
    stores["scheduler_store"].bind_position(
        position_id="position-0",
        envelope_proposal_id="envelope-0",
        ledger=stores["ledger"],
        lifecycle_store=stores["lifecycle_store"],
        now=1400,
    )
    for index in range(1, 4):
        cycle(stores, VCAPRuntimePhase.AWAITING_RECONCILIATION, index)
        close_position(stores, index)
    calls = []

    async def runner(client, **kwargs):
        calls.append(kwargs["position_id"])
        return {"phase": VCAPRuntimePhase.AWAITING_RECONCILIATION.value}

    result = tick(stores, runner, limit=2)
    assert result["active_position_count"] == 1
    assert result["service_position_count"] == 4
    assert result["selected_position_count"] == len(calls) == 2
    assert "position-0" in calls
    assert len(set(calls) & {"position-1", "position-2", "position-3"}) == 1


def test_closed_cycle_completed_during_selection_never_restarts(tmp_path, monkeypatch):
    stores = fleet(tmp_path)
    existing = cycle(stores, VCAPRuntimePhase.AWAITING_RECONCILIATION)
    close_position(stores)
    scheduler = stores["scheduler_store"]
    select = scheduler.select_positions

    def complete_then_select(**kwargs):
        selected = select(**kwargs)
        stores["runtime_store"].transition(
            existing.cycle_id, phase=VCAPRuntimePhase.COMPLETED, now=1900
        )
        return selected

    monkeypatch.setattr(scheduler, "select_positions", complete_then_select)

    async def runner(client, **kwargs):
        pytest.fail("closed completed cycle restarted")

    result = tick(stores, runner)
    assert result["selected_position_ids"] == ["position-0"]
    assert result["results"][0]["serviced"] is False
    assert result["results"][0]["reason"] == "inactive_position_new_mutation_blocked"


def test_closed_cycle_with_different_durable_envelope_is_not_scheduled(tmp_path):
    stores = fleet(tmp_path)
    stores["scheduler_store"].bind_position(
        position_id="position-0",
        envelope_proposal_id="envelope-0",
        ledger=stores["ledger"],
        lifecycle_store=stores["lifecycle_store"],
        now=1400,
    )
    cycle(
        stores,
        VCAPRuntimePhase.AWAITING_RECONCILIATION,
        envelope="other-envelope",
    )
    close_position(stores)

    async def runner(client, **kwargs):
        pytest.fail("different envelope resumed a closed owner")

    assert tick(stores, runner)["selected_position_count"] == 0


def test_closed_pending_reconciliation_survives_scheduler_restart(tmp_path):
    stores = fleet(tmp_path)
    existing = cycle(stores, VCAPRuntimePhase.AWAITING_RECONCILIATION)
    close_position(stores)
    calls = []

    async def runner(client, **kwargs):
        calls.append(kwargs["position_id"])
        return {
            "phase": VCAPRuntimePhase.AWAITING_RECONCILIATION.value,
            "cycle_id": existing.cycle_id,
        }

    tick(stores, runner)
    stores["ledger"] = EconomicPositionLedger(stores["ledger"].path)
    stores["runtime_store"] = VCAPRuntimeStore(stores["runtime_store"].path)
    stores["scheduler_store"] = RuntimeSchedulerStore(stores["scheduler_store"].path)
    result = tick(stores, runner, now=2010)
    assert result["active_position_count"] == 0
    assert calls == ["position-0", "position-0"]
    assert stores["scheduler_store"].service_record("position-0").service_count == 2
