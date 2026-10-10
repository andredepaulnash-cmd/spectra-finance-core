from __future__ import annotations

from typing import Any

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.vcap_execution import (
    VCAPExecutionDecision,
    VCAPLoopAction,
    carve_next_vcap_tranche,
)

from mcp_servers.hummingbot_api.tools.executor_create import create_dca_executor


async def execute_vcap_decision(
    client: Any,
    *,
    decision: VCAPExecutionDecision,
    governor: CapitalGovernor,
    account_name: str = "master_account",
    controller_id: str = "spectra",
    position_id: str | None = None,
) -> dict[str, Any]:
    """Execute one prevalidated VCAP tranche through the Spectra DCA path.

    Exact replay is idempotent. If the child proposal already has an execution
    receipt, the existing executor is returned and no new external mutation is
    attempted. If capital was committed but no receipt exists, the normal
    execution gate refuses a blind retry.
    """
    if decision.action != VCAPLoopAction.EXECUTE:
        return {
            "action": decision.action.value,
            "executed": False,
            "reasons": list(decision.reasons),
        }

    proposal = decision.next_proposal
    tranche = decision.next_tranche
    if proposal is None or tranche is None:
        raise ValueError("EXECUTE decision is missing tranche execution data")

    existing_executor = governor.get_execution_receipt(proposal.proposal_id)
    if existing_executor is not None:
        return {
            "action": "EXECUTE",
            "executed": True,
            "idempotent_replay": True,
            "proposal_id": proposal.proposal_id,
            "executor_id": existing_executor,
        }

    carved = carve_next_vcap_tranche(governor, decision)
    if not carved:
        return {
            "action": "EXECUTE",
            "executed": False,
            "proposal_id": proposal.proposal_id,
            "error": "VCAP recovery envelope could not carve the next tranche",
        }

    result = await create_dca_executor(
        client,
        connector_name="kraken",
        trading_pair=proposal.pair,
        side=1,
        amounts_quote=[float(proposal.amount_quote)],
        prices=[float(decision.current_price)],
        leverage=1,
        level_id=f"vcap-{decision.sequence:04d}",
        account_name=account_name,
        controller_id=controller_id,
        spectra_proposal_id=proposal.proposal_id,
        spectra_intent="RECOVERY",
        spectra_position_id=position_id,
    )

    return {
        "action": "EXECUTE",
        "executed": "error" not in result,
        "proposal_id": proposal.proposal_id,
        "tranche_sequence": decision.sequence,
        "quote_amount": str(proposal.amount_quote),
        "base_amount": str(tranche.base_amount),
        "result": result,
    }
