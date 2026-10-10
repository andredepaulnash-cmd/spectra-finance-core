"""Synthetic evidence admission and descriptive accounting boundaries."""

from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.danger_room.completed_run_diagnostics import (
    owner_cash_diagnostics, rung_funding_diagnostics, source_attribution,
    verified_completed_inputs,
)


def fixture(directory):
    run = Path(directory) / "synthetic_fixture"
    run.mkdir()
    payload = {"arm": run.name, "status": "COMPLETED", "completed_strategy_evaluation": True,
               "holdout_accessed": False, "declared_arm": {"owned_cash": True}}
    content = json.dumps(payload).encode()
    (run / "results.json").write_bytes(content)
    member = {"file": "results.json", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (run / "artifact_manifest.json").write_text(json.dumps([member]))
    receipt = {"arm": run.name, "passed": True, "status": "PASS", "artifacts": [member],
               "results_sha256": member["sha256"], "full_cash_owner_adapter_verified": True,
               "owner_proof": {"passed": True}}
    receipt_path = Path(directory) / "independent.receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    return run, receipt_path, receipt


class FinalizedEvidenceGateTests(unittest.TestCase):
    def test_only_completed_audited_digest_bound_results_are_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            run, receipt_path, receipt = fixture(directory)
            results, independent, binding = verified_completed_inputs(run, receipt_path)
            self.assertEqual(results["arm"], "synthetic_fixture")
            self.assertEqual(independent, receipt)
            self.assertEqual(binding["results_sha256"], receipt["results_sha256"])

    def test_failed_pending_or_misidentified_receipt_denies_before_financial_parse(self):
        for field, value in [("passed", False), ("status", "PENDING"), ("arm", "other")]:
            with tempfile.TemporaryDirectory() as directory:
                run, receipt_path, receipt = fixture(directory)
                receipt[field] = value
                receipt_path.write_text(json.dumps(receipt))
                (run / "results.json").write_text("NOT VALID JSON")
                with self.subTest(field=field), self.assertRaises(ValueError):
                    verified_completed_inputs(run, receipt_path)

    def test_incomplete_owner_proof_denies_before_financial_read(self):
        with tempfile.TemporaryDirectory() as directory:
            run, receipt_path, receipt = fixture(directory)
            receipt["owner_proof"]["passed"] = False
            receipt_path.write_text(json.dumps(receipt))
            (run / "results.json").unlink()
            with self.assertRaisesRegex(ValueError, "owner proof"):
                verified_completed_inputs(run, receipt_path)

    def test_null_owner_proof_is_an_explicit_admission_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            run, receipt_path, receipt = fixture(directory)
            receipt["owner_proof"] = None
            receipt_path.write_text(json.dumps(receipt))
            (run / "results.json").unlink()
            with self.assertRaisesRegex(ValueError, "owner proof"):
                verified_completed_inputs(run, receipt_path)

    def test_changed_member_bytes_hash_or_manifest_set_denies(self):
        for mutation in ["bytes", "hash", "manifest", "results_digest"]:
            with tempfile.TemporaryDirectory() as directory:
                run, receipt_path, receipt = fixture(directory)
                if mutation == "bytes":
                    (run / "results.json").write_bytes(b"x")
                elif mutation == "hash":
                    content = (run / "results.json").read_bytes()
                    (run / "results.json").write_bytes(content.replace(b"COMPLETED", b"INCOMPLETE"))
                elif mutation == "manifest":
                    (run / "artifact_manifest.json").write_text("[]")
                else:
                    receipt["results_sha256"] = "0" * 64
                    receipt_path.write_text(json.dumps(receipt))
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    verified_completed_inputs(run, receipt_path)

    def test_path_escape_duplicate_filename_or_symlink_denies(self):
        for mutation in ["escape", "duplicate", "symlink"]:
            with tempfile.TemporaryDirectory() as directory:
                run, receipt_path, receipt = fixture(directory)
                if mutation == "escape":
                    receipt["artifacts"][0]["file"] = "../results.json"
                elif mutation == "duplicate":
                    receipt["artifacts"].append(receipt["artifacts"][0].copy())
                else:
                    outside = Path(directory) / "outside.json"
                    outside.write_bytes((run / "results.json").read_bytes())
                    (run / "results.json").unlink()
                    (run / "results.json").symlink_to(outside)
                (run / "artifact_manifest.json").write_text(json.dumps(receipt["artifacts"]))
                receipt_path.write_text(json.dumps(receipt))
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    verified_completed_inputs(run, receipt_path)

    def test_manifest_bound_incomplete_or_holdout_results_still_deny(self):
        for field, value in [("status", "RUNNING"), ("completed_strategy_evaluation", False),
                             ("holdout_accessed", True)]:
            with tempfile.TemporaryDirectory() as directory:
                run, receipt_path, receipt = fixture(directory)
                payload = json.loads((run / "results.json").read_text())
                payload[field] = value
                content = json.dumps(payload).encode()
                (run / "results.json").write_bytes(content)
                receipt["artifacts"][0].update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
                receipt["results_sha256"] = receipt["artifacts"][0]["sha256"]
                (run / "artifact_manifest.json").write_text(json.dumps(receipt["artifacts"]))
                receipt_path.write_text(json.dumps(receipt))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    verified_completed_inputs(run, receipt_path)


class ExactDescriptiveAttributionTests(unittest.TestCase):
    def test_whole_parent_positive_sale_can_include_base_loss_and_recovery_gain(self):
        lots = [{"lot_id": "base", "source": "BASE_ENTRY", "position_id": "1", "pair": "A",
                 "base_remaining": "0", "cost_basis_remaining_quote": "0", "entry_fees_remaining_quote": "0"},
                {"lot_id": "recovery", "source": "VCAP_RECOVERY", "position_id": "1", "pair": "A",
                 "base_remaining": "0", "cost_basis_remaining_quote": "0", "entry_fees_remaining_quote": "0"}]
        sales = [{"receipt_id": "sale:1", "allocations": [
            {"lot_id": "base", "gross_proceeds": "90", "exit_fee": "1", "gross_basis_release": "100", "entry_fee_release": "1"},
            {"lot_id": "recovery", "gross_proceeds": "90", "exit_fee": "1", "gross_basis_release": "50", "entry_fee_release": "1"}]}]
        result = source_attribution(sales, lots, [])
        self.assertEqual(Fraction(result["BASE_ENTRY"]["realized_net_pnl"]), -12)
        self.assertEqual(Fraction(result["VCAP_RECOVERY"]["realized_net_pnl"]), 38)
        self.assertEqual(sum(Fraction(item["realized_net_pnl"]) for item in result.values()), 26)

    def test_open_entry_fees_and_losses_remain_in_marked_attribution(self):
        lots = [{"lot_id": "base", "source": "BASE_ENTRY", "position_id": "1", "pair": "A",
                 "base_remaining": "2", "cost_basis_remaining_quote": "100", "entry_fees_remaining_quote": "1"}]
        result = source_attribution([], lots, [{"position_id": 1, "pair": "A", "mark": "40"}])
        self.assertEqual(Fraction(result["BASE_ENTRY"]["open_basis"]), 101)
        self.assertEqual(Fraction(result["BASE_ENTRY"]["open_unrealized_pnl"]), -21)
        self.assertEqual(Fraction(result["VCAP_RECOVERY"]["marked_total_pnl"]), 0)

    def test_duplicate_disposal_or_missing_mark_denies(self):
        lot = {"lot_id": "base", "source": "BASE_ENTRY", "position_id": "1", "pair": "A",
               "base_remaining": "1", "cost_basis_remaining_quote": "1", "entry_fees_remaining_quote": "0"}
        sale = {"receipt_id": "sale:1", "allocations": []}
        with self.assertRaises(ValueError):
            source_attribution([sale, sale], [lot], [])
        with self.assertRaises(ValueError):
            source_attribution([], [lot], [{"position_id": 1, "pair": "A", "mark": None}])

    def test_owner_usable_cash_respects_both_owner_holds_and_physical_reserve(self):
        account = [{"timestamp": "1", "equity": "2000"}]
        owned = [{"timestamp": 1, "cash_total": "1500", "cash_by_owner": {"PRODUCTIVE": "1000", "VCAP": "500"},
                  "held_by_owner": {"PRODUCTIVE": "100", "VCAP": "499"}}]
        result = owner_cash_diagnostics(account, owned, fee_bps="40")
        usable = result["usable_after_owner_holds_and_physical_reserve"]
        self.assertEqual(Fraction(usable["PRODUCTIVE"]["ending"]), 401)
        self.assertEqual(Fraction(usable["VCAP"]["ending"]), 1)
        self.assertEqual(result["cash_only_readiness_counts"]["productive_cash_can_fund_fixed_10_25_ticket"], 1)
        self.assertEqual(result["cash_only_readiness_counts"]["VCAP_cash_can_fund_modeled_minimum_gross_5_plus_fee"], 0)
        self.assertEqual(Fraction(result["modeled_recovery_minimum_all_in"]), Fraction(502, 100))

    def test_physical_floor_can_remove_all_owner_usability(self):
        account = [{"timestamp": "1", "equity": "2000"}]
        owned = [{"timestamp": 1, "cash_total": "300", "cash_by_owner": {"PRODUCTIVE": "200", "VCAP": "100"},
                  "held_by_owner": {"PRODUCTIVE": "0", "VCAP": "0"}}]
        result = owner_cash_diagnostics(account, owned, fee_bps="40")
        self.assertEqual(Fraction(result["usable_after_owner_holds_and_physical_reserve"]["PRODUCTIVE"]["ending"]), 0)

    def test_owner_account_sample_mismatch_denies(self):
        with self.assertRaises(ValueError):
            owner_cash_diagnostics([{"timestamp": "1"}], [], fee_bps="40")


class SerializedRungFundingTests(unittest.TestCase):
    def example(self):
        assignment = [{"position_id": 1, "pair": "A", "category": "deep", "entry_seed_all_in": "10"},
                      {"position_id": 2, "pair": "B", "category": "UNKNOWN", "entry_seed_all_in": "10"}]
        profiles = {"deep": {"seed_multiples": ["1", "2"]}}
        orders = [{"kind": "RECOVERY", "order_id": 10, "position_id": 1, "submitted_at": 100, "budget": "6"},
                  {"kind": "RECOVERY", "order_id": 11, "position_id": 1, "submitted_at": 200, "budget": "8"},
                  {"kind": "RECOVERY", "order_id": 12, "position_id": 1, "submitted_at": 400, "budget": "20"}]
        fills = [{"kind": "RECOVERY", "order_id": 11, "position_id": 1, "timestamp": 300, "gross_quote": "7.5", "fee_quote": "0.5"},
                 {"kind": "RECOVERY", "order_id": 12, "position_id": 1, "timestamp": 500, "gross_quote": "19.5", "fee_quote": "0.5"}]
        terminal = [{"position_id": 1}, {"position_id": 2}]
        return assignment, orders, fills, profiles, terminal, {"LADDER_EXHAUSTED": 123, "UNKNOWN_LADDER_PROFILE": 456}

    def test_rejected_attempt_does_not_consume_rung_or_inflate_unique_requested_total(self):
        result = rung_funding_diagnostics(*self.example())
        self.assertEqual(result["submitted_recovery_orders"], 3)
        self.assertEqual(result["unique_observed_parent_rungs"], 2)
        self.assertEqual(Fraction(result["unique_observed_rung_requested_all_in"]), 30)
        self.assertEqual(Fraction(result["submitted_all_in_sum_including_retries"]), 34)
        self.assertEqual(result["parent_rungs"][0]["proposals"], 2)
        self.assertEqual(result["clipped_submitted_proposals"], 2)
        self.assertEqual(result["submitted_shortfalls_at_least_one_cent"], 2)
        self.assertEqual(result["confirmed_rungs_below_full_requested_amount"], 1)
        self.assertEqual(result["confirmed_rung_shortfalls_at_least_one_cent"], 1)
        self.assertEqual(Fraction(result["confirmed_rung_all_in"]), 28)

    def test_unknown_parent_and_reached_exhaustion_are_unique_parent_counts(self):
        result = rung_funding_diagnostics(*self.example())
        self.assertEqual(result["UNKNOWN_parents"], 1)
        self.assertEqual(result["ending_UNKNOWN_parents"], 1)
        self.assertEqual(result["parents_reaching_all_rungs_consumed"], 1)
        self.assertEqual(result["ending_parents_with_all_rungs_consumed"], 1)
        self.assertEqual(result["gate_evaluations_not_unique_orders"]["LADDER_EXHAUSTED"], 123)
        self.assertEqual(result["unserialized_cap_specific_denial_amounts_or_rung_reset_events"], "UNAVAILABLE")

    def test_confirmed_fill_without_submitted_order_and_unknown_recovery_deny(self):
        args = list(self.example())
        args[1] = []
        with self.assertRaises(ValueError):
            rung_funding_diagnostics(*args)
        args = list(self.example())
        args[1][0]["position_id"] = 2
        with self.assertRaises(ValueError):
            rung_funding_diagnostics(*args)

    def test_roundoff_within_tolerance_is_not_called_funding_clip(self):
        args = list(self.example())
        args[1] = [{"kind": "RECOVERY", "order_id": 10, "position_id": 1, "submitted_at": 100,
                    "budget": "9.9999999999999999999"}]
        args[2] = []
        result = rung_funding_diagnostics(*args)
        self.assertEqual(result["clipped_submitted_proposals"], 0)
        self.assertEqual(result["submitted_shortfalls_at_least_one_cent"], 0)

    def test_duplicate_identical_pending_snapshots_are_one_proposal(self):
        args = list(self.example())
        args[1].append(args[1][0].copy())
        result = rung_funding_diagnostics(*args)
        self.assertEqual(result["submitted_recovery_orders"], 3)


if __name__ == "__main__":
    unittest.main()
