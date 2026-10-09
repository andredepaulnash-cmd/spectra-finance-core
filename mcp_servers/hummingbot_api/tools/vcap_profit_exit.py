from __future__ import annotations

from typing import Any, Iterable

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.executor_accounting import (
    assess_recovery_exposure,
    executor_accounting_observation_from_raw,
)
from spectra_core.models import IntentKind
from spectra_core.vcap_profit_exit import (
    VCAPProfitExitAction,
    VCAPProfitExitDecision,
)


async def execute_vcap_profit_exit(
    client: Any,
    *,
    decision: VCAPProfitExitDecision,
    governor: CapitalGovernor,
    proposal_ids: Iterable[str],
) -> dict[str, Any]:
    """Reconcile legacy RECOVERY executors without dispatching new sales.

    New profit exits require the unified protected economic manifest. Each
    already-existing executor is fetched and reconciled through Spectra's
    strict accounting path, and assessed for remaining RECOVERY exposure.
    Exact replay is safe: an already-terminated executor is reconciled again
    without creating a second external order.
    """

    if decision.action != VCAPProfitExitAction.EXIT:
        return {
            "action": decision.action.value,
            "exit_requested": False,
            "reasons": list(decision.reasons),
        }

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in proposal_ids:
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValueError("proposal_ids must contain non-empty strings")
        proposal_id = raw_id.strip()
        if proposal_id in seen:
            continue
        seen.add(proposal_id)
        normalized.append(proposal_id)

    if not normalized:
        raise ValueError("at least one RECOVERY proposal_id is required")

    results: list[dict[str, Any]] = []
    all_released = True

    for proposal_id in normalized:
        provenance = governor.get_execution_provenance(proposal_id)
        if provenance is None:
            raise ValueError(
                f"missing execution provenance for proposal {proposal_id}"
            )
        if provenance.intent != IntentKind.RECOVERY:
            raise ValueError(
                f"proposal {proposal_id} is not RECOVERY intent"
            )
        if provenance.pair.strip().upper() != decision.pair:
            raise ValueError(
                f"proposal {proposal_id} pair does not match exit decision"
            )

        row: dict[str, Any] = {
            "proposal_id": proposal_id,
            "executor_id": provenance.executor_id,
        }

        try:
            raw = await client.executors.get_executor(
                provenance.executor_id
            )
            observation = executor_accounting_observation_from_raw(
                raw,
                expected_executor_id=provenance.executor_id,
            )
            delta = governor.reconcile_executor_accounting(observation)
            exposure = assess_recovery_exposure(
                observation,
                provenance,
            )

            released = exposure.exposure_quote == 0
            all_released = all_released and released

            row.update(
                {
                    "status": observation.status,
                    "close_type": observation.close_type,
                    "net_pnl_quote": (
                        None
                        if observation.net_pnl_quote is None
                        else str(observation.net_pnl_quote)
                    ),
                    "realized_pnl_quote": (
                        None
                        if observation.realized_pnl_quote is None
                        else str(observation.realized_pnl_quote)
                    ),
                    "remaining_recovery_exposure_quote": str(
                        exposure.exposure_quote
                    ),
                    "recovery_budget_released": released,
                    "accounting_filled_delta_quote": str(
                        delta.filled_delta_quote
                    ),
                    "accounting_fee_delta_quote": str(
                        delta.fee_delta_quote
                    ),
                }
            )
        except Exception as exc:
            all_released = False
            row["error"] = str(exc)

        results.append(row)

    return {
        "action": "EXIT",
        "exit_requested": False,
        "pair": decision.pair,
        "estimated_net_profit_bps": str(
            decision.estimated_net_profit_bps
        ),
        "all_recovery_budget_released": all_released,
        "capital_recycling_state": (
            "RELEASED"
            if all_released
            else "PENDING_RECONCILIATION"
        ),
        "executors": results,
        "reasons": [*decision.reasons, "legacy_profit_exit_requires_unified_protected_manifest"],
    }
