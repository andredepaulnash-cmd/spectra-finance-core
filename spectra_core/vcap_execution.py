from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite

from .asset_fingerprint import AssetFingerprint
from .capital_governor import CapitalGovernor
from .kraken_market_fabric import KrakenTradingRule
from .models import CapitalProposal, IntentKind
from .recovery_fingerprint import (
    RecoveryFingerprintPolicy,
    VCAPPositionState,
    VCAPRecoveryPlan,
    VCAPRecoveryPolicy,
    plan_vcap_recovery,
)
from .vcap_tranches import (
    VCAPTranche,
    VCAPTranchePlan,
    VCAPTranchePolicy,
    plan_vcap_tranches,
)


ZERO = Decimal("0")


class VCAPLoopAction(StrEnum):
    EXECUTE = "EXECUTE"
    HOLD = "HOLD"
    STOP = "STOP"


@dataclass(frozen=True)
class VCAPExecutionDecision:
    envelope_proposal_id: str
    sequence: int
    action: VCAPLoopAction
    remaining_authorized_quote: Decimal
    current_price: Decimal
    recovery_plan: VCAPRecoveryPlan | None
    tranche_plan: VCAPTranchePlan | None
    next_tranche: VCAPTranche | None
    next_proposal: CapitalProposal | None
    reasons: tuple[str, ...]


def _positive_decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite() or value <= ZERO:
        raise ValueError(f"{field} must be finite and positive")
    return value


def _non_negative_decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{field} must be finite and non-negative")
    return value


def vcap_tranche_proposal_id(
    envelope_proposal_id: str,
    sequence: int,
) -> str:
    envelope = envelope_proposal_id.strip()
    if not envelope:
        raise ValueError("envelope_proposal_id is required")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    return f"{envelope}:vcap:{sequence:04d}"


