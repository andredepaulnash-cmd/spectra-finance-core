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
MAX_OPEN_POSITIONS = 74
INITIAL_ENTRY_NOTIONAL = Decimal("20")
MAX_ASSET_GROSS_NOTIONAL = Decimal("1000")
BUY_FEE_RATE = Decimal("0.004")
SELL_FEE_RATE = Decimal("0.004")
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


validate_portfolio_policy()
