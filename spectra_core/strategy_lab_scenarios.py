from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Iterable

from .strategy_lab import (
    CapitalAuctionPolicy,
    CapitalAuctionResult,
    CapitalOpportunity,
    ExecutionPolicy,
    MarketMicrostructure,
    run_capital_auction,
)


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class AdversarialScenario:
    """Explicit stress hypothesis; magnitudes are research inputs, not truths."""

    name: str
    fee_multiplier: Decimal = ONE
    spread_multiplier: Decimal = ONE
    depth_multiplier: Decimal = ONE
    volatility_multiplier: Decimal = ONE
    return_shock: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name is required")
        for field in (
            "fee_multiplier",
            "spread_multiplier",
            "depth_multiplier",
            "volatility_multiplier",
        ):
            value = Decimal(getattr(self, field))
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{field} must be finite and non-negative")
            object.__setattr__(self, field, value)
        shock = Decimal(self.return_shock)
        if not shock.is_finite() or shock <= Decimal("-1"):
            raise ValueError("return_shock must be finite and greater than -1")
        object.__setattr__(self, "return_shock", shock)


FEE_SHOCK = AdversarialScenario(
    name="fee_shock",
    fee_multiplier=Decimal("1.75"),
)
LIQUIDITY_CRUNCH = AdversarialScenario(
    name="liquidity_crunch",
    spread_multiplier=Decimal("3"),
    depth_multiplier=Decimal("0.20"),
    volatility_multiplier=Decimal("2"),
)
CORRELATED_CRASH = AdversarialScenario(
    name="correlated_crash",
    spread_multiplier=Decimal("2.5"),
    depth_multiplier=Decimal("0.30"),
    volatility_multiplier=Decimal("3"),
    return_shock=Decimal("-0.20"),
)
TOKEN_COLLAPSE = AdversarialScenario(
    name="token_collapse",
    spread_multiplier=Decimal("5"),
    depth_multiplier=Decimal("0.05"),
    volatility_multiplier=Decimal("5"),
    return_shock=Decimal("-0.65"),
)
PUMP_DUMP = AdversarialScenario(
    name="pump_dump",
    spread_multiplier=Decimal("4"),
    depth_multiplier=Decimal("0.15"),
    volatility_multiplier=Decimal("4"),
    return_shock=Decimal("-0.35"),
)

DEFAULT_ADVERSARIAL_SCENARIOS = (
    FEE_SHOCK,
    LIQUIDITY_CRUNCH,
    CORRELATED_CRASH,
    TOKEN_COLLAPSE,
    PUMP_DUMP,
)


def stress_market(
    market: MarketMicrostructure,
    scenario: AdversarialScenario,
) -> MarketMicrostructure:
    return MarketMicrostructure(
        pair=market.pair,
        price=market.price * (ONE + scenario.return_shock),
        spread_bps=market.spread_bps * scenario.spread_multiplier,
        depth_quote=market.depth_quote * scenario.depth_multiplier,
        liquidity_score=max(
            ZERO,
            min(
                ONE,
                market.liquidity_score * scenario.depth_multiplier,
            ),
        ),
        volatility_bps=(
            market.volatility_bps * scenario.volatility_multiplier
        ),
    )


def stress_execution_policy(
    policy: ExecutionPolicy,
    scenario: AdversarialScenario,
) -> ExecutionPolicy:
    return replace(
        policy,
        maker_fee_bps=policy.maker_fee_bps * scenario.fee_multiplier,
        taker_fee_bps=policy.taker_fee_bps * scenario.fee_multiplier,
    )


def stress_return_path(
    returns: Iterable[Decimal],
    scenario: AdversarialScenario,
) -> tuple[Decimal, ...]:
    values = tuple(Decimal(value) for value in returns)
    if not values:
        return ()
    stressed = list(values)
    # Put the discrete shock in the middle so tests do not accidentally benefit
    # from always applying a crash only after the strategy has finished.
    middle = len(stressed) // 2
    stressed[middle] = (ONE + stressed[middle]) * (
        ONE + scenario.return_shock
    ) - ONE
    return tuple(stressed)


@dataclass
class CompoundingResearchAccount:
    """Simulation-only equity state with immediate realized-profit reuse."""

    equity_quote: Decimal

    def __post_init__(self) -> None:
        self.equity_quote = Decimal(self.equity_quote)
        if not self.equity_quote.is_finite() or self.equity_quote < ZERO:
            raise ValueError("equity_quote must be finite and non-negative")

    def book_realized_pnl(self, pnl_quote: Decimal) -> Decimal:
        pnl = Decimal(pnl_quote)
        if not pnl.is_finite():
            raise ValueError("pnl_quote must be finite")
        updated = self.equity_quote + pnl
        if updated < ZERO:
            raise ValueError("realized loss exceeds simulated equity")
        self.equity_quote = updated
        return self.equity_quote

    def auction(
        self,
        opportunities: Iterable[CapitalOpportunity],
        *,
        active_pairs_by_position: dict[str, str] | None = None,
        policy: CapitalAuctionPolicy | None = None,
    ) -> CapitalAuctionResult:
        return run_capital_auction(
            opportunities,
            equity_quote=self.equity_quote,
            active_pairs_by_position=active_pairs_by_position,
            policy=policy,
        )
