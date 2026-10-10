"""Finite Decimal sizing of new offline research admissions.

The target is one percent of the current completed marked account equity.
It is capped by the caller's already reconciled productive cash, remaining pair
authorization and physical spendable cash after reserve and pending holds.
No inventory is resized and no ladder anchor, fund owner or order is changed.
"""

from __future__ import annotations

from decimal import Decimal


FROZEN_SIZING_PROTOCOL_SHA256 = "de40c6ec9d865175dd6ecd9d59cb9af0bbc4a79d9bfa589dbc2fe406a0a23575"
EQUITY_FRACTION = Decimal("0.01")
MODELED_ALL_IN_MINIMUM = Decimal("5")
ZERO = Decimal("0")


def _money(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be finite and nonnegative")
    return ZERO if value == ZERO else value


def _one_percent_exact(value: Decimal) -> Decimal:
    # Move the exponent instead of multiplying under the ambient Decimal
    # context. An unusually low caller precision must not round a money cap up.
    parts = value.as_tuple()
    return Decimal((0, parts.digits, parts.exponent - 2)) if value else ZERO


def productive_entry_budget(
    marked_equity: Decimal,
    productive_available: Decimal,
    pair_remaining: Decimal,
    physical_spendable: Decimal,
) -> tuple[Decimal, dict]:
    """Return an all-in new-entry proposal and JSON-safe funding diagnostics.

    Each input must be an exact, finite, nonnegative Decimal from the current
    completed account state. Unknown or invalid inputs raise; an unfunded or
    subminimum proposal returns zero with its limiting reason. This function
    uses no past result, future bar, realized-profit balance, external cash or
    previous call. The owner must freeze an admitted proposal before execution.

    ``pair_remaining`` is the remaining five-percent pair authorization, not a
    replacement for total existing exposure. ``physical_spendable`` must already
    protect the owner's strict twenty-five-percent physical reserve and pending
    holds. This pure helper cannot authenticate those caller-side calculations.

    The five-dollar floor is the frozen modeled *all-in* floor. The preserved
    native ten-dollar parent basis, actual gross order minima, quantity steps
    and fees remain mandatory downstream admission checks. A returned proposal
    between five and ten dollars does not authorize a native parent entry.
    """
    equity = _money(marked_equity, "marked_equity")
    productive = _money(productive_available, "productive_available")
    pair = _money(pair_remaining, "pair_remaining")
    physical = _money(physical_spendable, "physical_spendable")
    target = _one_percent_exact(equity)
    caps = {
        "equity_fraction_target": target,
        "productive_available": productive,
        "pair_remaining": pair,
        "physical_spendable": physical,
    }
    capped = min(caps.values())
    admitted = capped >= MODELED_ALL_IN_MINIMUM
    budget = capped if admitted else ZERO
    diagnostic = {
        "protocol_sha256": FROZEN_SIZING_PROTOCOL_SHA256,
        "research_only": True,
        "equity_fraction": str(EQUITY_FRACTION),
        "marked_equity": str(equity),
        "target_all_in": str(target),
        "caps_all_in": {name: str(value) for name, value in caps.items()},
        "limiting_caps": [name for name, value in caps.items() if value == capped],
        "capped_all_in": str(capped),
        "modeled_all_in_minimum": str(MODELED_ALL_IN_MINIMUM),
        "budget_all_in": str(budget),
        "admitted": admitted,
        "reason": ("admitted" if admitted else "zero_marked_equity"
                   if equity == ZERO else "unfunded" if capped == ZERO
                   else "below_modeled_all_in_minimum"),
        "native_and_exchange_admission_required": True,
    }
    return budget, diagnostic
