"""Independent accounting and evidence-admission probes for terminal values."""

import copy
from decimal import Decimal as D, getcontext
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.danger_room.terminal_valuation import (
    analyze_files, evaluate_terminal, TERMINAL_PROTOCOL_SHA256,
)


END = 1782864000
PROTOCOL = Path(__file__).resolve().parents[1] / "research/danger_room/terminal_valuation_protocol.json"


def fixture():
    results = {"arm": "synthetic", "status": "COMPLETED", "completed_strategy_evaluation": True,
        "research_only": True, "production_activation": False, "holdout_accessed": False,
        "financial_metrics": {"starting_cash": "200", "ending_cash": "100",
            "ending_equity": "185", "total_pnl": "-15", "open_basis": "77", "ending_positions": 3}}
    terminal = []
    for i, (quantity, mark, basis, age) in enumerate([
            ("2", "10", "20", 0), ("1", "50", "45", 86401), ("3", "5", "12", 604801)], 1):
        terminal.append({"position_id": i, "pair": f"C{i}-USD", "quantity": quantity,
            "mark": mark, "basis": basis, "mark_timestamp": END - age, "mark_age_seconds": age,
            "opened_at": END - 1000000, "position_age_seconds": 1000000})
    return results, terminal


def calculate(results=None, terminal=None, **kwargs):
    original_results, original_terminal = fixture()
    return evaluate_terminal(original_results if results is None else results,
                             original_terminal if terminal is None else terminal,
                             fee_bps=kwargs.pop("fee_bps", "40"),
                             slippage_bps=kwargs.pop("slippage_bps", "5"),
                             expected_terminal_timestamp=END, **kwargs)


