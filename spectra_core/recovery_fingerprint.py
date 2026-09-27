from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from math import isfinite

from .asset_fingerprint import AssetFingerprint, FingerprintHorizon
from .models import CapitalProposal, IntentKind


ZERO = Decimal("0")
ONE = Decimal("1")


class RecoveryFingerprintMode(StrEnum):
    """Behavioral state exposed to the recovery controller.

    This is deliberately not capital authorization. The capital governor
    remains the final Decimal-based authority. Fingerprints may only throttle
    or block the controller's requested RECOVERY amount.
    """

    BLOCKED = "BLOCKED"
    CONSERVATIVE = "CONSERVATIVE"
    ADAPTIVE = "ADAPTIVE"


@dataclass(frozen=True)
class RecoveryFingerprintPolicy:
    """Deterministic policy for projecting behavior into a recovery throttle."""

    minimum_confidence: float = 0.20
    stale_half_lives: float = 3.0
    microstructure_max_age_seconds: float = 30.0

    conservative_multiplier: Decimal = Decimal("0.25")
    neutral_multiplier: Decimal = Decimal("0.50")
    max_multiplier: Decimal = Decimal("1.00")

    spread_stress_ratio: float = 2.0
    depth_stress_ratio: float = 0.50

    def __post_init__(self) -> None:
        if not isfinite(self.minimum_confidence):
            raise ValueError("minimum_confidence must be finite")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be in [0, 1]")

        for name, value in (
            ("stale_half_lives", self.stale_half_lives),
            ("microstructure_max_age_seconds", self.microstructure_max_age_seconds),
            ("spread_stress_ratio", self.spread_stress_ratio),
            ("depth_stress_ratio", self.depth_stress_ratio),
        ):
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

        for name, value in (
            ("conservative_multiplier", self.conservative_multiplier),
            ("neutral_multiplier", self.neutral_multiplier),
            ("max_multiplier", self.max_multiplier),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")

        if not (
            ZERO
            <= self.conservative_multiplier
            <= self.neutral_multiplier
            <= self.max_multiplier
            <= ONE
        ):
            raise ValueError(
                "recovery multipliers must satisfy "
                "0 <= conservative <= neutral <= max <= 1"
            )


@dataclass(frozen=True)
class RecoveryFingerprintDecision:
    pair: str
    mode: RecoveryFingerprintMode
    size_multiplier: Decimal
    effective_confidence: float
    mean_reversion_score: float | None
    microstructure_fresh: bool
    reasons: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return self.mode == RecoveryFingerprintMode.BLOCKED


def _clamp_score(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _adaptive_multiplier(
    score: float,
    policy: RecoveryFingerprintPolicy,
) -> Decimal:
    """Map [-1, 1] mean-reversion evidence into a bounded Decimal throttle."""

    score = _clamp_score(score)
    score_decimal = Decimal(str(score))

    if score >= 0:
        return policy.neutral_multiplier + (
            policy.max_multiplier - policy.neutral_multiplier
        ) * score_decimal

    return policy.neutral_multiplier + (
        policy.neutral_multiplier - policy.conservative_multiplier
    ) * score_decimal


def assess_recovery_fingerprint(
    fingerprint: AssetFingerprint,
    *,
    now: float,
    policy: RecoveryFingerprintPolicy | None = None,
) -> RecoveryFingerprintDecision:
    """Project one asset fingerprint into a deterministic recovery throttle.

    FAST and SHORT horizons drive recovery responsiveness. Their descriptive
    float statistics are confidence-weighted, then converted into a bounded
    Decimal multiplier. The output is advisory to the recovery controller:
    CapitalGovernor still owns every hard capital/risk invariant.
    """

    policy = policy or RecoveryFingerprintPolicy()

    try:
        now = float(now)
    except (TypeError, ValueError) as exc:
        raise ValueError("now must be numeric") from exc
    if not isfinite(now) or now <= 0:
        raise ValueError("now must be finite and positive")
    if now < fingerprint.last_seen_at:
        raise ValueError("now cannot precede fingerprint last_seen_at")

    fast = fingerprint.windows[FingerprintHorizon.FAST]
    short = fingerprint.windows[FingerprintHorizon.SHORT]

    usable: list[tuple[float, float]] = []
    confidences: list[float] = []
    reasons: list[str] = []

    for horizon, window in (
        (FingerprintHorizon.FAST, fast),
        (FingerprintHorizon.SHORT, short),
    ):
        if window.is_stale(
            now=now,
            max_half_lives=policy.stale_half_lives,
        ):
            reasons.append(f"{horizon.value.lower()}_stale")
            continue

        confidence = fingerprint.confidence(horizon, now=now)
        confidences.append(confidence)

        if window.direction_persistence is not None and confidence > 0:
            usable.append(
                (
                    confidence,
                    _clamp_score(-window.direction_persistence),
                )
            )

    microstructure_fresh = not fast.microstructure_is_stale(
        now=now,
        max_age_seconds=policy.microstructure_max_age_seconds,
    )

    if not confidences:
        return RecoveryFingerprintDecision(
            pair=fingerprint.trading_pair,
            mode=RecoveryFingerprintMode.BLOCKED,
            size_multiplier=ZERO,
            effective_confidence=0.0,
            mean_reversion_score=None,
            microstructure_fresh=microstructure_fresh,
            reasons=tuple(reasons or ("behavior_stale",)),
        )

    effective_confidence = max(confidences)

    if effective_confidence < policy.minimum_confidence or not usable:
        if effective_confidence < policy.minimum_confidence:
            reasons.append("low_confidence")
        if not usable:
            reasons.append("directional_history_unavailable")

        multiplier = policy.conservative_multiplier
        if not microstructure_fresh:
            reasons.append("microstructure_stale")

        return RecoveryFingerprintDecision(
            pair=fingerprint.trading_pair,
            mode=RecoveryFingerprintMode.CONSERVATIVE,
            size_multiplier=multiplier,
            effective_confidence=effective_confidence,
            mean_reversion_score=None,
            microstructure_fresh=microstructure_fresh,
            reasons=tuple(reasons),
        )

    total_weight = sum(weight for weight, _ in usable)
    score = sum(weight * value for weight, value in usable) / total_weight
    score = _clamp_score(score)

    multiplier = _adaptive_multiplier(score, policy)

    if not microstructure_fresh:
        reasons.append("microstructure_stale")
        multiplier = min(multiplier, policy.neutral_multiplier)
    else:
        fast_spread = fast.ema_spread_bps
        short_spread = short.ema_spread_bps
        if (
            fast_spread is not None
            and short_spread is not None
            and short_spread > 0
            and fast_spread > short_spread * policy.spread_stress_ratio
        ):
            reasons.append("spread_stress")
            multiplier = min(
                multiplier,
                policy.conservative_multiplier,
            )

        fast_depth = fast.ema_depth_quote
        short_depth = short.ema_depth_quote
        if (
            fast_depth is not None
            and short_depth is not None
            and short_depth > 0
            and fast_depth < short_depth * policy.depth_stress_ratio
        ):
            reasons.append("depth_stress")
            multiplier = min(
                multiplier,
                policy.conservative_multiplier,
            )

    if score > 0:
        reasons.append("reversal_support")
    elif score < 0:
        reasons.append("momentum_throttle")
    else:
        reasons.append("behavior_neutral")

    return RecoveryFingerprintDecision(
        pair=fingerprint.trading_pair,
        mode=RecoveryFingerprintMode.ADAPTIVE,
        size_multiplier=multiplier,
        effective_confidence=effective_confidence,
        mean_reversion_score=score,
        microstructure_fresh=microstructure_fresh,
        reasons=tuple(reasons),
    )


def shape_recovery_proposal(
    proposal: CapitalProposal,
    fingerprint: AssetFingerprint,
    *,
    now: float,
    policy: RecoveryFingerprintPolicy | None = None,
) -> tuple[CapitalProposal | None, RecoveryFingerprintDecision]:
    """Throttle a RECOVERY proposal without granting new capital authority.

    The returned proposal is smaller than or equal to the controller's original
    request. A blocked fingerprint returns None. Any non-None proposal must
    still pass through CapitalGovernor.reserve().
    """

    if proposal.intent != IntentKind.RECOVERY:
        raise ValueError("fingerprint recovery shaping requires RECOVERY intent")

    pair = proposal.pair.strip().upper()
    if pair != fingerprint.trading_pair:
        raise ValueError(
            f"proposal pair {pair} does not match fingerprint "
            f"{fingerprint.trading_pair}"
        )

    decision = assess_recovery_fingerprint(
        fingerprint,
        now=now,
        policy=policy,
    )

    if decision.blocked or decision.size_multiplier <= ZERO:
        return None, decision

    return (
        replace(
            proposal,
            pair=pair,
            amount_quote=proposal.amount_quote * decision.size_multiplier,
        ),
        decision,
    )
