"""Falsification tests for the frozen completed-close breadth gate."""

from pathlib import Path
import json
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.danger_room.market_regime import (
    BARS_PER_DAY, FIXED_ASSET_COUNT, causal_market_regime,
)


def tape(valid=50, positive=25, frames=290, index=288):
    rows = np.full((frames, FIXED_ASSET_COUNT, 6), np.nan)
    rows[index - BARS_PER_DAY, :valid, 3] = 100.0
    rows[index, :valid, 3] = 100.0
    rows[index, :positive, 3] = 101.0
    return rows


class AnchorOnlyArray:
    """Raise if the implementation tries to inspect an unneeded observation."""

    def __init__(self, rows, allowed_indices):
        self.rows = rows
        self.shape = rows.shape
        self.allowed_indices = set(allowed_indices)
        self.accesses = []

    def __array__(self, *args, **kwargs):
        raise AssertionError("the whole tape must never be converted")

    def __getitem__(self, key):
        index, assets, field = key
        if index not in self.allowed_indices or assets != slice(None) or field != 3:
            raise AssertionError(f"unnecessary or future tape access: {key}")
        self.accesses.append(index)
        return self.rows[key]


class MarketRegimeTests(unittest.TestCase):
    def test_exact_half_of_fifty_admits(self):
        allowed, diagnostic = causal_market_regime(tape(), 288)
        self.assertTrue(allowed)
        self.assertEqual(diagnostic["valid_assets"], 50)
        self.assertEqual(diagnostic["positive_assets"], 25)
        self.assertEqual(diagnostic["positive_fraction"], 0.5)
        self.assertEqual(diagnostic["anchor_index_24h"], 0)
        self.assertEqual(diagnostic["reason"], "allowed")

    def test_below_half_denies_even_with_fifty_valid(self):
        allowed, diagnostic = causal_market_regime(tape(positive=24), 288)
        self.assertFalse(allowed)
        self.assertEqual(diagnostic["reason"], "weak_market_breadth")

    def test_odd_denominator_requires_a_majority(self):
        for positive, expected in [(25, False), (26, True)]:
            with self.subTest(positive=positive):
                self.assertEqual(causal_market_regime(tape(valid=51, positive=positive), 288)[0], expected)

    def test_fewer_than_fifty_denies_even_if_all_rise(self):
        allowed, diagnostic = causal_market_regime(tape(valid=49, positive=49), 288)
        self.assertFalse(allowed)
        self.assertEqual(diagnostic["positive_fraction"], 1.0)
        self.assertEqual(diagnostic["reason"], "insufficient_valid_assets")

    def test_flat_assets_remain_in_denominator(self):
        allowed, diagnostic = causal_market_regime(tape(valid=100, positive=25), 288)
        self.assertFalse(allowed)
        self.assertEqual(diagnostic["valid_assets"], 100)
        self.assertEqual(diagnostic["positive_fraction"], 0.25)

    def test_invalid_price_at_either_exact_anchor_is_excluded(self):
        for anchor in [0, 288]:
            for invalid in [np.nan, np.inf, -np.inf, 0.0, -1.0]:
                with self.subTest(anchor=anchor, invalid=invalid):
                    rows = tape(valid=50, positive=50)
                    rows[anchor, 0, 3] = invalid
                    allowed, diagnostic = causal_market_regime(rows, 288)
                    self.assertFalse(allowed)
                    self.assertEqual(diagnostic["valid_assets"], 49)

    def test_missing_anchor_does_not_use_a_neighbor_or_forward_fill(self):
        rows = tape(valid=50, positive=50)
        rows[1, :50, 3] = 100.0
        rows[0, :50, 3] = np.nan
        allowed, diagnostic = causal_market_regime(rows, 288)
        self.assertFalse(allowed)
        self.assertEqual(diagnostic["valid_assets"], 0)
        self.assertIsNone(diagnostic["positive_fraction"])

    def test_only_two_exact_close_vectors_are_read(self):
        guarded = AnchorOnlyArray(tape(frames=320, index=300), {12, 300})
        self.assertTrue(causal_market_regime(guarded, 300)[0])
        self.assertEqual(guarded.accesses, [300, 12])

    def test_future_mutation_and_truncation_leave_result_identical(self):
        rows = tape(frames=310)
        original = causal_market_regime(rows, 288)
        rows[289:] = np.random.default_rng(17).normal(size=rows[289:].shape)
        rows[290:, :300, 3] = np.inf
        self.assertEqual(causal_market_regime(rows, 288), original)
        self.assertEqual(causal_market_regime(rows[:289], 288), original)

    def test_no_magnitude_division_can_overflow(self):
        rows = tape(valid=50, positive=0)
        rows[0, :50, 3] = np.nextafter(0.0, 1.0)
        rows[288, :50, 3] = np.finfo(float).max
        with np.errstate(all="raise"):
            allowed, diagnostic = causal_market_regime(rows, 288)
        self.assertTrue(allowed)
        self.assertEqual(diagnostic["positive_fraction"], 1.0)

    def test_unused_ohlc_and_volume_fields_do_not_change_close_only_gate(self):
        rows = tape()
        baseline = causal_market_regime(rows, 288)
        rows[:, :, [0, 1, 2, 4, 5]] = -np.inf
        self.assertEqual(causal_market_regime(rows, 288), baseline)

    def test_incomplete_day_denies_without_reading_any_price(self):
        guarded = AnchorOnlyArray(np.empty((288, FIXED_ASSET_COUNT, 6)), set())
        allowed, diagnostic = causal_market_regime(guarded, 287)
        self.assertFalse(allowed)
        self.assertEqual(diagnostic["reason"], "insufficient_24h_history")
        self.assertEqual(guarded.accesses, [])

    def test_invalid_indices_are_rejected(self):
        rows = tape()
        for index in [-1, len(rows), True, np.bool_(False), 288.0, "288"]:
            with self.subTest(index=index), self.assertRaises(ValueError):
                causal_market_regime(rows, index)
        self.assertTrue(causal_market_regime(rows, np.int64(288))[0])

    def test_declared_universe_and_field_shape_are_required(self):
        for rows in [np.empty((289, 618, 6)), np.empty((289, 620, 6)),
                     np.empty((289, 619, 5)), np.empty((289, 619)), []]:
            with self.subTest(shape=getattr(rows, "shape", None)), self.assertRaises(ValueError):
                causal_market_regime(rows, 288)

    def test_non_real_numeric_anchor_values_are_rejected(self):
        for dtype in [complex, object, bool, "U4"]:
            with self.subTest(dtype=dtype), self.assertRaises(ValueError):
                causal_market_regime(tape().astype(dtype), 288)

    def test_diagnostic_is_json_serializable(self):
        for rows in [tape(), tape(valid=0, positive=0), tape(valid=49, positive=20)]:
            allowed, diagnostic = causal_market_regime(rows, 288)
            self.assertEqual(diagnostic["allowed"], allowed)
            self.assertEqual(json.loads(json.dumps(diagnostic, allow_nan=False)), diagnostic)


if __name__ == "__main__":
    unittest.main()
