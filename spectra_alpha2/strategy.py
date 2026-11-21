"""Immutable-reference drawdown math and VCAP-only planning primitives."""
from __future__ import annotations

from decimal import Decimal

from .policy import VCAP_OUTER_BOUNDARY, LadderStep, next_eligible_vcap_step


def calculate_drawdown(reference_price: Decimal, current_price: Decimal) -> Decimal:
    """Calculate drawdown from the immutable initial-entry reference price."""
    for value, name in ((reference_price, "reference_price"), (current_price, "current_price")):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite positive Decimal")
    return current_price / reference_price - Decimal("1")


def next_vcap_shard(
    drawdown: Decimal,
    completed_shard_ids: set[str] | frozenset[str],
) -> LadderStep | None:
    """Return the next provisional VCAP shard; this planner is not enabled by default."""
    if not isinstance(drawdown, Decimal) or not drawdown.is_finite():
        raise TypeError("drawdown must be a finite Decimal")
    if drawdown <= VCAP_OUTER_BOUNDARY:
        return None
    return next_eligible_vcap_step(drawdown, completed_shard_ids)
