"""Bounded completed-bar feed. It never evaluates a trading strategy.

The close/high/low/volume at boundary T belong only to the bar ending T.
The execution open at T is exposed separately; its future range/volume is not.
Missing observed candles stay missing. All simulations need separate admission.
"""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path

import numpy as np

from spectra_core.reversal_signals import HourBar

D = Decimal
STEP = 300
ROSTER = ("BTC", "ETH", "SOL", "BCH", "LTC", "DOGE", "AVAX", "LINK",
          "DOT", "SHIB", "XLM", "HBAR", "ADA", "AAVE", "XTZ")


@dataclass(frozen=True)
class ObservedBar:
    pair: str
    opened_at: int
    completed_at: int
    open: D
    high: D
    low: D
    close: D
    volume: D
    trades: int

    @property
    def quote_volume_proxy(self):
        # No dated book/depth data: disclose this participation approximation.
        return self.volume * self.close


@dataclass(frozen=True)
class Boundary:
    at: int
    pairs: tuple[str, ...]
    completed: tuple[ObservedBar | None, ...]
    observed_opens: tuple[D | None, ...]
    completed_hours: tuple[HourBar | None, ...] | None
    evaluation_end: bool


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be nonnegative integer UTC seconds")


def _valid(row):
    if row.shape != (6,) or not np.isfinite(row).all():
        return False
    o, h, low, c, volume, trades = row
    return (min(o, h, low, c) > 0 and h >= max(o, c) and low <= min(o, c)
            and h >= low and volume >= 0 and trades >= 0 and trades == int(trades))


class CausalTape:
    def __init__(self, array, *, data_start: int, warmup_start: int, start: int,
                 end: int, roster=ROSTER, synthetic: bool = False):
        for name, value in (("data_start", data_start), ("warmup_start", warmup_start),
                            ("start", start), ("end", end)):
            _integer(value, name)
            if value % STEP:
                raise ValueError("five-minute UTC boundaries required")
        if not data_start <= warmup_start < start < end <= data_start + len(array) * STEP:
            raise ValueError("invalid bounded tape interval")
        if any(value % 3600 for value in (warmup_start, start, end)):
            raise ValueError("whole-hour research bounds required")
        if not isinstance(roster, tuple) or not roster or len(set(roster)) != len(roster):
            raise ValueError("immutable unique roster required")
        if array.ndim != 3 or array.shape[1:] != (len(roster), 6) or array.dtype != np.float64:
            raise ValueError("unexpected OHLCVT shape or type")
        self.array = array
        self.data_start, self.warmup_start, self.start, self.end = data_start, warmup_start, start, end
        self.pairs = tuple(asset + "-USD" for asset in roster)
        self.synthetic = synthetic
        self.input_identity = None
        self.source_file_sha256 = None

    def seal(self):
        """Freeze exact in-scope bytes before any strategy or resumed state opens.

        Caller-owned arrays may be edited while preparing a synthetic fixture.
        Once sealed, the strategy reads an immutable bytes-backed bounded copy,
        never that mutable source. No prices outside warmup/evaluation are copied.
        """
        if self.input_identity is not None:
            return dict(self.input_identity)
        original_start = self.data_start
        original_shape = list(self.array.shape)
        first = (self.warmup_start - original_start) // STEP
        last = (self.end - original_start) // STEP
        shape = (last - first, len(self.pairs), 6)
        raw = np.ascontiguousarray(self.array[first:last]).tobytes(order='C')
        self.input_identity = {
            'schema': 1, 'source_file_sha256': self.source_file_sha256,
            'source_data_start': original_start, 'source_array_shape': original_shape,
            'bounds': [self.warmup_start, self.start, self.end],
            'pairs': list(self.pairs), 'synthetic': self.synthetic,
            'dtype': self.array.dtype.str, 'bounded_shape': list(shape),
            'bounded_bytes': len(raw), 'bounded_sha256': hashlib.sha256(raw).hexdigest(),
        }
        self.array = np.frombuffer(raw, dtype=self.array.dtype).reshape(shape)
        self.data_start = self.warmup_start
        return dict(self.input_identity)

    def _index(self, opened_at):
        if not self.warmup_start <= opened_at < self.end:
            raise ValueError("read outside frozen warmup/evaluation scope")
        return (opened_at - self.data_start) // STEP

    def _bar(self, opened_at, j):
        row = self.array[self._index(opened_at), j]
        if not _valid(row):
            return None
        o, h, low, c, volume = (D(str(float(x))) for x in row[:5])
        return ObservedBar(self.pairs[j], opened_at, opened_at + STEP,
                           o, h, low, c, volume, int(row[5]))

    def hours_at(self, at):
        if at % 3600 or not self.warmup_start + 3600 <= at <= self.end:
            raise ValueError("complete in-scope hour required")
        first = self._index(at - 3600)
        result = []
        for j, pair in enumerate(self.pairs):
            rows = self.array[first:first + 12, j]
            if len(rows) != 12 or not all(_valid(row) for row in rows):
                result.append(None)
                continue
            result.append(HourBar(pair, at - 3600, float(rows[0, 0]),
                float(rows[:, 1].max()), float(rows[:, 2].min()), float(rows[-1, 3]),
                float(rows[:, 4].sum()), int(rows[:, 5].sum())))
        return tuple(result)

    def warmup_bars(self):
        for opened in range(self.warmup_start, self.start, STEP):
            yield opened + STEP, tuple(self._bar(opened, j) for j in range(len(self.pairs)))

    def warmup_hours(self):
        for at in range(self.warmup_start + 3600, self.start + 1, 3600):
            yield at, self.hours_at(at)

    def boundary(self, at):
        _integer(at, "at")
        if at % STEP or not self.start <= at <= self.end:
            raise ValueError("boundary outside evaluation window")
        completed = tuple(self._bar(at - STEP, j) for j in range(len(self.pairs)))
        # At end only the final completed mark exists in this experiment. No
        # following price is read and no pending order can receive an end fill.
        opens = tuple(None for _ in self.pairs)
        if at < self.end:
            idx = self._index(at)
            # An execution open must not depend on the future high/low/volume.
            opens = tuple(D(str(float(x))) if np.isfinite(x) and x > 0 else None
                          for x in self.array[idx, :, 0])
        hours = self.hours_at(at) if at % 3600 == 0 else None
        return Boundary(at, self.pairs, completed, opens, hours, at == self.end)

    def boundaries(self):
        for at in range(self.start, self.end + 1, STEP):
            yield self.boundary(at)


def load_admitted_tape(path, *, expected_sha256, admission, **kwargs):
    """No historical loading through the caller without an exact launch receipt.

    This verifies the immutable receipt fields, not external permission by itself.
    The campaign coordinator must save/confirm the registry fence first.
    """
    if (not isinstance(admission, dict) or admission.get("historical_execution_admitted") is not True
            or admission.get("confirmed_launch_fence") is not True
            or admission.get("input_sha256") != expected_sha256
            or not admission.get("protocol_sha256") or not admission.get("run_id")):
        raise ValueError("confirmed historical admission/fence required")
    # Read once into an immutable snapshot. Hashing one file generation and
    # later mapping the path would allow the admitted bytes to change between
    # verification and seal. Parsing and bounded sealing use this exact object.
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("historical input identity mismatch")
    tape = CausalTape(np.load(io.BytesIO(raw), allow_pickle=False), synthetic=False, **kwargs)
    tape.source_file_sha256 = expected_sha256
    tape.seal()
    return tape
