from __future__ import annotations

from dataclasses import replace
from typing import Any

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.executor_accounting import (
    executor_accounting_observation_from_raw,
)
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPRestartAction,
    VCAPRestartDecision,
    VCAPStateStore,
    reconcile_vcap_restart,
)


async def reconcile_persisted_vcap_state(
    client: Any,
    *,
    envelope_proposal_id: str,
    governor: CapitalGovernor,
    store: VCAPStateStore,
    now: float,
) -> VCAPRestartDecision:
    """Reconcile one persisted VCAP lifecycle after process restart.

    Only executor IDs reached through the state's exact RECOVERY proposal
    provenance are queried. Aggregate exchange/wallet balances are never used
    as ownership evidence, which prevents unrelated retained inventory from
    being commandeered by stale recovery state.
    """

    state = store.load(envelope_proposal_id)
    if state is None:
        raise ValueError(
            f"no persisted VCAP state for {envelope_proposal_id}"
        )

    observations = {}
    fetch_errors: list[str] = []

    for proposal_id in state.recovery_proposal_ids:
        provenance = governor.get_execution_provenance(proposal_id)
        if provenance is None:
            fetch_errors.append(f"{proposal_id}:missing_provenance")
            continue

        try:
            raw = await client.executors.get_executor(
                provenance.executor_id
            )
            observation = executor_accounting_observation_from_raw(
                raw,
                expected_executor_id=provenance.executor_id,
            )
            governor.reconcile_executor_accounting(observation)
            observations[proposal_id] = observation
        except Exception as exc:
            fetch_errors.append(
                f"{proposal_id}:live_reconciliation_failed:{exc}"
            )

    decision = reconcile_vcap_restart(
        state,
        governor=governor,
        observations_by_proposal=observations,
    )

    if fetch_errors:
        decision = VCAPRestartDecision(
            action=VCAPRestartAction.HOLD,
            state=state,
            confirmed_recovery_exposure_quote=(
                decision.confirmed_recovery_exposure_quote
            ),
            active_proposal_ids=decision.active_proposal_ids,
            requires_position_refresh=True,
            reasons=tuple(
                [
                    "restart_live_reconciliation_incomplete",
                    *fetch_errors,
                    *decision.reasons,
                ]
            ),
        )

    if decision.action == VCAPRestartAction.RETIRE:
        store.retire(
            state.envelope_proposal_id,
            updated_at=now,
        )
        retired = store.load(state.envelope_proposal_id)
        if retired is None:
            raise RuntimeError("retired VCAP state disappeared")
        return replace(decision, state=retired)

    status = (
        VCAPLifecycleStatus.ACTIVE
        if decision.action == VCAPRestartAction.RESUME
        else VCAPLifecycleStatus.HOLD
    )
    updated = replace(
        state,
        status=status,
        updated_at=float(now),
    )
    store.save(updated)
    return replace(decision, state=updated)
