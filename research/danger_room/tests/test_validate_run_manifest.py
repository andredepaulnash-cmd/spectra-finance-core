"""Synthetic contract fixtures only; these tests execute no trading strategy."""

import copy
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_run_manifest.py"
SPEC = importlib.util.spec_from_file_location("danger_room_manifest_gate", MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def synthetic_manifest(root):
    """An explicitly fictional complete declaration, never actual venue evidence."""
    template = MODULE_PATH.with_name("run-manifest.template.json")
    manifest = json.loads(template.read_text())

    def fill(value):
        if value is None:
            return "SYNTHETIC_TEST_ONLY_EXPLICIT_DECLARATION"
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value

    manifest = fill(manifest)
    manifest["campaign_id"] = "synthetic-contract-unit-tests-no-strategy-results"
    sections = manifest["sections"]
    source = sections["protocol_hash_and_frozen_source_commits"]
    for pin in source["source_pins"].values():
        pin["commit_sha"] = "a" * 40
    source["parameters_frozen_utc"] = "2026-10-10T00:00:00Z"
    sections["experiment_class_and_exact_period_selection"].update(
        experiment_class="historical_realistic",
        train_window={"start_utc": "2026-01-01T00:00:00Z", "end_exclusive_utc": "2026-02-01T00:00:00Z"},
        evaluation_window={"start_utc": "2026-02-01T00:00:00Z", "end_exclusive_utc": "2026-03-01T00:00:00Z"},
        observed_as_of_utc="2026-03-01T00:00:00Z",
    )
    books = sections["exact_initial_books_and_all_source_owners"]
    books.update(starting_equity_quote="2000", quote_currency="USD", cash_sources=[
        {"source_id": "synthetic-ordinary-1", "owner": "ORDINARY_CASH", "amount_quote": "1000", "unencumbered": True},
        {"source_id": "synthetic-vcap-1", "owner": "VCAP", "amount_quote": "100", "unencumbered": False},
    ])
    budget = sections["finite_hedge_budget_and_matched_control_allocation"]
    budget.update(budget_quote="100", cash_source_id="synthetic-ordinary-1", source_owner="ORDINARY_CASH")
    for key in gate.SECTION_FIELDS["finite_hedge_budget_and_matched_control_allocation"][3:]:
        budget[key] = True
    route = sections["route_specs_costs_account_eligibility_and_collateral_recourse"]
    route.update(evidence_captured_utc="2026-10-10T00:00:00Z", quote_currency="USD", settlement_currency="USD", contract_multiplier="1", minimum_quantity="0.01", quantity_increment="0.01", price_increment="0.1", position_limit="10", point_in_time_terms_verified=True)
    for key in ("eligibility_evidence", "api_availability", "cross_collateral_and_legal_recourse"):
        route[key]["verified"] = True
    route["maker_taker_fee_schedule"] = {"maker_bps": "2", "taker_bps": "5", "as_of_utc": "2026-01-01T00:00:00Z", "evidence_ref": "costs"}
    route["funding_formula_and_series"] = {"formula_version": "SYNTHETIC_FORMULA_FOR_TEST_ONLY", "series_ref": "funding", "verified": True}
    model = sections["causal_estimator_and_signal_state_machine"]
    model.update(lookback_bars="288", return_interval_seconds="300", minimum_observations="240", beta_min="0", beta_max="2", exposure_mark_max_age_seconds="300", rebalance_cadence_seconds="300", rebalance_tolerance_quote="1")
    model["triggers"] = {
        "states": ["NORMAL", "RISK_OFF_WATCH", "HEDGE_ACTIVE", "CAPITULATION", "REVERSAL_WATCH", "HEDGE_EXIT"],
        "signal_expression": {"version": "SYNTHETIC_ONLY_V1", "expression": "completed_BTC_return", "inputs": ["hedge_prices"]},
        "signal_timeframes_seconds": [300],
        "entry_thresholds": {"return_less_than": "-0.02"},
        "exit_thresholds": {"return_greater_than": "0.01"},
        "confirmation_bars": 2, "minimum_state_dwell_seconds": 300,
        "cooldown_seconds": 300,
        "transition_priority": ["forced_reduction", "exit", "enter", "hold"],
        "stale_signal_policy": "DENY_NEW_OR_INCREASED_HEDGE",
    }
    sections["execution_margin_funding_and_terminal_event_rules"].update(funding_mode="EMPIRICAL_VERIFIED", financial_performance_claims_allowed=True)
    for arm in sections["all_fixed_arms_and_ratios"]["arms"]:
        arm.update(starting_equity_quote="2000", hedge_budget_quote="100", cash_source_id="synthetic-ordinary-1")
    success = sections["adverse_scenario_matrix_and_numeric_success_bounds"]
    success.update(scenarios=[{"id": "SYNTHETIC_ADVERSE_TEST_ONLY", "cost_multiplier": "2"}], rebound_drag_bound_quote="10", minimum_liquidity_quote="10", minimum_liquidation_buffer_fraction="0.2", accounting_tolerance_quote="0.000001", all_scenarios_required_for_candidate=True)
    run = sections["run_budget_output_locations_and_abort_rules"]
    run.update(maximum_seconds_per_arm="900", incomplete_is_not_complete=True)
    for name, item in manifest["artifacts"].items():
        item["path"] = f"synthetic_inputs/{name}.txt"
        item["captured_utc"] = "2026-10-10T00:00:00Z"
        if item["kind"] == "market_data":
            item.update(start_utc="2026-01-01T00:00:00Z", end_exclusive_utc="2026-03-01T00:00:00Z")
        file = root / item["path"]
        file.parent.mkdir(exist_ok=True)
        content = f"SYNTHETIC CONTRACT ARTIFACT ONLY: {name}\n".encode()
        file.write_bytes(content)
        item["sha256"] = hashlib.sha256(content).hexdigest()
    preflight = sections["independent_preflight_receipt"]
    preflight.update(reviewer_id="SYNTHETIC_TEST_REVIEWER", independently_reviewed=True)
    bind(manifest)
    return manifest


def bind(manifest):
    manifest["sections"]["independent_preflight_receipt"]["contract_body_sha256"] = gate.contract_body_sha256(manifest)


class ManifestGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = synthetic_manifest(self.root)

    def result(self, mutate=None, rebind=True):
        manifest = copy.deepcopy(self.manifest)
        if mutate:
            mutate(manifest)
        if rebind:
            bind(manifest)
        return gate.validate_run_manifest(manifest, self.root)

    def blocked(self, mutate, code, rebind=True):
        result = self.result(mutate, rebind)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(code, {item["code"] for item in result["blockers"]})
        self.assertFalse(result["approved_for_live"])
        self.assertFalse(result["approved_for_strategy_execution"])
        return result

    def test_complete_synthetic_declaration_checks_only_local_coherence(self):
        result = self.result()
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["status"], "VALIDATED_DECLARED_CONTRACT")
        self.assertEqual(result["validation_scope"], "DECLARATION_AND_LOCAL_HASH_COHERENCE_ONLY")
        self.assertFalse(result["external_facts_verified"])
        self.assertFalse(result["approved_for_live"])
        self.assertFalse(result["approved_for_strategy_execution"])
        self.assertFalse(result["strategy_executed"])

    def test_real_unfilled_template_is_blocked(self):
        template = json.loads(MODULE_PATH.with_name("run-manifest.template.json").read_text())
        result = gate.validate_run_manifest(template, self.root)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("MISSING_CONTRACT", {item["code"] for item in result["blockers"]})

    def test_missing_training_window(self):
        self.blocked(lambda m: m["sections"]["experiment_class_and_exact_period_selection"].update(train_window=None), "CAUSAL_WINDOW")

    def test_overlapping_training_evaluation(self):
        self.blocked(lambda m: m["sections"]["experiment_class_and_exact_period_selection"]["train_window"].update(end_exclusive_utc="2026-02-02T00:00:00Z"), "TRAIN_EVALUATION_OVERLAP")

    def test_window_spills_into_seal(self):
        self.blocked(lambda m: m["sections"]["experiment_class_and_exact_period_selection"]["evaluation_window"].update(end_exclusive_utc="2026-10-10T00:00:00Z"), "CAUSAL_WINDOW")

    def test_market_metadata_blockers_prevent_all_artifact_reads(self):
        with patch.object(Path, "open", side_effect=AssertionError("must not read price or any artifact")):
            self.blocked(lambda m: m["artifacts"]["hedge_prices"].update(end_exclusive_utc="2026-10-10T00:00:00Z"), "ARTIFACT_CAUSAL_WINDOW")

    def test_market_role_cannot_be_relabelled_source(self):
        def mutate(m):
            item = m["artifacts"]["spot_prices"]
            item["kind"] = "source"
            item.pop("start_utc")
            item.pop("end_exclusive_utc")
        self.blocked(mutate, "MARKET_INPUT_KIND")

    def test_missing_market_role(self):
        self.blocked(lambda m: m["sections"]["input_files_and_coverage_hashes"].update(input_refs=["hedge_prices", "funding"]), "MARKET_INPUT_ROLES")

    def test_price_coverage_must_span_evaluation(self):
        self.blocked(lambda m: m["artifacts"]["spot_prices"].update(end_exclusive_utc="2026-02-01T00:00:00Z"), "MARKET_COVERAGE_GAP")

    def test_price_coverage_must_include_training(self):
        self.blocked(lambda m: m["artifacts"]["hedge_prices"].update(start_utc="2026-02-01T00:00:00Z"), "MARKET_COVERAGE_GAP")

    def test_funding_need_not_precede_evaluation_but_must_cover_it(self):
        self.assertEqual(self.result(lambda m: m["artifacts"]["funding"].update(start_utc="2026-02-01T00:00:00Z"))["blockers"], [])
        self.blocked(lambda m: m["artifacts"]["funding"].update(end_exclusive_utc="2026-02-28T00:00:00Z"), "MARKET_COVERAGE_GAP")

    def test_trigger_cannot_use_official_nonprice_metadata_as_market_input(self):
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"]["triggers"]["signal_expression"].update(inputs=["route_evidence"]), "TRIGGER_INPUT_KIND")

    def test_campaign_requires_nonempty_string(self):
        self.blocked(lambda m: m.update(campaign_id=False), "CAMPAIGN_ID")

    def test_whitespace_only_parameter_is_unresolved(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(initial_margin_schedule="   "), "MISSING_CONTRACT")

    def test_padded_unknown_parameter_is_unresolved(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(initial_margin_schedule=" UNKNOWN "), "MISSING_CONTRACT")

    def test_every_cash_owner_requires_string_identity(self):
        self.blocked(lambda m: m["sections"]["exact_initial_books_and_all_source_owners"]["cash_sources"][1].update(owner=False), "CASH_OWNERSHIP")

    def test_every_cash_encumbrance_requires_actual_boolean(self):
        self.blocked(lambda m: m["sections"]["exact_initial_books_and_all_source_owners"]["cash_sources"][1].update(unencumbered=0), "CASH_OWNERSHIP")

    def test_official_nonprice_metadata_after_seal_is_allowed(self):
        self.assertEqual(self.result()["blockers"], [])
        self.assertGreater(self.manifest["artifacts"]["route_evidence"]["captured_utc"], gate.SEALED_START)

    def test_sealed_path_not_opened(self):
        with patch.object(Path, "open", side_effect=AssertionError("must not open sealed files")):
            self.blocked(lambda m: m["artifacts"]["spot_prices"].update(path="research/holdout/prices.json"), "SEALED_SOURCE_PATH")

    def test_unequal_capital(self):
        self.blocked(lambda m: m["sections"]["all_fixed_arms_and_ratios"]["arms"][1].update(starting_equity_quote="2100"), "UNEQUAL_ARM_CAPITAL")

    def test_unequal_spot_trace(self):
        self.blocked(lambda m: m["sections"]["all_fixed_arms_and_ratios"]["arms"][1].update(spot_trace_ref="native_adapter"), "UNEQUAL_ARM_CAPITAL")

    def test_cash_source_double_claimed(self):
        self.blocked(lambda m: m["sections"]["exact_initial_books_and_all_source_owners"]["cash_sources"].append({"source_id": "synthetic-ordinary-1", "owner": "HEDGE_BOOK", "amount_quote": "100", "unencumbered": True}), "DOUBLE_CLAIM")

    def test_park_collateral_refused(self):
        self.blocked(lambda m: m["sections"]["finite_hedge_budget_and_matched_control_allocation"].update(source_owner="PARK", cash_source_id="PARK_CASH"), "CROSS_BOOK_CLAIM")

    def test_cash_greater_than_account_equity(self):
        self.blocked(lambda m: m["sections"]["exact_initial_books_and_all_source_owners"]["cash_sources"][0].update(amount_quote="2001"), "CASH_EXCEEDS_EQUITY")

    def test_unverified_trace_feasibility(self):
        self.blocked(lambda m: m["sections"]["finite_hedge_budget_and_matched_control_allocation"].update(unchanged_spot_trace_feasibility_verified=False), "MATCHED_CAPITAL")

    def test_unknown_costs(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(maker_taker_fee_schedule="UNKNOWN"), "UNKNOWN_COSTS")

    def test_unknown_funding(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(funding_formula_and_series=None), "UNKNOWN_FUNDING")

    def test_scenario_funding_is_not_performance_admission(self):
        self.blocked(lambda m: m["sections"]["execution_margin_funding_and_terminal_event_rules"].update(funding_mode="SCENARIO_ONLY", financial_performance_claims_allowed=False), "SCENARIO_ONLY_FUNDING")

    def test_malformed_hash(self):
        self.blocked(lambda m: m["artifacts"]["costs"].update(sha256="not-a-hash"), "HASH_FORMAT")

    def test_changed_local_artifact_bytes(self):
        (self.root / self.manifest["artifacts"]["costs"]["path"]).write_text("changed evidence")
        result = self.result()
        self.assertIn("HASH_MISMATCH", {item["code"] for item in result["blockers"]})

    def test_unknown_account_scope(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"]["eligibility_evidence"].update(scope="UNKNOWN"), "VENUE_SCOPE_UNKNOWN")

    def test_permission_escalation(self):
        self.blocked(lambda m: m.update(approved_for_live=True), "PERMISSION_ESCALATION")

    def test_arbitrary_permission_extension(self):
        self.blocked(lambda m: m["permissions"].update(place_real_orders=True), "PERMISSION_ESCALATION")

    def test_integer_zero_is_not_a_permission_boolean(self):
        self.blocked(lambda m: m["permissions"].update(submit_orders=0), "PERMISSION_ESCALATION")

    def test_trigger_thresholds_must_be_finite_numbers(self):
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"]["triggers"]["entry_thresholds"].update(return_less_than="NaN"), "INVALID_NUMBER")

    def test_signal_expression_cannot_be_an_untyped_integer(self):
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"]["triggers"].update(signal_expression=1), "TRIGGER_CONTRACT")

    def test_confirmation_bars_cannot_be_fractional(self):
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"]["triggers"].update(confirmation_bars="1.5"), "TRIGGER_CONTRACT")

    def test_unknown_top_level_live_endpoint(self):
        self.blocked(lambda m: m.update(execution_endpoint="https://example.invalid/orders"), "UNKNOWN_TOP_LEVEL_FIELD")

    def test_currency_mismatch(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(settlement_currency="BTC"), "CURRENCY_MISMATCH")

    def test_estimator_insufficient_lookback(self):
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"].update(minimum_observations="289"), "ESTIMATOR_CONTRACT")

    def test_inverted_beta_bounds(self):
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"].update(beta_min="3"), "ESTIMATOR_CONTRACT")

    def test_bare_not_applicable_not_accepted(self):
        self.blocked(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(borrow_and_rollover_costs_or_not_applicable="NOT_APPLICABLE"), "MISSING_CONTRACT")

    def test_evidenced_not_applicable_allowed_only_designated_route_field(self):
        result = self.result(lambda m: m["sections"]["route_specs_costs_account_eligibility_and_collateral_recourse"].update(borrow_and_rollover_costs_or_not_applicable={"not_applicable": True, "reason": "SYNTHETIC non-borrowed contract", "scope": "SYNTHETIC route only", "evidence_ref": "route_evidence"}))
        self.assertEqual(result["blockers"], [])
        self.blocked(lambda m: m["sections"]["causal_estimator_and_signal_state_machine"].update(missing_data_rule={"not_applicable": True, "reason": "test", "scope": "test", "evidence_ref": "route_evidence"}), "UNJUSTIFIED_NOT_APPLICABLE")

    def test_preflight_does_not_bind_mutated_contract(self):
        self.blocked(lambda m: m.update(campaign_id="different-run"), "PREFLIGHT_BINDING", rebind=False)

    def test_missing_required_artifact(self):
        (self.root / self.manifest["artifacts"]["funding"]["path"]).unlink()
        result = self.result()
        self.assertIn("ARTIFACT_UNREADABLE", {item["code"] for item in result["blockers"]})

    def test_symlinked_artifact_refused(self):
        target = self.root / self.manifest["artifacts"]["costs"]["path"]
        alias = target.with_name("alias.txt")
        alias.symlink_to(target)
        self.blocked(lambda m: m["artifacts"]["costs"].update(path=str(alias.relative_to(self.root))), "ARTIFACT_SYMLINK")

    def test_cli_rejects_duplicate_json_keys(self):
        manifest = self.root / "duplicate.json"
        manifest.write_text('{"approved_for_live":false,"approved_for_live":true}')
        output = io.StringIO()
        with redirect_stdout(output):
            status = gate.main([str(manifest), "--artifact-root", str(self.root)])
        self.assertEqual(status, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["blockers"][0]["code"], "INVALID_JSON_OR_CONTRACT")
        self.assertFalse(result["approved_for_live"])


if __name__ == "__main__":
    unittest.main()
