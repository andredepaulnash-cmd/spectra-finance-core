from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from .accounting_health import AccountingHealthDecision
from .asset_fingerprint import AssetFingerprint
from .capital_governor import CapitalGovernor
from .economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
)
from .kraken_market_fabric import KrakenTradingRule
from .partial_recycle import (
    PartialRecycleAction,
    PartialRecycleDecision,
    PartialRecyclePolicy,
    plan_partial_recycle,
)
from .recovery_fingerprint import (
    RecoveryFingerprintPolicy,
    VCAPPositionState,
    VCAPRecoveryPolicy,
)
from .vcap_execution import (
    VCAPExecutionDecision,
    VCAPLoopAction,
    replan_vcap_sequence,
)
from .vcap_profit_exit import (
    VCAPExitCostModel,
    VCAPProfitExitAction,
    VCAPProfitExitDecision,
    VCAPProfitExitPolicy,
    plan_vcap_profit_exit,
)
from .vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)
from .vcap_tranches import VCAPTranchePolicy


ZERO = Decimal("0")


class VCAPLifecycleAction(StrEnum):
    EXECUTE_TRANCHE = "EXECUTE_TRANCHE"
    HOLD = "HOLD"
    PROTECT_PROFIT = "PROTECT_PROFIT"
    PARTIAL_RECYCLE = "PARTIAL_RECYCLE"
    EXIT = "EXIT"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class VCAPLifecycleDecision:
    action: VCAPLifecycleAction
    state: VCAPLifecycleState
    profit_decision: VCAPProfitExitDecision | None
    execution_decision: VCAPExecutionDecision | None
    reasons: tuple[str, ...]
    partial_recycle_decision: PartialRecycleDecision | None = None


def _checkpoint_state(
    state: VCAPLifecycleState,
    *,
    position: VCAPPositionState,
    now: float,
    profit_decision: VCAPProfitExitDecision | None,
    status: VCAPLifecycleStatus | None = None,
    recovery_proposal_ids: tuple[str, ...] | None = None,
) -> VCAPLifecycleState:
    return replace(
        state,
        position_base=position.position_base,
        cost_basis_quote=position.cost_basis_quote,
        last_known_price=position.current_price,
        profit_state=(
            state.profit_state
            if profit_decision is None
            else profit_decision.state
        ),
        recovery_proposal_ids=(
            state.recovery_proposal_ids
            if recovery_proposal_ids is None
            else recovery_proposal_ids
        ),
        status=state.status if status is None else status,
        updated_at=float(now),
    )


def _append_proposal_id(
    proposal_ids: tuple[str, ...],
    proposal_id: str,
) -> tuple[str, ...]:
    if proposal_id in proposal_ids:
        return proposal_ids
    return (*proposal_ids, proposal_id)


