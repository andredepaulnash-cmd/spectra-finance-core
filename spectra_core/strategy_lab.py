from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import ceil
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def _decimal(value: Decimal | str | int | float, name: str) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _non_negative(value: Decimal | str | int | float, name: str) -> Decimal:
    result = _decimal(value, name)
    if result < ZERO:
        raise ValueError(f"{name} must be non-negative")
    return result


def _unit(value: Decimal | str | int | float, name: str) -> Decimal:
    result = _decimal(value, name)
    if result < ZERO or result > ONE:
        raise ValueError(f"{name} must be in [0, 1]")
    return result


def _pair(value: str) -> str:
    pair = str(value).strip().upper()
    if "-" not in pair:
        raise ValueError("pair must be BASE-QUOTE")
    return pair


def base_asset(pair: str) -> str:
    base = _pair(pair).rsplit("-", 1)[0]
    return {
        "XBT": "BTC",
        "XXBT": "BTC",
        "XDG": "DOGE",
        "XXDG": "DOGE",
        "XETH": "ETH",
        "XXRP": "XRP",
    }.get(base, base)


class LabAction(StrEnum):
    ENTRY = "ENTRY"
    RECOVERY = "RECOVERY"
    PARTIAL_RECYCLE = "PARTIAL_RECYCLE"
    HOLD = "HOLD"


class ExecutionStyle(StrEnum):
    PATIENT_MAKER = "PATIENT_MAKER"
    IMPROVE_MAKER = "IMPROVE_MAKER"
    AGGRESSIVE_LIMIT = "AGGRESSIVE_LIMIT"
    TAKER = "TAKER"


@dataclass(frozen=True)
class CapitalAuctionPolicy:
    """Transparent research weights, not production trading authority."""

    reserve_fraction: Decimal = Decimal("0.50")
    minimum_recovery_reserve_quote: Decimal = Decimal("25")
    target_fleet_min: int = 10
    target_fleet_normal: int = 20
    target_fleet_max: int = 40
    minimum_viable_position_quote: Decimal = Decimal("10")
    min_score: Decimal = Decimal("0")
    capital_time_weight: Decimal = Decimal("2")
    reversal_weight: Decimal = Decimal("40")
    liquidity_weight: Decimal = Decimal("25")
    recovery_efficiency_weight: Decimal = Decimal("35")
    urgency_weight: Decimal = Decimal("10")
    correlation_penalty_weight: Decimal = Decimal("30")
    dead_money_penalty_weight: Decimal = Decimal("45")
    execution_cost_weight: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        reserve = _unit(self.reserve_fraction, "reserve_fraction")
        object.__setattr__(self, "reserve_fraction", reserve)
        for field in (
            "minimum_recovery_reserve_quote",
            "minimum_viable_position_quote",
            "capital_time_weight",
            "reversal_weight",
            "liquidity_weight",
            "recovery_efficiency_weight",
            "urgency_weight",
            "correlation_penalty_weight",
            "dead_money_penalty_weight",
            "execution_cost_weight",
        ):
            object.__setattr__(
                self,
                field,
                _non_negative(getattr(self, field), field),
            )
        if not (
            1 <= self.target_fleet_min
            <= self.target_fleet_normal
            <= self.target_fleet_max
        ):
            raise ValueError("fleet targets must be ordered positive integers")


@dataclass(frozen=True)
class FleetCapacity:
    equity_quote: Decimal
    protected_reserve_quote: Decimal
    deployable_quote: Decimal
    feasible_positions: int
    target_positions: int
    target_min_constrained: bool


