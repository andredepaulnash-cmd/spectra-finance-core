"""Pure, causal daily BTC labels for the frozen paper short price-path proxy.

This module neither fetches prices nor submits orders. Observations and returned
statistics use Decimal, but ratios are deliberately evaluated as binary64, just
as in the frozen NumPy replay. Replacing that calculation with exact Decimal
arithmetic changes threshold-edge labels (for example, 90 / 100 - 1 > -0.10 in
binary64). This compatibility choice applies only to descriptive signals, never
to the paper book's monetary accounting.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from numbers import Integral
from typing import Iterable

BAR_SECONDS = 300
DAY_SECONDS = 86_400
FRESHNESS_SECONDS = 600


def _timestamp(value: int, *, name: str, interval: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer Unix timestamp")
    result = int(value)
    if result % interval:
        raise ValueError(f"{name} must be aligned to {interval} seconds")
    return result


def _observation_decimal(value: object) -> Decimal | None:
    """Normalize numeric observations; malformed/missing values stay unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Bar:
    """Original OHLCVT observation with a five-minute *opening* timestamp.

    Numeric strings/floats are normalized via Decimal(str(value)); no prices or
    missing candles are synthesized. An invalid full candle can still contain
    a usable observed open, which is a separate execution-time fact.
    """

    timestamp: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    trades: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp",
            _timestamp(self.timestamp, name="timestamp", interval=BAR_SECONDS),
        )
        for name in ("open", "high", "low", "close", "volume", "trades"):
            object.__setattr__(self, name, _observation_decimal(getattr(self, name)))

    @property
    def is_valid(self) -> bool:
        """The frozen finite-positive OHLCVT and integer-trade validator."""
        values = (self.open, self.high, self.low, self.close, self.volume, self.trades)
        if any(
            value is None or not value.is_finite() or value <= 0 for value in values
        ):
            return False
        assert self.open is not None and self.high is not None
        assert self.low is not None and self.close is not None
        assert self.trades is not None
        return (
            self.trades == self.trades.to_integral_value()
            and self.high >= max(self.open, self.close)
            and self.low <= min(self.open, self.close)
            and self.high >= self.low
        )

    @property
    def valid_open(self) -> Decimal | None:
        """Return the observed open without looking at future candle fields."""
        value = self.open
        return value if value is not None and value.is_finite() and value > 0 else None


@dataclass(frozen=True)
class DailySignal:
    decision_at: int
    label: str
    last_source_bar_open: int | None = None
    return_7d: Decimal | None = None
    return_1d: Decimal | None = None
    drawdown_7d: Decimal | None = None
    unknown_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-compatible evidence; Decimal values are lossless strings."""
        return {
            "decision_at": self.decision_at,
            "label": self.label,
            "last_source_bar_open": self.last_source_bar_open,
            "return_7d": None if self.return_7d is None else str(self.return_7d),
            "return_1d": None if self.return_1d is None else str(self.return_1d),
            "drawdown_7d": None if self.drawdown_7d is None else str(self.drawdown_7d),
            "unknown_reason": self.unknown_reason,
        }


def _same_observation(left: Bar, right: Bar) -> bool:
    # Decimal NaN is unequal to itself and sNaN raises on comparison. Repeated
    # identical malformed evidence must still be idempotent.
    for name in ("open", "high", "low", "close", "volume", "trades"):
        a, b = getattr(left, name), getattr(right, name)
        if a is None or b is None:
            if a is not b:
                return False
        elif a.is_finite() and b.is_finite():
            if a != b:
                return False
        elif str(a) != str(b):
            return False
    return True


def evaluate_btc_daily_signal(bars: Iterable[Bar], decision_at: int) -> DailySignal:
    """Evaluate one frozen daily label from original completed BTC candles.

    ``decision_at`` must be an integer midnight UTC. Bars opening at or after
    that decision are ignored, including contradictory future duplicates. All
    eligible bars are aligned, so strictly preceding bars are also completed.
    Repeated equal past observations are idempotent; contradictory past rows
    raise ValueError rather than silently selecting a revision.

    The latest valid close and both anchors may be at most ten minutes old.
    Anchors are the latest valid observations at or before latest-minus-seven-
    days/latest-minus-one-day, not decision-minus-lookback. The drawdown maximum
    includes only valid closes in [decision-minus-seven-days, decision).
    """
    decision_at = _timestamp(decision_at, name="decision_at", interval=DAY_SECONDS)
    observations: dict[int, Bar] = {}
    for bar in bars:
        if not isinstance(bar, Bar):
            raise TypeError("bars must contain Bar observations")
        if bar.timestamp >= decision_at:
            continue
        previous = observations.get(bar.timestamp)
        if previous is not None and not _same_observation(previous, bar):
            raise ValueError(f"Contradictory observations at timestamp {bar.timestamp}")
        observations[bar.timestamp] = bar

    history = sorted(
        (bar for bar in observations.values() if bar.is_valid),
        key=lambda bar: bar.timestamp,
    )
    if not history:
        return DailySignal(
            decision_at, "UNKNOWN", unknown_reason="NO_COMPLETED_VALID_BARS"
        )
    latest = history[-1]

    def unknown(reason: str) -> DailySignal:
        return DailySignal(
            decision_at, "UNKNOWN", latest.timestamp, unknown_reason=reason
        )

    if decision_at - latest.timestamp > FRESHNESS_SECONDS:
        return unknown("STALE_LATEST_BAR")
    timestamps = [bar.timestamp for bar in history]
    anchors: list[Bar] = []
    for days, name in ((7, "7D"), (1, "1D")):
        target = latest.timestamp - days * DAY_SECONDS
        index = bisect_right(timestamps, target) - 1
        if index < 0:
            return unknown(f"MISSING_{name}_ANCHOR")
        anchor = history[index]
        if target - anchor.timestamp > FRESHNESS_SECONDS:
            return unknown(f"STALE_{name}_ANCHOR")
        anchors.append(anchor)

    # The binary64 calculations and comparison order mirror the frozen replay.
    # Finite Decimal values outside binary64 range fail closed, rather than
    # producing a fabricated crash signal or raising on an underflowed divisor.
    peak = max(
        bar.close for bar in history if bar.timestamp >= decision_at - 7 * DAY_SECONDS
    )
    close, anchor_7d, anchor_1d, peak = (
        float(value)
        for value in (latest.close, anchors[0].close, anchors[1].close, peak)
    )
    if not all(
        isfinite(value) and value > 0 for value in (close, anchor_7d, anchor_1d, peak)
    ):
        return unknown("UNREPRESENTABLE_SIGNAL_INPUT")
    r7 = close / anchor_7d - 1.0
    r1 = close / anchor_1d - 1.0
    drawdown = close / peak - 1.0
    if not all(isfinite(value) for value in (r7, r1, drawdown)):
        return unknown("NON_FINITE_STATISTIC")
    label = (
        "CRASH"
        if drawdown <= -0.10 or r1 <= -0.05
        else "RISING" if r7 >= 0.05 else "FALLING" if r7 <= -0.05 else "SIDEWAYS"
    )
    return DailySignal(
        decision_at,
        label,
        latest.timestamp,
        Decimal(str(r7)),
        Decimal(str(r1)),
        Decimal(str(drawdown)),
    )
