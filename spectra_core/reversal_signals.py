"""Default-off completed-hour BBWP/RSI research signals; no order authority.

BBWP reproduces the recovered 20/252 population-width definition. Applying it
per asset, the bearish evidence rule, EMA8 and confirmation count are explicit
new implementation choices. Neither score is a promise of the next price move.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import json
import math

import numpy as np


@dataclass(frozen=True)
class HourBar:
    pair: str
    opened_at: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int
    constituents: int = 12

    def validate(self):
        if not self.pair or type(self.opened_at) is not int or self.opened_at < 0 or self.opened_at % 3600:
            raise ValueError("named asset and UTC hour required")
        if type(self.constituents) is not int or self.constituents != 12:
            raise ValueError("all twelve observed five-minute constituents required")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (self.open, self.high, self.low, self.close)):
            raise ValueError("finite positive prices required")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close) or self.high < self.low:
            raise ValueError("invalid OHLC geometry")
        if not math.isfinite(self.volume) or self.volume < 0 or type(self.trades) is not int or self.trades < 0:
            raise ValueError("observed nonnegative volume and integer trades required")


@dataclass(frozen=True)
class SignalPolicy:
    enabled: bool = False
    bbwp_minimum: float = 50.0
    bearish_evidence_minimum: int = 2
    bearish_confirmation_hours: int = 2
    rsi_soft_weight: float = 1.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("explicit enable flag required")
        if not math.isfinite(self.bbwp_minimum) or not 0 <= self.bbwp_minimum <= 100:
            raise ValueError("BBWP percentile must be in [0,100]")
        if type(self.bearish_evidence_minimum) is not int or not 1 <= self.bearish_evidence_minimum <= 3:
            raise ValueError("bearish evidence minimum must be 1..3")
        if type(self.bearish_confirmation_hours) is not int or self.bearish_confirmation_hours < 1:
            raise ValueError("positive confirmation hours required")
        if not math.isfinite(self.rsi_soft_weight) or not 0 <= self.rsi_soft_weight <= 1:
            raise ValueError("bounded soft ranking contribution required")


@dataclass(frozen=True)
class SignalObservation:
    pair: str
    completed_at: int
    earliest_fill_at: int
    expires_at: int
    consecutive_hours: int
    bbwp: float | None
    rsi: float | None
    atr14: float | None
    ema8: float
    return_1h: float | None
    return_4h: float | None
    bearish_evidence: tuple[str, ...]
    reversal_evidence: tuple[str, ...]
    close: float
    low: float

    def validate_at(self, now: int):
        if type(now) is not int or not self.completed_at <= now < self.expires_at:
            raise ValueError("future or expired completed-hour evidence")

    def reversal_families(self) -> dict:
        """Two correlated OHLC feature families, explicitly not independence.

        Structure is the conjunction of a higher low and an EMA reclaim. Two
        separate structure flags must never be counted using an accidental OR.
        The reducer additionally requires consecutive-hour confirmation.
        """
        flags = set(self.reversal_evidence)
        return {"momentum": "positive_completed_hour" in flags,
            "structure": {"higher_completed_low", "above_ema8"} <= flags}


class IndicatorHistory:
    """Streaming causal state, reset after any missing or incomplete hour."""
    def __init__(self, pair: str):
        if not isinstance(pair, str) or not pair.strip():
            raise ValueError("asset required")
        self.pair = pair
        self.reset()

    def reset(self):
        self.last_hour = None
        self.count = 0
        self.closes = deque(maxlen=20)
        self.widths = deque(maxlen=252)
        self.previous_low = None
        self.ema = None
        self.rsi_gain = self.rsi_loss = self.rsi_weight = 0.0
        self.rsi_count = 0
        self.tr_seed = []
        self.atr = None

    def push(self, bar: HourBar, *, observed_at: int) -> SignalObservation:
        bar.validate()
        if bar.pair != self.pair or type(observed_at) is not int or observed_at < bar.opened_at + 3600:
            raise ValueError("matching completed observed hour required")
        if self.last_hour is not None and bar.opened_at <= self.last_hour:
            raise ValueError("duplicate or reversed completed hour")
        if self.last_hour is not None and bar.opened_at != self.last_hour + 3600:
            self.reset()
        prior_close = self.closes[-1] if self.closes else None
        ret1 = bar.close / prior_close - 1 if prior_close else None
        ret4 = bar.close / self.closes[-4] - 1 if len(self.closes) >= 4 else None
        reversal = []
        if self.previous_low is not None and bar.low > self.previous_low:
            reversal.append("higher_completed_low")
        if ret1 is not None and ret1 > 0:
            reversal.append("positive_completed_hour")
        self.ema = bar.close if self.ema is None else (2 / 9) * bar.close + (7 / 9) * self.ema
        if bar.close > self.ema:
            reversal.append("above_ema8")
        if prior_close is not None:
            delta = bar.close - prior_close
            self.rsi_gain = (13 / 14) * self.rsi_gain + max(delta, 0)
            self.rsi_loss = (13 / 14) * self.rsi_loss + max(-delta, 0)
            self.rsi_weight = (13 / 14) * self.rsi_weight + 1
            self.rsi_count += 1
        tr = (bar.high - bar.low if prior_close is None else
            max(bar.high - bar.low, abs(bar.high - prior_close), abs(bar.low - prior_close)))
        if self.atr is None:
            self.tr_seed.append(tr)
            if len(self.tr_seed) == 14:
                self.atr = sum(self.tr_seed) / 14
        else:
            self.atr = (13 * self.atr + tr) / 14
        rsi = None
        if self.rsi_count >= 14:
            ratio = (self.rsi_gain / self.rsi_weight) / (self.rsi_loss / self.rsi_weight + 1e-10)
            rsi = 100 - 100 / (1 + ratio)
        self.closes.append(bar.close)
        width = None
        rank = None
        if len(self.closes) == 20:
            values = np.array(self.closes, dtype=float)
            width = float(4 * values.std(ddof=0) / values.mean())
            if len(self.widths) == 252:
                rank = 100.0 * sum(value < width for value in self.widths) / 252.0
            self.widths.append(width)
        bearish = []
        if bar.close < self.ema:
            bearish.append("below_ema8")
        if ret1 is not None and ret1 < 0:
            bearish.append("negative_completed_hour")
        if ret4 is not None and ret4 < 0:
            bearish.append("negative_completed_four_hours")
        self.count += 1
        self.last_hour = bar.opened_at
        self.previous_low = bar.low
        end = bar.opened_at + 3600
        return SignalObservation(self.pair, end, end + 300, end + 3900, self.count,
            rank, rsi, self.atr, self.ema, ret1, ret4, tuple(bearish), tuple(reversal), bar.close, bar.low)

    def dumps(self):
        return json.dumps({"schema": 1, "pair": self.pair, "state": {
            key: list(value) if isinstance(value, deque) else value
            for key, value in self.__dict__.items() if key != "pair"
        }}, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def loads(cls, payload):
        raw = json.loads(payload)
        if set(raw) != {"schema", "pair", "state"} or raw["schema"] != 1:
            raise ValueError("unknown indicator checkpoint")
        obj = cls(raw["pair"])
        if set(raw["state"]) != set(obj.__dict__) - {"pair"}:
            raise ValueError("incomplete indicator checkpoint")
        for key, value in raw["state"].items():
            setattr(obj, key, deque(value, maxlen=20 if key == "closes" else 252)
                if key in {"closes", "widths"} else value)
        if type(obj.count) is not int or obj.count < 0 or type(obj.rsi_count) is not int or obj.rsi_count != max(obj.count - 1, 0):
            raise ValueError("invalid checkpoint observation count")
        if len(obj.closes) != min(obj.count, 20) or len(obj.widths) != min(max(obj.count-19, 0), 252) or len(obj.tr_seed) != min(obj.count, 14):
            raise ValueError("checkpoint history length does not reconcile")
        finite_nonnegative = lambda value: type(value) in (int, float) and math.isfinite(value) and value >= 0
        if any(not finite_nonnegative(v) for v in [*obj.closes, *obj.widths, *obj.tr_seed, obj.rsi_gain, obj.rsi_loss, obj.rsi_weight]):
            raise ValueError("invalid numeric checkpoint history")
        if any(v <= 0 for v in obj.closes):
            raise ValueError("nonpositive close in checkpoint")
        if obj.count:
            if type(obj.last_hour) is not int or obj.last_hour < 0 or obj.last_hour % 3600 or not finite_nonnegative(obj.ema) or obj.ema <= 0 or not finite_nonnegative(obj.previous_low) or obj.previous_low <= 0:
                raise ValueError("invalid completed checkpoint boundary")
        elif any(v is not None for v in (obj.last_hour, obj.ema, obj.previous_low)):
            raise ValueError("empty history carries market state")
        if (obj.atr is None) != (obj.count < 14) or obj.atr is not None and not finite_nonnegative(obj.atr):
            raise ValueError("invalid ATR checkpoint state")
        if (obj.rsi_weight > 0) != (obj.rsi_count > 0):
            raise ValueError("invalid RSI checkpoint state")
        if obj.dumps() != payload:
            raise ValueError("noncanonical or invalid indicator checkpoint")
        return obj


class SignalGate:
    def __init__(self, policy: SignalPolicy):
        self.policy = policy
        self.last_completed = {}
        self.streaks = {}

    def dumps(self):
        return json.dumps({"schema": 1, "policy": asdict(self.policy), "last_completed": self.last_completed,
            "streaks": self.streaks}, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def loads(cls, payload):
        raw = json.loads(payload)
        if set(raw) != {"schema", "policy", "last_completed", "streaks"} or raw["schema"] != 1:
            raise ValueError("unknown signal checkpoint")
        result = cls(SignalPolicy(**raw["policy"]))
        if not isinstance(raw["last_completed"], dict) or not isinstance(raw["streaks"], dict) or set(raw["last_completed"]) != set(raw["streaks"]):
            raise ValueError("signal checkpoint asset mismatch")
        for key, end in raw["last_completed"].items():
            if not key or type(end) is not int or end < 0 or end % 3600 or type(raw["streaks"][key]) is not int or raw["streaks"][key] < 0:
                raise ValueError("invalid signal checkpoint state")
        result.last_completed, result.streaks = raw["last_completed"], raw["streaks"]
        if result.dumps() != payload:
            raise ValueError("noncanonical signal checkpoint")
        return result

    def evaluate(self, observation: SignalObservation, *, now: int) -> dict:
        observation.validate_at(now)
        pair, end = observation.pair, observation.completed_at
        if end <= self.last_completed.get(pair, -1):
            raise ValueError("one signal decision per completed hour required")
        consecutive = self.last_completed.get(pair) == end - 3600
        bearish = len(observation.bearish_evidence) >= self.policy.bearish_evidence_minimum
        self.streaks[pair] = ((self.streaks.get(pair, 0) if consecutive else 0) + 1) if bearish else 0
        self.last_completed[pair] = end
        ready = observation.bbwp is not None and observation.rsi is not None and observation.atr14 is not None
        percentile_pass = ready and observation.bbwp >= self.policy.bbwp_minimum
        confirmed = self.streaks[pair] >= self.policy.bearish_confirmation_hours
        reason = ("disabled" if not self.policy.enabled else "incomplete_indicator_history" if not ready
            else "bbwp_below_threshold" if not percentile_pass else "bearish_unconfirmed" if not confirmed else "signal_eligible")
        # RSI affects priority only. No RSI level can turn an eligible signal off.
        priority = self.policy.rsi_soft_weight * (100 - observation.rsi) / 100 if ready else None
        return {"pair": pair, "completed_at": end, "earliest_fill_at": observation.earliest_fill_at,
            "eligible": reason == "signal_eligible", "reason": reason, "rsi_priority": priority,
            "bearish_streak": self.streaks[pair], "bbwp": observation.bbwp,
            "rsi": observation.rsi, "implementation_choice": True}
