from __future__ import annotations

from typing import Any

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    VCAPLifecycleDecision,
    VCAPStateStore,
    checkpoint_vcap_exit_result,
    checkpoint_vcap_tranche_execution,
)

from mcp_servers.hummingbot_api.tools.vcap_execution import (
    execute_vcap_decision,
)
from mcp_servers.hummingbot_api.tools.vcap_profit_exit import (
    execute_vcap_profit_exit,
)


async def execute_vcap_lifecycle_decision(
    client: Any,
    *,
    decision: VCAPLifecycleDecision,
    governor: CapitalGovernor,
    store: VCAPStateStore,
    now: float,
    account_name: str = "master_account",
    controller_id: str = "spectra",
) -> dict[str, Any]:
    """Dispatch one unified VCAP lifecycle decision.

    Planning/checkpointing is done before this function is called. External
    mutation is therefore never invisible to the durable lifecycle state.
    """

    if decision.action == VCAPLifecycleAction.EXECUTE_TRANCHE:
        execution = decision.execution_decision
        if execution is None:
            raise ValueError("EXECUTE_TRANCHE is missing execution decision")

        result = await execute_vcap_decision(
            client,
            decision=execution,
            governor=governor,
            account_name=account_name,
            controller_id=controller_id,
        )

        checkpoint = checkpoint_vcap_tranche_execution(
            decision=decision,
            governor=governor,
            store=store,
            now=now,
        )

        return {
            "action": decision.action.value,
            "result": result,
            "lifecycle_status": checkpoint.status.value,
            "next_sequence": checkpoint.next_sequence,
            "remaining_authorized_quote": str(
                checkpoint.remaining_authorized_quote
            ),
        }

    if decision.action == VCAPLifecycleAction.EXIT:
        missing = [
            proposal_id
            for proposal_id in decision.state.recovery_proposal_ids
            if governor.get_execution_provenance(proposal_id) is None
        ]
        if missing:
            checkpoint = checkpoint_vcap_exit_result(
                state=decision.state,
                store=store,
                recovery_budget_released=False,
                now=now,
            )
            return {
                "action": decision.action.value,
                "exit_requested": False,
                "lifecycle_status": checkpoint.status.value,
                "error": (
                    "VCAP exit ownership is incomplete; refusing external "
                    "mutation until exact proposal provenance reconciles"
                ),
                "missing_proposal_provenance": missing,
            }

        result = await execute_vcap_profit_exit(
            client,
            decision=decision.profit_decision,
            governor=governor,
            proposal_ids=decision.state.recovery_proposal_ids,
        )

        checkpoint = checkpoint_vcap_exit_result(
            state=decision.state,
            store=store,
            recovery_budget_released=bool(
                result.get("all_recovery_budget_released")
            ),
            now=now,
        )

        return {
            "action": decision.action.value,
            "result": result,
            "lifecycle_status": checkpoint.status.value,
        }

    return {
        "action": decision.action.value,
        "executed": False,
        "lifecycle_status": decision.state.status.value,
        "reasons": list(decision.reasons),
    }
