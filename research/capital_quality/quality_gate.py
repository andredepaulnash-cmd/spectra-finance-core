"""Exploratory parked-source admission using completed actual candles only.

No feature fitting, future labels, per-coin allowlist, forward filling or
execution-candle inspection. The caller supplies the native decision CLOSE
timestamp, including for a contingent replacement funded at the next open.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import numpy as np


class CausalGate:
    def __init__(self, prepared, policy=None):
        prepared = Path(prepared)
        self.metadata = json.loads((prepared / "metadata.json").read_text())
        self.tape = np.load(prepared / "ohlcv.npy", mmap_mode="r")
        self.start = int(self.metadata["start"])
        self.interval = int(self.metadata["interval_seconds"])
        if self.interval != 300:
            raise ValueError("Frozen gate requires authentic five-minute tape")
        self.pair_index = {pair: i for i, pair in enumerate(self.metadata["pairs"])}
        self.cache = {}
        self.records = []
        self.counts = Counter()
        self.policy = policy

    def eligible(self, pair, kind, decision_timestamp):
        return self.evaluate(pair, kind, decision_timestamp)["eligible"]

    def evaluate(self, pair, kind, decision_timestamp):
        key = (pair, kind, int(decision_timestamp))
        if key in self.cache:
            return self.cache[key]
        pair, kind, timestamp = key
        elapsed = timestamp - self.start
        index = elapsed // self.interval - 1
        result = dict(pair=pair, kind=kind, decision_timestamp=timestamp,
            decision_index=index, maximum_feature_index=None, eligible=False,
            reason="missing_history", anchor_indices=[], features={})
        if elapsed % self.interval or index < 0 or index >= self.tape.shape[0]:
            result["reason"] = "outside_exact_close_grid"
        elif pair not in self.pair_index:
            result["reason"] = "unknown_pair"
        elif kind not in {"ENTRY", "RECOVERY"}:
            result["reason"] = "unsupported_purpose"
        else:
            anchors = ([index, index - 12, index - 288] if kind == "ENTRY"
                       else list(range(index - 5, index + 1)))
            result["anchor_indices"] = anchors
            if min(anchors) >= 0:
                # All indices are constructed from this cutoff. There is no
                # whole-array reduction or later index in this feature path.
                rows = self.tape[anchors, self.pair_index[pair], :]
                result["maximum_feature_index"] = max(anchors)
                if np.isfinite(rows).all() and (rows[:, :4] > 0).all():
                    if kind == "ENTRY":
                        close, hour, day = map(float, rows[:, 3])
                        features = dict(close=close, close_1h=hour, close_24h=day,
                            return_1h=close/hour-1, return_24h=close/day-1)
                        good = close > hour and close > day
                        reason = "positive_1h_and_24h" if good else "nonpositive_entry_trend"
                    else:
                        close, prior = float(rows[-1, 3]), float(rows[-4, 3])
                        earlier_low = float(np.min(rows[:3, 2]))
                        recent_low = float(np.min(rows[3:, 2]))
                        features = dict(close=close, close_15m=prior,
                            return_15m=close/prior-1,
                            earlier_15m_low=earlier_low, recent_15m_low=recent_low)
                        good = close > prior and recent_low > earlier_low
                        reason = "rebound_and_higher_low" if good else "unconfirmed_recovery"
                    result.update(eligible=bool(good), reason=reason, features=features)
                else:
                    result["reason"] = "missing_actual_anchor_or_candle"
        self.cache[key] = result
        self.records.append(result)
        self.counts[(kind, result["reason"])] += 1
        return result

    def summary(self):
        return dict(research_only=True, fixed_rules=True, trained_model=False,
            fitted_parameters=False, future_labels_used=False,
            unique_evaluations=len(self.records),
            counts={f"{kind}:{reason}": count
                    for (kind, reason), count in sorted(self.counts.items())},
            maximum_feature_index_never_after_decision=all(
                row["maximum_feature_index"] is None or
                row["maximum_feature_index"] <= row["decision_index"]
                for row in self.records))
