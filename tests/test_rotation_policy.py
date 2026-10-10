"""Synthetic causal evidence and exact-friction falsification; no outcome tape."""

from decimal import Decimal, Inexact, ROUND_UP, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.danger_room.opportunity_selector import OpportunitySelector, SelectorConfig
from research.danger_room.rotation_policy import (
    BAR_SECONDS, DAY_SECONDS, FEE_RATE, FROZEN_ROTATION_PROTOCOL_SHA256,
    FROZEN_SELECTOR_SOURCE_SHA256, REPLACEMENT_BUDGET, SLIPPAGE_RATE,
    RotationTracker, fee_hurdle,
)


D = Decimal
DAY = DAY_SECONDS
BAR = BAR_SECONDS


def bar(price=100.0):
    return np.array([[price, price, price, price, 100.0, 5.0]], dtype=float)


def consume(tracker, end=7 * DAY, *, start=0, prices=None, invalid=None, skipped=()):
    prices = prices or (lambda timestamp: 100.0)
    skipped = set(skipped)
    for timestamp in range(start, end + BAR, BAR):
        if timestamp in skipped:
            continue
        values = bar(prices(timestamp))
        if invalid is not None:
            invalid(timestamp, values)
        tracker.update(timestamp, values)
    return tracker


def exact(diagnostic, key):
    receipt = diagnostic[key]
    return Fraction(int(receipt["numerator"]), int(receipt["denominator"]))


