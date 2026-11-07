from __future__ import annotations

from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping

from mcp_servers.hummingbot_api.tools.vcap_runtime import (
    run_vcap_runtime_cycle,
)
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
)
from spectra_core.models import BreakerMode
from spectra_core.runtime_scheduler import (
    FleetPositionState,
    RuntimeSchedulerPolicy,
    RuntimeSchedulerStore,
)
from spectra_core.vcap_runtime import (
    VCAPRuntimePhase,
    VCAPRuntimeStore,
)
from spectra_core.vcap_state import VCAPStateStore

CycleRunner = Callable[..., Awaitable[dict[str, Any]]]

_RECONCILIATION_PHASES = {
    VCAPRuntimePhase.MUTATION_DISPATCHING,
    VCAPRuntimePhase.MUTATION_DISPATCHED,
    VCAPRuntimePhase.AWAITING_RECONCILIATION,
}


def _fleet_state_for_result(result: Mapping[str, Any]) -> FleetPositionState:
    raw = str(result.get("phase") or "").strip().upper()
    if raw in {
        VCAPRuntimePhase.MUTATION_DISPATCHING.value,
        VCAPRuntimePhase.MUTATION_DISPATCHED.value,
        VCAPRuntimePhase.AWAITING_RECONCILIATION.value,
    }:
        return FleetPositionState.WAITING
    if raw == VCAPRuntimePhase.HOLD.value:
        return FleetPositionState.HOLD
    if raw == VCAPRuntimePhase.CONFLICT.value:
        return FleetPositionState.CONFLICT
    return FleetPositionState.ACTIVE


