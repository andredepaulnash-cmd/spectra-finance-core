import importlib.util
import json
import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from spectra_core.reversal_signals import HourBar, IndicatorHistory, SignalGate, SignalPolicy


def bars(n=600):
    for i in range(n):
        p = 100 + i * .01 + 5 * math.sin(i / 13) + .5 * math.cos(i / 3)
        yield HourBar("X-USD", 1700002800 + i * 3600, p, p + 1, p - 1, p, 1000., 20)


def observations(n=600):
    history = IndicatorHistory("X-USD")
    return [history.push(b, observed_at=b.opened_at + 3600) for b in bars(n)]


def test_authentic_population_bbwp_and_strict_prior_ties():
    rows = observations()
    c = np.array([b.close for b in bars()])
    widths = np.full(len(c), np.nan)
    for h in range(19, len(c)):
        w = c[h-19:h+1]
        widths[h] = 4 * w.std(ddof=0) / w.mean()
    assert all(r.bbwp is None for r in rows[:271])
    for h in range(271, len(c)):
        expected = 100.0 * np.count_nonzero(widths[h-252:h] < widths[h]) / 252.0
        assert rows[h].bbwp == expected
    h = IndicatorHistory("X-USD")
    for b in bars(300):
        r = h.push(replace(b, open=100., high=101., low=99., close=100.), observed_at=b.opened_at+3600)
    assert r.bbwp == 0


def test_rsi_matches_recovered_pandas_adjusted_ewm():
    rows = observations()
    c = pd.Series([b.close for b in bars()])
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
    expected = 100 - 100 / (1 + gain / (loss + 1e-10))
    for i, r in enumerate(rows):
        if i < 14:
            assert r.rsi is None
        else:
            assert r.rsi == pytest.approx(expected.iloc[i], abs=1e-12)


def test_missing_hour_resets_entire_readiness():
    history = IndicatorHistory("X-USD")
    tape = list(bars(550))
    for b in tape[:300]:
        result = history.push(b, observed_at=b.opened_at+3600)
    assert result.bbwp is not None
    result = history.push(tape[301], observed_at=tape[301].opened_at+3600)
    assert result.consecutive_hours == 1
    assert result.bbwp is None and result.atr14 is None and result.rsi is None


def test_incomplete_hour_is_rejected_without_mutation():
    h = IndicatorHistory("X-USD")
    b = next(bars())
    before = h.dumps()
    with pytest.raises(ValueError):
        h.push(replace(b, constituents=11), observed_at=b.opened_at+3600)
    assert h.dumps() == before


def test_future_duplicate_reversed_and_wrong_asset_rejected():
    h = IndicatorHistory("X-USD")
    b = next(bars())
    with pytest.raises(ValueError):
        h.push(b, observed_at=b.opened_at+3599)
    with pytest.raises(ValueError):
        h.push(replace(b, pair="OTHER"), observed_at=b.opened_at+3600)
    h.push(b, observed_at=b.opened_at+3600)
    before = h.dumps()
    with pytest.raises(ValueError):
        h.push(b, observed_at=b.opened_at+3600)
    assert h.dumps() == before


def test_restart_matches_every_future_record():
    a = IndicatorHistory("X-USD")
    tape = list(bars())
    for row in tape[:333]:
        a.push(row, observed_at=row.opened_at+3600)
    b = IndicatorHistory.loads(a.dumps())
    for row in tape[333:]:
        assert a.push(row, observed_at=row.opened_at+3600) == b.push(row, observed_at=row.opened_at+3600)
        assert a.dumps() == b.dumps()


def test_incomplete_checkpoint_rejected():
    h = IndicatorHistory("X-USD")
    raw = json.loads(h.dumps())
    del raw["state"]["count"]
    with pytest.raises(ValueError):
        IndicatorHistory.loads(json.dumps(raw))


def test_corrupt_numeric_checkpoint_rejected():
    h = IndicatorHistory("X-USD")
    for row in bars(300):
        h.push(row, observed_at=row.opened_at+3600)
    for field, value in [("count", -1), ("rsi_count", 10), ("widths", [-1.]*252), ("atr", None), ("last_hour", True)]:
        raw = json.loads(h.dumps())
        raw["state"][field] = value
        with pytest.raises(ValueError):
            IndicatorHistory.loads(json.dumps(raw, sort_keys=True, separators=(",", ":")))


