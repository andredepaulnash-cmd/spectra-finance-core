"""Confirmed Alpha 2.0 portfolio limits and fee-accounting constants.

VCAP shard selection, batching, sizing and recycling remain unconfigured until
the Architect-approved governor policy is supplied. No DCA ladder is defined.
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")

PORTFOLIO_EQUITY = Decimal("25000")
INITIAL_ENTRY_BUDGET = Decimal("6250")
VCAP_RECOVERY_POOL = Decimal("18750")
# Hard concurrent-position ceiling: RESERVED + OPEN; reject the 75th reservation.
MAX_OPEN_POSITIONS = 74
# Initial operating target, not the hard limit; never open positions just to fill it.
INITIAL_TARGET_POSITIONS = 50
# Maximum new positions admitted per cycle; default and upper bound are both 10.
MAX_NEW_POSITIONS_PER_CYCLE = 10
INITIAL_ENTRY_NOTIONAL = Decimal("30")
MAX_ASSET_GROSS_NOTIONAL = Decimal("1000")
BUY_FEE_RATE = Decimal("0.004")
SELL_FEE_RATE = Decimal("0.004")
NET_TAKE_PROFIT_RATE = Decimal("0.0174")
VCAP_OUTER_BOUNDARY = Decimal("-0.24")
MAX_ACTIVE_VCAP_SHARDS_PER_ASSET = 2

# The initial-entry pool is shared by all non-recovery entry actions.
INITIAL_ENTRY_POOL_ACTIONS = frozenset({"ENTRY", "PRESS", "ROTATION"})


def _require_decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{name} must be a finite Decimal")
    return value


def validate_portfolio_policy() -> None:
    if INITIAL_ENTRY_BUDGET + VCAP_RECOVERY_POOL != PORTFOLIO_EQUITY:
        raise ValueError("capital pools must equal simulated portfolio equity")
    if INITIAL_ENTRY_NOTIONAL <= ZERO or INITIAL_ENTRY_NOTIONAL > MAX_ASSET_GROSS_NOTIONAL:
        raise ValueError("initial entry must remain within the per-asset gross-notional cap")
    if not 1 <= INITIAL_TARGET_POSITIONS <= MAX_OPEN_POSITIONS:
        raise ValueError("initial target must be between 1 and the hard position ceiling")
    if not 1 <= MAX_NEW_POSITIONS_PER_CYCLE <= MAX_OPEN_POSITIONS:
        raise ValueError("per-cycle admission ceiling must be a positive integer")
    if MAX_ACTIVE_VCAP_SHARDS_PER_ASSET != 2:
        raise ValueError("Alpha 2.0 VCAP concurrency ceiling must remain two shards per asset")
    if INITIAL_ENTRY_POOL_ACTIONS != frozenset({"ENTRY", "PRESS", "ROTATION"}):
        raise ValueError("ENTRY, PRESS and ROTATION must share the initial-entry pool")


def can_admit_position(open_positions: int) -> bool:
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        raise ValueError("open_positions must be a non-negative integer")
    return open_positions < MAX_OPEN_POSITIONS


def validate_asset_exposure(
    committed_gross_notional: Decimal, proposed_addition: Decimal
) -> Decimal:
    committed = _require_decimal(committed_gross_notional, "committed_gross_notional")
    addition = _require_decimal(proposed_addition, "proposed_addition")
    if committed < ZERO or addition <= ZERO:
        raise ValueError("exposure must be non-negative and addition positive")
    total = committed + addition
    if total > MAX_ASSET_GROSS_NOTIONAL:
        raise ValueError("per-asset gross-notional cap exceeded")
    return total


def gross_sell_notional_for_net_target(fee_inclusive_cost_basis: Decimal) -> Decimal:
    """Gross proceeds required to realize the confirmed net target after sell fees."""
    basis = _require_decimal(fee_inclusive_cost_basis, "fee_inclusive_cost_basis")
    if basis <= ZERO:
        raise ValueError("fee_inclusive_cost_basis must be positive")
    return basis * (ONE + NET_TAKE_PROFIT_RATE) / (ONE - SELL_FEE_RATE)


validate_portfolio_policy()
