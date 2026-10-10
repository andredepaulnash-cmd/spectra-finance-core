"""Frozen causal rotation evidence and dollar gates for offline research.

The tracker receives completed observations, never an unread tape suffix. Its
price-recovery events reproduce the frozen selector's float comparisons; its
daily return means and strict dollar comparisons use exact rational arithmetic.
These helpers do not authorize cash, release a slot, or create execution fills.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from decimal import Decimal, Inexact, MAX_EMAX, MIN_EMIN, Rounded, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from typing import Sequence

import numpy as np

from .opportunity_selector import SelectorConfig


FROZEN_ROTATION_PROTOCOL_SHA256 = "6b6b3a83a159e09d0df8d9407d169cd85bf1b800c6462644a3c30d6a06e91da2"
FROZEN_SELECTOR_SOURCE_SHA256 = "db4878d392738b11df75f42fec0e779b24fcfed95b79020a2111acf3883bb305"
BAR_SECONDS = 300
DAY_SECONDS = 86400
BARS_PER_DAY = 288
FORECAST_DAYS = 7
MINIMUM_VALID_ROWS_PER_DAY = 274
STALE_AGE_SECONDS = 14 * DAY_SECONDS
EPISODE_LOOKBACK_SECONDS = 7 * DAY_SECONDS
REPLACEMENT_BUDGET = Decimal("10.25")
FEE_RATE = Fraction(40, 10000)
SLIPPAGE_RATE = Fraction(5, 10000)
DISPLAY_PRECISION = 50


def _timestamp(value, name: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an aligned integer completed timestamp")
    value = int(value)
    if value < 0 or value % BAR_SECONDS or value > np.iinfo(np.int64).max - STALE_AGE_SECONDS:
        raise ValueError(f"{name} is outside the supported aligned calendar")
    return value


def _fraction_text(value: Fraction) -> str:
    # Formatting never changes the exact rational used for either strict gate.
    with localcontext() as context:
        context.prec = DISPLAY_PRECISION
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        context.rounding = ROUND_HALF_EVEN
        context.traps[Inexact] = False
        context.traps[Rounded] = False
        context.clear_flags()
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _fraction_receipt(value: Fraction) -> dict:
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def _price_fraction(value: float) -> Fraction:
    return Fraction(Decimal(str(float(value))))


def _decimal_fraction(value, name: str, *, positive: bool = False) -> Fraction:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a finite Decimal")
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return Fraction(value)


def _mean_fraction(value, name: str) -> Fraction:
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        result = Fraction(value)
    else:
        raise TypeError(f"{name} must be an exact Fraction or finite Decimal")
    if result <= -1:
        raise ValueError(f"{name} cannot imply a nonpositive future price")
    return result


def _receipt() -> dict:
    return {"protocol_sha256": FROZEN_ROTATION_PROTOCOL_SHA256,
            "selector_source_sha256": FROZEN_SELECTOR_SOURCE_SHA256,
            "research_only": True, "cash_authorized": False}


class RotationTracker:
    """Pure streaming evidence with the frozen selector's event definition.

    ``bars`` has columns O/H/L/C/base-volume/trade-count, one row per declared
    pair. ``completed_at`` is the aligned actual bar CLOSE time. Missing rows
    remain missing. Missing calendar times are explicit gaps, not forward fills.
    Consuming future observations cannot be undone: the adapter must update once
    per completed frame, before planning, in the same chronology as the selector.
    """

    def __init__(self, pairs: Sequence[str]) -> None:
        self.pairs = tuple(pairs)
        if (not self.pairs or any(not isinstance(pair, str) or not pair for pair in self.pairs)
                or len(set(self.pairs)) != len(self.pairs)):
            raise ValueError("pairs must be unique, nonempty strings")
        self._pair_index = {pair: index for index, pair in enumerate(self.pairs)}
        self._n = len(self.pairs)
        self._config = SelectorConfig()
        self._day = None
        self._first_completed_at = None
        self._completed_at = None
        self._day_count = np.zeros(self._n, dtype=np.int64)
        self._completed_days = deque(maxlen=FORECAST_DAYS)
        self._boundaries = deque(maxlen=FORECAST_DAYS + 1)
        self._latest_close = np.full(self._n, np.nan)
        self._close_times = deque()
        self._closes = {}
        self._trough = np.full(self._n, np.nan)
        self._trough_at = np.zeros(self._n, dtype=np.int64)
        self._last_episode_at = np.full(self._n, -1, dtype=np.int64)
        self._episode_count = np.zeros(self._n, dtype=np.int64)
        self._forecast_cache = {}

    @property
    def completed_at(self) -> int | None:
        return self._completed_at

    def _index(self, pair: str) -> int:
        try:
            return self._pair_index[pair]
        except (KeyError, TypeError) as exc:
            raise ValueError("pair is outside the declared tracker universe") from exc

    def _finish_day(self, day: int) -> None:
        self._completed_days.append((day, self._day_count.copy()))
        end = (day + 1) * DAY_SECONDS
        boundary = self._latest_close.copy() if self._completed_at == end else None
        self._boundaries.append((end, boundary))
        self._day_count.fill(0)
        self._forecast_cache.clear()

    def update(self, completed_at: int, bars: np.ndarray) -> None:
        """Consume only this completed frame; retain exact event timestamps."""
        completed_at = _timestamp(completed_at, "completed_at")
        if self._completed_at is not None and completed_at <= self._completed_at:
            raise ValueError("completed observations must be strictly chronological")
        values = np.asarray(bars)
        if values.shape != (self._n, 6):
            raise ValueError("bars must have shape (number of declared pairs, 6)")
        if (not np.issubdtype(values.dtype, np.number)
                or np.issubdtype(values.dtype, np.complexfloating)):
            raise ValueError("bars must contain real numeric values")
        values = values.astype(float, copy=False)
        day = (completed_at - 1) // DAY_SECONDS
        if self._day is None:
            self._day = day
            self._first_completed_at = completed_at
            self._forecast_cache.clear()
        while self._day < day:
            self._finish_day(self._day)
            self._day += 1
        if self._completed_at is not None and completed_at != self._completed_at + BAR_SECONDS:
            self._trough.fill(np.nan)
        self._completed_at = completed_at
        o, high, low, close, volume, count = values.T
        valid = (np.isfinite(values).all(axis=1) & (o > 0) & (low > 0)
                 & (high >= np.maximum(o, close)) & (low <= np.minimum(o, close))
                 & (close > 0) & (volume > 0) & (count > 0)
                 & (count == np.floor(count)))
        self._latest_close = close.copy()
        self._closes[completed_at] = close.copy()
        self._close_times.append(completed_at)
        while self._close_times[0] < completed_at - DAY_SECONDS:
            del self._closes[self._close_times.popleft()]
        self._day_count += valid
        self._trough[~valid] = np.nan
        new_trough = valid & (np.isnan(self._trough) | (close < self._trough)
                             | (completed_at - self._trough_at
                                > self._config.maximum_recovery_days * DAY_SECONDS))
        self._trough[new_trough] = close[new_trough]
        self._trough_at[new_trough] = completed_at
        with np.errstate(over="ignore", invalid="ignore"):
            recovered = valid & ~new_trough & (
                close >= self._trough * self._config.gross_recovery_ratio)
        self._last_episode_at[recovered] = completed_at
        self._episode_count += recovered
        self._trough[recovered] = close[recovered]
        self._trough_at[recovered] = completed_at
        if completed_at % DAY_SECONDS == 0:
            self._finish_day(self._day)
            self._day += 1

    def _coverage(self, index: int) -> tuple[bool, list[dict]]:
        if self._completed_at is None:
            return False, []
        last_end = self._completed_at // DAY_SECONDS * DAY_SECONDS
        counts = {day: values for day, values in self._completed_days}
        required = []
        for day in range(last_end // DAY_SECONDS - FORECAST_DAYS, last_end // DAY_SECONDS):
            count = int(counts[day][index]) if day in counts else None
            required.append({"day_start": day * DAY_SECONDS,
                             "day_end": (day + 1) * DAY_SECONDS,
                             "valid_rows": count,
                             "sufficient": count is not None and count >= MINIMUM_VALID_ROWS_PER_DAY})
        return all(item["sufficient"] for item in required), required

    def daily_forecast(self, pair: str) -> tuple[Fraction | None, dict]:
        """Return exact seven-day signed mean, or None with an explicit denial.

        Cached values use only fully closed UTC days. Current-day observations
        can complete episodes but never enter this forecast before UTC midnight.
        Exact means are Fractions; JSON diagnostics contain display strings and
        an exact numerator/denominator receipt, not floating NaN or Infinity.
        """
        index = self._index(pair)
        if pair in self._forecast_cache:
            mean, diagnostic = self._forecast_cache[pair]
            return mean, deepcopy(diagnostic)
        covered, coverage = self._coverage(index)
        end = None if self._completed_at is None else self._completed_at // DAY_SECONDS * DAY_SECONDS
        diagnostic = {**_receipt(), "pair": pair, "forecast_cutoff": end,
                      "horizon_seconds": DAY_SECONDS, "required_days": FORECAST_DAYS,
                      "minimum_valid_rows_each_day": MINIMUM_VALID_ROWS_PER_DAY,
                      "coverage": coverage, "boundaries": [], "daily_gross_returns": [],
                      "mean_gross_return": None, "mean_exact": None,
                      "available": False, "reason": "insufficient_completed_days"}
        if not covered:
            diagnostic["reason"] = "insufficient_day_coverage" if coverage else "no_completed_observation"
            self._forecast_cache[pair] = (None, diagnostic)
            return None, deepcopy(diagnostic)
        boundaries = dict(self._boundaries)
        prices = []
        for timestamp in range(end - FORECAST_DAYS * DAY_SECONDS, end + 1, DAY_SECONDS):
            values = boundaries.get(timestamp)
            value = None if values is None else float(values[index])
            usable = value is not None and np.isfinite(value) and value > 0
            diagnostic["boundaries"].append({"completed_at": timestamp,
                                               "close": str(value) if usable else None,
                                               "available": bool(usable)})
            prices.append(_price_fraction(value) if usable else None)
        if any(value is None for value in prices):
            diagnostic["reason"] = "missing_exact_daily_boundary"
            self._forecast_cache[pair] = (None, diagnostic)
            return None, deepcopy(diagnostic)
        returns = [prices[i + 1] / prices[i] - 1 for i in range(FORECAST_DAYS)]
        mean = sum(returns, Fraction(0)) / FORECAST_DAYS
        diagnostic.update(daily_gross_returns=[_fraction_text(value) for value in returns],
                          mean_gross_return=_fraction_text(mean), mean_exact=_fraction_receipt(mean),
                          available=True, reason="available")
        self._forecast_cache[pair] = (mean, diagnostic)
        return mean, deepcopy(diagnostic)

    def stale_gate(self, pair: str, opened_at: int) -> tuple[bool, dict]:
        """Evaluate frozen age/observed-episode/coverage/signed-24h conditions.

        Native exit priority, armed profit-floor protection, parent identity,
        pending intent and executable whole-sale guards remain upstream.
        """
        index = self._index(pair)
        opened_at = _timestamp(opened_at, "opened_at")
        completed_at = self._completed_at
        covered, coverage = self._coverage(index)
        last_episode = int(self._last_episode_at[index])
        last_episode = last_episode if last_episode >= 0 else None
        diagnostic = {**_receipt(), "pair": pair, "decision_completed_at": completed_at,
                      "opened_at": opened_at, "age_seconds": None,
                      "minimum_age_seconds": STALE_AGE_SECONDS,
                      "episode_absent_after": None, "last_completed_episode_at": last_episode,
                      "observed_episode_count": int(self._episode_count[index]),
                      "fully_tracked_lookback": False, "coverage": coverage,
                      "anchor_completed_at_24h": None, "current_close": None,
                      "anchor_close_24h": None, "signed_24h_return": None,
                      "requires_native_priority_and_protection_checks": True,
                      "allowed": False, "reason": "no_completed_observation"}
        if completed_at is None:
            return False, diagnostic
        if opened_at > completed_at:
            raise ValueError("parent cannot open after the completed decision")
        cutoff = completed_at - EPISODE_LOOKBACK_SECONDS
        tracked = self._first_completed_at - BAR_SECONDS <= cutoff
        age = completed_at - opened_at
        diagnostic.update(age_seconds=age, episode_absent_after=cutoff,
                          fully_tracked_lookback=tracked,
                          anchor_completed_at_24h=completed_at - DAY_SECONDS)
        if age < STALE_AGE_SECONDS:
            diagnostic["reason"] = "parent_too_young"
        elif not tracked:
            diagnostic["reason"] = "untracked_seven_day_interval"
        elif not covered:
            diagnostic["reason"] = "insufficient_day_coverage"
        elif last_episode is not None and last_episode > cutoff:
            diagnostic["reason"] = "recent_completed_episode"
        else:
            earlier = self._closes.get(completed_at - DAY_SECONDS)
            current = float(self._latest_close[index])
            anchor = None if earlier is None else float(earlier[index])
            if (anchor is None or not np.isfinite(current) or not np.isfinite(anchor)
                    or current <= 0 or anchor <= 0):
                diagnostic["reason"] = "missing_exact_24h_anchor"
            else:
                signed = _price_fraction(current) / _price_fraction(anchor) - 1
                diagnostic.update(current_close=str(current), anchor_close_24h=str(anchor),
                                  signed_24h_return=_fraction_text(signed))
                diagnostic["allowed"] = current < anchor
                diagnostic["reason"] = "allowed" if current < anchor else "nonnegative_signed_24h"
        return diagnostic["allowed"], diagnostic


def fee_hurdle(remaining_quantity: Decimal, current_completed_close: Decimal,
               mean_old: Fraction | Decimal, mean_new: Fraction | Decimal) -> tuple[bool, dict]:
    """Evaluate the frozen strictly positive new gain AND incremental USD gain.

    Fixed B=10.25 is an unfunded forecast input. This gate has no old cost-basis
    argument: existing marked losses must not be deducted a second time. The
    adapter records the full actual signed disposal loss at confirmation.
    Ranking, fifty occupied slots, native eligibility, profit protection and
    actual post-sale cash authorization are independent upstream constraints.
    """
    quantity = _decimal_fraction(remaining_quantity, "remaining_quantity", positive=True)
    close = _decimal_fraction(current_completed_close, "current_completed_close", positive=True)
    old_mean = _mean_fraction(mean_old, "mean_old")
    new_mean = _mean_fraction(mean_new, "mean_new")
    budget = Fraction(REPLACEMENT_BUDGET)
    mark = quantity * close
    exit_net = mark * (1 - SLIPPAGE_RATE) * (1 - FEE_RATE)
    conversion = 1 / ((1 + SLIPPAGE_RATE) * (1 + FEE_RATE))
    new_mark = budget * conversion * (1 + new_mean)
    keep_mark = mark * (1 + old_mean)
    new_gain = new_mark - budget
    delta = exit_net - budget + new_mark - keep_mark
    allowed = new_gain > 0 and delta > 0
    diagnostic = {**_receipt(), "allowed": allowed,
                  "reason": "allowed" if allowed else "nonpositive_new_marked_gain"
                  if new_gain <= 0 else "nonpositive_incremental_dollar_gain",
                  "replacement_budget_all_in": str(REPLACEMENT_BUDGET),
                  "fee_bps": "40", "slippage_bps": "5", "horizon_seconds": DAY_SECONDS,
                  "remaining_quantity": str(remaining_quantity),
                  "current_completed_close": str(current_completed_close),
                  "mean_old": _fraction_text(old_mean), "mean_new": _fraction_text(new_mean),
                  "mean_old_exact": _fraction_receipt(old_mean),
                  "mean_new_exact": _fraction_receipt(new_mean),
                  "old_mark_M": _fraction_text(mark), "estimated_exit_net_E": _fraction_text(exit_net),
                  "buy_conversion_k": _fraction_text(conversion),
                  "estimated_old_exit_friction": _fraction_text(mark - exit_net),
                  "estimated_replacement_buy_friction": _fraction_text(budget - budget * conversion),
                  "estimated_keep_one_day_mark": _fraction_text(keep_mark),
                  "estimated_new_one_day_mark": _fraction_text(new_mark),
                  "new_gain_USD": _fraction_text(new_gain), "new_gain_exact": _fraction_receipt(new_gain),
                  "delta_USD": _fraction_text(delta), "delta_exact": _fraction_receipt(delta),
                  "existing_unrealized_loss_subtracted_again": False,
                  "actual_full_signed_disposal_loss_required": True,
                  "cash_authorized": False, "replacement_fill_established": False}
    return allowed, diagnostic
