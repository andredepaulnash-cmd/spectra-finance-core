"""Economic bookkeeping and causal failure probes on fictional data only."""

import copy
import json
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
import subprocess
import sys
import unittest

from research.danger_room.hedge_replay import (
    Arm, EMBARGO_START, ReplayBlocked, REPLAY_CALCULATION_PRECISION,
    run_matrix, validate_constructed_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "docs/research-results/danger-room-preparation-20261010"


def fixture(name="base"):
    return json.loads((FIXTURES / f"hedge-synthetic-{name}.fixture.json").read_text())


class HedgeReplayTests(unittest.TestCase):
    def test_five_arms_equal_initial_equity_and_original_owners(self):
        m = fixture()
        results = run_matrix(m)["arms"]
        self.assertEqual([item["arm"] for item in results], ["PARK_OFF", "PARK_BTC_25", "PARK_BTC_50", "PARK_BTC_75", "PARK_BTC_100"])
        self.assertEqual(len({item["starting_equity"] for item in results}), 1)
        for arm in results:
            self.assertFalse(arm["economic_edge_claim"])
            self.assertFalse(arm["approved_for_live"])
            self.assertFalse(arm["historical_execution_allowed"])
            self.assertEqual(arm["cash_by_original_owner"]["PARK"], "1100")
            self.assertEqual(arm["cash_by_original_owner"]["VCAP"], "400")

    def test_spot_units_basis_and_dust_are_identical_across_unseized_arms(self):
        results = run_matrix(fixture())["arms"]
        for arm in results:
            self.assertEqual(arm["ending_inventory"], results[0]["ending_inventory"])
            self.assertEqual(arm["ending_inventory"]["FICTIONAL_ALT_A"]["units"], "100.75")
            self.assertEqual(arm["ending_inventory"]["FICTIONAL_DUST"]["units"], "0.000001")
            self.assertEqual(arm["ending_inventory"]["FICTIONAL_DUST"]["all_in_basis"], "0.000003")

    def test_finite_funding_does_not_raid_other_owners(self):
        m = fixture()
        m["capital"]["hedge_cash"] = "1"
        results = run_matrix(m)["arms"]
        self.assertTrue(all(arm["hedge_fees"] == "0" for arm in results))
        self.assertTrue(all(all(frame["short_contracts"] == 0 for frame in arm["frames"]) for arm in results))
        self.assertTrue(any(event["kind"] == "CASH_OR_MINIMUM_DENIAL" for event in results[-1]["events"]))

    def test_protected_cash_funding_source_rejected(self):
        for source in ("VCAP", "PARK", "ordinary_or_whatever", ""):
            m = fixture()
            m["capital"]["source"] = source
            with self.assertRaises(ReplayBlocked):
                run_matrix(m)

    def test_no_fill_means_no_invented_pnl_costs_or_position(self):
        arms = run_matrix(fixture("no-fill"))["arms"]
        for arm in arms:
            self.assertEqual(arm["ending_equity"], arms[0]["ending_equity"])
            self.assertEqual(arm["hedge_fees"], "0")
            self.assertEqual(arm["funding_signed_cash"], "0E-7")
            self.assertTrue(all(frame["short_contracts"] == 0 for frame in arm["frames"]))

    def test_declared_partial_fill_never_rounds_up(self):
        arm = run_matrix(fixture("partial"))["arms"][1]
        entries = [event for event in arm["events"] if event["kind"] == "DELAYED_SHORT"]
        self.assertTrue(entries)
        self.assertLess(entries[0]["contracts"], 3)
        self.assertGreater(arm["frames"][-1]["short_contracts"], 0)
        self.assertNotEqual(Decimal(arm["hedge_unrealized_pnl"]), 0)

    def test_execution_strictly_after_decision_and_next_bar_only(self):
        for arm in run_matrix(fixture())["arms"]:
            for event in arm["events"]:
                if event["kind"] == "DELAYED_SHORT":
                    self.assertGreater(event["bar"], event["decision_bar"])
                    self.assertGreater(datetime.fromisoformat(event["executed_at"]), datetime.fromisoformat(event["submitted_at"]))

    def test_same_bar_pending_order_cannot_fill(self):
        m = fixture()
        arm = Arm(m, "PARK_BTC_25", "0.25")
        bar = m["bars"][0]
        timestamp = datetime.fromisoformat(bar["open_time"])
        arm.pending = 3, timestamp, timestamp, 0
        arm.execute(bar, 0)
        self.assertEqual(arm.contracts, 0)
        self.assertEqual(arm.events[-1]["kind"], "WAIT_LATENCY")

    def test_reversal_cancels_stale_unfilled_increase(self):
        m = fixture()
        m["execution"]["latency_seconds"] = "1800"
        arm = run_matrix(m)["arms"][1]
        self.assertFalse(any(event["kind"] == "DELAYED_SHORT" for event in arm["events"]))
        self.assertTrue(any(event["kind"] == "CAUSAL_CANCEL_OBSOLETE_TARGET" for event in arm["events"]))

    def test_unchanged_pending_target_preserves_original_readiness(self):
        m = fixture()
        with localcontext() as context:
            context.prec = REPLAY_CALCULATION_PRECISION
            arm = Arm(m, "PARK_BTC_25", "0.25")
            arm.active = True
            arm.btc_closes = [Decimal("100000")] * 4
            arm.decide(m["bars"][4], 4, Decimal(1))
            original = arm.pending
            # Exposure/benchmark proportionality keeps the exact whole-contract target unchanged.
            arm.btc_closes = [Decimal("100000")] * 5
            arm.decide(m["bars"][5], 5, Decimal(1))
            self.assertEqual(arm.pending, original)

    def test_future_tail_does_not_change_earlier_decisions_or_frames(self):
        m = fixture()
        full = run_matrix(m)
        shorter = copy.deepcopy(m)
        shorter["bars"] = shorter["bars"][:8]
        prefix = run_matrix(shorter)
        for original, truncated in zip(full["arms"], prefix["arms"]):
            self.assertEqual(original["frames"][:8], truncated["frames"])
            self.assertEqual([event for event in original["events"] if event["bar"] < 8], truncated["events"])

    def test_not_yet_published_close_cannot_enter_signal(self):
        m = fixture()
        bar = m["bars"][4]
        bar["published_at"] = (datetime.fromisoformat(bar["close_time"]) + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ReplayBlocked, "not published"):
            run_matrix(m)

    def test_embargo_and_overlapping_bars_rejected(self):
        m = fixture()
        m["bars"][0]["open_time"] = EMBARGO_START.isoformat()
        with self.assertRaisesRegex(ReplayBlocked, "embargo"):
            run_matrix(m)
        m = fixture()
        m["bars"][1]["open_time"] = m["bars"][0]["close_time"]
        with self.assertRaises(ReplayBlocked):
            run_matrix(m)

    def test_actual_historical_class_is_unconditionally_blocked(self):
        m = fixture()
        m["experiment_class"] = "historical_realistic"
        with self.assertRaisesRegex(ReplayBlocked, "historical replay disabled"):
            run_matrix(m)

    def test_approved_live_or_nonfixture_flag_rejected(self):
        for key, value in (("approved_for_live", True), ("fixture_only", False), ("historical_inputs_deferred", False)):
            m = fixture()
            m[key] = value
            with self.assertRaises(ReplayBlocked):
                run_matrix(m)

    def test_btc_eth_canonical_spot_names_and_nonstring_id_rejected(self):
        for identity in ("BTC", "ETH", "BTC-USD", "ETH-USD", "XBT/USD", 4):
            m = fixture()
            m["initial_spot"][identity] = m["initial_spot"].pop("FICTIONAL_ALT_A")
            with self.assertRaises(ReplayBlocked):
                run_matrix(m)

    def test_missing_marks_funding_or_implicit_execution_defaults_rejected(self):
        for key in ("btc_adverse_mark", "funding_rate", "fill_fraction", "instrument_online"):
            m = fixture()
            del m["bars"][1][key]
            with self.assertRaises(ReplayBlocked):
                run_matrix(m)
        m = fixture()
        del m["bars"][1]["spot_close_marks"]["FICTIONAL_DUST"]
        with self.assertRaises(ReplayBlocked):
            run_matrix(m)

    def test_float_and_nonfinite_costs_rejected(self):
        for value in (0.2, True, "NaN", "Infinity", "-0.1"):
            m = fixture()
            m["route"]["entry_fee_per_contract"] = value
            with self.assertRaises(ValueError):
                run_matrix(m)

    def test_fees_and_funding_counted_once_in_account_equity(self):
        with localcontext() as context:
            context.prec = REPLAY_CALCULATION_PRECISION
            arms = run_matrix(fixture())["arms"]
            for arm in arms:
                reported_fees = sum((Decimal(event["fee"]) for event in arm["events"] if event["kind"] in {"DELAYED_SHORT", "DELAYED_COVER"}), Decimal(0))
                funding = sum((Decimal(event["signed_cash"]) for event in arm["events"] if event["kind"] == "FUNDING_SETTLEMENT"), Decimal(0))
                self.assertEqual(Decimal(arm["hedge_fees"]), reported_fees)
                self.assertEqual(Decimal(arm["funding_signed_cash"]), funding)
                delta = (Decimal(arm["hedge_realized_pnl_gross"]) + Decimal(arm["hedge_unrealized_pnl"])
                         - reported_fees + funding)
                self.assertEqual(Decimal(arm["ending_equity"]) - Decimal(arms[0]["ending_equity"]), delta)

    def test_known_one_contract_cash_equation_and_locked_margin(self):
        m = fixture()
        arm = Arm(m, "PARK_BTC_25", "0.25")
        submitted = datetime.fromisoformat(m["bars"][0]["close_time"])
        arm.pending = 1, submitted, submitted + timedelta(seconds=0.5), 0
        arm.execute(m["bars"][1], 1)
        self.assertEqual(arm.contracts, 1)
        self.assertEqual(arm.entry, Decimal("99850"))
        self.assertEqual(arm.locked, Decimal("200"))
        self.assertEqual(arm.free, Decimal("4799.85"))
        self.assertEqual(arm.free + arm.locked, Decimal("4999.85"))
        arm.cash_delta(Decimal(1))
        arm.funding += 1
        arm.reduce(1, Decimal("90000"), 2, "DELAYED_COVER")
        self.assertEqual(arm.free, Decimal("5099.20"))
        self.assertEqual(arm.locked, 0)
        self.assertEqual(arm.hedge_realized_gross, Decimal("98.50"))
        self.assertEqual(arm.hedge_fees, Decimal("0.30"))

    def test_beta_above_one_never_admits_net_short_speculation(self):
        m = fixture()
        arm = Arm(m, "PARK_BTC_100", "1")
        arm.active = True
        arm.btc_closes = [Decimal("100000")] * 4
        arm.decide(m["bars"][4], 4, Decimal(3))
        target = arm.pending[0]
        notional = target * Decimal("0.01") * Decimal(m["bars"][4]["btc_close"])
        self.assertLessEqual(notional, arm.spot_value(m["bars"][4]["spot_close_marks"]))

    def test_zero_variance_stays_unknown_instead_of_fabricating_beta_zero(self):
        m = fixture()
        for bar in m["bars"]:
            bar["btc_open"] = bar["btc_close"] = bar["btc_adverse_mark"] = "100000"
        arm = run_matrix(m)["arms"][-1]
        self.assertTrue(all(frame["beta"] is None for frame in arm["frames"]))
        self.assertEqual(arm["hedge_fees"], "0")

    def test_unpaid_liquidation_loss_is_retained_in_equity(self):
        arms = run_matrix(fixture("gap-cash-only"))["arms"]
        self.assertEqual(arms[-1]["liquidations"], 1)
        self.assertGreater(Decimal(arms[-1]["unpaid_liability"]), 0)
        self.assertLess(Decimal(arms[-1]["ending_equity"]), 0)
        self.assertEqual(arms[-1]["ending_inventory"], arms[0]["ending_inventory"])
        self.assertTrue(all(Decimal(frame["free_hedge_cash"]) >= 0 and Decimal(frame["locked_margin"]) >= 0 for frame in arms[-1]["frames"]))

    def test_same_account_recourse_seizes_actual_assets_and_original_owner_cash(self):
        arm = run_matrix(fixture("gap-same-account"))["arms"][-1]
        self.assertTrue(arm["spot_trace_diverged_by_recourse"])
        self.assertEqual(Decimal(arm["ending_inventory"]["FICTIONAL_ALT_A"]["units"]), 0)
        self.assertEqual(Decimal(arm["cash_by_original_owner"]["VCAP"]), 0)
        self.assertEqual(Decimal(arm["cash_by_original_owner"]["PARK"]), 0)
        self.assertGreater(Decimal(arm["collateral_conversion_fees_in_spot_net_pnl"]), 0)
        self.assertTrue(arm["conservation_checks_passed"])

    def test_spot_cash_and_units_overdraft_abort_instead_of_borrowing(self):
        for mutate in ("overspend", "oversell"):
            m = fixture()
            event = m["bars"][2]["spot_events"][0]
            event["units"] = "1000"
            if mutate == "oversell":
                event["side"] = "SELL"
            with self.assertRaises(ReplayBlocked):
                run_matrix(m)

    def test_conservation_probe_detects_deleted_dust_and_basis(self):
        m = fixture()
        arm = Arm(m, "PARK_OFF", "0")
        arm.assets["FICTIONAL_DUST"].units = Decimal(0)
        with self.assertRaisesRegex(AssertionError, "unit conservation"):
            arm.check(m["bars"][0]["spot_open_marks"], Decimal("100000"))
        arm = Arm(m, "PARK_OFF", "0")
        arm.assets["FICTIONAL_ALT_A"].basis -= 1
        with self.assertRaisesRegex(AssertionError, "basis conservation"):
            arm.check(m["bars"][0]["spot_open_marks"], Decimal("100000"))

    def test_cli_refuses_arbitrary_or_holdout_filename_before_open(self):
        completed = subprocess.run([sys.executable, str(ROOT / "research/danger_room/hedge_replay.py"),
                                    "/does/not/exist/holdout/prices.json", "--output", "/tmp/never-written-spectra.json"],
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("refused before open", completed.stderr)


if __name__ == "__main__":
    unittest.main()
