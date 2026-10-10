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


BPS_DECIMAL = Decimal("10000")


def _finite_decimal(
    value: Decimal,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and value <= ZERO:
        raise ValueError(f"{field} must be positive")
    if non_negative and value < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return value


@dataclass(frozen=True)
class VCAPPositionState:
    """Exact position geometry used to size a VCAP recovery request."""

    pair: str
    position_base: Decimal
    cost_basis_quote: Decimal
    current_price: Decimal

    def __post_init__(self) -> None:
        pair = self.pair.strip().upper()
        if "-" not in pair:
            raise ValueError("pair must be BASE-QUOTE")
        object.__setattr__(self, "pair", pair)
        _finite_decimal(self.position_base, "position_base", positive=True)
        _finite_decimal(self.cost_basis_quote, "cost_basis_quote", positive=True)
        _finite_decimal(self.current_price, "current_price", positive=True)

    @property
    def average_entry_price(self) -> Decimal:
        return self.cost_basis_quote / self.position_base

    @property
    def market_value_quote(self) -> Decimal:
        return self.position_base * self.current_price

    @property
    def rebound_bps_to_break_even(self) -> Decimal:
        return max(
            ZERO,
            (self.average_entry_price / self.current_price - ONE) * BPS_DECIMAL,
        )


@dataclass(frozen=True)
class VCAPRecoveryPolicy:
    """Controls how aggressively recovery geometry may expand a seed request.

    Expansion is a controller request only. It never overrides CapitalGovernor.
    """

    target_rebound_bps: Decimal = Decimal("500")
    max_seed_multiplier: Decimal = Decimal("4")
    max_patch_to_position_ratio: Decimal = Decimal("1")
    minimum_rebound_improvement_bps: Decimal = Decimal("25")
    aggressive_signal_threshold: float = 0.25

    def __post_init__(self) -> None:
        _finite_decimal(
            self.target_rebound_bps,
            "target_rebound_bps",
            positive=True,
        )
        _finite_decimal(
            self.max_seed_multiplier,
            "max_seed_multiplier",
            positive=True,
        )
        _finite_decimal(
            self.max_patch_to_position_ratio,
            "max_patch_to_position_ratio",
            positive=True,
        )
        _finite_decimal(
            self.minimum_rebound_improvement_bps,
            "minimum_rebound_improvement_bps",
            non_negative=True,
        )
        if self.max_seed_multiplier < ONE:
            raise ValueError("max_seed_multiplier must be >= 1")
        if not isfinite(self.aggressive_signal_threshold):
            raise ValueError("aggressive_signal_threshold must be finite")
        if not 0.0 <= self.aggressive_signal_threshold <= 1.0:
            raise ValueError("aggressive_signal_threshold must be in [0, 1]")


@dataclass(frozen=True)
class VCAPRecoveryPlan:
    pair: str
    seed_amount_quote: Decimal
    planned_amount_quote: Decimal
    capital_weight_ratio: Decimal
    before_rebound_bps: Decimal
    after_rebound_bps: Decimal
    rebound_improvement_bps: Decimal
    target_patch_quote: Decimal
    behavior: RecoveryFingerprintDecision
    aggressive: bool
    reasons: tuple[str, ...]
    proposal: CapitalProposal | None

    @property
    def expanded(self) -> bool:
        return self.planned_amount_quote > self.seed_amount_quote


def _rebound_bps_after_patch(
    position: VCAPPositionState,
    patch_quote: Decimal,
) -> Decimal:
    _finite_decimal(patch_quote, "patch_quote", non_negative=True)
    if patch_quote == ZERO:
        return position.rebound_bps_to_break_even

    patch_base = patch_quote / position.current_price
    average_after = (
        position.cost_basis_quote + patch_quote
    ) / (
        position.position_base + patch_base
    )
    return max(
        ZERO,
        (average_after / position.current_price - ONE) * BPS_DECIMAL,
    )


def _patch_quote_for_target_rebound(
    position: VCAPPositionState,
    target_rebound_bps: Decimal,
) -> Decimal:
    """Return quote capital needed to move break-even to the target rebound."""

    _finite_decimal(
        target_rebound_bps,
        "target_rebound_bps",
        positive=True,
    )

    if position.rebound_bps_to_break_even <= target_rebound_bps:
        return ZERO

    target_ratio = target_rebound_bps / BPS_DECIMAL
    target_average = position.current_price * (ONE + target_ratio)

    numerator = (
        position.cost_basis_quote
        - target_average * position.position_base
    )
    if numerator <= ZERO:
        return ZERO

    return numerator / target_ratio


def plan_vcap_recovery(
    proposal: CapitalProposal,
    fingerprint: AssetFingerprint,
    position: VCAPPositionState,
    *,
    now: float,
    fingerprint_policy: RecoveryFingerprintPolicy | None = None,
    vcap_policy: VCAPRecoveryPolicy | None = None,
) -> VCAPRecoveryPlan:
    """Plan aggressive VCAP recovery from position geometry plus behavior.

    Recovery remains objective one: first measure how much quote weight actually
    moves break-even toward the configured rebound target. Objective two is
    capital productivity: when fresh, confident reversal behavior supports the
    recovery and the extra capital materially improves the exit geometry, the
    controller may request more than its seed patch.

    This function never authorizes funds. Every returned proposal must still
    pass through CapitalGovernor.reserve(), which owns the hard capital limits.
    """

    if proposal.intent != IntentKind.RECOVERY:
        raise ValueError("VCAP planning requires RECOVERY intent")

    pair = proposal.pair.strip().upper()
    if pair != fingerprint.trading_pair:
        raise ValueError(
            f"proposal pair {pair} does not match fingerprint "
            f"{fingerprint.trading_pair}"
        )
    if pair != position.pair:
        raise ValueError(
            f"proposal pair {pair} does not match position {position.pair}"
        )

    seed = _finite_decimal(
        proposal.amount_quote,
        "amount_quote",
        positive=True,
    )
    policy = vcap_policy or VCAPRecoveryPolicy()
    behavior = assess_recovery_fingerprint(
        fingerprint,
        now=now,
        policy=fingerprint_policy,
    )

    before = position.rebound_bps_to_break_even
    target_patch = _patch_quote_for_target_rebound(
        position,
        policy.target_rebound_bps,
    )
    reasons: list[str] = []

    if behavior.blocked:
        reasons.extend(behavior.reasons)
        reasons.append("recovery_behavior_blocked")
        return VCAPRecoveryPlan(
            pair=pair,
            seed_amount_quote=seed,
            planned_amount_quote=ZERO,
            capital_weight_ratio=ZERO,
            before_rebound_bps=before,
            after_rebound_bps=before,
            rebound_improvement_bps=ZERO,
            target_patch_quote=target_patch,
            behavior=behavior,
            aggressive=False,
            reasons=tuple(reasons),
            proposal=None,
        )

    # Conservative behavior still gets a recovery request, but at the existing
    # fingerprint throttle. Expansion is reserved for fresh, confident reversal
    # evidence because extra quote weight increases concentration.
    planned = seed * behavior.size_multiplier

    score = behavior.mean_reversion_score
    aggressive_signal = (
        behavior.mode == RecoveryFingerprintMode.ADAPTIVE
        and behavior.microstructure_fresh
        and score is not None
        and score >= policy.aggressive_signal_threshold
        and target_patch > seed
    )

    if aggressive_signal:
        score_weight = Decimal(str(max(0.0, min(1.0, score))))
        confidence_weight = Decimal(
            str(max(0.0, min(1.0, behavior.effective_confidence)))
        )
        support = score_weight * confidence_weight

        behavior_cap = seed * (
            ONE + (policy.max_seed_multiplier - ONE) * support
        )
        seed_cap = seed * policy.max_seed_multiplier
        position_cap = (
            position.cost_basis_quote
            * policy.max_patch_to_position_ratio
        )

        candidate = min(
            target_patch,
            behavior_cap,
            seed_cap,
            position_cap,
        )

        candidate_after = _rebound_bps_after_patch(position, candidate)
        candidate_improvement = max(ZERO, before - candidate_after)

        if (
            candidate > seed
            and candidate_improvement
            >= policy.minimum_rebound_improvement_bps
        ):
            planned = candidate
            reasons.append("aggressive_reversal_weight")
            reasons.append("recovery_geometry_improved")
        else:
            reasons.append("aggressive_weight_not_material")
    elif target_patch == ZERO:
        reasons.append("already_inside_recovery_target")
    else:
        reasons.append("aggressive_signal_unavailable")

    after = _rebound_bps_after_patch(position, planned)
    improvement = max(ZERO, before - after)

    if planned > seed:
        reasons.append("capital_request_expanded")
    elif planned < seed:
        reasons.append("capital_request_throttled")
    else:
        reasons.append("capital_request_held")

    reasons.extend(
        reason
        for reason in behavior.reasons
        if reason not in reasons
    )

    shaped = (
        None
        if planned <= ZERO
        else replace(
            proposal,
            pair=pair,
            amount_quote=planned,
        )
    )

    return VCAPRecoveryPlan(
        pair=pair,
        seed_amount_quote=seed,
        planned_amount_quote=planned,
        capital_weight_ratio=planned / position.cost_basis_quote,
        before_rebound_bps=before,
        after_rebound_bps=after,
        rebound_improvement_bps=improvement,
        target_patch_quote=target_patch,
        behavior=behavior,
        aggressive=planned > seed,
        reasons=tuple(reasons),
        proposal=shaped,
    )