def plan_vcap_lifecycle(
    *,
    state: VCAPLifecycleState,
    store: VCAPStateStore,
    position: VCAPPositionState,
    fingerprint: AssetFingerprint,
    trading_rule: KrakenTradingRule,
    seed_quote: Decimal,
    costs: VCAPExitCostModel,
    now: float,
    leverage: Decimal = Decimal("1"),
    fingerprint_policy: RecoveryFingerprintPolicy | None = None,
    recovery_policy: VCAPRecoveryPolicy | None = None,
    tranche_policy: VCAPTranchePolicy | None = None,
    profit_policy: VCAPProfitExitPolicy | None = None,
) -> VCAPLifecycleDecision:
    """Run one deterministic VCAP lifecycle planning cycle and checkpoint it.

    Lifecycle priority is intentional:
      1. retired state stays complete;
      2. protected profit can never fall back into recovery patching;
      3. profit exits outrank new recovery capital;
      4. below the profit floor, PR #9 may replan exactly one tranche;
      5. every planned external mutation is checkpointed before execution.

    The function never places an exchange order. It persists the exact intent
    first so a crash cannot make the next external mutation invisible to restart
    reconciliation.
    """

    if state.pair != position.pair:
        raise ValueError("persisted VCAP pair does not match position")
    if state.pair != fingerprint.trading_pair:
        raise ValueError("persisted VCAP pair does not match fingerprint")
    if state.pair != trading_rule.trading_pair.strip().upper():
        raise ValueError("persisted VCAP pair does not match trading rule")

    if state.status == VCAPLifecycleStatus.RETIRED:
        decision = VCAPLifecycleDecision(
            action=VCAPLifecycleAction.COMPLETE,
            state=state,
            profit_decision=None,
            execution_decision=None,
            reasons=("lifecycle_already_retired",),
        )
        store.save(state)
        return decision

    profit = plan_vcap_profit_exit(
        position,
        costs=costs,
        now=now,
        fingerprint=fingerprint,
        previous_state=state.profit_state,
        policy=profit_policy,
    )

    checkpoint = _checkpoint_state(
        state,
        position=position,
        now=now,
        profit_decision=profit,
        status=VCAPLifecycleStatus.ACTIVE,
    )

    if profit.action == VCAPProfitExitAction.EXIT:
        store.save(checkpoint)
        return VCAPLifecycleDecision(
            action=VCAPLifecycleAction.EXIT,
            state=checkpoint,
            profit_decision=profit,
            execution_decision=None,
            reasons=tuple(profit.reasons) + ("profit_exit_has_priority",),
        )

    if (
        profit.action == VCAPProfitExitAction.EXTEND
        and profit.profit_floor_reached
    ):
        store.save(checkpoint)
        return VCAPLifecycleDecision(
            action=VCAPLifecycleAction.PROTECT_PROFIT,
            state=checkpoint,
            profit_decision=profit,
            execution_decision=None,
            reasons=tuple(profit.reasons)
            + ("recovery_patching_disabled_after_profit_floor",),
        )

    execution = replan_vcap_sequence(
        envelope_proposal_id=state.envelope_proposal_id,
        sequence=state.next_sequence,
        pair=state.pair,
        seed_quote=seed_quote,
        remaining_authorized_quote=state.remaining_authorized_quote,
        position=position,
        fingerprint=fingerprint,
        trading_rule=trading_rule,
        current_price=position.current_price,
        now=now,
        leverage=leverage,
        fingerprint_policy=fingerprint_policy,
        recovery_policy=recovery_policy,
        tranche_policy=tranche_policy,
    )

    if execution.action == VCAPLoopAction.EXECUTE:
        if execution.next_proposal is None:
            raise RuntimeError("EXECUTE lifecycle decision missing proposal")
        proposal_ids = _append_proposal_id(
            checkpoint.recovery_proposal_ids,
            execution.next_proposal.proposal_id,
        )
        checkpoint = _checkpoint_state(
            checkpoint,
            position=position,
            now=now,
            profit_decision=profit,
            status=VCAPLifecycleStatus.ACTIVE,
            recovery_proposal_ids=proposal_ids,
        )
        store.save(checkpoint)
        return VCAPLifecycleDecision(
            action=VCAPLifecycleAction.EXECUTE_TRANCHE,
            state=checkpoint,
            profit_decision=profit,
            execution_decision=execution,
            reasons=tuple(profit.reasons)
            + tuple(execution.reasons)
            + ("external_mutation_checkpointed",),
        )

    # PR #9 STOP means no more patch is justified now; it does not mean the
    # economic lifecycle is complete. Until PR #10 realizes the profit exit,
    # the position remains owned and monitored.
    lifecycle_reason = (
        "awaiting_profitable_exit"
        if execution.action == VCAPLoopAction.STOP
        else "recovery_cycle_on_hold"
    )
    hold_state = replace(
        checkpoint,
        status=VCAPLifecycleStatus.HOLD,
    )
    store.save(hold_state)
    return VCAPLifecycleDecision(
        action=VCAPLifecycleAction.HOLD,
        state=hold_state,
        profit_decision=profit,
        execution_decision=execution,
        reasons=tuple(profit.reasons)
        + tuple(execution.reasons)
        + (lifecycle_reason,),
    )


