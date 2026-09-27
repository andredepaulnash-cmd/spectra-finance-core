from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from math import isfinite

from .kraken_market_fabric import KrakenTradingRule
from .recovery_fingerprint import VCAPRecoveryPlan


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def _decimal(
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


def _quantize_down(value: Decimal, increment: Decimal) -> Decimal:
    _decimal(value, "value", non_negative=True)
    _decimal(increment, "increment", non_negative=True)
    if increment == ZERO:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


@dataclass(frozen=True)
class VCAPTranchePolicy:
    """Deterministic policy for fracturing authorized VCAP recovery capital."""

    max_tranches: int = 5
    aggressive_min_tranches: int = 2
    sellability_buffer_bps: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        if isinstance(self.max_tranches, bool) or not isinstance(
            self.max_tranches,
            int,
        ):
            raise TypeError("max_tranches must be int")
        if isinstance(self.aggressive_min_tranches, bool) or not isinstance(
            self.aggressive_min_tranches,
            int,
        ):
            raise TypeError("aggressive_min_tranches must be int")
        if self.max_tranches < 1:
            raise ValueError("max_tranches must be >= 1")
        if not 1 <= self.aggressive_min_tranches <= self.max_tranches:
            raise ValueError(
                "aggressive_min_tranches must be between 1 and max_tranches"
            )

        _decimal(
            self.sellability_buffer_bps,
            "sellability_buffer_bps",
            non_negative=True,
        )
        if self.sellability_buffer_bps >= BPS:
            raise ValueError("sellability_buffer_bps must be < 10000")


@dataclass(frozen=True)
class VCAPTranche:
    sequence: int
    quote_amount: Decimal
    base_amount: Decimal
    weight_ratio: Decimal
    independently_sellable: bool = True


@dataclass(frozen=True)
class VCAPTranchePlan:
    pair: str
    authorized_quote: Decimal
    deployable_quote: Decimal
    unallocated_quote: Decimal
    desired_tranche_count: int
    actual_tranche_count: int
    tranches: tuple[VCAPTranche, ...]
    reasons: tuple[str, ...]
    replan_after_fill: bool = True

    @property
    def fully_allocated(self) -> bool:
        return self.unallocated_quote == ZERO


def _desired_tranche_count(
    recovery: VCAPRecoveryPlan,
    policy: VCAPTranchePolicy,
) -> int:
    if not recovery.aggressive:
        return policy.max_tranches

    score = recovery.behavior.mean_reversion_score
    confidence = recovery.behavior.effective_confidence
    if score is None or not isfinite(score) or not isfinite(confidence):
        return policy.max_tranches

    support = max(0.0, min(1.0, score * confidence))
    span = policy.max_tranches - policy.aggressive_min_tranches
    reduction = int(round(support * span))
    return max(
        policy.aggressive_min_tranches,
        policy.max_tranches - reduction,
    )


def _weights(count: int, *, aggressive: bool) -> tuple[Decimal, ...]:
    if count < 1:
        raise ValueError("count must be positive")
    if not aggressive:
        weight = ONE / Decimal(count)
        return tuple(weight for _ in range(count))

    raw = tuple(Decimal(value) for value in range(count, 0, -1))
    total = sum(raw, ZERO)
    return tuple(value / total for value in raw)


def _sellability_thresholds(
    rule: KrakenTradingRule,
    policy: VCAPTranchePolicy,
) -> tuple[Decimal, Decimal]:
    factor = ONE + policy.sellability_buffer_bps / BPS
    min_base = rule.min_order_size * factor
    min_quote = rule.effective_min_quote * factor
    return min_base, min_quote


def _candidate_tranches(
    *,
    envelope_quote: Decimal,
    price: Decimal,
    count: int,
    aggressive: bool,
    rule: KrakenTradingRule,
    policy: VCAPTranchePolicy,
) -> tuple[VCAPTranche, ...] | None:
    weights = _weights(count, aggressive=aggressive)
    min_base, min_quote = _sellability_thresholds(rule, policy)

    rows: list[VCAPTranche] = []
    for index, weight in enumerate(weights, start=1):
        quote_budget = envelope_quote * weight
        if rule.min_quote_amount_increment > ZERO:
            quote_budget = _quantize_down(
                quote_budget,
                rule.min_quote_amount_increment,
            )

        base = _quantize_down(
            quote_budget / price,
            rule.min_base_amount_increment,
        )
        quote = base * price

        if base < min_base or quote < min_quote:
            return None

        rows.append(
            VCAPTranche(
                sequence=index,
                quote_amount=quote,
                base_amount=base,
                weight_ratio=weight,
            )
        )

    # Rounding may leave safe quote capital unused. Add whole base increments
    # back into the earliest tranches without ever crossing the envelope.
    deployed = sum((row.quote_amount for row in rows), ZERO)
    leftover = envelope_quote - deployed
    increment_cost = rule.min_base_amount_increment * price

    if increment_cost > ZERO and leftover >= increment_cost:
        mutable = list(rows)
        while leftover >= increment_cost:
            changed = False
            for idx, row in enumerate(mutable):
                if leftover < increment_cost:
                    break
                new_base = row.base_amount + rule.min_base_amount_increment
                new_quote = new_base * price
                mutable[idx] = VCAPTranche(
                    sequence=row.sequence,
                    quote_amount=new_quote,
                    base_amount=new_base,
                    weight_ratio=row.weight_ratio,
                )
                leftover -= increment_cost
                changed = True
            if not changed:
                break
        rows = mutable

    if sum((row.quote_amount for row in rows), ZERO) > envelope_quote:
        raise RuntimeError("tranche quantization exceeded authorized envelope")

    return tuple(rows)


def plan_vcap_tranches(
    recovery: VCAPRecoveryPlan,
    *,
    authorized_quote: Decimal,
    current_price: Decimal,
    trading_rule: KrakenTradingRule,
    policy: VCAPTranchePolicy | None = None,
) -> VCAPTranchePlan:
    """Fracture authorized VCAP recovery capital into sellable exchange parcels.

    PR #7 decides how much recovery weight is useful. This planner decides how
    that weight can be split without creating exchange dust. The plan is always
    bounded by the smaller of the recovery request and the externally authorized
    quote amount.

    Every tranche is quantized to Kraken's base increment and must remain above
    both the base-size and quote-notional minima with configurable sellability
    headroom. If the desired split would create dust, the tranche count is
    reduced until every parcel is independently exit-capable. Capital that
    cannot form a valid parcel remains unallocated rather than becoming dust.

    The schedule is intentionally replanned after each fill because VCAP
    position geometry, remaining authorization, and exchange rules can change.
    """

    policy = policy or VCAPTranchePolicy()
    authorized_quote = _decimal(
        authorized_quote,
        "authorized_quote",
        non_negative=True,
    )
    current_price = _decimal(
        current_price,
        "current_price",
        positive=True,
    )

    pair = recovery.pair.strip().upper()
    rule_pair = trading_rule.trading_pair.strip().upper()
    if pair != rule_pair:
        raise ValueError(
            f"recovery pair {pair} does not match trading rule {rule_pair}"
        )

    if not (
        trading_rule.supports_limit_orders
        or trading_rule.supports_market_orders
    ):
        raise ValueError("trading rule exposes no supported exit order type")

    if recovery.planned_amount_quote < ZERO:
        raise ValueError("recovery planned amount cannot be negative")

    envelope = min(
        authorized_quote,
        recovery.planned_amount_quote,
    )
    desired = _desired_tranche_count(recovery, policy)
    reasons: list[str] = []

    if envelope <= ZERO or recovery.proposal is None:
        reasons.append("no_authorized_recovery_capital")
        return VCAPTranchePlan(
            pair=pair,
            authorized_quote=authorized_quote,
            deployable_quote=ZERO,
            unallocated_quote=envelope,
            desired_tranche_count=desired,
            actual_tranche_count=0,
            tranches=(),
            reasons=tuple(reasons),
        )

    if authorized_quote < recovery.planned_amount_quote:
        reasons.append("governor_reduced_recovery_envelope")

    selected: tuple[VCAPTranche, ...] | None = None
    selected_count = 0

    for count in range(desired, 0, -1):
        candidate = _candidate_tranches(
            envelope_quote=envelope,
            price=current_price,
            count=count,
            aggressive=recovery.aggressive,
            rule=trading_rule,
            policy=policy,
        )
        if candidate is not None:
            selected = candidate
            selected_count = count
            break

    if selected is None:
        reasons.append("below_exchange_sellability_minimum")
        return VCAPTranchePlan(
            pair=pair,
            authorized_quote=authorized_quote,
            deployable_quote=ZERO,
            unallocated_quote=envelope,
            desired_tranche_count=desired,
            actual_tranche_count=0,
            tranches=(),
            reasons=tuple(reasons),
        )

    if selected_count < desired:
        reasons.append("tranche_count_reduced_for_sellability")
    if recovery.aggressive:
        reasons.append("aggressive_front_loaded_fracture")
    else:
        reasons.append("equal_weight_fracture")

    deployed = sum((row.quote_amount for row in selected), ZERO)
    unallocated = envelope - deployed
    if unallocated > ZERO:
        reasons.append("rounding_residual_held_unallocated")

    min_base, min_quote = _sellability_thresholds(trading_rule, policy)
    for row in selected:
        if row.base_amount < min_base or row.quote_amount < min_quote:
            raise RuntimeError("planner emitted an unsellable VCAP tranche")
        if row.quote_amount <= ZERO or row.base_amount <= ZERO:
            raise RuntimeError("planner emitted a non-positive VCAP tranche")

    if deployed > authorized_quote:
        raise RuntimeError("VCAP tranches exceed governor authorization")
    if deployed > recovery.planned_amount_quote:
        raise RuntimeError("VCAP tranches exceed recovery plan")

    return VCAPTranchePlan(
        pair=pair,
        authorized_quote=authorized_quote,
        deployable_quote=deployed,
        unallocated_quote=unallocated,
        desired_tranche_count=desired,
        actual_tranche_count=selected_count,
        tranches=selected,
        reasons=tuple(reasons),
    )
