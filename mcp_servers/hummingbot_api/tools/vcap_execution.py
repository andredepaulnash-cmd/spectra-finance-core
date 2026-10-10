from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from mcp_servers.hummingbot_api.tools.executor_create import create_dca_executor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.vcap_execution import (
    VCAPExecutionDecision,
    VCAPLoopAction,
    carve_next_vcap_tranche,
)


async def execute_vcap_decision(
    client: Any,
    *,
    decision: VCAPExecutionDecision,
    governor: CapitalGovernor,
    account_name: str = "master_account",
    controller_id: str = "spectra",
    position_id: str | None = None,
    buy_max_price_slippage_bps: Decimal | None = None,
    buy_fee_allowance_bps: Decimal | None = None,
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

    account = account_name.strip()
    pair = proposal.pair.strip().upper()
    if not account or "-" not in pair or not pair.rsplit("-", 1)[1]:
        raise ValueError(
            "VCAP execution requires explicit account and pair quote scope"
        )
    scope = (account, "kraken", pair.rsplit("-", 1)[1])
    for field, actual in zip(("account_name", "connector_name", "quote_asset"), scope):
        supplied = getattr(proposal, field)
        if supplied is not None and supplied != actual:
            raise ValueError("VCAP proposal scope does not match execution caller")
    proposal = replace(
        proposal,
        account_name=scope[0],
        connector_name=scope[1],
        quote_asset=scope[2],
    )
    decision = replace(decision, next_proposal=proposal)

    existing_executor = governor.get_execution_receipt(proposal.proposal_id)
    if existing_executor is not None:
        provenance = governor.get_execution_provenance(proposal.proposal_id)
        cash_active = governor.cash_authority_snapshot() is not None
        known_mismatch = provenance is not None and (
            provenance.executor_id != existing_executor
            or provenance.pair.strip().upper() != pair
            or (
                provenance.account_name is not None
                and provenance.account_name != account
            )
            or (
                provenance.connector_name is not None
                and provenance.connector_name.lower() != "kraken"
            )
        )
        incomplete = provenance is None or (
            provenance.account_name is None or provenance.connector_name is None
        )
        if known_mismatch or (cash_active and incomplete):
            raise RuntimeError("VCAP replay execution provenance scope changed")
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
        amounts_quote=[str(proposal.amount_quote)],
        prices=[str(decision.current_price)],
        leverage=1,
        level_id=f"vcap-{decision.sequence:04d}",
        account_name=account,
        controller_id=controller_id,
        spectra_proposal_id=proposal.proposal_id,
        spectra_intent="RECOVERY",
        spectra_position_id=position_id,
        spectra_max_price_slippage_bps=buy_max_price_slippage_bps,
        spectra_fee_allowance_bps=buy_fee_allowance_bps,
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
