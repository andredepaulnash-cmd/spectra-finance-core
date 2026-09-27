from __future__ import annotations

from typing import Any

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_exit import (
    EconomicExitManifestStore,
    EconomicExitProgressState,
    assess_position_exit_manifest,
    create_or_load_position_exit_manifest,
)
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_accounting import (
    assess_recovery_exposure,
    executor_accounting_observation_from_raw,
)
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import IntentKind
from spectra_core.vcap_profit_exit import (
    VCAPProfitExitAction,
    VCAPProfitExitDecision,
)


async def execute_economic_position_exit(
    client: Any,
    *,
    decision: VCAPProfitExitDecision,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    position_id: str,
    trading_rule: KrakenTradingRule,
    now: float,
) -> dict[str, Any]:
    """Request closure of every exact ACTIVE_TRADE owner in one position.

    The immutable manifest is built from economic lots, never wallet balance.
    ENTRY and RECOVERY executors are both eligible when they own active lots.
    STRATEGIC_RETAINED and UNATTRIBUTED inventory never appear in the manifest.

    A stop request is not treated as economic completion. Confirmed SELL fills
    must later reconcile through the exact executor/order/trade bridge and drive
    ACTIVE_TRADE inventory to zero.
    """

    if decision.action != VCAPProfitExitAction.EXIT:
        return {
            "action": decision.action.value,
            "exit_requested": False,
            "reasons": list(decision.reasons),
        }

    if decision.pair != trading_rule.trading_pair.strip().upper():
        raise ValueError("profit exit pair does not match Kraken trading rule")

    store = EconomicExitManifestStore(ledger.path)
    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=governor,
        position_id=position_id,
        trading_rule=trading_rule,
        current_price=decision.current_price,
        now=now,
        store=store,
    )

    owner_results: list[dict[str, Any]] = []
    all_stop_requests_accepted = True
    all_recovery_budget_released = True
    attempted = False

    for owner in manifest.owners:
        active_lot_ids = ledger.active_lot_ids_for_provenance(
            position_id=manifest.position_id,
            proposal_id=owner.proposal_id,
            executor_id=owner.executor_id,
        )
        row: dict[str, Any] = {
            "proposal_id": owner.proposal_id,
            "executor_id": owner.executor_id,
            "intent": owner.intent.value,
            "target_base": str(owner.target_base),
            "active_lot_ids": list(active_lot_ids),
        }

        if not active_lot_ids:
            row["state"] = "ECONOMICALLY_FLAT"
            owner_results.append(row)
            continue

        attempted = True
        try:
            stop_result = await client.executors.stop_executor(
                executor_id=owner.executor_id,
                keep_position=False,
            )
            row["stop_result"] = stop_result
            row["state"] = "STOP_REQUESTED"

            raw = await client.executors.get_executor(owner.executor_id)
            observation = executor_accounting_observation_from_raw(
                raw,
                expected_executor_id=owner.executor_id,
            )
            delta = governor.reconcile_executor_accounting(observation)
            row.update(
                {
                    "executor_status": observation.status,
                    "close_type": observation.close_type,
                    "accounting_filled_delta_quote": str(
                        delta.filled_delta_quote
                    ),
                    "accounting_fee_delta_quote": str(
                        delta.fee_delta_quote
                    ),
                }
            )

            if owner.intent == IntentKind.RECOVERY:
                exposure = assess_recovery_exposure(
                    observation,
                    governor.get_execution_provenance(
                        owner.proposal_id
                    ),
                )
                released = exposure.exposure_quote == 0
                all_recovery_budget_released = (
                    all_recovery_budget_released and released
                )
                row["remaining_recovery_exposure_quote"] = str(
                    exposure.exposure_quote
                )
                row["recovery_budget_released"] = released
        except Exception as exc:
            all_stop_requests_accepted = False
            if owner.intent == IntentKind.RECOVERY:
                all_recovery_budget_released = False
            row["state"] = "STOP_OUTCOME_AMBIGUOUS"
            row["error"] = str(exc)

        owner_results.append(row)

    progress = assess_position_exit_manifest(
        manifest,
        ledger=ledger,
        store=store,
        now=now,
    )

    return {
        "action": "EXIT",
        "exit_requested": attempted or progress.state == EconomicExitProgressState.COMPLETED,
        "manifest_id": manifest.manifest_id,
        "position_id": manifest.position_id,
        "pair": manifest.pair,
        "target_active_base": str(manifest.target_active_base),
        "remaining_active_base": str(progress.remaining_active_base),
        "realized_net_pnl_quote": str(progress.realized_pnl_quote),
        "strategic_retained_base": str(
            progress.strategic_retained_base
        ),
        "retained_inventory_in_manifest": False,
        "all_stop_requests_accepted": all_stop_requests_accepted,
        "all_recovery_budget_released": all_recovery_budget_released,
        "economic_exit_state": progress.state.value,
        "capital_recycling_state": (
            "RELEASED"
            if (
                progress.state == EconomicExitProgressState.COMPLETED
                and all_recovery_budget_released
            )
            else (
                "DUST_BLOCKED"
                if progress.state == EconomicExitProgressState.DUST_BLOCKED
                else "PENDING_RECONCILIATION"
            )
        ),
        "owners": owner_results,
        "reasons": [
            *decision.reasons,
            *progress.reasons,
        ],
    }
