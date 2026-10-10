from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite

from .asset_fingerprint import AssetFingerprint, FingerprintHorizon
from .recovery_fingerprint import VCAPPositionState


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


def _time(value: float, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


class VCAPProfitExitAction(StrEnum):
    WAIT = "WAIT"
    EXTEND = "EXTEND"
    EXIT = "EXIT"


@dataclass(frozen=True)
class VCAPExitCostModel:
    """Exact costs already paid plus conservative costs expected on exit."""

    accrued_fees_quote: Decimal = Decimal("0")
    expected_exit_fee_bps: Decimal = Decimal("0")
    slippage_allowance_bps: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _decimal(
            self.accrued_fees_quote,
            "accrued_fees_quote",
            non_negative=True,
        )
        _decimal(
            self.expected_exit_fee_bps,
            "expected_exit_fee_bps",
            non_negative=True,
        )
        _decimal(
            self.slippage_allowance_bps,
            "slippage_allowance_bps",
            non_negative=True,
        )
        if self.expected_exit_fee_bps + self.slippage_allowance_bps >= BPS:
            raise ValueError("combined exit cost bps must be < 10000")

    @property
    def expected_exit_cost_bps(self) -> Decimal:
        return self.expected_exit_fee_bps + self.slippage_allowance_bps


@dataclass(frozen=True)
class VCAPProfitExitPolicy:
    """Profit-oriented exit policy for recovered VCAP capital."""

    target_net_profit_bps: Decimal = Decimal("174")
    stagnation_seconds: int = 2 * 60 * 60
    meaningful_progress_bps: Decimal = Decimal("25")
    max_profit_giveback_bps: Decimal = Decimal("50")
    continuation_threshold: float = 0.55
    suggested_trailing_delta_bps: Decimal = Decimal("35")

    def __post_init__(self) -> None:
        for field, value, positive in (
            ("target_net_profit_bps", self.target_net_profit_bps, True),
            ("meaningful_progress_bps", self.meaningful_progress_bps, True),
            ("max_profit_giveback_bps", self.max_profit_giveback_bps, True),
            ("suggested_trailing_delta_bps", self.suggested_trailing_delta_bps, True),
        ):
            _decimal(value, field, positive=positive)

        if (
            isinstance(self.stagnation_seconds, bool)
            or not isinstance(self.stagnation_seconds, int)
            or self.stagnation_seconds <= 0
        ):
            raise ValueError("stagnation_seconds must be a positive integer")

        if not isfinite(self.continuation_threshold):
            raise ValueError("continuation_threshold must be finite")
        if not 0.0 <= self.continuation_threshold <= 1.0:
            raise ValueError("continuation_threshold must be in [0, 1]")


@dataclass(frozen=True)
class VCAPProfitState:
    """Minimal persistent state required for profit lock and stagnation."""

    updated_at: float
    profit_floor_reached_at: float | None = None
    peak_net_profit_bps: Decimal = Decimal("0")
    peak_at: float | None = None

    def __post_init__(self) -> None:
        _time(self.updated_at, "updated_at")
        _decimal(
            self.peak_net_profit_bps,
            "peak_net_profit_bps",
            non_negative=True,
        )
        if self.profit_floor_reached_at is not None:
            _time(self.profit_floor_reached_at, "profit_floor_reached_at")
        if self.peak_at is not None:
            _time(self.peak_at, "peak_at")


@dataclass(frozen=True)
class VCAPProfitExitDecision:
    pair: str
    action: VCAPProfitExitAction
    current_price: Decimal
    target_exit_price: Decimal
    protected_floor_price: Decimal
    estimated_net_proceeds_quote: Decimal
    estimated_net_profit_quote: Decimal
    estimated_net_profit_bps: Decimal
    continuation_support: float
    state: VCAPProfitState
    suggested_trailing_delta_bps: Decimal
    reasons: tuple[str, ...]

    @property
    def profit_floor_reached(self) -> bool:
        return self.state.profit_floor_reached_at is not None


def _continuation_support(
    fingerprint: AssetFingerprint | None,
    *,
    pair: str,
    now: float,
) -> float:
    if fingerprint is None:
        return 0.0
    if fingerprint.trading_pair != pair:
        raise ValueError("fingerprint pair does not match VCAP position")

    scores: list[float] = []
    weights: list[float] = []

    for horizon in (FingerprintHorizon.FAST, FingerprintHorizon.SHORT):
        window = fingerprint.windows[horizon]
        confidence = window.confidence(now=now)
        if confidence <= 0.0 or window.is_stale(now=now):
            continue

        momentum = window.momentum_persistence
        mean_return = window.ema_return_bps
        abs_return = window.ema_abs_return_bps

        if momentum is None or mean_return is None or mean_return <= 0.0:
            directional = 0.0
        elif abs_return is None or abs_return <= 0.0:
            directional = momentum
        else:
            directional = momentum * max(
                0.0,
                min(1.0, mean_return / abs_return),
            )

        scores.append(max(0.0, min(1.0, directional)))
        weights.append(confidence)

    if not weights or sum(weights) <= 0.0:
        return 0.0

    return max(
        0.0,
        min(
            1.0,
            sum(score * weight for score, weight in zip(scores, weights))
            / sum(weights),
        ),
    )


def _profit_geometry(
    position: VCAPPositionState,
    costs: VCAPExitCostModel,
) -> tuple[Decimal, Decimal, Decimal]:
    gross = position.market_value_quote
    exit_cost_rate = costs.expected_exit_cost_bps / BPS
    net_proceeds = gross * (ONE - exit_cost_rate)
    capital_basis = position.cost_basis_quote + costs.accrued_fees_quote
    net_profit = net_proceeds - capital_basis
    net_profit_bps = (
        net_profit / capital_basis * BPS
        if capital_basis > ZERO
        else ZERO
    )
    return net_proceeds, net_profit, net_profit_bps


def _target_exit_price(
    position: VCAPPositionState,
    costs: VCAPExitCostModel,
    target_net_profit_bps: Decimal,
) -> Decimal:
    capital_basis = position.cost_basis_quote + costs.accrued_fees_quote
    target_net_proceeds = capital_basis * (
        ONE + target_net_profit_bps / BPS
    )
    exit_keep_ratio = ONE - costs.expected_exit_cost_bps / BPS
    if exit_keep_ratio <= ZERO:
        raise ValueError("exit costs leave no executable proceeds")
    return target_net_proceeds / (
        position.position_base * exit_keep_ratio
    )


def plan_vcap_profit_exit(
    position: VCAPPositionState,
    *,
    costs: VCAPExitCostModel,
    now: float,
    fingerprint: AssetFingerprint | None = None,
    previous_state: VCAPProfitState | None = None,
    policy: VCAPProfitExitPolicy | None = None,
) -> VCAPProfitExitDecision:
    """Decide whether recovered VCAP capital should wait, extend, or exit.

    Break-even is not a successful recovery target. The default target is a
    174 bps *net* profit after accrued entry fees plus configured exit fee and
    slippage allowances.

    Once the target has been reached, Spectra treats it as protected profit:
    favorable continuation can extend the winner, but a material giveback or
    two hours without meaningful new profit progress harvests the position so
    capital can be recycled. A previously reached profit floor that is later
    breached also triggers an exit rather than silently returning to recovery.
    """

    policy = policy or VCAPProfitExitPolicy()
    now_value = _time(now, "now")

    pair = position.pair
    net_proceeds, net_profit, net_profit_bps = _profit_geometry(
        position,
        costs,
    )
    target_price = _target_exit_price(
        position,
        costs,
        policy.target_net_profit_bps,
    )
    continuation = _continuation_support(
        fingerprint,
        pair=pair,
        now=now_value,
    )

    prior_floor_at = (
        None if previous_state is None else previous_state.profit_floor_reached_at
    )
    prior_peak = (
        ZERO if previous_state is None else previous_state.peak_net_profit_bps
    )
    prior_peak_at = (
        None if previous_state is None else previous_state.peak_at
    )

    target_reached_now = net_profit_bps >= policy.target_net_profit_bps
    floor_at = prior_floor_at
    if target_reached_now and floor_at is None:
        floor_at = now_value

    peak = prior_peak
    peak_at = prior_peak_at

    if floor_at is not None:
        if peak_at is None:
            peak = max(policy.target_net_profit_bps, net_profit_bps)
            peak_at = now_value
        elif net_profit_bps >= peak + policy.meaningful_progress_bps:
            peak = net_profit_bps
            peak_at = now_value

    state = VCAPProfitState(
        updated_at=now_value,
        profit_floor_reached_at=floor_at,
        peak_net_profit_bps=max(ZERO, peak),
        peak_at=peak_at,
    )

    reasons: list[str] = []

    if floor_at is None:
        reasons.append("net_profit_target_not_reached")
        return VCAPProfitExitDecision(
            pair=pair,
            action=VCAPProfitExitAction.WAIT,
            current_price=position.current_price,
            target_exit_price=target_price,
            protected_floor_price=target_price,
            estimated_net_proceeds_quote=net_proceeds,
            estimated_net_profit_quote=net_profit,
            estimated_net_profit_bps=net_profit_bps,
            continuation_support=continuation,
            state=state,
            suggested_trailing_delta_bps=policy.suggested_trailing_delta_bps,
            reasons=tuple(reasons),
        )

    # If the target was previously reached but has already slipped below it,
    # harvest immediately. Execution latency or gaps can still realize less than
    # 174 bps, but the planner never intentionally reclassifies it as recovery.
    if net_profit_bps < policy.target_net_profit_bps:
        reasons.append("protected_profit_floor_breached")
        return VCAPProfitExitDecision(
            pair=pair,
            action=VCAPProfitExitAction.EXIT,
            current_price=position.current_price,
            target_exit_price=target_price,
            protected_floor_price=target_price,
            estimated_net_proceeds_quote=net_proceeds,
            estimated_net_profit_quote=net_profit,
            estimated_net_profit_bps=net_profit_bps,
            continuation_support=continuation,
            state=state,
            suggested_trailing_delta_bps=policy.suggested_trailing_delta_bps,
            reasons=tuple(reasons),
        )

    giveback = max(ZERO, peak - net_profit_bps)
    if giveback >= policy.max_profit_giveback_bps:
        reasons.extend(
            (
                "profit_giveback_limit_reached",
                "recycle_profitable_capital",
            )
        )
        return VCAPProfitExitDecision(
            pair=pair,
            action=VCAPProfitExitAction.EXIT,
            current_price=position.current_price,
            target_exit_price=target_price,
            protected_floor_price=target_price,
            estimated_net_proceeds_quote=net_proceeds,
            estimated_net_profit_quote=net_profit,
            estimated_net_profit_bps=net_profit_bps,
            continuation_support=continuation,
            state=state,
            suggested_trailing_delta_bps=policy.suggested_trailing_delta_bps,
            reasons=tuple(reasons),
        )

    seconds_since_progress = (
        0.0 if peak_at is None else max(0.0, now_value - peak_at)
    )
    if seconds_since_progress >= policy.stagnation_seconds:
        reasons.extend(
            (
                "profitable_capital_stagnant",
                "recycle_profitable_capital",
            )
        )
        return VCAPProfitExitDecision(
            pair=pair,
            action=VCAPProfitExitAction.EXIT,
            current_price=position.current_price,
            target_exit_price=target_price,
            protected_floor_price=target_price,
            estimated_net_proceeds_quote=net_proceeds,
            estimated_net_profit_quote=net_profit,
            estimated_net_profit_bps=net_profit_bps,
            continuation_support=continuation,
            state=state,
            suggested_trailing_delta_bps=policy.suggested_trailing_delta_bps,
            reasons=tuple(reasons),
        )

    reasons.append("net_profit_floor_protected")
    if continuation >= policy.continuation_threshold:
        reasons.append("dttp_dtsl_extension_supported")
    else:
        reasons.append("profit_extension_under_tight_protection")

    return VCAPProfitExitDecision(
        pair=pair,
        action=VCAPProfitExitAction.EXTEND,
        current_price=position.current_price,
        target_exit_price=target_price,
        protected_floor_price=target_price,
        estimated_net_proceeds_quote=net_proceeds,
        estimated_net_profit_quote=net_profit,
        estimated_net_profit_bps=net_profit_bps,
        continuation_support=continuation,
        state=state,
        suggested_trailing_delta_bps=policy.suggested_trailing_delta_bps,
        reasons=tuple(reasons),
    )

