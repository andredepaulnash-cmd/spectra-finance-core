import asyncio
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.runtime_scheduler import (
    run_vcap_scheduler_tick,
)
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.models import BreakerMode, CapitalPolicy
from spectra_core.runtime_scheduler import (
    FleetPositionState,
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


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "scheduler-governor.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("10000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("2500"),
            max_recovery_quote=D("2500"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def setup_fleet(tmp_path, count=3):
    ledger = EconomicPositionLedger(tmp_path / "scheduler-economic.sqlite3")
    lifecycle = VCAPStateStore(tmp_path / "scheduler-vcap.sqlite3")
    runtime = VCAPRuntimeStore(tmp_path / "scheduler-runtime.sqlite3")
    scheduler = RuntimeSchedulerStore(tmp_path / "scheduler.sqlite3")
    health = AccountingHealthSupervisor(ledger.path)
    bindings = {}

    for index in range(1, count + 1):
        position_id = f"position-{index}"
        envelope = f"envelope-{index}"
        pair = f"ASSET{index}-USD"
        ledger.open_position(
            position_id=position_id,
            pair=pair,
            created_at=1_000 + index,
        )
        ledger.record_base_fill(
            position_id=position_id,
            lot_id=f"lot-{index}",
            pair=pair,
            base_amount=D("10"),
            cost_basis_quote=D("100"),
            occurred_at=1_100 + index,
        )
        lifecycle.save(
            VCAPLifecycleState(
                envelope_proposal_id=envelope,
                pair=pair,
                next_sequence=1,
                remaining_authorized_quote=D("100"),
                recovery_proposal_ids=(),
                position_base=D("10"),
                cost_basis_quote=D("100"),
                last_known_price=D("10"),
                profit_state=None,
                status=VCAPLifecycleStatus.ACTIVE,
                updated_at=1_200 + index,
            )
        )
        bindings[position_id] = envelope

    return ledger, lifecycle, runtime, scheduler, health, bindings


def policy(max_positions=2):
    return RuntimeSchedulerPolicy(
        max_positions_per_tick=max_positions,
        active_interval_seconds=10,
        reconciliation_interval_seconds=2,
        hold_interval_seconds=20,
        conflict_interval_seconds=60,
        halted_interval_seconds=5,
        stale_heartbeat_seconds=30,
    )


def test_binding_is_exact_and_envelope_cannot_be_reused_across_assets(tmp_path):
    ledger, lifecycle, _, scheduler, _, bindings = setup_fleet(tmp_path, 2)

    first = scheduler.bind_position(
        position_id="position-1",
        envelope_proposal_id=bindings["position-1"],
        ledger=ledger,
        lifecycle_store=lifecycle,
        now=2_000,
    )
    assert first.pair == "ASSET1-USD"

    with pytest.raises(RuntimeError, match="already bound"):
        scheduler.bind_position(
            position_id="position-2",
            envelope_proposal_id=bindings["position-1"],
            ledger=ledger,
            lifecycle_store=lifecycle,
            now=2_001,
        )

    restarted = RuntimeSchedulerStore(scheduler.path)
    assert restarted.binding_for_position("position-1") == first


def test_scheduler_services_multiple_positions_and_isolates_failure(tmp_path):
    ledger, lifecycle, runtime, scheduler, health, bindings = setup_fleet(
        tmp_path, 3
    )
    calls = []

    async def runner(client, **kwargs):
        position_id = kwargs["position_id"]
        calls.append(position_id)
        if position_id == "position-1":
            raise RuntimeError("position one failed")
        return {
            "cycle_id": f"{position_id}:fake:1",
            "phase": VCAPRuntimePhase.COMPLETED.value,
            "terminal": True,
        }

    result = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            governor=governor(tmp_path),
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            scheduler_store=scheduler,
            accounting_supervisor=health,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_000,
            breaker_mode=BreakerMode.NORMAL,
            scheduler_policy=policy(2),
            position_envelopes=bindings,
            cycle_runner=runner,
        )
    )

    assert result["selected_position_count"] == 2
    assert calls == ["position-1", "position-2"]
    assert result["results"][0]["fleet_state"] == FleetPositionState.ERROR.value
    assert result["results"][1]["fleet_state"] == FleetPositionState.ACTIVE.value


