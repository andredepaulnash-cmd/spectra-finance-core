"""Offline conditional cash feasibility; no venue, order, signal, or price requests.

The functions admit constructed scenarios only. They do not establish actual
historical margin, collateral segregation, instrument access, or liquidation
finality. Protected spot/PARK/VCAP owners cannot fund the separate hedge book.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from typing import Any

CONTRACT_SIZE = Decimal("0.01")
RATIOS = (Decimal("0"), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal("1"))
CALCULATION_PRECISION = 200


def decimal_value(value: Any, name: str = "value", *, nonnegative: bool = False) -> Decimal:
    """Parse bounded exact values; binary floats and Boolean integers are invalid."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} requires an exact Decimal, integer, or decimal string")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if len(result.as_tuple().digits) > 80 or abs(result.as_tuple().exponent) > 130 or (result and abs(result.adjusted()) > 50):
        raise ValueError(f"{name} exceeds supported exact arithmetic bounds")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _fraction(value: Any, name: str) -> Decimal:
    result = decimal_value(value, name, nonnegative=True)
    if result > 1:
        raise ValueError(f"{name} must be at most one")
    return result


def whole_contract_target(
    spot_value: Any,
    ratio: Any,
    btc_quote: Any,
    size: Any = CONTRACT_SIZE,
    economic_notional_cap: Any | None = None,
) -> int:
    """Floor the protective USD target to whole contracts, never round upward."""
    spot = decimal_value(spot_value, "spot_value", nonnegative=True)
    requested_ratio = _fraction(ratio, "ratio")
    price = decimal_value(btc_quote, "btc_quote", nonnegative=True)
    contract_size = decimal_value(size, "size", nonnegative=True)
    if price == 0 or contract_size == 0:
        raise ValueError("btc_quote and size must be positive")
    cap = spot if economic_notional_cap is None else decimal_value(
        economic_notional_cap, "economic_notional_cap", nonnegative=True
    )
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        bounded_target = min(spot * requested_ratio, spot, cap)
        return int((bounded_target / (price * contract_size)).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class FundingScenario:
    """Explicit hypothetical parameters. None preserves an unresolved real input."""

    btc_quote: Any | None = None
    initial_margin_rate: Any | None = None
    maintenance_margin_rate: Any | None = None
    adverse_rally_fraction: Any | None = None
    funding_cost_fraction_bound: Any | None = None
    entry_fee_per_contract: Any | None = None
    exit_fee_per_contract: Any | None = None
    liquidation_cash_buffer: Any | None = None
    fixed_buffer: Any | None = None


def _validated_scenario(scenario: FundingScenario) -> tuple[dict[str, Decimal], list[str]]:
    result: dict[str, Decimal] = {}
    unknown = []
    for name in scenario.__dataclass_fields__:
        value = getattr(scenario, name)
        if value is None:
            unknown.append(name)
        else:
            result[name] = decimal_value(value, name, nonnegative=True)
    for name in ("initial_margin_rate", "maintenance_margin_rate", "funding_cost_fraction_bound"):
        if name in result and result[name] > 1:
            raise ValueError(f"{name} must be at most one")
    if "btc_quote" in result and result["btc_quote"] == 0:
        raise ValueError("btc_quote must be positive")
    if "initial_margin_rate" in result and result["initial_margin_rate"] == 0:
        raise ValueError("initial_margin_rate must be positive")
    if "maintenance_margin_rate" in result and result["maintenance_margin_rate"] == 0:
        raise ValueError("maintenance_margin_rate must be positive")
    if {"initial_margin_rate", "maintenance_margin_rate"} <= result.keys():
        if result["maintenance_margin_rate"] > result["initial_margin_rate"]:
            raise ValueError("maintenance margin exceeds initial margin")
    return result, unknown


def assess_funding(
    spot_value: Any,
    ratio: Any,
    hedge_cash: Any,
    scenario: FundingScenario,
    *,
    max_contracts: int = 10000,
) -> dict[str, Any]:
    """Conservative reserve test with exact cash and capped whole-contract size.

    Reserve per contract = max(initial margin, stressed maintenance margin)
    + adverse short loss + entry/exit fees + bounded adverse funding.
    The liquidation and fixed cash buffers apply once to any nonzero position.
    These are reserves, not double-booked economic charges. A finite stress
    assumption cannot guarantee solvency against an unbounded short loss.
    """
    spot = decimal_value(spot_value, "spot_value", nonnegative=True)
    requested_ratio = _fraction(ratio, "ratio")
    budget = decimal_value(hedge_cash, "hedge_cash", nonnegative=True)
    if isinstance(max_contracts, bool) or not isinstance(max_contracts, int) or max_contracts <= 0:
        raise ValueError("max_contracts must be a positive integer")
    parameters, unknown = _validated_scenario(scenario)
    result: dict[str, Any] = {
        "scope": "CONDITIONAL_CONSTRUCTED_SCENARIO_ONLY",
        "ratio": str(requested_ratio),
        "spot_value": str(spot),
        "hedge_cash": str(budget),
        "unknown_parameters": unknown,
        "actual_historical_feasibility_proven": False,
        "approved_for_live": False,
    }
    if requested_ratio == 0:
        result.update(status="OFF", target_contracts=0, admitted_contracts=0,
                      total_cash_reserved="0", remaining_unreserved_cash=str(budget))
        return result
    if unknown:
        result.update(status="BLOCKED_UNKNOWN_INPUTS", target_contracts=None, admitted_contracts=0)
        return result
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        target = whole_contract_target(spot, requested_ratio, parameters["btc_quote"])
        venue_capped = min(target, max_contracts)
        notional = CONTRACT_SIZE * parameters["btc_quote"]
        stress_notional = notional * (1 + parameters["adverse_rally_fraction"])
        initial_margin = notional * parameters["initial_margin_rate"]
        maintenance = stress_notional * parameters["maintenance_margin_rate"]
        adverse_loss = notional * parameters["adverse_rally_fraction"]
        fees = parameters["entry_fee_per_contract"] + parameters["exit_fee_per_contract"]
        funding = stress_notional * parameters["funding_cost_fraction_bound"]
        unit_reserve = max(initial_margin, maintenance) + adverse_loss + fees + funding
        once_buffer = parameters["liquidation_cash_buffer"] + parameters["fixed_buffer"]
        cash_target = max(Decimal(0), budget - once_buffer)
        affordable = int((cash_target / unit_reserve).to_integral_value(rounding=ROUND_FLOOR))
        admitted = min(venue_capped, affordable)
        reserved = admitted * unit_reserve + (once_buffer if admitted else 0)
        if not 0 <= reserved <= budget:
            raise AssertionError("finite cash conservation violated")
        required_for_target = target * unit_reserve + (once_buffer if target else 0)
        result.update(
            status=("OFF" if requested_ratio == 0 else "BELOW_ONE_CONTRACT" if target == 0
                    else "CONDITIONAL_TARGET_FUNDED" if admitted == target
                    else "CONDITIONAL_TARGET_PARTLY_FUNDED" if admitted else "NO_CONTRACT_FUNDED"),
            target_contracts=target,
            venue_capped_contracts=venue_capped,
            admitted_contracts=admitted,
            notional_per_contract=str(notional),
            target_notional=str(spot * requested_ratio),
            admitted_notional=str(admitted * notional),
            per_contract_reserve=str(unit_reserve),
            required_cash_for_target=str(required_for_target),
            total_cash_reserved=str(reserved),
            remaining_unreserved_cash=str(budget - reserved),
            reserve_components_per_contract={
                "initial_margin": str(initial_margin),
                "stressed_maintenance_margin": str(maintenance),
                "selected_margin_reserve": str(max(initial_margin, maintenance)),
                "adverse_short_loss": str(adverse_loss),
                "entry_and_exit_fees": str(fees),
                "adverse_funding_bound": str(funding),
            },
            once_only_buffers=str(once_buffer),
        )
        return result


@dataclass(frozen=True)
class CashOwners:
    """Disjoint USD owners; protected ownership is explicit, never inferred."""

    physical_cash: Any
    park_owned: Any
    vcap_owned: Any
    ordinary_owned: Any
    hedge_owned: Any

    def validated(self) -> dict[str, Decimal]:
        amounts = {
            field: decimal_value(getattr(self, field), field, nonnegative=True)
            for field in self.__dataclass_fields__
        }
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            partition = sum((amounts[name] for name in ("park_owned", "vcap_owned", "ordinary_owned", "hedge_owned")), Decimal(0))
            if partition != amounts["physical_cash"]:
                raise ValueError("cash ownership partitions must reconcile exactly")
        return amounts


def allocate_equal_extra_cash(owners: CashOwners, extra_cash: Any) -> list[dict[str, str]]:
    """Deposit the same declared scenario-only extra cash into every hedge book.

    Existing spot owner balances remain byte-for-value unchanged. This creates a
    new capital experiment, not evidence that the historical book was funded.
    """
    amounts = owners.validated()
    extra = decimal_value(extra_cash, "extra_cash", nonnegative=True)
    if amounts["hedge_owned"] != 0:
        raise ValueError("starting hedge book must be empty for equal extra-cash construction")
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        records = []
        for ratio in RATIOS:
            updated = {name: str(value) for name, value in amounts.items()}
            updated["physical_cash"] = str(amounts["physical_cash"] + extra)
            updated["hedge_owned"] = str(extra)
            CashOwners(**updated).validated()
            updated.update(
                ratio=str(ratio),
                extra_deposit=str(extra),
                scope="CONSTRUCTED_EQUAL_EXTRA_CAPITAL_ONLY",
                spot_owners_unchanged="true",
                approved_for_live="false",
            )
            records.append(updated)
        return records


def reconcile_saved_cash_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit known historical row partitions; rows cannot prove event-level free cash.

    This descriptive function deliberately returns no admitted historical hedge
    budget. Pending orders and intra-frame commitments require their own replay.
    """
    if not rows:
        raise ValueError("saved cash rows must not be empty")
    parsed = []
    previous_timestamp = None
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        for row in rows:
            if set(("timestamp", "cash", "parked", "ordinary", "marked_inventory", "equity")) - row.keys():
                raise ValueError("saved cash row lacks required fields")
            timestamp = row["timestamp"]
            if isinstance(timestamp, bool) or not isinstance(timestamp, (str, int)):
                raise ValueError("timestamp must be an exact whole integer")
            text = str(timestamp)
            if not text.isdigit():
                raise ValueError("timestamp must be an exact whole integer")
            timestamp = int(text)
            if previous_timestamp is not None and timestamp != previous_timestamp + 300:
                raise ValueError("saved account frames must be continuous at five minutes")
            previous_timestamp = timestamp
            values = {name: decimal_value(row[name], name, nonnegative=True)
                      for name in ("cash", "parked", "ordinary", "marked_inventory", "equity")}
            if values["cash"] != values["parked"] + values["ordinary"]:
                raise ValueError("saved cash partition does not reconcile")
            if abs(values["equity"] - values["cash"] - values["marked_inventory"]) > Decimal("1e-20"):
                raise ValueError("saved cash and inventory do not reconcile to equity")
            parsed.append({"timestamp": timestamp, **values})
        minimum = min(x["ordinary"] for x in parsed)
        maximum = max(x["ordinary"] for x in parsed)
        return {
            "frames_checked": len(parsed),
            "first_frame_timestamp": parsed[0]["timestamp"],
            "last_frame_timestamp": parsed[-1]["timestamp"],
            "first_ordinary_cash": str(parsed[0]["ordinary"]),
            "last_ordinary_cash": str(parsed[-1]["ordinary"]),
            "minimum_ordinary_cash": str(minimum),
            "maximum_ordinary_cash": str(maximum),
            "frames_ordinary_at_least_10_usd": sum(x["ordinary"] >= 10 for x in parsed),
            "static_trace_carveout_necessary_row_upper_bound": str(minimum),
            "USD_cent_floor_of_static_row_upper_bound": str((minimum * 100).to_integral_value(rounding=ROUND_FLOOR) / 100),
            "cash_partitions_reconciled_exactly": True,
            "equity_absolute_tolerance": "1e-20",
            "pending_order_commitments_replayed": False,
            "all_intra_frame_cash_events_replayed": False,
            "admitted_historical_hedge_budget": None,
            "trace_cash_admission_status": "BLOCKED_TRACE_CASH_UNKNOWN",
            "scope": "NECESSARY_ROW_BOUND_ONLY_NOT_SUFFICIENT_EVENT_CASH_PROOF",
        }