class RotationForecastTests(unittest.TestCase):
    def test_seven_signed_returns_include_losing_and_flat_days(self):
        boundaries = [100, 110, 110, 99, 108.9, 108.9, 98.01, 98.01]
        tracker = consume(RotationTracker(["AUSD"]), prices=lambda t: boundaries[t // DAY])
        mean, diagnostic = tracker.daily_forecast("AUSD")
        self.assertEqual(mean, Fraction(0))
        self.assertEqual(diagnostic["daily_gross_returns"], ["0.1", "0", "-0.1", "0.1", "0", "-0.1", "0"])
        self.assertEqual(exact(diagnostic, "mean_exact"), mean)
        self.assertTrue(diagnostic["available"])

    def test_full_seven_days_still_require_the_eighth_exact_boundary(self):
        tracker = consume(RotationTracker(["AUSD"]), start=BAR)
        mean, diagnostic = tracker.daily_forecast("AUSD")
        self.assertIsNone(mean)
        self.assertEqual(diagnostic["reason"], "missing_exact_daily_boundary")
        self.assertFalse(diagnostic["boundaries"][0]["available"])
        self.assertTrue(all(item["valid_rows"] == 288 for item in diagnostic["coverage"]))

    def test_minimum_daily_coverage_is_exactly_274_not_273(self):
        for invalid_count, expected in [(14, True), (15, False)]:
            def missing(timestamp, values):
                offset = timestamp % DAY
                if 0 < offset <= invalid_count * BAR:
                    values[:] = np.nan
            tracker = consume(RotationTracker(["AUSD"]), invalid=missing)
            mean, diagnostic = tracker.daily_forecast("AUSD")
            with self.subTest(invalid_count=invalid_count):
                self.assertEqual(mean is not None, expected)
                self.assertTrue(all(item["valid_rows"] == 288 - invalid_count for item in diagnostic["coverage"]))

    def test_missing_midnight_is_not_replaced_by_first_or_last_observed_close(self):
        tracker = consume(RotationTracker(["AUSD"]), skipped=[3 * DAY])
        mean, diagnostic = tracker.daily_forecast("AUSD")
        self.assertIsNone(mean)
        self.assertEqual(diagnostic["reason"], "missing_exact_daily_boundary")
        self.assertFalse(diagnostic["boundaries"][3]["available"])
        self.assertEqual(diagnostic["coverage"][2]["valid_rows"], 287)

    def test_zero_and_nonfinite_daily_boundary_prices_deny(self):
        for bad in [0.0, -1.0, float("nan"), float("inf")]:
            tracker = consume(RotationTracker(["AUSD"]), prices=lambda t: bad if t == 4 * DAY else 100)
            with self.subTest(price=bad):
                self.assertIsNone(tracker.daily_forecast("AUSD")[0])

    def test_boundary_price_is_distinct_from_whole_row_coverage_validity(self):
        def missing_volume(timestamp, values):
            if timestamp == 3 * DAY:
                values[0, 4] = 0
        tracker = consume(RotationTracker(["AUSD"]), invalid=missing_volume)
        mean, diagnostic = tracker.daily_forecast("AUSD")
        self.assertEqual(mean, Fraction(0))
        self.assertTrue(diagnostic["boundaries"][3]["available"])
        self.assertEqual(diagnostic["coverage"][2]["valid_rows"], 287)

    def test_current_partial_day_never_enters_closed_day_forecast(self):
        tracker = consume(RotationTracker(["AUSD"]))
        before = tracker.daily_forecast("AUSD")
        tracker.update(7 * DAY + BAR, bar(1000000))
        self.assertEqual(tracker.daily_forecast("AUSD"), before)

    def test_forecast_cache_advances_exactly_at_midnight(self):
        tracker = consume(RotationTracker(["AUSD"]))
        before, _ = tracker.daily_forecast("AUSD")
        consume(tracker, end=8 * DAY, start=7 * DAY + BAR, prices=lambda t: 200)
        after, diagnostic = tracker.daily_forecast("AUSD")
        self.assertEqual(before, Fraction(0))
        self.assertEqual(after, Fraction(1, 7))
        self.assertEqual(diagnostic["forecast_cutoff"], 8 * DAY)

    def test_whole_missing_day_remains_undercovered(self):
        tracker = consume(RotationTracker(["AUSD"]), skipped=range(2 * DAY + BAR, 3 * DAY + BAR, BAR))
        mean, diagnostic = tracker.daily_forecast("AUSD")
        self.assertIsNone(mean)
        self.assertEqual(diagnostic["coverage"][2]["valid_rows"], 0)

    def test_future_suffix_mutation_and_truncation_do_not_change_completed_evidence(self):
        tape = np.repeat(bar()[None, :, :], 8 * 288 + 1, axis=0)
        prefix_end = 7 * 288
        def replay(source):
            tracker = RotationTracker(["AUSD"])
            for i in range(prefix_end + 1):
                tracker.update(i * BAR, source[i])
            return tracker.daily_forecast("AUSD")
        before = replay(tape)
        tape[prefix_end + 1:] = np.nan
        self.assertEqual(replay(tape), before)
        self.assertEqual(replay(tape[:prefix_end + 1]), before)

    def test_copied_input_and_diagnostics_cannot_mutate_tracker_evidence(self):
        tracker = consume(RotationTracker(["AUSD"]))
        mean, diagnostic = tracker.daily_forecast("AUSD")
        diagnostic["coverage"][0]["valid_rows"] = 0
        diagnostic["boundaries"][0]["close"] = "999999"
        self.assertEqual(tracker.daily_forecast("AUSD")[0], mean)
        self.assertEqual(tracker.daily_forecast("AUSD")[1]["coverage"][0]["valid_rows"], 288)
        values = bar(101)
        tracker.update(7 * DAY + BAR, values)
        values[:] = 0
        self.assertEqual(tracker.daily_forecast("AUSD")[0], mean)

    def test_no_observation_cache_does_not_survive_first_update(self):
        tracker = RotationTracker(["AUSD"])
        self.assertEqual(tracker.daily_forecast("AUSD")[1]["reason"], "no_completed_observation")
        tracker.update(BAR, bar())
        self.assertEqual(tracker.daily_forecast("AUSD")[1]["forecast_cutoff"], 0)


class RotationEpisodeAndStaleTests(unittest.TestCase):
    def test_observed_episode_counts_and_timestamps_match_frozen_selector(self):
        pairs = ["AUSD", "BUSD", "CUSD"]
        tracker = RotationTracker(pairs)
        selector = OpportunitySelector(pairs)
        counts = np.zeros(3, dtype=np.int64)
        last = [None] * 3
        for step in range(4 * 288 + 1):
            timestamp = step * BAR
            if step == 73:
                continue  # A missing timestamp resets both trackers.
            prices = np.array([50 + step % 9, 100 + step % 11, 200 - step % 7])
            values = np.column_stack([prices, prices, prices, prices, np.full(3, 100), np.full(3, 5)]).astype(float)
            if step % 19 == 0:
                values[1] = np.nan
            tracker.update(timestamp, values)
            selector.update(timestamp, values)
            current = selector._cycles + sum((day["cycles"] for day in selector._history), np.zeros(3, dtype=np.int64))
            for i, pair in enumerate(pairs):
                if current[i] > counts[i]:
                    last[i] = timestamp
                diagnostic = tracker.stale_gate(pair, 0)[1]
                self.assertEqual(diagnostic["observed_episode_count"], int(current[i]))
                self.assertEqual(diagnostic["last_completed_episode_at"], last[i])
            counts = current.copy()

    def test_just_completed_episode_vetoes_in_current_partial_day_and_at_midnight(self):
        for decision in [14 * DAY, 14 * DAY + BAR]:
            prices = lambda t: 50 if t == decision - BAR else 52 if t == decision else 100
            tracker = consume(RotationTracker(["AUSD"]), end=decision, prices=prices)
            allowed, diagnostic = tracker.stale_gate("AUSD", 0)
            with self.subTest(decision=decision):
                self.assertFalse(allowed)
                self.assertEqual(diagnostic["reason"], "recent_completed_episode")
                self.assertEqual(diagnostic["last_completed_episode_at"], decision)

    def test_lower_seven_day_completion_boundary_is_strictly_excluded(self):
        for event_time, expected in [(7 * DAY, True), (7 * DAY + BAR, False)]:
            def prices(timestamp):
                if timestamp == event_time - BAR:
                    return 50
                if timestamp >= event_time:
                    return 52 - (timestamp - event_time) / DAY * 0.1
                return 100
            tracker = consume(RotationTracker(["AUSD"]), end=14 * DAY, prices=prices)
            allowed, diagnostic = tracker.stale_gate("AUSD", 0)
            with self.subTest(event_time=event_time):
                self.assertEqual(diagnostic["last_completed_episode_at"], event_time)
                self.assertEqual(allowed, expected)

    def test_negative_return_and_exact_fourteen_day_age_are_both_required(self):
        tracker = consume(RotationTracker(["AUSD"]), end=14 * DAY, prices=lambda t: 100 - t / DAY)
        self.assertTrue(tracker.stale_gate("AUSD", 0)[0])
        self.assertEqual(tracker.stale_gate("AUSD", BAR)[1]["reason"], "parent_too_young")
        flat = consume(RotationTracker(["AUSD"]), end=14 * DAY)
        self.assertEqual(flat.stale_gate("AUSD", 0)[1]["reason"], "nonnegative_signed_24h")

    def test_stale_requires_full_tracked_interval_and_seven_covered_closed_days(self):
        tracker = consume(RotationTracker(["AUSD"]), start=7 * DAY + 2 * BAR, end=14 * DAY,
                          prices=lambda t: 100 - t / DAY)
        allowed, diagnostic = tracker.stale_gate("AUSD", 0)
        self.assertFalse(allowed)
        self.assertEqual(diagnostic["reason"], "untracked_seven_day_interval")
        def sparse(timestamp, values):
            if timestamp % DAY in range(BAR, 16 * BAR, BAR):
                values[:] = np.nan
        sparse_tracker = consume(RotationTracker(["AUSD"]), end=14 * DAY,
                                 prices=lambda t: 100 - t / DAY, invalid=sparse)
        self.assertEqual(sparse_tracker.stale_gate("AUSD", 0)[1]["reason"], "insufficient_day_coverage")

    def test_missing_exact_24h_anchor_denies_without_forward_filling(self):
        tracker = consume(RotationTracker(["AUSD"]), end=14 * DAY,
                          prices=lambda t: 100 - t / DAY, skipped=[13 * DAY])
        self.assertEqual(tracker.stale_gate("AUSD", 0)[1]["reason"], "missing_exact_24h_anchor")

    def test_row_and_calendar_gaps_reset_trough_not_last_completed_event(self):
        for row_gap in [True, False]:
            tracker = RotationTracker(["AUSD"])
            tracker.update(0, bar(50))
            if row_gap:
                tracker.update(BAR, np.full((1, 6), np.nan))
            tracker.update(2 * BAR, bar(52))
            self.assertEqual(tracker.stale_gate("AUSD", 0)[1]["observed_episode_count"], 0)
            tracker.update(3 * BAR, bar(54))
            tracker.update(5 * BAR, bar(56))
            diagnostic = tracker.stale_gate("AUSD", 0)[1]
            self.assertEqual(diagnostic["observed_episode_count"], 1)
            self.assertEqual(diagnostic["last_completed_episode_at"], 3 * BAR)

    def test_trough_age_reset_is_strictly_after_seven_days(self):
        for final_time, expected in [(7 * DAY, 1), (7 * DAY + BAR, 0)]:
            tracker = consume(RotationTracker(["AUSD"]), end=final_time,
                              prices=lambda t: 50 if t == 0 else 52 if t == final_time else 51)
            self.assertEqual(tracker.stale_gate("AUSD", 0)[1]["observed_episode_count"], expected)

    def test_high_low_spikes_do_not_complete_a_close_to_close_episode(self):
        tracker = RotationTracker(["AUSD"])
        tracker.update(0, bar(100))
        values = bar(100)
        values[0, 1:3] = [1000, 1]
        tracker.update(BAR, values)
        self.assertEqual(tracker.stale_gate("AUSD", 0)[1]["observed_episode_count"], 0)

    def test_recovery_crossing_requires_selector_full_row_validity(self):
        invalid_rows = [(0, 0), (1, 51), (2, 0), (4, 0), (5, 1.5), (5, float("nan"))]
        for field, bad in invalid_rows:
            tracker = RotationTracker(["AUSD"])
            tracker.update(0, bar(50))
            values = bar(52)
            values[0, field] = bad
            tracker.update(BAR, values)
            with self.subTest(field=field, bad=bad):
                self.assertEqual(tracker.stale_gate("AUSD", 0)[1]["observed_episode_count"], 0)


class RotationDollarHurdleTests(unittest.TestCase):
    def test_negative_old_positive_new_hurdle_matches_independent_rational_oracle(self):
        old_mean, new_mean = Fraction(-2, 100), Fraction(4, 100)
        allowed, diagnostic = fee_hurdle(D("1"), D("100"), old_mean, new_mean)
        fee, slip, budget = Fraction(40, 10000), Fraction(5, 10000), Fraction(1025, 100)
        exit_net = 100 * (1 - slip) * (1 - fee)
        new_mark = budget * (1 + new_mean) / ((1 + slip) * (1 + fee))
        delta = exit_net - budget + new_mark - 100 * (1 + old_mean)
        self.assertTrue(allowed)
        self.assertEqual(exact(diagnostic, "delta_exact"), delta)
        self.assertEqual(D(diagnostic["estimated_old_exit_friction"]), D("0.4498000"))
        self.assertFalse(diagnostic["existing_unrealized_loss_subtracted_again"])
        self.assertTrue(diagnostic["actual_full_signed_disposal_loss_required"])
        self.assertFalse(diagnostic["cash_authorized"])

    def test_positive_new_gross_return_can_fail_after_both_switch_costs(self):
        allowed, diagnostic = fee_hurdle(D("1"), D("100"), D("0"), D("0.01"))
        self.assertFalse(allowed)
        self.assertGreater(exact(diagnostic, "new_gain_exact"), 0)
        self.assertLess(exact(diagnostic, "delta_exact"), 0)
        self.assertEqual(diagnostic["reason"], "nonpositive_incremental_dollar_gain")

    def test_strict_new_marked_gain_rejects_zero_even_with_positive_delta(self):
        cost_only_return = (1 + FEE_RATE) * (1 + SLIPPAGE_RATE) - 1
        allowed, diagnostic = fee_hurdle(D("1"), D("100"), Fraction(-1, 10), cost_only_return)
        self.assertFalse(allowed)
        self.assertEqual(exact(diagnostic, "new_gain_exact"), 0)
        self.assertGreater(exact(diagnostic, "delta_exact"), 0)

    def test_strict_dollar_zero_and_tiny_signed_differences_use_exact_arithmetic(self):
        budget = Fraction(REPLACEMENT_BUDGET)
        new_mean = Fraction(4, 100)
        new_mark = budget * (1 + new_mean) / ((1 + SLIPPAGE_RATE) * (1 + FEE_RATE))
        exit_net = 100 * (1 - SLIPPAGE_RATE) * (1 - FEE_RATE)
        neutral_old = (exit_net - budget + new_mark) / 100 - 1
        for offset, expected in [(Fraction(0), False), (Fraction(1, 10**100), False),
                                 (Fraction(-1, 10**100), True)]:
            allowed, diagnostic = fee_hurdle(D("1"), D("100"), neutral_old + offset, new_mean)
            self.assertEqual(allowed, expected)
            self.assertEqual(exact(diagnostic, "delta_exact"), -100 * offset)

    def test_ambient_decimal_precision_rounding_flags_and_traps_do_not_change_gate(self):
        reference = fee_hurdle(D("1.0000000000000000001"), D("100"), D("-0.02"), D("0.04"))
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_UP
            context.traps[Inexact] = True
            context.clear_flags()
            self.assertEqual(fee_hurdle(D("1.0000000000000000001"), D("100"), D("-0.02"), D("0.04")), reference)
            self.assertFalse(context.flags[Inexact])

    def test_invalid_money_or_return_inputs_cannot_create_a_forecast(self):
        for bad in [D("0"), D("-1"), D("NaN"), D("sNaN"), D("Infinity")]:
            for field in [0, 1]:
                args = [D("1"), D("100"), D("-0.02"), D("0.04")]
                args[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    fee_hurdle(*args)
        for bad in [D("NaN"), D("Infinity"), D("-1"), Fraction(-2)]:
            with self.subTest(return_input=bad), self.assertRaises(ValueError):
                fee_hurdle(D("1"), D("100"), bad, D("0.04"))
        for bad in [None, True, "1", 1.0]:
            with self.subTest(implicit_numeric=bad), self.assertRaises(TypeError):
                fee_hurdle(bad, D("100"), D("-0.02"), D("0.04"))

    def test_all_diagnostics_are_finite_json_with_protocol_receipts(self):
        tracker = consume(RotationTracker(["AUSD"]), end=14 * DAY, prices=lambda t: 100 - t / DAY)
        diagnostics = [tracker.daily_forecast("AUSD")[1], tracker.stale_gate("AUSD", 0)[1],
                       fee_hurdle(D("1"), D("100"), D("-0.02"), D("0.04"))[1]]
        for diagnostic in diagnostics:
            self.assertEqual(json.loads(json.dumps(diagnostic, allow_nan=False)), diagnostic)
            self.assertEqual(diagnostic["protocol_sha256"], FROZEN_ROTATION_PROTOCOL_SHA256)
            self.assertEqual(diagnostic["selector_source_sha256"], FROZEN_SELECTOR_SOURCE_SHA256)

    def test_protocol_and_selector_bytes_match_frozen_source_receipts(self):
        directory = Path(__file__).resolve().parents[1] / "research/danger_room"
        self.assertEqual(hashlib.sha256((directory / "rotation_protocol.json").read_bytes()).hexdigest(),
                         FROZEN_ROTATION_PROTOCOL_SHA256)
        self.assertEqual(hashlib.sha256((directory / "opportunity_selector.py").read_bytes()).hexdigest(),
                         FROZEN_SELECTOR_SOURCE_SHA256)
        protocol = json.loads((directory / "rotation_protocol.json").read_text())
        self.assertEqual(protocol["daily_forecast"]["minimum_valid_rows_each_day"], 274)
        self.assertEqual(protocol["daily_forecast"]["days"], 7)
        self.assertEqual(SelectorConfig().maximum_recovery_days, 7)

    def test_invalid_chronology_shape_dtype_and_parent_identity_are_rejected(self):
        for pairs in [[], ["AUSD", "AUSD"], [""]]:
            with self.assertRaises(ValueError):
                RotationTracker(pairs)
        tracker = RotationTracker(["AUSD"])
        for bad in [True, -BAR, 1, 1.0]:
            with self.assertRaises(ValueError):
                tracker.update(bad, bar())
        for values in [np.ones((1, 5)), np.ones((1, 6), dtype=complex), np.full((1, 6), "x")]:
            with self.assertRaises(ValueError):
                tracker.update(0, values)
        tracker.update(0, bar())
        with self.assertRaises(ValueError):
            tracker.update(0, bar())
        with self.assertRaises(ValueError):
            tracker.stale_gate("AUSD", BAR)
        with self.assertRaises(ValueError):
            tracker.daily_forecast("UNKNOWN")


if __name__ == "__main__":
    unittest.main()