class TerminalValuationTests(unittest.TestCase):
    def test_exact_mark_and_separate_multiplicative_cost_accounting(self):
        analysis = calculate()
        scenarios = analysis["valuation_sensitivities"]
        self.assertEqual(D(scenarios["marked"]["equity"]), D(185))
        factor = D("0.9995") * D("0.996")
        self.assertEqual(D(scenarios["modeled_exit_cost"]["equity"]), D(100) + D(85) * factor)
        self.assertEqual(D(scenarios["stale_over_24h_zero_value"]["equity"]), D(100) + D(20) * factor)
        self.assertEqual(D(scenarios["stale_over_7d_zero_value"]["equity"]), D(100) + D(70) * factor)
        self.assertEqual(analysis["stale_marks"]["over_24h"]["count"], 2)
        self.assertEqual(D(analysis["stale_marks"]["over_24h"]["marked_value"]), D(65))
        self.assertEqual(analysis["stale_marks"]["over_7d"]["count"], 1)
        self.assertEqual(analysis["strategy_evaluations_added"], 0)
        self.assertFalse(analysis["orders_or_fills_created"])

    def test_exact_stale_boundaries_are_retained_not_zeroed(self):
        results, terminal = fixture()
        terminal[1]["mark_age_seconds"] = 86400
        terminal[1]["mark_timestamp"] = END - 86400
        terminal[2]["mark_age_seconds"] = 604800
        terminal[2]["mark_timestamp"] = END - 604800
        analysis = calculate(results, terminal)
        self.assertEqual(analysis["stale_marks"]["over_24h"]["count"], 1)
        self.assertEqual(analysis["stale_marks"]["over_7d"]["count"], 0)

    def test_cash_only_account_needs_no_fabricated_terminal_price(self):
        results, _ = fixture()
        results["financial_metrics"].update(ending_cash="200", ending_equity="200",
            total_pnl="0", open_basis="0", ending_positions=0)
        analysis = calculate(results, [])
        self.assertEqual(analysis["terminal_parent_count"], 0)
        self.assertTrue(all(D(case["equity"]) == 200 for case in analysis["valuation_sensitivities"].values()))

    def test_zero_mark_is_zero_valued_and_disclosed(self):
        results, terminal = fixture()
        terminal[2]["mark"] = "0"
        results["financial_metrics"].update(ending_equity="170", total_pnl="-30")
        analysis = calculate(results, terminal)
        self.assertEqual(analysis["zero_mark_parent_count"], 1)
        self.assertEqual(analysis["parents"][2]["marked_value"], "0")

    def test_negative_cash_or_mark_and_nonpositive_quantity_rejected(self):
        for location, field, value in [("results", "ending_cash", "-1"),
                                       ("terminal", "mark", "-1"), ("terminal", "quantity", "0")]:
            results, terminal = fixture()
            if location == "results":
                results["financial_metrics"][field] = value
            else:
                terminal[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_missing_price_and_nonfinite_numbers_fail_closed(self):
        for value in [None, "NaN", "Infinity", "bad", True, 10.1]:
            results, terminal = fixture()
            terminal[0]["mark"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_missing_terminal_mark_is_not_imputed(self):
        results, terminal = fixture()
        del terminal[0]["mark"]
        with self.assertRaises(ValueError):
            calculate(results, terminal)

    def test_equity_basis_and_pnl_must_reconcile_independently(self):
        for field in ["ending_equity", "open_basis", "total_pnl"]:
            results, terminal = fixture()
            results["financial_metrics"][field] = str(D(results["financial_metrics"][field]) + 1)
            with self.subTest(field=field), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_tiny_native_rounding_difference_is_disclosed(self):
        results, terminal = fixture()
        results["financial_metrics"].update(ending_equity="185.00000000000000000001",
                                              total_pnl="-14.99999999999999999999")
        analysis = calculate(results, terminal)
        self.assertEqual(D(analysis["reconciliation"]["difference"]), D("-1e-20"))
        results["financial_metrics"]["ending_equity"] = "185.00000000000000001"
        with self.assertRaises(ValueError):
            calculate(results, terminal)

    def test_position_count_pair_and_identity_uniqueness(self):
        for mutation in ["count", "pair", "id"]:
            results, terminal = fixture()
            if mutation == "count":
                results["financial_metrics"]["ending_positions"] = 2
            elif mutation == "pair":
                terminal[1]["pair"] = terminal[0]["pair"]
            else:
                terminal[1]["position_id"] = terminal[0]["position_id"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_mark_time_and_position_time_must_match_terminal(self):
        for field in ["mark_timestamp", "mark_age_seconds", "position_age_seconds"]:
            results, terminal = fixture()
            terminal[0][field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_age_integer_guards_and_no_future_marks(self):
        for value in [-1, True, 0.5, "1"]:
            results, terminal = fixture()
            terminal[0]["mark_age_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_incomplete_live_or_holdout_results_are_rejected(self):
        for field, value in [("status", "RUNNING"), ("completed_strategy_evaluation", False),
                             ("research_only", False), ("production_activation", True),
                             ("holdout_accessed", True), ("holdout_accessed", None)]:
            results, terminal = fixture()
            results[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                calculate(results, terminal)

    def test_malformed_fees_and_slippage_are_rejected(self):
        for field in ["fee_bps", "slippage_bps"]:
            for value in ["-1", "10000", "Infinity", None, True]:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    calculate(**{field: value})

    def test_zero_cost_changes_only_stale_markdown_not_recorded_account(self):
        analysis = calculate(fee_bps="0", slippage_bps="0")
        self.assertEqual(analysis["valuation_sensitivities"]["marked"]["equity"],
                         analysis["valuation_sensitivities"]["modeled_exit_cost"]["equity"])

    def test_local_precision_does_not_change_native_context_or_inputs(self):
        results, terminal = fixture()
        before = copy.deepcopy((results, terminal))
        precision = getcontext().prec
        calculate(results, terminal)
        self.assertEqual(getcontext().prec, precision)
        self.assertEqual((results, terminal), before)

    def files_fixture(self, directory, *, declared=True):
        results, terminal = fixture()
        strategy = {"research_only": True, "holdout_access": False,
            "window": {"end_exclusive": END},
            "arms": [{"name": "synthetic", "fee_bps": "40", "slippage_bps": "5"}]}
        strategy_path = directory / "strategy_protocol.json"
        strategy_path.write_text(json.dumps(strategy))
        results["protocol_sha256"] = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
        if declared:
            results["declared_arm"] = strategy["arms"][0]
        results_path = directory / "results.json"
        terminal_path = directory / "terminal.json"
        results_path.write_text(json.dumps(results))
        terminal_path.write_text(json.dumps(terminal))
        manifest_path = directory / "artifact_manifest.json"
        self.write_manifest(manifest_path, [results_path, terminal_path])
        return results_path, terminal_path, strategy_path, manifest_path

    @staticmethod
    def write_manifest(path, paths):
        path.write_text(json.dumps([{"file": item.name, "bytes": item.stat().st_size,
            "sha256": hashlib.sha256(item.read_bytes()).hexdigest()} for item in paths]))

    def test_file_analysis_binds_exact_artifacts_and_result_costs(self):
        self.assertEqual(hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(), TERMINAL_PROTOCOL_SHA256)
        with tempfile.TemporaryDirectory() as folder:
            results, terminal, strategy, _ = self.files_fixture(Path(folder))
            analysis = analyze_files(results, terminal, PROTOCOL, strategy_protocol_paths=[strategy])
            self.assertEqual(analysis["cost_binding"]["source"], "results.json#/declared_arm")
            self.assertEqual(analysis["source_files"][str(results)], hashlib.sha256(results.read_bytes()).hexdigest())

    def test_legacy_costs_require_exact_hash_bound_frozen_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            results, terminal, strategy, _ = self.files_fixture(Path(folder), declared=False)
            analysis = analyze_files(results, terminal, PROTOCOL, strategy_protocol_paths=[strategy])
            self.assertEqual(analysis["cost_binding"]["source"], "frozen_strategy_protocol#/arms")
            with self.assertRaises(ValueError):
                analyze_files(results, terminal, PROTOCOL)
            strategy.write_text(strategy.read_text() + "\n")
            with self.assertRaises(ValueError):
                analyze_files(results, terminal, PROTOCOL, strategy_protocol_paths=[strategy])

    def test_completed_manifest_detects_modified_terminal_before_outcome_read(self):
        with tempfile.TemporaryDirectory() as folder:
            results, terminal, strategy, _ = self.files_fixture(Path(folder))
            terminal.write_text(terminal.read_text() + "\n")
            with self.assertRaises(ValueError):
                analyze_files(results, terminal, PROTOCOL, strategy_protocol_paths=[strategy])

    def test_result_costs_cannot_disagree_with_matching_frozen_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            results, terminal, strategy, manifest = self.files_fixture(Path(folder))
            data = json.loads(results.read_text())
            data["declared_arm"]["fee_bps"] = "0"
            results.write_text(json.dumps(data))
            self.write_manifest(manifest, [results, terminal])
            with self.assertRaises(ValueError):
                analyze_files(results, terminal, PROTOCOL, strategy_protocol_paths=[strategy])

    def test_mutated_terminal_protocol_and_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            results, terminal, strategy, manifest = self.files_fixture(directory)
            changed = directory / "terminal_protocol.json"
            changed.write_bytes(PROTOCOL.read_bytes() + b"\n")
            with self.assertRaises(ValueError):
                analyze_files(results, terminal, changed, strategy_protocol_paths=[strategy])
            results.write_text('{"status":"RUNNING","status":"COMPLETED"}')
            self.write_manifest(manifest, [results, terminal])
            with self.assertRaises(ValueError):
                analyze_files(results, terminal, PROTOCOL, strategy_protocol_paths=[strategy])


if __name__ == "__main__":
    unittest.main()