async def run_vcap_scheduler_tick(
    client: Any,
    *,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    lifecycle_store: VCAPStateStore,
    runtime_store: VCAPRuntimeStore,
    scheduler_store: RuntimeSchedulerStore,
    accounting_supervisor: AccountingHealthSupervisor,
    seed_quote: Decimal,
    expected_exit_fee_bps: Decimal,
    slippage_allowance_bps: Decimal,
    now: float,
    breaker_mode: BreakerMode,
    scheduler_policy: RuntimeSchedulerPolicy | None = None,
    position_envelopes: Mapping[str, str] | None = None,
    cycle_runner: CycleRunner = run_vcap_runtime_cycle,
    account_name: str = "master_account",
    controller_id: str = "spectra",
    buy_max_price_slippage_bps: Decimal | None = None,
    buy_fee_allowance_bps: Decimal | None = None,
) -> dict[str, Any]:
    """Service a bounded fleet of active positions and terminal reconciliation.

    The scheduler owns orchestration, never strategy sizing. Each selected
    position delegates to the existing restart-safe VCAP runtime, which remains
    the only component allowed to plan/dispatch that position's capital
    mutation. Calls are deliberately sequential so one scheduler tick can never
    create an unbounded burst of external mutations.

    HALTED still services already-dispatched reconciliation cycles, but it does
    not start or advance a cycle that could reach a new mutation. DEFENSIVE is
    delegated to the existing execution boundary, which already authorizes only
    the live modes permitted by Spectra's breaker contract.
    """

    if not isinstance(breaker_mode, BreakerMode):
        raise TypeError("breaker_mode must be BreakerMode")
    policy = scheduler_policy or RuntimeSchedulerPolicy()
    active_ids = ledger.list_active_position_ids()
    active_set = set(active_ids)
    # Confirmed SELL fills can make a position CLOSED before its executor and
    # capital ownership have terminal proof. Keep only that existing mutation
    # in the service pool; a closed position never gets a fresh strategy cycle.
    retirement_ids: list[str] = []
    pending_ids = dict.fromkeys(
        (
            *ledger.list_pending_retirement_position_ids(),
            *runtime_store.list_pending_reconciliation_position_ids(),
        )
    )
    for position_id in pending_ids:
        if position_id in active_set:
            continue
        try:
            snapshot = ledger.snapshot(position_id)
        except (KeyError, ValueError):
            # Runtime history cannot synthesize a missing economic owner.
            continue
        if snapshot.status not in {
            EconomicPositionStatus.CLOSED,
            EconomicPositionStatus.RESIDUAL,
        }:
            continue
        latest = runtime_store.latest_for_position(position_id)
        binding = scheduler_store.binding_for_position(position_id)
        if (
            latest is not None
            and not latest.terminal
            and latest.phase in _RECONCILIATION_PHASES
            and (
                binding is None
                or binding.envelope_proposal_id == latest.envelope_proposal_id
            )
        ):
            retirement_ids.append(position_id)
    service_ids = active_ids + tuple(retirement_ids)
    service_set = set(service_ids)

    # Explicit bindings are a migration/bootstrap surface, not a pair-based
    # guess. Once persisted, a position/envelope relationship is immutable.
    if position_envelopes is not None:
        unknown = sorted(set(position_envelopes) - service_set)
        if unknown:
            raise ValueError(
                "cannot bind positions outside the service fleet: " + ", ".join(unknown)
            )
        for position_id, envelope_id in position_envelopes.items():
            scheduler_store.bind_position(
                position_id=position_id,
                envelope_proposal_id=envelope_id,
                ledger=ledger,
                lifecycle_store=lifecycle_store,
                now=now,
            )

    # Migrate exact ownership already proven by PR #17 runtime history. We never
    # infer a binding merely because a lifecycle state shares the same pair.
    for position_id in service_ids:
        if scheduler_store.binding_for_position(position_id) is not None:
            continue
        latest = runtime_store.latest_for_position(position_id)
        if latest is None:
            continue
        scheduler_store.bind_position(
            position_id=position_id,
            envelope_proposal_id=latest.envelope_proposal_id,
            ledger=ledger,
            lifecycle_store=lifecycle_store,
            now=now,
        )

    selected = scheduler_store.select_positions(
        active_position_ids=service_ids,
        runtime_store=runtime_store,
        policy=policy,
        now=now,
    )
    tick = scheduler_store.begin_tick(
        breaker_mode=breaker_mode.value,
        active_position_ids=active_ids,
        selected_position_ids=selected,
        now=now,
    )

    results: list[dict[str, Any]] = []
    try:
        for position_id in selected:
            binding = scheduler_store.binding_for_position(position_id)
            if binding is None:
                record = scheduler_store.record_outcome(
                    position_id=position_id,
                    state=FleetPositionState.UNBOUND,
                    now=now,
                    policy=policy,
                    error="exact VCAP envelope binding is not proven",
                )
                results.append(
                    {
                        "position_id": position_id,
                        "serviced": False,
                        "fleet_state": record.state.value,
                        "reason": "vcap_envelope_unbound",
                    }
                )
                continue

            latest = runtime_store.latest_for_position(position_id)
            # Recheck after selection: another reconciler may have completed
            # the cycle or retired the position during this tick. Exact durable
            # envelope identity is required to resume a closed owner.
            snapshot = ledger.snapshot(position_id)
            if snapshot.status != EconomicPositionStatus.ACTIVE and not (
                snapshot.status
                in {EconomicPositionStatus.CLOSED, EconomicPositionStatus.RESIDUAL}
                and position_id in retirement_ids
                and latest is not None
                and not latest.terminal
                and latest.phase in _RECONCILIATION_PHASES
                and latest.envelope_proposal_id == binding.envelope_proposal_id
            ):
                record = scheduler_store.record_outcome(
                    position_id=position_id,
                    state=FleetPositionState.BLOCKED,
                    now=now,
                    policy=policy,
                    cycle_id=None if latest is None else latest.cycle_id,
                    runtime_phase=None if latest is None else latest.phase.value,
                    error="inactive position has no exact pending reconciliation",
                )
                results.append(
                    {
                        "position_id": position_id,
                        "serviced": False,
                        "fleet_state": record.state.value,
                        "reason": "inactive_position_new_mutation_blocked",
                    }
                )
                continue
            if breaker_mode == BreakerMode.HALTED and not (
                latest is not None and latest.phase in _RECONCILIATION_PHASES
            ):
                record = scheduler_store.record_outcome(
                    position_id=position_id,
                    state=FleetPositionState.BLOCKED,
                    now=now,
                    policy=policy,
                    cycle_id=None if latest is None else latest.cycle_id,
                    runtime_phase=None if latest is None else latest.phase.value,
                    error="global breaker HALTED; new capital mutation path blocked",
                )
                results.append(
                    {
                        "position_id": position_id,
                        "serviced": False,
                        "fleet_state": record.state.value,
                        "reason": "global_breaker_halted",
                    }
                )
                continue

            try:
                result = await cycle_runner(
                    client,
                    position_id=position_id,
                    envelope_proposal_id=binding.envelope_proposal_id,
                    governor=governor,
                    ledger=ledger,
                    lifecycle_store=lifecycle_store,
                    runtime_store=runtime_store,
                    accounting_supervisor=accounting_supervisor,
                    seed_quote=seed_quote,
                    expected_exit_fee_bps=expected_exit_fee_bps,
                    slippage_allowance_bps=slippage_allowance_bps,
                    now=now,
                    account_name=account_name,
                    controller_id=controller_id,
                    allow_new_dispatch=(
                        breaker_mode != BreakerMode.HALTED
                        and snapshot.status == EconomicPositionStatus.ACTIVE
                    ),
                    buy_max_price_slippage_bps=buy_max_price_slippage_bps,
                    buy_fee_allowance_bps=buy_fee_allowance_bps,
                )
                fleet_state = _fleet_state_for_result(result)
                record = scheduler_store.record_outcome(
                    position_id=position_id,
                    state=fleet_state,
                    now=now,
                    policy=policy,
                    cycle_id=(
                        None
                        if result.get("cycle_id") is None
                        else str(result["cycle_id"])
                    ),
                    runtime_phase=(
                        None if result.get("phase") is None else str(result["phase"])
                    ),
                    error=(
                        None if result.get("error") is None else str(result["error"])
                    ),
                )
                results.append(
                    {
                        "position_id": position_id,
                        "serviced": True,
                        "fleet_state": record.state.value,
                        "runtime": result,
                    }
                )
            except Exception as exc:
                # Failure isolation is intentional: one broken position is
                # recorded against itself and the fleet continues.
                record = scheduler_store.record_outcome(
                    position_id=position_id,
                    state=FleetPositionState.ERROR,
                    now=now,
                    policy=policy,
                    cycle_id=None if latest is None else latest.cycle_id,
                    runtime_phase=None if latest is None else latest.phase.value,
                    error=str(exc),
                )
                results.append(
                    {
                        "position_id": position_id,
                        "serviced": False,
                        "fleet_state": record.state.value,
                        "error": str(exc),
                    }
                )
    finally:
        scheduler_store.complete_tick(
            tick.tick_id,
            cursor_position_id=(None if not selected else selected[-1]),
            now=now,
        )

    return {
        "tick_id": tick.tick_id,
        "tick_sequence": tick.sequence,
        "breaker_mode": breaker_mode.value,
        "active_position_count": len(active_ids),
        "service_position_count": len(service_ids),
        "retirement_position_ids": retirement_ids,
        "selected_position_count": len(selected),
        "selected_position_ids": list(selected),
        "results": results,
        "fleet": list(
            scheduler_store.fleet_status(
                active_position_ids=service_ids,
                runtime_store=runtime_store,
                now=now,
                policy=policy,
            )
        ),
    }