def checkpoint_vcap_tranche_execution(
    *,
    decision: VCAPLifecycleDecision,
    governor: CapitalGovernor,
    store: VCAPStateStore,
    now: float,
) -> VCAPLifecycleState:
    """Advance sequence/envelope only after exact execution receipt exists.

    Exact replay is idempotent. A planned proposal without a receipt remains in
    the durable ownership set but does not consume the persisted remaining
    envelope or advance the sequence.
    """

    if decision.action != VCAPLifecycleAction.EXECUTE_TRANCHE:
        raise ValueError("lifecycle decision is not EXECUTE_TRANCHE")
    execution = decision.execution_decision
    if execution is None or execution.next_proposal is None:
        raise ValueError("execution decision is incomplete")

    proposal = execution.next_proposal
    receipt = governor.get_execution_receipt(proposal.proposal_id)
    if receipt is None:
        hold = replace(
            decision.state,
            status=VCAPLifecycleStatus.HOLD,
            updated_at=float(now),
        )
        store.save(hold)
        return hold

    state = store.load(decision.state.envelope_proposal_id)
    if state is None:
        raise RuntimeError("VCAP lifecycle checkpoint disappeared")

    # The same successful result can be replayed without subtracting capital
    # twice. Sequence is the durable monotonic marker.
    if state.next_sequence > execution.sequence:
        return state
    if state.next_sequence != execution.sequence:
        raise RuntimeError("VCAP sequence diverged during execution checkpoint")

    amount = proposal.amount_quote
    if amount > state.remaining_authorized_quote:
        raise RuntimeError("executed tranche exceeds persisted recovery envelope")

    updated = replace(
        state,
        next_sequence=state.next_sequence + 1,
        remaining_authorized_quote=(
            state.remaining_authorized_quote - amount
        ),
        status=VCAPLifecycleStatus.ACTIVE,
        updated_at=float(now),
    )
    store.save(updated)
    return updated


def checkpoint_vcap_exit_result(
    *,
    state: VCAPLifecycleState,
    store: VCAPStateStore,
    recovery_budget_released: bool,
    now: float,
) -> VCAPLifecycleState:
    """Persist terminal exit reconciliation.

    A close request alone never completes the lifecycle. Only confirmed release
    of all recovery exposure retires the VCAP state.
    """

    if recovery_budget_released:
        retired = replace(
            state,
            status=VCAPLifecycleStatus.RETIRED,
            remaining_authorized_quote=ZERO,
            updated_at=float(now),
        )
        store.save(retired)
        return retired

    hold = replace(
        state,
        status=VCAPLifecycleStatus.HOLD,
        updated_at=float(now),
    )
    store.save(hold)
    return hold