def dynamic_fleet_capacity(
    equity_quote: Decimal,
    *,
    policy: CapitalAuctionPolicy | None = None,
) -> FleetCapacity:
    """Compute an economically feasible fleet instead of forcing a count."""

    p = policy or CapitalAuctionPolicy()
    equity = _non_negative(equity_quote, "equity_quote")
    reserve = max(
        p.minimum_recovery_reserve_quote,
        equity * p.reserve_fraction,
    )
    reserve = min(reserve, equity)
    deployable = equity - reserve
    if p.minimum_viable_position_quote == ZERO:
        feasible = p.target_fleet_max
    else:
        feasible = int(deployable // p.minimum_viable_position_quote)
    feasible = max(0, min(feasible, p.target_fleet_max))
    target = min(feasible, p.target_fleet_normal)
    return FleetCapacity(
        equity_quote=equity,
        protected_reserve_quote=reserve,
        deployable_quote=deployable,
        feasible_positions=feasible,
        target_positions=target,
        target_min_constrained=feasible < p.target_fleet_min,
    )


@dataclass(frozen=True)
class CapitalOpportunity:
    opportunity_id: str
    pair: str
    action: LabAction
    requested_quote: Decimal
    expected_net_edge_bps: Decimal
    expected_holding_hours: Decimal
    liquidity_score: Decimal
    reversal_score: Decimal
    marginal_recovery_efficiency: Decimal = ZERO
    urgency_score: Decimal = ZERO
    correlation_penalty: Decimal = ZERO
    dead_money_penalty: Decimal = ZERO
    expected_execution_cost_bps: Decimal = ZERO
    position_id: str | None = None

    def __post_init__(self) -> None:
        if not self.opportunity_id.strip():
            raise ValueError("opportunity_id is required")
        object.__setattr__(self, "pair", _pair(self.pair))
        object.__setattr__(
            self,
            "requested_quote",
            _non_negative(self.requested_quote, "requested_quote"),
        )
        object.__setattr__(
            self,
            "expected_net_edge_bps",
            _decimal(self.expected_net_edge_bps, "expected_net_edge_bps"),
        )
        hours = _decimal(self.expected_holding_hours, "expected_holding_hours")
        if hours <= ZERO:
            raise ValueError("expected_holding_hours must be positive")
        object.__setattr__(self, "expected_holding_hours", hours)
        for field in (
            "liquidity_score",
            "reversal_score",
            "marginal_recovery_efficiency",
            "urgency_score",
            "correlation_penalty",
            "dead_money_penalty",
        ):
            object.__setattr__(self, field, _unit(getattr(self, field), field))
        object.__setattr__(
            self,
            "expected_execution_cost_bps",
            _non_negative(
                self.expected_execution_cost_bps,
                "expected_execution_cost_bps",
            ),
        )
        if self.action == LabAction.ENTRY and self.position_id is not None:
            raise ValueError("ENTRY opportunity cannot already have position_id")
        if self.action in {
            LabAction.RECOVERY,
            LabAction.PARTIAL_RECYCLE,
        } and not self.position_id:
            raise ValueError(f"{self.action} requires position_id")

    @property
    def capital_time_edge_bps_per_hour(self) -> Decimal:
        return self.expected_net_edge_bps / self.expected_holding_hours


@dataclass(frozen=True)
class RankedOpportunity:
    opportunity: CapitalOpportunity
    score: Decimal
    components: Mapping[str, Decimal]


def score_opportunity(
    opportunity: CapitalOpportunity,
    *,
    policy: CapitalAuctionPolicy | None = None,
) -> RankedOpportunity:
    p = policy or CapitalAuctionPolicy()
    components = {
        "capital_time": (
            opportunity.capital_time_edge_bps_per_hour
            * p.capital_time_weight
        ),
        "reversal": opportunity.reversal_score * p.reversal_weight,
        "liquidity": opportunity.liquidity_score * p.liquidity_weight,
        "recovery_efficiency": (
            opportunity.marginal_recovery_efficiency
            * p.recovery_efficiency_weight
        ),
        "urgency": opportunity.urgency_score * p.urgency_weight,
        "correlation": (
            -opportunity.correlation_penalty
            * p.correlation_penalty_weight
        ),
        "dead_money": (
            -opportunity.dead_money_penalty
            * p.dead_money_penalty_weight
        ),
        "execution_cost": (
            -opportunity.expected_execution_cost_bps
            * p.execution_cost_weight
        ),
    }
    return RankedOpportunity(
        opportunity=opportunity,
        score=sum(components.values(), ZERO),
        components=components,
    )


@dataclass(frozen=True)
class AuctionAllocation:
    opportunity_id: str
    pair: str
    action: LabAction
    position_id: str | None
    score: Decimal
    allocated_quote: Decimal


@dataclass(frozen=True)
class CapitalAuctionResult:
    equity_quote: Decimal
    protected_reserve_quote: Decimal
    deployable_quote: Decimal
    target_positions: int
    allocations: tuple[AuctionAllocation, ...]
    skipped: Mapping[str, str]
    unallocated_quote: Decimal

    @property
    def proposed_quote(self) -> Decimal:
        return sum((row.allocated_quote for row in self.allocations), ZERO)


def run_capital_auction(
    opportunities: Iterable[CapitalOpportunity],
    *,
    equity_quote: Decimal,
    active_pairs_by_position: Mapping[str, str] | None = None,
    policy: CapitalAuctionPolicy | None = None,
) -> CapitalAuctionResult:
    """Rank scarce capital without reserving or dispatching it.

    The result is a research proposal only. Live code must still submit any
    chosen mutation to CapitalGovernor and the normal one-mutation runtime.
    """

    p = policy or CapitalAuctionPolicy()
    capacity = dynamic_fleet_capacity(equity_quote, policy=p)
    active = {
        str(position_id): _pair(pair)
        for position_id, pair in (active_pairs_by_position or {}).items()
    }
    active_bases = {base_asset(pair) for pair in active.values()}
    active_count = len(active)

    ranked = sorted(
        (score_opportunity(item, policy=p) for item in opportunities),
        key=lambda row: (
            row.score,
            row.opportunity.expected_net_edge_bps,
            row.opportunity.opportunity_id,
        ),
        reverse=True,
    )

    remaining = capacity.deployable_quote
    allocations: list[AuctionAllocation] = []
    skipped: dict[str, str] = {}
    mutated_positions: set[str] = set()
    planned_entry_bases: set[str] = set()
    planned_entries = 0

    for row in ranked:
        item = row.opportunity
        if item.action == LabAction.HOLD:
            skipped[item.opportunity_id] = "hold_has_no_capital_mutation"
            continue
        if row.score < p.min_score:
            skipped[item.opportunity_id] = "score_below_auction_floor"
            continue
        if item.requested_quote <= ZERO:
            skipped[item.opportunity_id] = "non_positive_request"
            continue

        if item.action == LabAction.ENTRY:
            asset = base_asset(item.pair)
            if asset in active_bases or asset in planned_entry_bases:
                skipped[item.opportunity_id] = "base_asset_already_active"
                continue
            if active_count + planned_entries >= capacity.target_positions:
                skipped[item.opportunity_id] = "fleet_target_reached"
                continue
        else:
            if item.position_id not in active:
                skipped[item.opportunity_id] = "position_not_active"
                continue
            if item.position_id in mutated_positions:
                skipped[item.opportunity_id] = (
                    "one_mutation_per_position_per_auction"
                )
                continue
            if active[item.position_id] != item.pair:
                skipped[item.opportunity_id] = "position_pair_mismatch"
                continue

        allocation = min(item.requested_quote, remaining)
        if allocation < p.minimum_viable_position_quote:
            skipped[item.opportunity_id] = "insufficient_deployable_capital"
            continue

        allocations.append(
            AuctionAllocation(
                opportunity_id=item.opportunity_id,
                pair=item.pair,
                action=item.action,
                position_id=item.position_id,
                score=row.score,
                allocated_quote=allocation,
            )
        )
        remaining -= allocation
        if item.action == LabAction.ENTRY:
            planned_entry_bases.add(base_asset(item.pair))
            planned_entries += 1
        elif item.position_id is not None:
            mutated_positions.add(item.position_id)

        if remaining < p.minimum_viable_position_quote:
            break

    return CapitalAuctionResult(
        equity_quote=capacity.equity_quote,
        protected_reserve_quote=capacity.protected_reserve_quote,
        deployable_quote=capacity.deployable_quote,
        target_positions=capacity.target_positions,
        allocations=tuple(allocations),
        skipped=skipped,
        unallocated_quote=remaining,
    )


@dataclass(frozen=True)
class MarketMicrostructure:
    pair: str
    price: Decimal
    spread_bps: Decimal
    depth_quote: Decimal
    liquidity_score: Decimal
    volatility_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _pair(self.pair))
        price = _decimal(self.price, "price")
        if price <= ZERO:
            raise ValueError("price must be positive")
        object.__setattr__(self, "price", price)
        for field in ("spread_bps", "depth_quote", "volatility_bps"):
            object.__setattr__(
                self,
                field,
                _non_negative(getattr(self, field), field),
            )
        object.__setattr__(
            self,
            "liquidity_score",
            _unit(self.liquidity_score, "liquidity_score"),
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    base_latency_bps: Decimal = Decimal("0.5")
    impact_at_full_depth_bps: Decimal = Decimal("25")
    maker_fill_floor: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        for field in (
            "maker_fee_bps",
            "taker_fee_bps",
            "base_latency_bps",
            "impact_at_full_depth_bps",
        ):
            object.__setattr__(
                self,
                field,
                _non_negative(getattr(self, field), field),
            )
        object.__setattr__(
            self,
            "maker_fill_floor",
            _unit(self.maker_fill_floor, "maker_fill_floor"),
        )


@dataclass(frozen=True)
class ExecutionEstimate:
    style: ExecutionStyle
    requested_quote: Decimal
    filled_quote: Decimal
    fill_ratio: Decimal
    fee_quote: Decimal
    slippage_quote: Decimal
    total_cost_quote: Decimal
    total_cost_bps: Decimal


def choose_execution_style(
    *,
    urgency_score: Decimal,
    market: MarketMicrostructure,
) -> ExecutionStyle:
    urgency = _unit(urgency_score, "urgency_score")
    if urgency >= Decimal("0.85"):
        return ExecutionStyle.TAKER
    if urgency >= Decimal("0.60") or market.liquidity_score < Decimal("0.35"):
        return ExecutionStyle.AGGRESSIVE_LIMIT
    if urgency >= Decimal("0.30"):
        return ExecutionStyle.IMPROVE_MAKER
    return ExecutionStyle.PATIENT_MAKER


def estimate_execution(
    *,
    requested_quote: Decimal,
    urgency_score: Decimal,
    market: MarketMicrostructure,
    policy: ExecutionPolicy,
    style: ExecutionStyle | None = None,
) -> ExecutionEstimate:
    """Deterministic execution stress model for research comparisons."""

    requested = _non_negative(requested_quote, "requested_quote")
    if requested <= ZERO:
        raise ValueError("requested_quote must be positive")
    chosen = style or choose_execution_style(
        urgency_score=urgency_score,
        market=market,
    )

    depth_ratio = (
        ONE
        if market.depth_quote <= ZERO
        else min(ONE, requested / market.depth_quote)
    )
    if chosen == ExecutionStyle.PATIENT_MAKER:
        fill_ratio = max(
            policy.maker_fill_floor,
            min(ONE, market.liquidity_score * Decimal("0.70")),
        )
        crossing_bps = ZERO
        fee_bps = policy.maker_fee_bps
        latency_bps = policy.base_latency_bps * Decimal("0.25")
    elif chosen == ExecutionStyle.IMPROVE_MAKER:
        fill_ratio = max(
            policy.maker_fill_floor,
            min(
                ONE,
                Decimal("0.20")
                + market.liquidity_score * Decimal("0.75"),
            ),
        )
        crossing_bps = market.spread_bps * Decimal("0.10")
        fee_bps = policy.maker_fee_bps
        latency_bps = policy.base_latency_bps * Decimal("0.50")
    elif chosen == ExecutionStyle.AGGRESSIVE_LIMIT:
        fill_ratio = max(
            Decimal("0.50"),
            min(
                ONE,
                Decimal("0.50")
                + market.liquidity_score * Decimal("0.50"),
            ),
        )
        crossing_bps = market.spread_bps * Decimal("0.35")
        fee_bps = policy.taker_fee_bps
        latency_bps = policy.base_latency_bps * Decimal("0.75")
    else:
        fill_ratio = ONE
        crossing_bps = market.spread_bps * Decimal("0.50")
        fee_bps = policy.taker_fee_bps
        latency_bps = policy.base_latency_bps

    filled = requested * fill_ratio
    impact_bps = policy.impact_at_full_depth_bps * depth_ratio * depth_ratio
    volatility_latency = (
        market.volatility_bps
        * (ONE - market.liquidity_score)
        * Decimal("0.05")
    )
    slippage_bps = (
        crossing_bps + impact_bps + latency_bps + volatility_latency
    )
    fee_quote = filled * fee_bps / BPS
    slippage_quote = filled * slippage_bps / BPS
    total = fee_quote + slippage_quote
    total_bps = ZERO if filled == ZERO else total * BPS / filled
    return ExecutionEstimate(
        style=chosen,
        requested_quote=requested,
        filled_quote=filled,
        fill_ratio=fill_ratio,
        fee_quote=fee_quote,
        slippage_quote=slippage_quote,
        total_cost_quote=total,
        total_cost_bps=total_bps,
    )


@dataclass(frozen=True)
class EquitySample:
    timestamp: float
    equity_quote: Decimal
    committed_quote: Decimal

    def __post_init__(self) -> None:
        if self.timestamp < 0:
            raise ValueError("timestamp must be non-negative")
        object.__setattr__(
            self,
            "equity_quote",
            _non_negative(self.equity_quote, "equity_quote"),
        )
        object.__setattr__(
            self,
            "committed_quote",
            _non_negative(self.committed_quote, "committed_quote"),
        )


@dataclass(frozen=True)
class PerformanceMetrics:
    start_equity_quote: Decimal
    end_equity_quote: Decimal
    net_profit_quote: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    cvar_95: Decimal
    capital_hours: Decimal
    capital_time_yield_quote_per_dollar_hour: Decimal
    turnover_quote: Decimal
    fee_drag_quote: Decimal
    time_underwater_hours: Decimal


def _period_returns(samples: Sequence[EquitySample]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, current in zip(samples, samples[1:]):
        if previous.equity_quote <= ZERO:
            continue
        returns.append(
            (current.equity_quote - previous.equity_quote)
            / previous.equity_quote
        )
    return returns


def calculate_performance_metrics(
    samples: Sequence[EquitySample],
    *,
    realized_net_profit_quote: Decimal,
    turnover_quote: Decimal,
    fee_drag_quote: Decimal,
) -> PerformanceMetrics:
    if len(samples) < 2:
        raise ValueError("at least two equity samples are required")
    ordered = sorted(samples, key=lambda row: row.timestamp)
    if list(samples) != ordered:
        raise ValueError("equity samples must be timestamp ordered")
    if any(
        current.timestamp <= previous.timestamp
        for previous, current in zip(samples, samples[1:])
    ):
        raise ValueError("equity sample timestamps must strictly increase")

    realized = _decimal(
        realized_net_profit_quote,
        "realized_net_profit_quote",
    )
    turnover = _non_negative(turnover_quote, "turnover_quote")
    fees = _non_negative(fee_drag_quote, "fee_drag_quote")

    peak = samples[0].equity_quote
    max_drawdown = ZERO
    underwater_seconds = Decimal("0")
    capital_hours = ZERO

    for previous, current in zip(samples, samples[1:]):
        elapsed_seconds = Decimal(str(current.timestamp - previous.timestamp))
        capital_hours += (
            previous.committed_quote
            * elapsed_seconds
            / Decimal("3600")
        )
        if previous.equity_quote < peak:
            underwater_seconds += elapsed_seconds
        peak = max(peak, current.equity_quote)
        if peak > ZERO:
            drawdown = (peak - current.equity_quote) / peak
            max_drawdown = max(max_drawdown, drawdown)

    returns = sorted(_period_returns(samples))
    if returns:
        tail_count = max(1, int(ceil(len(returns) * 0.05)))
        cvar = sum(returns[:tail_count], ZERO) / Decimal(tail_count)
    else:
        cvar = ZERO

    start = samples[0].equity_quote
    end = samples[-1].equity_quote
    total_return = ZERO if start == ZERO else (end - start) / start
    capital_time_yield = (
        ZERO if capital_hours == ZERO else realized / capital_hours
    )
    return PerformanceMetrics(
        start_equity_quote=start,
        end_equity_quote=end,
        net_profit_quote=realized,
        total_return=total_return,
        max_drawdown=max_drawdown,
        cvar_95=cvar,
        capital_hours=capital_hours,
        capital_time_yield_quote_per_dollar_hour=capital_time_yield,
        turnover_quote=turnover,
        fee_drag_quote=fees,
        time_underwater_hours=underwater_seconds / Decimal("3600"),
    )


def effective_exposure_count(
    weights: Mapping[str, Decimal],
    correlations: Mapping[tuple[str, str], Decimal],
) -> Decimal:
    """Correlation-aware effective number of independent exposures.

    Absolute correlation is deliberately conservative: strongly inverse and
    strongly positive crypto exposures both reduce the diversification credit.
    """

    clean = {
        str(asset): _non_negative(weight, f"weight[{asset}]")
        for asset, weight in weights.items()
        if _decimal(weight, f"weight[{asset}]") > ZERO
    }
    total = sum(clean.values(), ZERO)
    if total <= ZERO:
        return ZERO
    normalized = {asset: weight / total for asset, weight in clean.items()}
    concentration = ZERO
    for left, left_weight in normalized.items():
        for right, right_weight in normalized.items():
            if left == right:
                correlation = ONE
            else:
                correlation = abs(
                    _decimal(
                        correlations.get(
                            (left, right),
                            correlations.get((right, left), ZERO),
                        ),
                        f"correlation[{left},{right}]",
                    )
                )
                if correlation > ONE:
                    raise ValueError("correlation magnitude cannot exceed 1")
            concentration += left_weight * right_weight * correlation
    return ZERO if concentration <= ZERO else ONE / concentration
