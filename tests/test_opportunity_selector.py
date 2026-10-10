"""Falsification tests for causal price-opportunity screening."""

import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.danger_room.opportunity_selector import (
    BAR_SECONDS, BARS_PER_DAY, DAY_SECONDS, MatrixOpportunitySelector,
    OpportunitySelector, SelectorConfig,
)


START = 1767225600


def bars(prices, volume=100.0, trades=10.0):
    price = np.asarray(prices, dtype=float)
    if price.ndim == 1:
        price = price[:, None]
    rows = np.empty((*price.shape, 6))
    rows[:, :, :4] = price[:, :, None]
    rows[:, :, 4] = volume
    rows[:, :, 5] = trades
    return rows


def config(**kwargs):
    return SelectorConfig(history_days=7, minimum_history_days=1,
                          minimum_daily_dollar_volume=0,
                          minimum_daily_trades=0, **kwargs)


def daily_waves(days=7):
    return np.tile(np.repeat([100.0, 103.0, 100.0, 103.0], 72), days)


class OpportunitySelectorTests(unittest.TestCase):
    def test_cost_ratio_includes_both_sides_and_net_target(self):
        c = config()
        self.assertAlmostEqual(c.gross_recovery_ratio, 1.0174 * 1.0005 * 1.004 / (0.9995 * 0.996))
        self.assertGreater(c.gross_recovery_ratio, 1.0174)

    def test_costs_can_remove_apparent_opportunity(self):
        rows = bars(np.repeat([100.0, 101.0], 144))
        cheap = MatrixOpportunitySelector(rows, ["A-USD"], START,
                                         config(net_target_bps=50, fee_bps_per_side=0,
                                                slippage_bps_per_side=0))
        costly = MatrixOpportunitySelector(rows, ["A-USD"], START,
                                          config(net_target_bps=50))
        self.assertEqual(cheap.rank_at_index(len(rows))[0], ["A-USD"])
        ordered, diagnostics = costly.rank_at_index(len(rows))
        self.assertEqual(ordered, [])
        self.assertEqual(diagnostics["A-USD"]["nonoverlapping_recovery_count"], 0)

    def test_recurring_waves_outweigh_one_monthly_spike(self):
        frequent = daily_waves(28)
        rare = np.full(len(frequent), 100.0)
        rare[-BARS_PER_DAY:] = daily_waves(1)
        rows = bars(np.column_stack([frequent, rare]))
        selector = MatrixOpportunitySelector(rows, ["FREQUENT-USD", "RARE-USD"], START,
                                           SelectorConfig())
        ordered, diagnostics = selector.rank_at_index(len(rows))
        self.assertEqual(ordered, ["FREQUENT-USD"])
        self.assertEqual(diagnostics["FREQUENT-USD"]["opportunity_days_fraction"], 1)
        self.assertAlmostEqual(diagnostics["RARE-USD"]["opportunity_days_fraction"], 1 / 28)
        self.assertGreater(diagnostics["FREQUENT-USD"]["score"], diagnostics["RARE-USD"]["score"])

    def test_daily_close_gain_is_not_required_for_price_opportunities(self):
        prices = np.tile(np.repeat([100.0, 103.0, 100.0, 103.0, 100.0, 100.0], 48), 7)
        ordered, diagnostic = MatrixOpportunitySelector(bars(prices), ["A-USD"], START,
                                                       config()).rank_at_index(len(prices))
        self.assertEqual(ordered, ["A-USD"])
        self.assertEqual(diagnostic["A-USD"]["signed_return_fraction"], 0)
        self.assertGreater(diagnostic["A-USD"]["nonoverlapping_recovery_count"], 0)

    def test_high_low_range_does_not_create_a_close_recovery(self):
        prices = np.column_stack([daily_waves(7), np.linspace(103, 90, 7 * BARS_PER_DAY)])
        rows = bars(prices)
        rows[:, :, 1] = 110
        rows[:, :, 2] = 80
        ordered, diagnostic = MatrixOpportunitySelector(rows, ["WAVES-USD", "FALL-USD"], START,
                                                       config()).rank_at_index(len(rows))
        self.assertEqual(ordered, ["WAVES-USD"])
        self.assertEqual(diagnostic["FALL-USD"]["nonoverlapping_recovery_count"], 0)
        self.assertLess(diagnostic["FALL-USD"]["relative_strength_fraction"], 0)

    def test_cycles_are_nonoverlapping_not_every_higher_close(self):
        prices = np.concatenate([[100, 103, 104, 106], np.full(BARS_PER_DAY - 4, 106)])
        _, diagnostic = MatrixOpportunitySelector(bars(prices), ["A-USD"], START,
                                                 config()).rank_at_index(len(prices))
        self.assertEqual(diagnostic["A-USD"]["nonoverlapping_recovery_count"], 2)
        self.assertGreaterEqual(diagnostic["A-USD"]["mean_modeled_net_recovery_fraction"], 0.0174)

    def test_future_mutation_cannot_change_prior_membership_or_scores(self):
        rows = bars(daily_waves(8))
        cutoff = 7 * BARS_PER_DAY
        mutated = rows.copy()
        mutated[cutoff:, :, :4] *= 1000
        mutated[cutoff:, :, 4:] = 0
        a = MatrixOpportunitySelector(rows, ["A-USD"], START, config()).rank_at_index(cutoff)
        b = MatrixOpportunitySelector(mutated, ["A-USD"], START, config()).rank_at_index(cutoff)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(a[1]["A-USD"]["evidence_cutoff"], START + cutoff * BAR_SECONDS)

    def test_current_open_row_is_not_consumed(self):
        rows = bars(daily_waves(2))
        selector = MatrixOpportunitySelector(rows, ["A-USD"], START, config())
        before = selector.rank_at_index(BARS_PER_DAY)
        rows[BARS_PER_DAY] = np.nan
        self.assertEqual(before, selector.rank_at_index(BARS_PER_DAY))
        self.assertEqual(selector.rank_at_index(BARS_PER_DAY + 1)[0], [])

    def test_missing_bar_resets_a_cross_gap_recovery(self):
        prices = np.concatenate([np.full(100, 100), [np.nan], np.full(187, 110)])
        _, diagnostic = MatrixOpportunitySelector(bars(prices), ["A-USD"], START,
                                                 config()).rank_at_index(len(prices))
        self.assertEqual(diagnostic["A-USD"]["nonoverlapping_recovery_count"], 0)

    def test_low_coverage_excludes_a_sparse_asset(self):
        prices = daily_waves(7)
        rows = bars(prices)
        rows[::3] = np.nan
        self.assertEqual(MatrixOpportunitySelector(rows, ["A-USD"], START,
                                                  config()).rank_at_index(len(rows))[0], [])

    def test_missing_and_zero_volume_latest_rows_exclude_admission(self):
        rows = bars(np.column_stack([daily_waves(7), daily_waves(7)]))
        rows[-1, 0] = np.nan
        rows[-1, 1, 4] = 0
        ordered, diagnostics = MatrixOpportunitySelector(rows, ["MISSING-USD", "EMPTY-USD"], START,
                                                        config()).rank_at_index(len(rows))
        self.assertEqual(ordered, [])
        self.assertEqual(diagnostics["EMPTY-USD"]["reason"], "missing_or_stale_latest_bar")

    def test_no_volume_or_trades_cannot_pass_liquidity_gate(self):
        rows = bars(daily_waves(7), volume=0, trades=0)
        self.assertEqual(MatrixOpportunitySelector(rows, ["A-USD"], START,
                                                  SelectorConfig()).rank_at_index(len(rows))[0], [])

    def test_isolated_large_trade_does_not_validate_zero_trade_price_waves(self):
        rows = bars(daily_waves(7), volume=0, trades=0)
        rows[-1, 0, 4:] = [1000000, 1000000]
        ordered, diagnostic = MatrixOpportunitySelector(rows, ["A-USD"], START,
                                                       config()).rank_at_index(len(rows))
        self.assertEqual(ordered, [])
        self.assertEqual(diagnostic["A-USD"]["nonoverlapping_recovery_count"], 0)

    def test_cached_features_do_not_retain_holdings_or_expose_mutable_diagnostics(self):
        rows = bars(np.column_stack([daily_waves(2), daily_waves(2)]))
        selector = MatrixOpportunitySelector(rows, ["A-USD", "B-USD"], START, config())
        ordered, diagnostic = selector.rank_at_index(BARS_PER_DAY, held_pairs={"A-USD"})
        self.assertEqual(ordered, ["B-USD"])
        diagnostic["B-USD"]["score"] = 100000
        ordered, later = selector.rank_at_index(BARS_PER_DAY + 12, held_pairs={"B-USD"})
        self.assertEqual(ordered, ["A-USD"])
        self.assertNotEqual(later["B-USD"]["score"], 100000)
        self.assertEqual(later["A-USD"]["evidence_cutoff"], START + (BARS_PER_DAY + 12) * BAR_SECONDS)

    def test_exact_cost_adjusted_target_boundary_is_counted_once(self):
        c = config()
        prices = np.concatenate([np.full(144, 100), np.full(144, 100 * c.gross_recovery_ratio)])
        _, diagnostics = MatrixOpportunitySelector(bars(prices), ["A-USD"], START,
                                                  c).rank_at_index(len(prices))
        self.assertEqual(diagnostics["A-USD"]["nonoverlapping_recovery_count"], 1)
        self.assertAlmostEqual(diagnostics["A-USD"]["mean_modeled_net_recovery_fraction"], 0.0174)

    def test_cold_start_does_not_invent_history(self):
        rows = bars(daily_waves(6))
        ordered, diagnostic = MatrixOpportunitySelector(rows, ["A-USD"], START).rank_at_index(len(rows))
        self.assertEqual(ordered, [])
        self.assertEqual(diagnostic["A-USD"]["reason"], "insufficient_completed_history")

    def test_partial_first_day_is_not_a_full_warmup_day(self):
        rows = bars(daily_waves(1)[:144])
        selector = MatrixOpportunitySelector(rows, ["A-USD"], START + DAY_SECONDS // 2, config())
        ordered, diagnostic = selector.rank_at_index(len(rows))
        self.assertEqual(ordered, [])
        self.assertEqual(diagnostic["A-USD"]["observed_completed_days"], 0)

    def test_holdings_are_excluded_without_changing_categories_or_input_map(self):
        rows = bars(np.column_stack([daily_waves(7), daily_waves(7)]))
        categories = {"A-USD": "crypto_candidate", "B-USD": "crypto_candidate"}
        ladders = {"A-USD": "architect_verified_category"}
        selector = MatrixOpportunitySelector(rows, ["A-USD", "B-USD"], START,
                                             config(), categories, ladders)
        ordered, diagnostic = selector.rank_at_index(len(rows), held_pairs={"A-USD"}, limit=50)
        self.assertEqual(ordered, ["B-USD"])
        self.assertEqual(diagnostic["A-USD"]["ladder_category"], "architect_verified_category")
        self.assertEqual(diagnostic["B-USD"]["ladder_category"], "UNKNOWN")
        self.assertEqual(ladders, {"A-USD": "architect_verified_category"})

    def test_btc_eth_and_noncrypto_categories_remain_excluded(self):
        pairs = ["BTC-USD", "XBTUSD", "ETH/USD", "A-USD", "GOLD-USD"]
        rows = bars(np.column_stack([daily_waves(7)] * len(pairs)))
        ordered, _ = MatrixOpportunitySelector(rows, pairs, START, config(),
                                              {"GOLD-USD": "peg_fiat_metal"}).rank_at_index(len(rows))
        self.assertEqual(ordered, ["A-USD"])

    def test_stale_decision_time_is_not_eligible(self):
        selector = OpportunitySelector(["A-USD"], config())
        for i, row in enumerate(bars(daily_waves(1))):
            selector.update(START + (i + 1) * BAR_SECONDS, row)
        self.assertEqual(selector.rank(START + DAY_SECONDS)[0], ["A-USD"])
        self.assertEqual(selector.rank(START + DAY_SECONDS + BAR_SECONDS)[0], [])

    def test_time_gap_is_missing_calendar_history_and_no_cross_gap_cycle(self):
        selector = OpportunitySelector(["A-USD"], config())
        selector.update(START + BAR_SECONDS, bars([100])[0])
        selector.update(START + 3 * DAY_SECONDS, bars([110])[0])
        _, diagnostics = selector.rank(START + 3 * DAY_SECONDS)
        self.assertEqual(diagnostics["A-USD"]["nonoverlapping_recovery_count"], 0)
        self.assertEqual(diagnostics["A-USD"]["completed_days_in_window"], 3)

    def test_rolling_window_forgets_old_spike(self):
        prices = np.concatenate([daily_waves(1), np.full(7 * BARS_PER_DAY, 100)])
        ordered, diagnostics = MatrixOpportunitySelector(bars(prices), ["A-USD"], START,
                                                        config()).rank_at_index(len(prices))
        self.assertEqual(ordered, [])
        self.assertEqual(diagnostics["A-USD"]["nonoverlapping_recovery_count"], 0)

    def test_cross_day_downside_is_observed_separately_from_volatility(self):
        prices = np.concatenate([np.full(BARS_PER_DAY, 100), np.full(BARS_PER_DAY, 75)])
        _, diagnostic = MatrixOpportunitySelector(bars(prices), ["A-USD"], START,
                                                 config()).rank_at_index(len(prices))
        self.assertAlmostEqual(diagnostic["A-USD"]["observed_close_drawdown_fraction"], 0.25)
        self.assertAlmostEqual(diagnostic["A-USD"]["signed_return_fraction"], -0.25)

    def test_invalid_shapes_prices_and_fractional_trade_counts_fail_closed(self):
        rows = bars(daily_waves(1))
        rows[:, 0, 5] = 0.5
        self.assertEqual(MatrixOpportunitySelector(rows, ["A-USD"], START,
                                                  config()).rank_at_index(len(rows))[0], [])
        with self.assertRaises(ValueError):
            OpportunitySelector(["A-USD"]).update(START + BAR_SECONDS, np.ones((1, 5)))

    def test_constructor_rejects_invalid_parameters_and_maps(self):
        for args in ({"minimum_history_days": 29}, {"history_days": True},
                     {"fee_bps_per_side": -1}, {"slippage_bps_per_side": float("nan")},
                     {"minimum_opportunity_days_fraction": 2}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                SelectorConfig(**args)
        with self.assertRaises(ValueError):
            OpportunitySelector(["A-USD", "A-USD"])
        with self.assertRaises(ValueError):
            OpportunitySelector(["A-USD"], ladder_categories={"B-USD": "X"})

    def test_rewind_or_future_evidence_ranking_is_rejected(self):
        rows = bars(daily_waves(1))
        selector = MatrixOpportunitySelector(rows, ["A-USD"], START, config())
        selector.rank_at_index(10)
        with self.assertRaises(ValueError):
            selector.rank_at_index(9)
        with self.assertRaises(ValueError):
            selector.selector.rank(START + 9 * BAR_SECONDS)
        with self.assertRaises(ValueError):
            selector.selector.update(START + 10 * BAR_SECONDS, rows[0])

    def test_deterministic_ties_and_no_fabricated_fifty_positions(self):
        rows = bars(np.column_stack([daily_waves(7)] * 2))
        ordered, diagnostic = MatrixOpportunitySelector(rows, ["B-USD", "A-USD"], START,
                                                        config()).rank_at_index(len(rows), limit=50)
        self.assertEqual(ordered, ["A-USD", "B-USD"])
        self.assertFalse(diagnostic["A-USD"]["actual_order_fill_proven"])
        self.assertTrue(diagnostic["A-USD"]["screening_proxy_only"])


if __name__ == "__main__":
    unittest.main()
