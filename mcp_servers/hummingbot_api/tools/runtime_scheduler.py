from __future__ import annotations

from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping

from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
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

from mcp_servers.hummingbot_api.tools.vcap_runtime import (
    run_vcap_runtime_cycle,
)


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
) -> dict[str, Any]:
    """Service a bounded fleet of active economic positions.

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

    # Explicit bindings are a migration/bootstrap surface, not a pair-based
    # guess. Once persisted, a position/envelope relationship is immutable.
    if position_envelopes is not None:
        unknown = sorted(set(position_envelopes) - active_set)
        if unknown:
            raise ValueError(
                "cannot bind inactive economic positions: " + ", ".join(unknown)
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
    for position_id in active_ids:
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
        active_position_ids=active_ids,
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
                        None
                        if result.get("phase") is None
                        else str(result["phase"])
                    ),
                    error=(
                        None
                        if result.get("error") is None
                        else str(result["error"])
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
        "selected_position_count": len(selected),
        "selected_position_ids": list(selected),
        "results": results,
        "fleet": list(
            scheduler_store.fleet_status(
                active_position_ids=active_ids,
                runtime_store=runtime_store,
                now=now,
                policy=policy,
            )
        ),
    }