def test_gate_restart_retains_confirmation_exactly():
    row = replace(observations()[-1], bbwp=90., bearish_evidence=("a", "b"))
    a = SignalGate(SignalPolicy(enabled=True))
    a.evaluate(row, now=row.completed_at)
    b = SignalGate.loads(a.dumps())
    next_row = replace(row, completed_at=row.completed_at+3600, earliest_fill_at=row.earliest_fill_at+3600, expires_at=row.expires_at+3600)
    assert a.evaluate(next_row, now=next_row.completed_at) == b.evaluate(next_row, now=next_row.completed_at)
    assert a.dumps() == b.dumps()


def test_signal_default_off_and_freshness():
    row = observations()[-1]
    gate = SignalGate(SignalPolicy())
    assert gate.evaluate(row, now=row.completed_at)["reason"] == "disabled"
    with pytest.raises(ValueError):
        SignalGate(SignalPolicy()).evaluate(row, now=row.completed_at-1)
    with pytest.raises(ValueError):
        SignalGate(SignalPolicy()).evaluate(row, now=row.expires_at)


def test_rsi_is_soft_only_even_at_extremes():
    row = observations()[-1]
    row = replace(row, bbwp=90., bearish_evidence=("a", "b", "c"))
    decisions = []
    for rsi in [0., 50., 100.]:
        gate = SignalGate(SignalPolicy(enabled=True, bearish_confirmation_hours=1))
        decisions.append(gate.evaluate(replace(row, rsi=rsi), now=row.completed_at))
    assert all(d["eligible"] for d in decisions)
    assert [d["rsi_priority"] for d in decisions] == [1., .5, 0.]


def test_loose_bbwp_is_only_threshold_difference():
    row = replace(observations()[-1], bbwp=25., bearish_evidence=("a", "b"))
    old = SignalGate(SignalPolicy(enabled=True, bearish_confirmation_hours=1))
    new = SignalGate(SignalPolicy(enabled=True, bbwp_minimum=20., bearish_confirmation_hours=1))
    assert old.evaluate(row, now=row.completed_at)["reason"] == "bbwp_below_threshold"
    assert new.evaluate(row, now=row.completed_at)["eligible"]


def test_confirmation_requires_consecutive_hours_and_no_duplicate():
    row = replace(observations()[-1], bbwp=90., bearish_evidence=("a", "b"))
    gate = SignalGate(SignalPolicy(enabled=True))
    assert not gate.evaluate(row, now=row.completed_at)["eligible"]
    with pytest.raises(ValueError):
        gate.evaluate(row, now=row.completed_at)
    gap = replace(row, completed_at=row.completed_at+7200, earliest_fill_at=row.earliest_fill_at+7200, expires_at=row.expires_at+7200)
    assert not gate.evaluate(gap, now=gap.completed_at)["eligible"]
    after = replace(gap, completed_at=gap.completed_at+3600, earliest_fill_at=gap.earliest_fill_at+3600, expires_at=gap.expires_at+3600)
    assert gate.evaluate(after, now=after.completed_at)["eligible"]


def test_exact_delayed_execution_boundary():
    row = observations()[-1]
    assert row.earliest_fill_at == row.completed_at + 300
    assert row.expires_at == row.completed_at + 3900


def test_reversal_structure_is_and_not_or():
    row = observations()[-1]
    for flags, expected in [
        ((), {"momentum":False,"structure":False}),
        (("higher_completed_low",), {"momentum":False,"structure":False}),
        (("above_ema8",), {"momentum":False,"structure":False}),
        (("higher_completed_low","above_ema8"), {"momentum":False,"structure":True}),
        (("positive_completed_hour",), {"momentum":True,"structure":False}),
        (("positive_completed_hour","higher_completed_low","above_ema8"), {"momentum":True,"structure":True}),
    ]:
        assert replace(row,reversal_evidence=flags).reversal_families() == expected
