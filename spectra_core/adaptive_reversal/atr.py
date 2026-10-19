"""Provisional ATR14 Wilder over completed, contiguous UTC hourly bars only."""
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
try:
    from .policy import CompletedBar, MATH_CONTEXT
except ImportError:
    from policy import CompletedBar, MATH_CONTEXT


@dataclass(frozen=True)
class WilderATR:
    period: int = 14
    bar_seconds: int = 3600
    count: int = 0
    seed_sum: Decimal = Decimal("0")
    value: Decimal | None = None
    previous_close: Decimal | None = None
    last_bar: CompletedBar | None = None

    def __post_init__(self):
        if type(self.period) is not int or self.period <= 0 or type(self.bar_seconds) is not int or self.bar_seconds <= 0:
            raise ValueError("positive integer period and interval required")
        if type(self.count) is not int or self.count < 0:
            raise ValueError("invalid ATR count")
        for value in (self.seed_sum, self.value, self.previous_close):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError("invalid ATR decimal")
        if ((self.count == 0) != (self.last_bar is None) or
                (self.count == 0) != (self.previous_close is None) or
                (self.count >= self.period) != (self.value is not None)):
            raise ValueError("inconsistent ATR warmup state")
        if self.last_bar is not None and self.previous_close != self.last_bar.close:
            raise ValueError("ATR previous close differs from last bar")
        if self.count == 0 and self.seed_sum != 0:
            raise ValueError("nonempty seed in empty ATR")


def update_atr(state: WilderATR, bar: CompletedBar, observed_at: int) -> WilderATR:
    if type(observed_at) is not int or observed_at < 0:
        raise ValueError("invalid ATR observation time")
    if bar.close_at > observed_at:
        raise ValueError("unfinished/future bar")
    if bar.close_at - bar.open_at != state.bar_seconds or bar.close_at % state.bar_seconds:
        raise ValueError("wrong ATR interval")
    if state.last_bar == bar:
        return state
    if state.last_bar is not None and bar.open_at != state.last_bar.close_at:
        raise ValueError("ATR bars must be contiguous and unique")
    with localcontext(MATH_CONTEXT):
        tr = bar.high - bar.low
        if state.previous_close is not None:
            tr = max(tr, abs(bar.high - state.previous_close), abs(bar.low - state.previous_close))
        count = state.count + 1
        seed_sum = state.seed_sum + tr if state.value is None else state.seed_sum
        value = state.value
        if count == state.period:
            value = seed_sum / state.period
        elif count > state.period:
            value = (state.value * (state.period - 1) + tr) / state.period
        return replace(state, count=count, seed_sum=seed_sum, value=value,
                       previous_close=bar.close, last_bar=bar)


def snapshot_atr(state: WilderATR) -> str:
    """Exact companion snapshot; persist atomically with the policy snapshot."""
    import json
    bar = state.last_bar
    return json.dumps({"schema_version": 1, "period": state.period, "bar_seconds": state.bar_seconds,
                       "count": state.count, "seed_sum": str(state.seed_sum),
                       "value": None if state.value is None else str(state.value),
                       "previous_close": None if state.previous_close is None else str(state.previous_close),
                       "last_bar": None if bar is None else {"open_at": bar.open_at, "close_at": bar.close_at,
                                    "high": str(bar.high), "low": str(bar.low), "close": str(bar.close)}},
                      sort_keys=True, separators=(",", ":"))


def restore_atr(raw: str) -> WilderATR:
    import json
    data = json.loads(raw)
    if set(data) != {"schema_version", "period", "bar_seconds", "count", "seed_sum", "value",
                     "previous_close", "last_bar"} or data.pop("schema_version") != 1:
        raise ValueError("unsupported ATR snapshot schema")
    for key in ("seed_sum", "value", "previous_close"):
        if data[key] is not None:
            data[key] = Decimal(data[key])
            if not data[key].is_finite():
                raise ValueError("invalid ATR snapshot decimal")
    if data["last_bar"] is not None:
        bar = data["last_bar"]
        data["last_bar"] = CompletedBar(**{key: Decimal(value) if key in ("high", "low", "close") else value
                                          for key, value in bar.items()})
    return WilderATR(**data)
