from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Awaitable, Callable

from mcp_servers.hummingbot_api.tools.economic_exit import (
    execute_economic_position_exit,
)
from mcp_servers.hummingbot_api.tools.partial_recycle import (
    execute_partial_recycle,
)
from mcp_servers.hummingbot_api.tools.vcap_execution import (
    execute_vcap_decision,
)
from spectra_core.accounting_health import AccountingHealthDecision
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.partial_attempt import PartialAttemptAssessment
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    VCAPLifecycleDecision,
    VCAPStateStore,
    checkpoint_vcap_exit_result,
    checkpoint_vcap_tranche_execution,
)
from spectra_core.vcap_state import VCAPLifecycleStatus


async def execute_vcap_lifecycle_decision(
    client: Any,
    *,
    decision: VCAPLifecycleDecision,
    governor: CapitalGovernor,
    store: VCAPStateStore,
    now: float,
    position_id: str | None = None,
    ledger: EconomicPositionLedger | None = None,
    trading_rule: KrakenTradingRule | None = None,
    current_price: Decimal | None = None,
    accounting_health: AccountingHealthDecision | None = None,
    account_name: str = "master_account",
    controller_id: str = "spectra",
    partial_attempt_guard: (
        Callable[[], Awaitable[PartialAttemptAssessment]] | None
    ) = None,
) -> dict[str, Any]:
    """Dispatch one unified VCAP lifecycle decision.

    Planning/checkpointing is done before this function is called. External
    mutation is therefore never invisible to the durable lifecycle state.
    """

    mutating = decision.action in {
        VCAPLifecycleAction.EXECUTE_TRANCHE,
        VCAPLifecycleAction.PARTIAL_RECYCLE,
        VCAPLifecycleAction.EXIT,
    }
    if mutating:
        blocked_reason: str | None = None
        if accounting_health is None:
            blocked_reason = "accounting_health_missing"
        elif position_id is None:
            blocked_reason = "economic_position_id_missing"
        elif accounting_health.position_id != position_id:
            blocked_reason = "accounting_health_position_mismatch"
        elif not accounting_health.allows_live_mutation(now=now):
            blocked_reason = (
                f"accounting_health_{accounting_health.state.value.lower()}"
            )

        if blocked_reason is not None:
            checkpoint = replace(
                decision.state,
                status=VCAPLifecycleStatus.HOLD,
                updated_at=float(now),
            )
            store.save(checkpoint)
            reasons = ["accounting_health_gate_blocked", blocked_reason]
            if accounting_health is not None:
                reasons.extend(accounting_health.reasons)
            return {
                "action": decision.action.value,
                "executed": False,
                "lifecycle_status": checkpoint.status.value,
                "reasons": reasons,
            }

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
            "remaining_authorized_quote": str(checkpoint.remaining_authorized_quote),
        }

    if decision.action == VCAPLifecycleAction.PARTIAL_RECYCLE:
        if (
            position_id is None
            or ledger is None
            or trading_rule is None
            or current_price is None
        ):
            checkpoint = replace(
                decision.state,
                status=VCAPLifecycleStatus.HOLD,
                updated_at=float(now),
            )
            store.save(checkpoint)
            return {
                "action": decision.action.value,
                "executed": False,
                "lifecycle_status": checkpoint.status.value,
                "error": (
                    "partial recycle requires position_id, ledger, "
                    "current trading_rule, and current_price"
                ),
            }
        if decision.partial_recycle_decision is None:
            raise ValueError(
                "PARTIAL_RECYCLE lifecycle decision is missing recycle decision"
            )

        result = await execute_partial_recycle(
            client,
            decision=decision.partial_recycle_decision,
            governor=governor,
            ledger=ledger,
            trading_rule=trading_rule,
            current_price=current_price,
            now=now,
            account_name=account_name,
            controller_id=controller_id,
            attempt_guard=partial_attempt_guard,
        )
        return {
            "action": decision.action.value,
            "executed": bool(result.get("executed")),
            "manifest_id": result.get("manifest_id"),
            "sell_executor_id": result.get("sell_executor_id"),
            "result": result,
            "lifecycle_status": decision.state.status.value,
        }

    if decision.action == VCAPLifecycleAction.EXIT:
        if position_id is None or ledger is None or trading_rule is None:
            checkpoint = checkpoint_vcap_exit_result(
                state=decision.state,
                store=store,
                recovery_budget_released=False,
                now=now,
            )
            return {
                "action": decision.action.value,
                "exit_requested": False,
                "executed": False,
                "lifecycle_status": checkpoint.status.value,
                "error": (
                    "unified economic exit requires position_id, ledger, "
                    "and current Kraken trading_rule"
                ),
            }

        if decision.profit_decision is None:
            raise ValueError("EXIT lifecycle decision is missing profit decision")

        result = await execute_economic_position_exit(
            client,
            decision=decision.profit_decision,
            governor=governor,
            ledger=ledger,
            position_id=position_id,
            trading_rule=trading_rule,
            now=now,
        )

        economically_complete = result.get(
            "economic_exit_state"
        ) == "COMPLETED" and bool(result.get("all_recovery_budget_released"))
        checkpoint = checkpoint_vcap_exit_result(
            state=decision.state,
            store=store,
            recovery_budget_released=economically_complete,
            now=now,
        )

        return {
            "action": decision.action.value,
            "exit_requested": bool(result.get("exit_requested")),
            "executed": bool(result.get("exit_requested")),
            "manifest_id": result.get("manifest_id"),
            "result": result,
            "lifecycle_status": checkpoint.status.value,
        }

    return {
        "action": decision.action.value,
        "executed": False,
        "lifecycle_status": decision.state.status.value,
        "reasons": list(decision.reasons),
    }
