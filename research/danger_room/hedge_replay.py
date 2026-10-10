"""Constructed-input hedge bookkeeping pilot. Historical replay is disabled.

This module has no market, account, credential, or production imports. It tests
mechanics on explicitly fictional observations. Its outputs cannot establish
economic edge, historical feasibility, venue finality, or deployment approval.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Any

try:
    from .funding_feasibility import (
        CALCULATION_PRECISION, CONTRACT_SIZE, FundingScenario, assess_funding, decimal_value,
        whole_contract_target,
    )
except ImportError:  # Direct script invocation; the sibling is the same offline module.
    from funding_feasibility import (
        CALCULATION_PRECISION, CONTRACT_SIZE, FundingScenario, assess_funding, decimal_value,
        whole_contract_target,
    )

ZERO = Decimal(0)
ONE = Decimal(1)
EMBARGO_START = datetime.fromisoformat("2026-10-09T15:36:31.317254+00:00")
ARMS = (("PARK_OFF", "0"), ("PARK_BTC_25", "0.25"), ("PARK_BTC_50", "0.50"),
        ("PARK_BTC_75", "0.75"), ("PARK_BTC_100", "1.00"))
OWNERS = ("SPOT_ORDINARY", "VCAP", "PARK")
SCOPE = "MECHANICAL_SYNTHETIC_ONLY"
REPLAY_CALCULATION_PRECISION = 60


class ReplayBlocked(ValueError):
    pass


def _require(mapping: dict, names: tuple[str, ...], location: str) -> None:
    if not isinstance(mapping, dict):
        raise ReplayBlocked(f"{location} must be an object")
    missing = [name for name in names if name not in mapping or mapping[name] is None]
    if missing:
        raise ReplayBlocked(f"{location} missing explicit fields: {', '.join(missing)}")


def _number(value: Any, name: str, *, positive: bool = False, fraction: bool = False) -> Decimal:
    number = decimal_value(value, name, nonnegative=positive or fraction)
    if positive and number <= 0:
        raise ReplayBlocked(f"{name} must be positive")
    if fraction and number > 1:
        raise ReplayBlocked(f"{name} must be at most one")
    return number


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReplayBlocked(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayBlocked(f"{name} requires a timezone-aware UTC timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayBlocked(f"invalid {name}") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
        raise ReplayBlocked(f"{name} requires UTC")
    if timestamp >= EMBARGO_START:
        raise ReplayBlocked(f"{name} overlaps the sealed holdout embargo")
    return timestamp


def _duration(value: Any, name: str) -> timedelta:
    seconds = decimal_value(value, name, nonnegative=True)
    micros = seconds * Decimal(1000000)
    if micros != micros.to_integral_value() or seconds > Decimal(86400):
        raise ReplayBlocked(f"{name} requires exact microseconds and at most one day")
    return timedelta(microseconds=int(micros))


def validate_constructed_manifest(manifest: dict) -> None:
    """Validate declarations; fictional labels cannot authenticate real provenance."""
    _require(manifest, ("schema", "experiment_class", "fixture_only", "historical_inputs_deferred",
                       "approved_for_live", "capital", "initial_spot", "cash_owners", "estimator",
                       "signal", "execution", "route", "bars", "terminal_policy",
                       "accounting_tolerance", "calculation_precision"), "manifest")
    if manifest["schema"] != "spectra-constructed-hedge-pilot-v1":
        raise ReplayBlocked("unsupported manifest schema")
    if manifest["experiment_class"] != "CONSTRUCTED_PREEMBARGO_FIXTURE":
        raise ReplayBlocked("historical replay disabled: acquisition and independent input review deferred")
    if manifest["fixture_only"] is not True or manifest["historical_inputs_deferred"] is not True:
        raise ReplayBlocked("only explicit fictional fixtures are admitted")
    if manifest["approved_for_live"] is not False:
        raise ReplayBlocked("live approval is forbidden")
    if manifest["calculation_precision"] != REPLAY_CALCULATION_PRECISION:
        raise ReplayBlocked("constructed Decimal calculation precision must be explicitly 60")
    if manifest["terminal_policy"] != "CANCEL_PENDING_AND_MARK_OPEN_POSITIONS":
        raise ReplayBlocked("terminal behavior must be explicit and supported")
    _number(manifest["accounting_tolerance"], "accounting_tolerance", positive=True)
    capital = manifest["capital"]
    _require(capital, ("hedge_cash", "source", "same_extra_cash_all_arms"), "capital")
    if capital["source"] != "EXPLICIT_ADDITIONAL_COMMON_RESEARCH_CASH" or capital["same_extra_cash_all_arms"] is not True:
        raise ReplayBlocked("protected PARK/VCAP cash cannot fund a hedge; equal extra cash must be explicit")
    _number(capital["hedge_cash"], "hedge_cash", fraction=False)
    if decimal_value(capital["hedge_cash"]) < 0:
        raise ReplayBlocked("negative hedge cash")
    if not isinstance(manifest["cash_owners"], dict) or set(manifest["cash_owners"]) != set(OWNERS):
        raise ReplayBlocked("exact disjoint SPOT_ORDINARY/VCAP/PARK ownership required")
    for owner, amount in manifest["cash_owners"].items():
        decimal_value(amount, owner, nonnegative=True)
    assets = manifest["initial_spot"]
    if (not isinstance(assets, dict) or not assets
            or any(not isinstance(asset, str) or not (asset.startswith("FICTIONAL_ALT_") or asset == "FICTIONAL_DUST") for asset in assets)):
        raise ReplayBlocked("fixture requires explicit fictional alt/dust identities; BTC/ETH spot identities excluded")
    for asset, item in assets.items():
        _require(item, ("units", "all_in_basis", "source_owner"), asset)
        decimal_value(item["units"], f"{asset}.units", nonnegative=True)
        decimal_value(item["all_in_basis"], f"{asset}.basis", nonnegative=True)
        if not isinstance(item["source_owner"], str) or item["source_owner"] not in OWNERS:
            raise ReplayBlocked("unknown spot source owner")
    est = manifest["estimator"]
    _require(est, ("formula", "lookback_returns", "minimum_observations", "beta_lower", "beta_upper",
                   "zero_variance_policy", "missing_beta_policy"), "estimator")
    if est["formula"] != "OLS_INTERCEPT_COMPLETED_SIMPLE_RETURNS_V1" or est["zero_variance_policy"] != "UNKNOWN" or est["missing_beta_policy"] != "TARGET_ZERO_NEXT_EVENT":
        raise ReplayBlocked("unsupported estimator or missingness rule")
    lookback = _integer(est["lookback_returns"], "lookback_returns", 2)
    minimum = _integer(est["minimum_observations"], "minimum_observations", 2)
    if minimum > lookback:
        raise ReplayBlocked("minimum observations exceeds lookback")
    lower = decimal_value(est["beta_lower"], nonnegative=True)
    upper = decimal_value(est["beta_upper"], nonnegative=True)
    if lower > upper:
        raise ReplayBlocked("invalid beta bounds")
    signal = manifest["signal"]
    _require(signal, ("formula", "entry_return", "exit_return", "entry_confirmations", "exit_confirmations",
                      "minimum_dwell_bars", "cooldown_bars", "decision_lag_seconds", "rebalance_every_bars"), "signal")
    if signal["formula"] != "BTC_LOOKBACK_RETURN_CONFIRMATION_V1":
        raise ReplayBlocked("unsupported signal")
    entry, exit_ = decimal_value(signal["entry_return"]), decimal_value(signal["exit_return"])
    if not -1 < entry < 0 < exit_:
        raise ReplayBlocked("distinct negative entry and positive exit thresholds required")
    for name in ("entry_confirmations", "exit_confirmations", "rebalance_every_bars"):
        _integer(signal[name], name, 1)
    for name in ("minimum_dwell_bars", "cooldown_bars"):
        _integer(signal[name], name)
    _duration(signal["decision_lag_seconds"], "decision_lag_seconds")
    exe = manifest["execution"]
    _require(exe, ("latency_seconds", "spread_fraction", "slippage_fraction", "liquidation_slippage_fraction",
                   "fill_policy", "event_order", "stale_policy"), "execution")
    _duration(exe["latency_seconds"], "latency_seconds")
    for name in ("spread_fraction", "slippage_fraction", "liquidation_slippage_fraction"):
        _number(exe[name], name, fraction=True)
    if exe["fill_policy"] != "DECLARED_NEXT_BAR_OPEN_FRACTION_FLOORED" or exe["event_order"] != "OPEN_RISK_FILL_FUNDING_ADVERSE_MARK_SPOT_CLOSE_SIGNAL" or exe["stale_policy"] != "ABORT_MISSING_DECISIVE_MARK":
        raise ReplayBlocked("unsupported execution assumptions")
    route = manifest["route"]
    _require(route, ("instrument_id", "contract_size_btc", "minimum_contracts", "position_limit_contracts",
                    "initial_margin_rate", "maintenance_margin_rate", "entry_fee_per_contract", "exit_fee_per_contract",
                    "liquidation_fee", "liquidation_trigger_buffer", "funding_sign_convention", "recourse",
                    "collateral_conversion_fee_fraction", "funding_reserve", "forced_liquidation_execution"), "route")
    if not str(route["instrument_id"]).startswith("FICTIONAL_") or decimal_value(route["contract_size_btc"]) != CONTRACT_SIZE:
        raise ReplayBlocked("this pilot supports explicit fictional 0.01 BTC whole contracts only")
    _integer(route["minimum_contracts"], "minimum_contracts", 1)
    _integer(route["position_limit_contracts"], "position_limit_contracts", 1)
    im = _number(route["initial_margin_rate"], "initial_margin_rate", positive=True, fraction=True)
    mm = _number(route["maintenance_margin_rate"], "maintenance_margin_rate", positive=True, fraction=True)
    if mm > im:
        raise ReplayBlocked("maintenance margin exceeds initial margin")
    for name in ("entry_fee_per_contract", "exit_fee_per_contract", "liquidation_fee", "liquidation_trigger_buffer"):
        decimal_value(route[name], name, nonnegative=True)
    if route["funding_sign_convention"] != "POSITIVE_RATE_CREDITS_SHORT":
        raise ReplayBlocked("funding sign must be explicit")
    if route["forced_liquidation_execution"] != "SYNTHETIC_INSTANT_COVER_AT_ADVERSE_MARK_EVEN_DURING_OUTAGE":
        raise ReplayBlocked("forced liquidation execution assumption must be explicit")
    if route["recourse"] not in {"SYNTHETIC_HEDGE_CASH_ONLY_WITH_UNPAID_LIABILITY", "SYNTHETIC_SPOT_THEN_ALL_OWNER_CASH"}:
        raise ReplayBlocked("unmodeled collateral recourse")
    _number(route["collateral_conversion_fee_fraction"], "conversion fee", fraction=True)
    if decimal_value(route["collateral_conversion_fee_fraction"]) == 1:
        raise ReplayBlocked("conversion fee must be less than one")
    reserve = route["funding_reserve"]
    _require(reserve, ("adverse_rally_fraction", "funding_cost_fraction_bound", "liquidation_cash_buffer", "fixed_buffer"), "funding_reserve")
    for name, value in reserve.items():
        decimal_value(value, name, nonnegative=True)
    bars = manifest["bars"]
    if not isinstance(bars, list) or len(bars) < minimum + 2:
        raise ReplayBlocked("insufficient constructed bars")
    previous_close = None
    for index, bar in enumerate(bars):
        _require(bar, ("open_time", "close_time", "published_at", "btc_open", "btc_close", "btc_adverse_mark",
                       "spot_open_marks", "spot_close_marks", "fill_fraction", "instrument_online", "funding_rate",
                       "spot_events"), f"bar {index}")
        open_, close, published = (_timestamp(bar[key], f"bar {index}.{key}") for key in ("open_time", "close_time", "published_at"))
        if not open_ < close <= published or (previous_close is not None and open_ <= previous_close):
            raise ReplayBlocked("noncausal or overlapping constructed bars")
        previous_close = close
        for name in ("btc_open", "btc_close", "btc_adverse_mark"):
            _number(bar[name], name, positive=True)
        if decimal_value(bar["btc_adverse_mark"]) < max(decimal_value(bar["btc_open"]), decimal_value(bar["btc_close"])):
            raise ReplayBlocked("short adverse mark must include declared open and close")
        for marks_name in ("spot_open_marks", "spot_close_marks"):
            if not isinstance(bar[marks_name], dict) or set(bar[marks_name]) != set(assets):
                raise ReplayBlocked("all owned spot marks are decisive; no fabricated zero")
            for asset, value in bar[marks_name].items():
                _number(value, f"{marks_name}.{asset}", positive=True)
        _number(bar["fill_fraction"], "fill_fraction", fraction=True)
        if not isinstance(bar["instrument_online"], bool):
            raise ReplayBlocked("instrument_online must be explicit boolean")
        decimal_value(bar["funding_rate"], "funding_rate")
        if not isinstance(bar["spot_events"], list):
            raise ReplayBlocked("spot_events must be explicit")
        for event in bar["spot_events"]:
            _require(event, ("asset", "side", "units", "price", "fee", "source_owner"), "spot event")
            if (not isinstance(event["asset"], str) or event["asset"] not in assets
                    or not isinstance(event["side"], str) or event["side"] not in {"BUY", "SELL"}
                    or not isinstance(event["source_owner"], str)
                    or event["source_owner"] != assets[event["asset"]]["source_owner"]):
                raise ReplayBlocked("spot asset/side/ownership mismatch")
            _number(event["units"], "spot units", positive=True)
            _number(event["price"], "spot price", positive=True)
            decimal_value(event["fee"], "spot fee", nonnegative=True)


@dataclass
class Asset:
    units: Decimal
    basis: Decimal
    owner: str
    acquired: Decimal = ZERO
    sold: Decimal = ZERO
    seized: Decimal = ZERO
    basis_retired: Decimal = ZERO


class Arm:
    def __init__(self, manifest: dict, name: str, ratio: str):
        self.m, self.name, self.ratio = manifest, name, Decimal(ratio)
        self.assets = {asset: Asset(decimal_value(item["units"]), decimal_value(item["all_in_basis"]), item["source_owner"])
                       for asset, item in manifest["initial_spot"].items()}
        self.cash = {owner: decimal_value(value) for owner, value in manifest["cash_owners"].items()}
        self.free = decimal_value(manifest["capital"]["hedge_cash"])
        self.locked = ZERO
        self.liability = ZERO
        self.contracts = 0
        self.entry = ZERO
        self.spot_realized_net = ZERO
        self.hedge_realized_gross = ZERO
        self.hedge_fees = ZERO
        self.funding = ZERO
        self.liquidation_fees = ZERO
        self.conversion_fees = ZERO
        self.state = "NORMAL"
        self.active = False
        self.entry_count = self.exit_count = 0
        self.changed_index = -10**9
        self.cooldown_until = -1
        self.pending = None
        self.observations: list[tuple[Decimal, Decimal]] = []
        self.btc_closes: list[Decimal] = []
        self.previous_marks = None
        self.previous_units = None
        self.previous_btc = None
        self.events = []
        self.frames = []
        self.liquidations = 0
        self.trace_diverged = False
        self.start_spot_unrealized = self.spot_unreal(manifest["bars"][0]["spot_open_marks"])
        self.start_equity = self.equity(manifest["bars"][0]["spot_open_marks"], decimal_value(manifest["bars"][0]["btc_open"]))
        self.peak = self.start_equity
        self.drawdown = ZERO
        self.peak_margin_utilization = ZERO
        self.locked_contract_bar_sum = ZERO

    def spot_value(self, marks: dict) -> Decimal:
        return sum((item.units * decimal_value(marks[asset]) for asset, item in self.assets.items()), ZERO)

    def spot_unreal(self, marks: dict) -> Decimal:
        return self.spot_value(marks) - sum((item.basis for item in self.assets.values()), ZERO)

    def hedge_unreal(self, mark: Decimal) -> Decimal:
        return self.contracts * CONTRACT_SIZE * (self.entry - mark)

    def equity(self, marks: dict, btc: Decimal) -> Decimal:
        return sum(self.cash.values(), ZERO) + self.spot_value(marks) + self.free + self.locked + self.hedge_unreal(btc) - self.liability

    def cash_delta(self, delta: Decimal) -> None:
        """Paid cash is finite; unpaid loss is an explicit liability, never credit."""
        if delta >= 0:
            self.free += delta
            return
        due = -delta
        from_free = min(self.free, due)
        self.free -= from_free
        due -= from_free
        from_locked = min(self.locked, due)
        self.locked -= from_locked
        self.liability += due - from_locked

    def reduce(self, amount: int, price: Decimal, index: int, kind: str) -> None:
        if amount <= 0:
            return
        if amount > self.contracts:
            raise AssertionError("cannot close unowned short contracts")
        released = self.locked * Decimal(amount) / Decimal(self.contracts)
        self.locked -= released
        self.free += released
        gross = amount * CONTRACT_SIZE * (self.entry - price)
        fee = amount * decimal_value(self.m["route"]["exit_fee_per_contract"])
        self.cash_delta(gross - fee)
        self.hedge_realized_gross += gross
        self.hedge_fees += fee
        self.contracts -= amount
        if not self.contracts:
            self.entry = ZERO
        self.events.append({"bar": index, "kind": kind, "contracts": amount, "price": str(price), "gross_pnl": str(gross), "fee": str(fee)})

    def seize_recourse(self, marks: dict, index: int) -> None:
        if not self.liability or self.m["route"]["recourse"] != "SYNTHETIC_SPOT_THEN_ALL_OWNER_CASH":
            return
        rate = decimal_value(self.m["route"]["collateral_conversion_fee_fraction"])
        for asset in sorted(self.assets):
            item, mark = self.assets[asset], decimal_value(marks[asset])
            if item.units == 0 or self.liability == 0:
                continue
            units = min(item.units, self.liability / (mark * (ONE - rate)))
            retired = item.basis * units / item.units
            gross = units * mark
            fee = gross * rate
            item.units -= units
            item.basis -= retired
            item.seized += units
            item.basis_retired += retired
            self.spot_realized_net += gross - fee - retired
            self.conversion_fees += fee
            self.liability = max(ZERO, self.liability - (gross - fee))
            self.trace_diverged = True
            self.events.append({"bar": index, "kind": "VENUE_RECOURSE_SPOT_SEIZURE", "asset": asset,
                                "units": str(units), "basis_retired": str(retired), "conversion_fee": str(fee)})
        for owner in OWNERS:
            paid = min(self.cash[owner], self.liability)
            if paid:
                self.cash[owner] -= paid
                self.liability -= paid
                self.trace_diverged = True
                self.events.append({"bar": index, "kind": "VENUE_RECOURSE_OWNER_CASH_SEIZURE", "owner": owner, "amount": str(paid)})

    def liquidation_check(self, btc: Decimal, marks: dict, index: int) -> None:
        if not self.contracts:
            return
        threshold = self.contracts * CONTRACT_SIZE * btc * decimal_value(self.m["route"]["maintenance_margin_rate"])
        threshold += decimal_value(self.m["route"]["liquidation_trigger_buffer"])
        hedge_equity = self.free + self.locked + self.hedge_unreal(btc) - self.liability
        utilization = threshold / hedge_equity if hedge_equity > 0 else ONE
        self.peak_margin_utilization = max(self.peak_margin_utilization, utilization)
        if hedge_equity <= threshold:
            price = btc * (ONE + decimal_value(self.m["execution"]["liquidation_slippage_fraction"]))
            self.reduce(self.contracts, price, index, "FORCED_SYNTHETIC_LIQUIDATION")
            fee = decimal_value(self.m["route"]["liquidation_fee"])
            self.cash_delta(-fee)
            self.liquidation_fees += fee
            self.liquidations += 1
            self.pending = None
            self.seize_recourse(marks, index)
            self.active = False
            self.state = "HEDGE_EXIT"
            self.cooldown_until = index + self.m["signal"]["cooldown_bars"]
            self.events.append({"bar": index, "kind": "LIQUIDATION_COST", "fee": str(fee), "unpaid_liability": str(self.liability)})

    def execute(self, bar: dict, index: int) -> None:
        if self.pending is None:
            return
        target, submitted, ready, decision_index = self.pending
        open_ = _timestamp(bar["open_time"], "open_time")
        if open_ <= submitted or open_ < ready or index <= decision_index:
            self.events.append({"bar": index, "kind": "WAIT_LATENCY"})
            return
        if not bar["instrument_online"] or decimal_value(bar["fill_fraction"]) == 0:
            self.events.append({"bar": index, "kind": "NO_FILL", "reason": "OUTAGE_OR_DECLARED_ZERO_CAPACITY"})
            return
        price = decimal_value(bar["btc_open"])
        exposure = self.spot_value(bar["spot_open_marks"])
        target = min(target, whole_contract_target(exposure, ONE, price), self.m["route"]["position_limit_contracts"])
        delta = target - self.contracts
        quantity = int((Decimal(abs(delta)) * decimal_value(bar["fill_fraction"])).to_integral_value(rounding=ROUND_FLOOR))
        if quantity == 0:
            self.events.append({"bar": index, "kind": "NO_FILL", "reason": "WHOLE_CONTRACT_PARTIAL_ROUNDS_TO_ZERO"})
            return
        friction = decimal_value(self.m["execution"]["spread_fraction"]) / 2 + decimal_value(self.m["execution"]["slippage_fraction"])
        if delta < 0:
            self.reduce(quantity, price * (ONE + friction), index, "DELAYED_COVER")
        elif delta > 0 and not self.liability:
            fill = price * (ONE - friction)
            if fill <= 0:
                raise ReplayBlocked("invalid short execution price")
            route = self.m["route"]
            reserve = route["funding_reserve"]
            available_equity = max(ZERO, self.free + self.locked + self.hedge_unreal(price) - self.liability)
            admission = assess_funding(exposure, ONE, available_equity,
                                      FundingScenario(btc_quote=price, initial_margin_rate=route["initial_margin_rate"],
                                                      maintenance_margin_rate=route["maintenance_margin_rate"],
                                                      entry_fee_per_contract=route["entry_fee_per_contract"],
                                                      exit_fee_per_contract=route["exit_fee_per_contract"], **reserve),
                                      max_contracts=route["position_limit_contracts"])
            quantity = min(quantity, max(0, admission["admitted_contracts"] - self.contracts))
            margin_unit = price * CONTRACT_SIZE * decimal_value(route["initial_margin_rate"])
            fee_unit = decimal_value(route["entry_fee_per_contract"])
            affordable = int((self.free / (margin_unit + fee_unit)).to_integral_value(rounding=ROUND_FLOOR))
            quantity = min(quantity, affordable)
            if quantity < route["minimum_contracts"]:
                self.events.append({"bar": index, "kind": "CASH_OR_MINIMUM_DENIAL", "requested_target": target})
                return
            fee, margin = quantity * fee_unit, quantity * margin_unit
            self.free -= margin + fee
            self.locked += margin
            self.entry = (self.entry * self.contracts + fill * quantity) / Decimal(self.contracts + quantity)
            self.contracts += quantity
            self.hedge_fees += fee
            self.events.append({"bar": index, "kind": "DELAYED_SHORT", "decision_bar": decision_index,
                                "submitted_at": submitted.isoformat(), "executed_at": open_.isoformat(),
                                "contracts": quantity, "price": str(fill), "fee": str(fee), "locked": str(margin)})
        self.pending = None

    def spot_events(self, bar: dict) -> None:
        for event in bar["spot_events"]:
            item = self.assets[event["asset"]]
            quantity, price, fee = (decimal_value(event[name]) for name in ("units", "price", "fee"))
            owner = event["source_owner"]
            if event["side"] == "BUY":
                cost = quantity * price + fee
                if cost > self.cash[owner]:
                    raise ReplayBlocked("unchanged spot trace is infeasible: insufficient owner cash")
                self.cash[owner] -= cost
                item.units += quantity
                item.basis += cost
                item.acquired += quantity
            else:
                if quantity > item.units or fee > quantity * price:
                    raise ReplayBlocked("spot disposal violates units or finite sale proceeds")
                retired = item.basis * quantity / item.units
                self.cash[owner] += quantity * price - fee
                self.spot_realized_net += quantity * price - fee - retired
                item.units -= quantity
                item.basis -= retired
                item.basis_retired += retired
                item.sold += quantity

    def observe(self, bar: dict) -> Decimal | None:
        btc = decimal_value(bar["btc_close"])
        marks = bar["spot_close_marks"]
        if self.previous_marks is not None:
            prior_value = sum((self.previous_units[asset] * decimal_value(self.previous_marks[asset]) for asset in self.assets), ZERO)
            market_change = sum((self.previous_units[asset] * (decimal_value(marks[asset]) - decimal_value(self.previous_marks[asset])) for asset in self.assets), ZERO)
            if prior_value > 0:
                self.observations.append((btc / self.previous_btc - ONE, market_change / prior_value))
        self.previous_marks = copy.deepcopy(marks)
        self.previous_units = {asset: item.units for asset, item in self.assets.items()}
        self.previous_btc = btc
        self.btc_closes.append(btc)
        est = self.m["estimator"]
        window = self.observations[-est["lookback_returns"]:]
        if len(window) < est["minimum_observations"]:
            return None
        xmean = sum((x for x, _ in window), ZERO) / Decimal(len(window))
        ymean = sum((y for _, y in window), ZERO) / Decimal(len(window))
        variance = sum(((x - xmean) ** 2 for x, _ in window), ZERO)
        if variance == 0:
            return None
        beta = sum(((x - xmean) * (y - ymean) for x, y in window), ZERO) / variance
        return max(decimal_value(est["beta_lower"]), min(decimal_value(est["beta_upper"]), beta))

    def decide(self, bar: dict, index: int, beta: Decimal | None) -> None:
        signal = self.m["signal"]
        lookback = self.m["estimator"]["lookback_returns"]
        decision = _timestamp(bar["close_time"], "close_time")
        decision += _duration(signal["decision_lag_seconds"], "decision_lag_seconds")
        if decision >= EMBARGO_START:
            raise ReplayBlocked("decision overlaps embargo")
        if _timestamp(bar["published_at"], "published_at") > decision:
            raise ReplayBlocked("observation was not published before decision")
        trend = None if len(self.btc_closes) <= lookback else self.btc_closes[-1] / self.btc_closes[-lookback - 1] - ONE
        if self.active:
            self.exit_count = self.exit_count + 1 if trend is not None and trend >= decimal_value(signal["exit_return"]) else 0
            self.state = "REVERSAL_WATCH" if self.exit_count else "HEDGE_ACTIVE"
            if self.exit_count >= signal["exit_confirmations"] and index - self.changed_index >= signal["minimum_dwell_bars"]:
                self.active, self.state, self.changed_index = False, "HEDGE_EXIT", index
                self.cooldown_until = index + signal["cooldown_bars"]
                self.exit_count = self.entry_count = 0
        elif index > self.cooldown_until:
            self.entry_count = self.entry_count + 1 if trend is not None and trend <= decimal_value(signal["entry_return"]) else 0
            self.state = "RISK_OFF_WATCH" if self.entry_count else "NORMAL"
            if self.entry_count >= signal["entry_confirmations"]:
                self.active, self.state, self.changed_index = True, "HEDGE_ACTIVE", index
                self.entry_count = 0
        if self.ratio == 0:
            return
        effective_ratio = min(ONE, max(ZERO, beta or ZERO) * self.ratio) if self.active else ZERO
        target = whole_contract_target(self.spot_value(bar["spot_close_marks"]), effective_ratio, decimal_value(bar["btc_close"]))
        target = min(target, self.m["route"]["position_limit_contracts"])
        if index % signal["rebalance_every_bars"] == 0 or beta is None or not self.active:
            if target == self.contracts:
                if self.pending is not None:
                    self.events.append({"bar": index, "kind": "CAUSAL_CANCEL_OBSOLETE_TARGET"})
                self.pending = None
            elif self.pending is None or self.pending[0] != target:
                ready = decision + _duration(self.m["execution"]["latency_seconds"], "latency_seconds")
                self.pending = target, decision, ready, index
                self.events.append({"bar": index, "kind": "CAUSAL_DECISION", "beta": None if beta is None else str(beta),
                                    "state": self.state, "target_contracts": target, "decision_at": decision.isoformat()})
            # An unchanged target keeps its original causal submission/readiness.

    def check(self, marks: dict, btc: Decimal) -> None:
        tolerance = decimal_value(self.m["accounting_tolerance"])
        if self.free < 0 or self.locked < 0 or self.liability < 0 or any(amount < 0 for amount in self.cash.values()):
            raise AssertionError("negative spendable or owned cash")
        for asset, item in self.assets.items():
            initial = decimal_value(self.m["initial_spot"][asset]["units"])
            if abs(initial + item.acquired - item.sold - item.seized - item.units) > tolerance or item.units < 0 or item.basis < 0:
                raise AssertionError("spot unit conservation")
            initial_basis = decimal_value(self.m["initial_spot"][asset]["all_in_basis"])
            acquired_basis = sum((decimal_value(event["units"]) * decimal_value(event["price"]) + decimal_value(event["fee"])
                                  for bar in self.m["bars"][:len(self.frames) + 1] for event in bar["spot_events"]
                                  if event["asset"] == asset and event["side"] == "BUY"), ZERO)
            if abs(initial_basis + acquired_basis - item.basis_retired - item.basis) > tolerance:
                raise AssertionError("spot basis conservation")
        expected = (self.spot_realized_net + self.spot_unreal(marks) - self.start_spot_unrealized + self.hedge_realized_gross
                    + self.hedge_unreal(btc) - self.hedge_fees + self.funding - self.liquidation_fees)
        residual = self.equity(marks, btc) - self.start_equity - expected
        if abs(residual) > tolerance:
            raise AssertionError(f"account equity conservation: {residual}")

    def run(self) -> dict:
        for index, bar in enumerate(self.m["bars"]):
            # Mark/risk is checked before discretionary orders at an adverse opening gap.
            self.liquidation_check(decimal_value(bar["btc_open"]), bar["spot_open_marks"], index)
            self.execute(bar, index)
            funded = self.contracts * CONTRACT_SIZE * decimal_value(bar["btc_open"]) * decimal_value(bar["funding_rate"])
            self.cash_delta(funded)
            self.funding += funded
            if funded:
                self.events.append({"bar": index, "kind": "FUNDING_SETTLEMENT", "signed_cash": str(funded)})
            self.liquidation_check(decimal_value(bar["btc_adverse_mark"]), bar["spot_open_marks"], index)
            self.spot_events(bar)
            beta = self.observe(bar)
            self.decide(bar, index, beta)
            btc, marks = decimal_value(bar["btc_close"]), bar["spot_close_marks"]
            self.check(marks, btc)
            equity = self.equity(marks, btc)
            self.peak = max(self.peak, equity)
            dd = (self.peak - equity) / self.peak if self.peak > 0 else ZERO
            self.drawdown = max(self.drawdown, dd)
            self.locked_contract_bar_sum += self.locked
            self.frames.append({"bar": index, "equity": str(equity), "free_hedge_cash": str(self.free),
                                "locked_margin": str(self.locked), "unpaid_liability": str(self.liability),
                                "short_contracts": self.contracts, "short_notional": str(self.contracts * CONTRACT_SIZE * btc),
                                "spot_value": str(self.spot_value(marks)), "beta": None if beta is None else str(beta), "state": self.state})
        pending_cancelled = self.pending is not None
        self.pending = None
        end = self.m["bars"][-1]
        return {"arm": self.name, "ratio": str(self.ratio), "scope": SCOPE, "approved_for_live": False,
                "historical_execution_allowed": False, "economic_edge_claim": False,
                "starting_equity": str(self.start_equity), "ending_equity": self.frames[-1]["equity"],
                "maximum_observed_close_drawdown": str(self.drawdown), "peak_margin_utilization": str(self.peak_margin_utilization),
                "locked_cash_bar_sum_not_opportunity_cost": str(self.locked_contract_bar_sum),
                "spot_realized_pnl_net": str(self.spot_realized_net), "hedge_realized_pnl_gross": str(self.hedge_realized_gross),
                "hedge_unrealized_pnl": str(self.hedge_unreal(decimal_value(end["btc_close"]))),
                "hedge_fees": str(self.hedge_fees), "funding_signed_cash": str(self.funding),
                "liquidation_fees": str(self.liquidation_fees), "collateral_conversion_fees_in_spot_net_pnl": str(self.conversion_fees),
                "cash_by_original_owner": {owner: str(amount) for owner, amount in self.cash.items()},
                "ending_inventory": {asset: {"units": str(item.units), "all_in_basis": str(item.basis), "source_owner": item.owner,
                                             "acquired_units": str(item.acquired), "sold_units": str(item.sold), "seized_units": str(item.seized)}
                                     for asset, item in self.assets.items()},
                "liquidations": self.liquidations, "spot_trace_diverged_by_recourse": self.trace_diverged,
                "unpaid_liability": str(self.liability), "pending_order_cancelled_at_terminal": pending_cancelled,
                "conservation_checks_passed": True, "frames": self.frames, "events": self.events}


def run_matrix(manifest: dict) -> dict:
    """Only a constructed pilot: gate acceptance never authorizes historical work."""
    validate_constructed_manifest(manifest)
    with localcontext() as context:
        context.prec = REPLAY_CALCULATION_PRECISION
        results = [Arm(copy.deepcopy(manifest), name, ratio).run() for name, ratio in ARMS]
        if len({arm["starting_equity"] for arm in results}) != 1:
            raise AssertionError("unequal initial consolidated equity")
        baseline = Decimal(results[0]["ending_equity"])
        for arm in results:
            arm["descriptive_synthetic_equity_delta_vs_off"] = str(Decimal(arm["ending_equity"]) - baseline)
        return {"schema": "spectra-constructed-hedge-pilot-result-v1", "scope": SCOPE,
                "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "fixture_only": True, "historical_execution_allowed": False, "approved_for_live": False,
                "economic_edge_claim": False, "arms": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("constructed_manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        fixture_root = Path(__file__).resolve().parents[2] / "docs/research-results/danger-room-preparation-20261010"
        allowed_names = {f"hedge-synthetic-{name}.fixture.json" for name in ("base", "no-fill", "partial", "gap-cash-only", "gap-same-account")}
        supplied = arguments.constructed_manifest.absolute()
        if supplied.parent != fixture_root or supplied.name not in allowed_names or supplied.is_symlink() or supplied.resolve() != supplied:
            raise ReplayBlocked("CLI only reads the five shipped constructed fixture files; arbitrary/holdout inputs refused before open")
        result = run_matrix(json.loads(arguments.constructed_manifest.read_text(encoding="utf-8")))
    except (ReplayBlocked, ValueError, AssertionError) as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    arguments.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