def replan_vcap_sequence(
    *,
    envelope_proposal_id: str,
    sequence: int,
    pair: str,
    seed_quote: Decimal,
    remaining_authorized_quote: Decimal,
    position: VCAPPositionState,
    fingerprint: AssetFingerprint,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    now: float,
    leverage: Decimal = Decimal("1"),
    fingerprint_policy: RecoveryFingerprintPolicy | None = None,
    recovery_policy: VCAPRecoveryPolicy | None = None,
    tranche_policy: VCAPTranchePolicy | None = None,
) -> VCAPExecutionDecision:
    """Recompute VCAP after a fill and decide execute, hold, or stop.

    This is deliberately one-tranche-at-a-time. It never carries an old ladder
    forward blindly. Current position geometry, current fingerprint evidence,
    current exchange rules, and the remaining reserved recovery envelope are
    all reconsidered before another tranche is eligible to execute.
    """
    envelope = envelope_proposal_id.strip()
    if not envelope:
        raise ValueError("envelope_proposal_id is required")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("sequence must be a positive integer")

    normalized_pair = pair.strip().upper()
    if not normalized_pair:
        raise ValueError("pair is required")
    if normalized_pair != position.pair:
        raise ValueError("pair does not match position")
    if normalized_pair != fingerprint.trading_pair:
        raise ValueError("pair does not match fingerprint")
    if normalized_pair != trading_rule.trading_pair.strip().upper():
        raise ValueError("pair does not match trading rule")

    seed_quote = _positive_decimal(seed_quote, "seed_quote")
    remaining = _non_negative_decimal(
        remaining_authorized_quote,
        "remaining_authorized_quote",
    )
    current_price = _positive_decimal(current_price, "current_price")
    leverage = _positive_decimal(leverage, "leverage")

    try:
        now_value = float(now)
    except (TypeError, ValueError) as exc:
        raise ValueError("now must be numeric") from exc
    if not isfinite(now_value) or now_value <= 0:
        raise ValueError("now must be finite and positive")

    if remaining == ZERO:
        return VCAPExecutionDecision(
            envelope_proposal_id=envelope,
            sequence=sequence,
            action=VCAPLoopAction.STOP,
            remaining_authorized_quote=ZERO,
            current_price=current_price,
            recovery_plan=None,
            tranche_plan=None,
            next_tranche=None,
            next_proposal=None,
            reasons=("recovery_envelope_exhausted",),
        )

    seed = min(seed_quote, remaining)
    planning_proposal = CapitalProposal(
        proposal_id=f"{envelope}:replan:{sequence:04d}",
        pair=normalized_pair,
        intent=IntentKind.RECOVERY,
        amount_quote=seed,
        leverage=leverage,
    )

    recovery = plan_vcap_recovery(
        planning_proposal,
        fingerprint,
        position,
        now=now_value,
        fingerprint_policy=fingerprint_policy,
        vcap_policy=recovery_policy,
    )

    if recovery.behavior.blocked:
        return VCAPExecutionDecision(
            envelope_proposal_id=envelope,
            sequence=sequence,
            action=VCAPLoopAction.HOLD,
            remaining_authorized_quote=remaining,
            current_price=current_price,
            recovery_plan=recovery,
            tranche_plan=None,
            next_tranche=None,
            next_proposal=None,
            reasons=tuple(recovery.reasons) + ("fresh_recovery_evidence_required",),
        )

    if recovery.target_patch_quote == ZERO:
        return VCAPExecutionDecision(
            envelope_proposal_id=envelope,
            sequence=sequence,
            action=VCAPLoopAction.STOP,
            remaining_authorized_quote=remaining,
            current_price=current_price,
            recovery_plan=recovery,
            tranche_plan=None,
            next_tranche=None,
            next_proposal=None,
            reasons=tuple(recovery.reasons) + ("recovery_target_satisfied",),
        )

    tranches = plan_vcap_tranches(
        recovery,
        authorized_quote=remaining,
        current_price=current_price,
        trading_rule=trading_rule,
        policy=tranche_policy,
    )

    if not tranches.tranches:
        return VCAPExecutionDecision(
            envelope_proposal_id=envelope,
            sequence=sequence,
            action=VCAPLoopAction.HOLD,
            remaining_authorized_quote=remaining,
            current_price=current_price,
            recovery_plan=recovery,
            tranche_plan=tranches,
            next_tranche=None,
            next_proposal=None,
            reasons=tuple(recovery.reasons)
            + tuple(tranches.reasons)
            + ("no_sellable_tranche_now",),
        )

    next_tranche = tranches.tranches[0]
    proposal = CapitalProposal(
        proposal_id=vcap_tranche_proposal_id(envelope, sequence),
        pair=normalized_pair,
        intent=IntentKind.RECOVERY,
        amount_quote=next_tranche.quote_amount,
        leverage=leverage,
    )

    return VCAPExecutionDecision(
        envelope_proposal_id=envelope,
        sequence=sequence,
        action=VCAPLoopAction.EXECUTE,
        remaining_authorized_quote=remaining,
        current_price=current_price,
        recovery_plan=recovery,
        tranche_plan=tranches,
        next_tranche=next_tranche,
        next_proposal=proposal,
        reasons=tuple(recovery.reasons)
        + tuple(tranches.reasons)
        + ("next_tranche_revalidated",),
    )


def carve_next_vcap_tranche(
    governor: CapitalGovernor,
    decision: VCAPExecutionDecision,
) -> bool:
    """Carve the exact next tranche from the reserved recovery envelope."""
    if decision.action != VCAPLoopAction.EXECUTE:
        raise ValueError("only EXECUTE decisions can carve a VCAP tranche")
    if decision.next_proposal is None or decision.next_tranche is None:
        raise ValueError("EXECUTE decision is missing its next tranche")

    return governor.carve_reserved_recovery_tranche(
        envelope_proposal_id=decision.envelope_proposal_id,
        tranche_proposal=decision.next_proposal,
    )