def plan_vcap_lifecycle_from_ledger(
    *,
    position_id: str,
    ledger: EconomicPositionLedger,
    state: VCAPLifecycleState,
    store: VCAPStateStore,
    fingerprint: AssetFingerprint,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    seed_quote: Decimal,
    expected_exit_fee_bps: Decimal,
    slippage_allowance_bps: Decimal,
    now: float,
    leverage: Decimal = Decimal("1"),
    fingerprint_policy: RecoveryFingerprintPolicy | None = None,
    recovery_policy: VCAPRecoveryPolicy | None = None,
    tranche_policy: VCAPTranchePolicy | None = None,
    profit_policy: VCAPProfitExitPolicy | None = None,
    partial_recycle_policy: PartialRecyclePolicy | None = None,
    governor: CapitalGovernor | None = None,
    accounting_health: AccountingHealthDecision | None = None,
) -> VCAPLifecycleDecision:
    """Plan one VCAP cycle from authoritative economic-position ownership.

    Only ACTIVE_TRADE lots participate in recovery and profit geometry.
    STRATEGIC_RETAINED and UNATTRIBUTED inventory may share the same asset and
    account but are excluded by the ledger snapshot and cannot be commandeered
    by this controller.
    """

    if accounting_health is None:
        hold_state = replace(
            state,
            status=VCAPLifecycleStatus.HOLD,
            updated_at=float(now),
        )
        store.save(hold_state)
        return VCAPLifecycleDecision(
            action=VCAPLifecycleAction.HOLD,
            state=hold_state,
            profit_decision=None,
            execution_decision=None,
            reasons=(
                "accounting_health_gate_blocked",
                "accounting_health_missing",
            ),
        )

    if accounting_health.position_id != position_id:
        raise ValueError(
            "accounting health belongs to another economic position"
        )

    if not accounting_health.allows_live_mutation(now=now):
        hold_state = replace(
            state,
            status=VCAPLifecycleStatus.HOLD,
            updated_at=float(now),
        )
        store.save(hold_state)
        return VCAPLifecycleDecision(
            action=VCAPLifecycleAction.HOLD,
            state=hold_state,
            profit_decision=None,
            execution_decision=None,
            reasons=(
                "accounting_health_gate_blocked",
                f"accounting_health_{accounting_health.state.value.lower()}",
                *accounting_health.reasons,
            ),
        )

    snapshot = ledger.snapshot(position_id)
    if snapshot.pair != state.pair:
        raise ValueError(
            "economic position pair does not match VCAP lifecycle state"
        )
    if snapshot.status != EconomicPositionStatus.ACTIVE:
        raise ValueError("economic position is not active")

    position = snapshot.to_vcap_position(current_price=current_price)
    costs = VCAPExitCostModel(
        accrued_fees_quote=snapshot.active_entry_fees_quote,
        expected_exit_fee_bps=expected_exit_fee_bps,
        slippage_allowance_bps=slippage_allowance_bps,
    )

    # Whole-position profit protection remains higher priority than parcel
    # harvesting. Only when the economic position itself is below that exit
    # path do we consider monetizing independently profitable RECOVERY owners.
    profit = plan_vcap_profit_exit(
        position,
        costs=costs,
        now=now,
        fingerprint=fingerprint,
        previous_state=state.profit_state,
        policy=profit_policy,
    )
    if (
        governor is not None
        and profit.action not in {
            VCAPProfitExitAction.EXIT,
            VCAPProfitExitAction.EXTEND,
        }
    ):
        partial = plan_partial_recycle(
            ledger=ledger,
            governor=governor,
            position_id=position_id,
            trading_rule=trading_rule,
            current_price=current_price,
            expected_exit_fee_bps=expected_exit_fee_bps,
            slippage_allowance_bps=slippage_allowance_bps,
            now=now,
            policy=partial_recycle_policy,
        )
        if partial.action == PartialRecycleAction.RECYCLE:
            checkpoint = _checkpoint_state(
                state,
                position=position,
                now=now,
                profit_decision=profit,
                status=VCAPLifecycleStatus.ACTIVE,
            )
            store.save(checkpoint)
            return VCAPLifecycleDecision(
                action=VCAPLifecycleAction.PARTIAL_RECYCLE,
                state=checkpoint,
                profit_decision=profit,
                execution_decision=None,
                reasons=tuple(profit.reasons) + tuple(partial.reasons),
                partial_recycle_decision=partial,
            )

    return plan_vcap_lifecycle(
        state=state,
        store=store,
        position=position,
        fingerprint=fingerprint,
        trading_rule=trading_rule,
        seed_quote=seed_quote,
        costs=costs,
        now=now,
        leverage=leverage,
        fingerprint_policy=fingerprint_policy,
        recovery_policy=recovery_policy,
        tranche_policy=tranche_policy,
        profit_policy=profit_policy,
    )
