"""Immutable-reference drawdown calculation for Alpha 2.0 position monitoring."""
from __future__ import annotations

from decimal import Decimal


def calculate_drawdown(reference_price: Decimal, current_price: Decimal) -> Decimal:
    """Calculate drawdown from the immutable initial-entry reference price."""
    for value, name in ((reference_price, "reference_price"), (current_price, "current_price")):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite positive Decimal")
    return current_price / reference_price - Decimal("1")