def test_round_robin_fairness_prevents_starvation_across_ticks(tmp_path):
    ledger, lifecycle, runtime, scheduler, health, bindings = setup_fleet(
        tmp_path, 3
    )
    calls = []

    async def runner(client, **kwargs):
        position_id = kwargs["position_id"]
        calls.append(position_id)
        return {
            "cycle_id": f"{position_id}:fake:{len(calls)}",
            "phase": VCAPRuntimePhase.COMPLETED.value,
            "terminal": True,
        }

    common = dict(
        governor=governor(tmp_path),
        ledger=ledger,
        lifecycle_store=lifecycle,
        runtime_store=runtime,
        scheduler_store=scheduler,
        accounting_supervisor=health,
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
        breaker_mode=BreakerMode.NORMAL,
        scheduler_policy=policy(2),
        cycle_runner=runner,
    )

    first = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **common,
            now=2_000,
            position_envelopes=bindings,
        )
    )
    second = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **common,
            now=2_011,
        )
    )

    assert first["selected_position_ids"] == ["position-1", "position-2"]
    assert second["selected_position_ids"][0] == "position-3"
    assert set(calls) == {"position-1", "position-2", "position-3"}


def test_halted_breaker_blocks_new_cycles_but_allows_reconciliation(tmp_path):
    ledger, lifecycle, runtime, scheduler, health, bindings = setup_fleet(
        tmp_path, 1
    )
    calls = []

    async def runner(client, **kwargs):
        calls.append(kwargs["position_id"])
        return {
            "cycle_id": "position-1:cycle:00000001",
            "phase": VCAPRuntimePhase.AWAITING_RECONCILIATION.value,
            "terminal": False,
        }

    common = dict(
        governor=governor(tmp_path),
        ledger=ledger,
        lifecycle_store=lifecycle,
        runtime_store=runtime,
        scheduler_store=scheduler,
        accounting_supervisor=health,
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
        breaker_mode=BreakerMode.HALTED,
        scheduler_policy=policy(1),
        cycle_runner=runner,
    )

    blocked = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **common,
            now=2_000,
            position_envelopes=bindings,
        )
    )
    assert calls == []
    assert blocked["results"][0]["fleet_state"] == FleetPositionState.BLOCKED.value

    cycle = runtime.begin_or_resume(
        position_id="position-1",
        envelope_proposal_id="envelope-1",
        now=2_001,
    )
    runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
        now=2_002,
        expected_phases=(VCAPRuntimePhase.STARTED,),
    )

    resumed = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **common,
            now=2_006,
        )
    )
    assert calls == ["position-1"]
    assert resumed["results"][0]["fleet_state"] == FleetPositionState.WAITING.value


def test_position_can_be_serviced_repeatedly_after_completed_cycles(tmp_path):
    ledger, lifecycle, runtime, scheduler, health, bindings = setup_fleet(
        tmp_path, 1
    )
    calls = []

    async def runner(client, **kwargs):
        calls.append(kwargs["position_id"])
        return {
            "cycle_id": f"position-1:fake:{len(calls)}",
            "phase": VCAPRuntimePhase.COMPLETED.value,
            "terminal": True,
        }

    common = dict(
        governor=governor(tmp_path),
        ledger=ledger,
        lifecycle_store=lifecycle,
        runtime_store=runtime,
        scheduler_store=scheduler,
        accounting_supervisor=health,
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
        breaker_mode=BreakerMode.NORMAL,
        scheduler_policy=policy(1),
        cycle_runner=runner,
    )

    asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **common,
            now=2_000,
            position_envelopes=bindings,
        )
    )
    asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            **common,
            now=2_011,
        )
    )

    assert calls == ["position-1", "position-1"]
    assert scheduler.service_record("position-1").service_count == 2


def test_unbound_position_is_reported_truthfully_and_never_dispatched(tmp_path):
    ledger, lifecycle, runtime, scheduler, health, _ = setup_fleet(tmp_path, 1)
    calls = []

    async def runner(client, **kwargs):
        calls.append(kwargs["position_id"])
        return {}

    result = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            governor=governor(tmp_path),
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            scheduler_store=scheduler,
            accounting_supervisor=health,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_000,
            breaker_mode=BreakerMode.NORMAL,
            scheduler_policy=policy(1),
            cycle_runner=runner,
        )
    )

    assert calls == []
    assert result["fleet"][0]["fleet_state"] == FleetPositionState.UNBOUND.value
    assert result["results"][0]["reason"] == "vcap_envelope_unbound"


def test_closed_position_disappears_from_active_fleet(tmp_path):
    ledger, lifecycle, runtime, scheduler, health, bindings = setup_fleet(
        tmp_path, 1
    )

    ledger.record_partial_exit(
        event_id="close-position-1",
        position_id="position-1",
        base_sold=D("10"),
        gross_proceeds_quote=D("110"),
        exit_fee_quote=D("0"),
        occurred_at=1_500,
    )

    result = asyncio.run(
        run_vcap_scheduler_tick(
            object(),
            governor=governor(tmp_path),
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            scheduler_store=scheduler,
            accounting_supervisor=health,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_000,
            breaker_mode=BreakerMode.NORMAL,
            scheduler_policy=policy(2),
            position_envelopes={},
        )
    )

    assert result["active_position_count"] == 0
    assert result["selected_position_count"] == 0
    assert result["fleet"] == []
