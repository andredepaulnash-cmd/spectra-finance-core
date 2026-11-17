"""Immutable Alpha 2.0 portfolio, DCA, VCAP and fee policy."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")
ONE = Decimal("1")

PORTFOLIO_EQUITY = Decimal("25000")
INITIAL_ENTRY_BUDGET = Decimal("6250")
VCAP_RECOVERY_POOL = Decimal("18750")
MAX_OPEN_POSITIONS = 74
INITIAL_ENTRY_NOTIONAL = Decimal("20")
MAX_ASSET_GROSS_NOTIONAL = Decimal("1000")
DCA_ADDITION_NOTIONAL = Decimal("20")
BUY_FEE_RATE = Decimal("0.004")
SELL_FEE_RATE = Decimal("0.004")
NET_TAKE_PROFIT_RATE = Decimal("0.0174")
VCAP_OUTER_BOUNDARY = Decimal("-0.24")
MAX_ACTIVE_VCAP_SHARDS_PER_ASSET = 2


class VCAPPhase(StrEnum):
    DCA = "DCA"\n    FAT_1 = "FAT_1"
    SKINNY = "SKINNY"
    FAT_2 = "FAT_2"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True, slots=True)
class LadderStep:
    step_id: str
    drawdown: Decimal
    addition: Decimal
    phase: VCAPPhase


DCA_STEPS: tuple[LadderStep, ...] = (
    LadderStep("DCA_1", Decimal("-0.0274"), DCA_ADDITION_NOTIONAL, VCAPPhase.DCA),
    LadderStep("DCA_2", Decimal("-0.0444"), DCA_ADDITION_NOTIONAL, VCAPPhase.FAT_1),
    LadderStep("DCA_3", Decimal("-0.0674"), DCA_ADDITION_NOTIONAL, VCAPPhase.FAT_1),
)

VCAP_STEPS: tuple[LadderStep, ...] = (
    LadderStep("VCAP_FAT1_1", Decimal("-0.0777"), Decimal("20"), VCAPPhase.FAT_1),
    LadderStep("VCAP_FAT1_2", Decimal("-0.0974"), Decimal("40"), VCAPPhase.FAT_1),
    LadderStep("VCAP_FAT1_3", Decimal("-0.1074"), Decimal("80"), VCAPPhase.FAT_1),
    LadderStep("VCAP_FAT1_4", Decimal("-0.1174"), Decimal("160"), VCAPPhase.FAT_1),
    LadderStep("VCAP_SKINNY_1", Decimal("-0.1274"), Decimal("30"), VCAPPhase.SKINNY),
    LadderStep("VCAP_SKINNY_2", Decimal("-0.1424"), Decimal("30"), VCAPPhase.SKINNY),
    LadderStep("VCAP_SKINNY_3", Decimal("-0.1574"), Decimal("40"), VCAPPhase.SKINNY),
    LadderStep("VCAP_SKINNY_4", Decimal("-0.1724"), Decimal("40"), VCAPPhase.SKINNY),
    LadderStep("VCAP_FAT2_1", Decimal("-0.1924"), Decimal("160"), VCAPPhase.FAT_2),
    LadderStep("VCAP_FAT2_2", Decimal("-0.2174"), Decimal("320"), VCAPPhase.FAT_2),
)


def _require_decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{name} must be a finite Decimal")
    return value


def validate_portfolio_policy() -> None:
    if INITIAL_ENTRY_BUDGET + VCAP_RECOVERY_POOL != PORTFOLIO_EQUITY:
        raise ValueError("capital pools must equal simulated portfolio equity")
    if sum((step.addition for step in DCA_STEPS), ZERO) != Decimal("60"):
        raise ValueError("DCA additions must total $60 per asset")
    if sum((step.addition for step in VCAP_STEPS), ZERO) != Decimal("920"):
        raise ValueError("VCAP ladder must total $920 per asset")
    scheduled = INITIAL_ENTRY_NOTIONAL + sum((s.addition for s in DCA_STEPS), ZERO) + sum((s.addition for s in VCAP_STEPS), ZERO)
    if scheduled != MAX_ASSET_GROSS_NOTIONAL:
        raise ValueError("scheduled per-asset notional must equal the $1,000 cap")


def next_eligible_vcap_step(drawdown: Decimal, completed_step_ids: set[str] | frozenset[str]) -> LadderStep | None:
    """Return the next sequential ladder step; never skip an unfunded step."""
    drawdown = _require_decimal(drawdown, "drawdown")
    if drawdown <= VCAP_OUTER_BOUNDARY:
        return None
    for step in VCAP_STEPS:
        if step.step_id in completed_step_ids:
            continue
        return step if drawdown <= step.drawdown else None
    return None


def vcap_phase_for_drawdown(drawdown: Decimal) -> VCAPPhase | None:
    drawdown = _require_decimal(drawdown, "drawdown")
    if drawdown <= VCAP_OUTER_BOUNDARY:
        return VCAPPhase.MANUAL_REVIEW
    if drawdown <= Decimal("-0.1777"):
        return VCAPPhase.FAT_2
    if drawdown < Decimal("-0.1174"):
        return VCAPPhase.SKINNY
    if drawdown <= Decimal("-0.0777"):
        return VCAPPhase.FAT_1
    return None


def can_admit_position(open_positions: int) -> bool:
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        raise ValueError("open_positions must be a non-negative integer")
    return open_positions < MAX_OPEN_POSITIONS


def validate_asset_exposure(committed_gross_notional: Decimal, proposed_addition: Decimal) -> Decimal:
    committed = _require_decimal(committed_gross_notional, "committed_gross_notional")
    addition = _require_decimal(proposed_addition, "proposed_addition")
    if committed < ZERO or addition <= ZERO:
        raise ValueError("exposure must be non-negative and addition positive")
    total = committed + addition
    if total > MAX_ASSET_GROSS_NOTIONAL:
        raise ValueError("per-asset gross-notional cap exceeded")
    return total


def gross_sell_notional_for_net_target(fee_inclusive_cost_basis: Decimal) -> Decimal:
    """Gross sale quote needed to net +1.74% after the estimated sell fee.

    The supplied cost basis must already include buy fees. Sell fees are then
    removed from gross proceeds before comparing against the net target.
    """
    cost = _require_decimal(fee_inclusive_cost_basis, "fee_inclusive_cost_basis")
    if cost <= ZERO:
        raise ValueError("fee-inclusive cost basis must be positive")
    target_net_proceeds = cost * (ONE + NET_TAKE_PROFIT_RATE)
    return target_net_proceeds / (ONE - SELL_FEE_RATE)


validate_portfolio_policy()
