from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Iterable


def exact_decimal_sum(values: Iterable[Decimal]) -> Decimal:
    """Add finite journal amounts without rounding stored ownership away."""
    items = tuple(values)
    if not items:
        return Decimal("0")
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in items):
        raise ValueError("ledger sum requires finite Decimal values")
    nonzero = [value for value in items if value]
    if not nonzero:
        return Decimal("0")
    lowest = min(value.as_tuple().exponent for value in nonzero)
    highest = max(value.adjusted() for value in nonzero)
    with localcontext() as context:
        context.prec = max(28, highest - lowest + len(str(len(items))) + 2)
        return sum(items, Decimal("0"))


def exact_decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    return exact_decimal_sum((left, right.copy_negate()))
