"""Read-only H1 research contract checks; never an executor or live approval.

The gate checks declared causal coverage and exact local artifact bytes. It does
not establish the truth of venue/account attestations, parse every data format,
or replace independent preflight. A valid contract still cannot submit orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "spectra-danger-room-run-manifest-v1"
SEALED_START = "2026-10-09T15:36:31.317254Z"
SEALED_END = "2026-11-09T00:00:00Z"
PERMISSIONS = {
    "network_access": False,
    "submit_orders": False,
    "load_credentials": False,
    "mutate_production": False,
    "mutate_holdout": False,
    "live_execution": False,
}
SECTION_FIELDS = {
    "protocol_hash_and_frozen_source_commits": (
        "protocol_ref", "source_pins", "parameters_frozen_utc"
    ),
    "experiment_class_and_exact_period_selection": (
        "experiment_class", "selection_rule", "train_window", "evaluation_window",
        "observed_as_of_utc", "no_post_outcome_tuning"
    ),
    "point_in_time_universe_and_asset_identity_hashes": (
        "universe_ref", "asset_identity_ref", "membership_is_point_in_time",
        "maximum_spot_assets", "excluded_spot_assets"
    ),
    "input_files_and_coverage_hashes": ("input_refs", "coverage_ref"),
    "spot_control_and_native_adapter_hashes": (
        "spot_trace_ref", "native_adapter_ref", "trace_immutable",
        "hedge_profit_feedback_to_spot"
    ),
    "exact_initial_books_and_all_source_owners": (
        "starting_equity_quote", "quote_currency", "initial_books_ref",
        "source_owners_ref", "cash_sources", "same_initial_units_basis_all_arms"
    ),
    "route_specs_costs_account_eligibility_and_collateral_recourse": (
        "route_id", "route_type", "evidence_captured_utc", "official_evidence_ref",
        "account_jurisdiction", "eligibility_evidence", "api_availability",
        "allowed_direction", "instrument_id", "underlying", "contract_type",
        "quote_currency", "settlement_currency", "contract_multiplier",
        "minimum_quantity", "quantity_increment", "price_increment", "position_limit",
        "borrow_availability_or_not_applicable", "initial_margin_schedule",
        "maintenance_margin_schedule", "margin_change_events_or_frozen_adverse_scenarios",
        "collateral_eligibility", "collateral_haircuts", "cross_collateral_and_legal_recourse",
        "liquidation_waterfall", "liquidation_costs", "funding_formula_and_series",
        "borrow_and_rollover_costs_or_not_applicable", "maker_taker_fee_schedule",
        "reduce_only_semantics", "order_types", "conditional_stop_semantics",
        "suspension_and_contract_removal_policy", "tradeable_status_history",
        "point_in_time_terms_verified"
    ),
    "finite_hedge_budget_and_matched_control_allocation": (
        "budget_quote", "cash_source_id", "source_owner", "unencumbered_verified",
        "same_budget_reserved_all_arms", "control_holds_budget_as_cash",
        "unchanged_spot_trace_feasibility_verified", "no_park_or_vcap_claim"
    ),
    "causal_estimator_and_signal_state_machine": (
        "estimator_formula_version", "lookback_bars", "return_interval_seconds",
        "minimum_observations", "missing_data_rule", "beta_min", "beta_max",
        "exposure_mark_max_age_seconds", "rebalance_cadence_seconds",
        "rebalance_tolerance_quote", "target_formula", "economic_notional_cap",
        "triggers", "decision_execution_order"
    ),
    "execution_margin_funding_and_terminal_event_rules": (
        "decision_timestamp_rule", "submission_latency", "order_type", "time_in_force",
        "spread_data_or_conservative_model", "slippage_model",
        "order_capacity_and_participation", "partial_and_no_fill_model",
        "minimum_and_increment_enforcement", "mark_index_and_basis_paths",
        "stale_price_policy", "unavailable_api_policy", "gap_and_stop_execution",
        "rebound_and_short_squeeze_stress", "margin_and_liquidation_event_order",
        "open_terminal_order_policy", "execution_finality_rule", "funding_mode",
        "funding_ref", "costs_ref", "financial_performance_claims_allowed"
    ),
    "all_fixed_arms_and_ratios": ("arms",),
    "adverse_scenario_matrix_and_numeric_success_bounds": (
        "scenarios", "rebound_drag_bound_quote", "minimum_liquidity_quote",
        "minimum_liquidation_buffer_fraction", "accounting_tolerance_quote",
        "all_scenarios_required_for_candidate"
    ),
    "report_definitions_checkpoint_censoring_and_tolerances": (
        "equity_identity", "cost_counting_convention", "unit_and_basis_conservation",
        "inventory_definition", "concentration_denominators", "checkpoints",
        "checkpoint_censoring", "unknown_mark_policy", "report_all_arms_and_regimes"
    ),
    "run_budget_output_locations_and_abort_rules": (
        "maximum_seconds_per_arm", "output_directory", "abort_rules",
        "incomplete_is_not_complete"
    ),
    "independent_preflight_receipt": (
        "receipt_ref", "reviewer_id", "independently_reviewed", "contract_body_sha256"
    ),
}
ARM_RATIOS = {
    "PARK_OFF": "0", "PARK_BTC_25": "0.25", "PARK_BTC_50": "0.50",
    "PARK_BTC_75": "0.75", "PARK_BTC_100": "1.00",
}
TRIGGER_FIELDS = (
    "states", "signal_expression", "signal_timeframes_seconds", "entry_thresholds",
    "exit_thresholds", "confirmation_bars", "minimum_state_dwell_seconds",
    "cooldown_seconds", "transition_priority", "stale_signal_policy"
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
UNKNOWN = {"UNKNOWN", "TBD", "BLOCKED", "PENDING", "UNVERIFIED", "NOT_APPLICABLE", "N/A", "NA"}
NOT_APPLICABLE_FIELDS = {
    "borrow_availability_or_not_applicable",
    "borrow_and_rollover_costs_or_not_applicable",
    "conditional_stop_semantics",
}


def contract_body_sha256(manifest: dict) -> str:
    """Receipt binds canonical manifest without its own circular references."""
    body = json.loads(json.dumps(manifest))
    sections = body.get("sections", {})
    preflight = sections.pop("independent_preflight_receipt", {})
    ref = preflight.get("receipt_ref") if isinstance(preflight, dict) else None
    if isinstance(body.get("artifacts"), dict):
        body["artifacts"].pop(ref, None)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_run_manifest(manifest: dict, artifact_root: Path) -> dict:
    blockers = []

    def fail(code, field, message):
        blockers.append({"code": code, "field": field, "message": message})

    def number(value, field, minimum=Decimal("0"), positive=False):
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise InvalidOperation
            result = Decimal(value)
            if not result.is_finite() or result < minimum or (positive and result <= 0):
                raise InvalidOperation
            return result
        except (InvalidOperation, ValueError, TypeError):
            fail("INVALID_NUMBER", field, "An explicit finite decimal in range is required.")
            return None

    def timestamp(value, field):
        try:
            if not isinstance(value, str) or not value.endswith("Z"):
                raise ValueError
            result = datetime.fromisoformat(value[:-1] + "+00:00")
            if result.utcoffset().total_seconds() != 0:
                raise ValueError
            return result
        except (ValueError, TypeError, AttributeError):
            fail("INVALID_TIMESTAMP", field, "Explicit UTC ISO-8601 timestamp ending Z required.")
            return None

    def complete(value, field):
        if value is None or (isinstance(value, str) and (not value.strip() or value.strip().upper() in UNKNOWN)):
            fail("MISSING_CONTRACT", field, "Required value is absent or unresolved.")
        elif isinstance(value, dict):
            if not value:
                fail("MISSING_CONTRACT", field, "Required object is empty.")
            if "not_applicable" in value:
                if field.rsplit(".", 1)[-1] not in NOT_APPLICABLE_FIELDS or set(value) != {"not_applicable", "reason", "evidence_ref", "scope"} or value.get("not_applicable") is not True:
                    fail("UNJUSTIFIED_NOT_APPLICABLE", field, "Only designated route fields accept explicit evidenced non-applicability.")
            for key, item in value.items():
                complete(item, f"{field}.{key}")
        elif isinstance(value, list):
            if not value:
                fail("MISSING_CONTRACT", field, "Required list is empty.")
            for index, item in enumerate(value):
                complete(item, f"{field}[{index}]")

    if not isinstance(manifest, dict):
        fail("INVALID_MANIFEST", "$", "JSON object required.")
        manifest = {}
    allowed = {"schema", "campaign_id", "stage", "research_only", "approved_for_live",
               "permissions", "holdout", "artifacts", "sections"}
    if set(manifest) - allowed:
        fail("UNKNOWN_TOP_LEVEL_FIELD", "$", "Unknown fields cannot expand this contract.")
    if manifest.get("schema") != SCHEMA or manifest.get("stage") != "H1":
        fail("SCHEMA_MISMATCH", "schema/stage", "Only the declared H1 schema is supported.")
    complete(manifest.get("campaign_id"), "campaign_id")
    if not isinstance(manifest.get("campaign_id"), str):
        fail("CAMPAIGN_ID", "campaign_id", "A nonempty explicit string research identity is required.")
    if manifest.get("research_only") is not True or manifest.get("approved_for_live") is not False:
        fail("PERMISSION_ESCALATION", "research_only/approved_for_live", "Research-only and live=false required.")
    permissions = manifest.get("permissions")
    if not isinstance(permissions, dict) or set(permissions) != set(PERMISSIONS) or any(value is not False for value in permissions.values()):
        fail("PERMISSION_ESCALATION", "permissions", "All six read-only permission fields must be false.")
    expected_holdout = {
        "start_utc": SEALED_START, "end_exclusive_utc": SEALED_END,
        "read_prices": False, "score": False, "mutate": False,
    }
    holdout = manifest.get("holdout")
    if not isinstance(holdout, dict) or set(holdout) != set(expected_holdout) or any(holdout.get(key) != expected_holdout[key] for key in ("start_utc", "end_exclusive_utc")) or any(holdout.get(key) is not False for key in ("read_prices", "score", "mutate")):
        fail("HOLDOUT_BOUNDARY", "holdout", "Sealed holdout must remain unread, unscored and unchanged.")

    sections = manifest.get("sections", {})
    if not isinstance(sections, dict):
        sections = {}
    if set(sections) != set(SECTION_FIELDS):
        fail("SECTION_SET", "sections", "All fifteen declared sections, and no extras, are required.")
    for name, required in SECTION_FIELDS.items():
        section = sections.get(name)
        if not isinstance(section, dict):
            fail("MISSING_SECTION", name, "Required section object is missing.")
            continue
        if set(section) != set(required):
            fail("SECTION_FIELDS", name, "Section must contain exactly its declared fields.")
        for key in required:
            complete(section.get(key), f"{name}.{key}")
    get = lambda name: sections.get(name) if isinstance(sections.get(name), dict) else {}
    period = get("experiment_class_and_exact_period_selection")
    sealed = datetime.fromisoformat(SEALED_START[:-1] + "+00:00")
    windows = {}
    for name in ("train_window", "evaluation_window"):
        window = period.get(name)
        if not isinstance(window, dict) or set(window) != {"start_utc", "end_exclusive_utc"}:
            fail("CAUSAL_WINDOW", name, "Explicit start and exclusive end required.")
            continue
        start = timestamp(window.get("start_utc"), f"{name}.start_utc")
        end = timestamp(window.get("end_exclusive_utc"), f"{name}.end_exclusive_utc")
        if start and end:
            if start >= end or end > sealed:
                fail("CAUSAL_WINDOW", name, "Window must be nonempty and wholly before sealed prices.")
            windows[name] = (start, end)
    if len(windows) == 2 and windows["train_window"][1] > windows["evaluation_window"][0]:
        fail("TRAIN_EVALUATION_OVERLAP", "windows", "Training must end before evaluation begins.")
    observed = timestamp(period.get("observed_as_of_utc"), "observed_as_of_utc")
    if observed and (observed > sealed or any(end > observed for _, end in windows.values())):
        fail("CAUSAL_CUTOFF", "observed_as_of_utc", "Every price window must end by observed cutoff before seal.")
    experiment_class = period.get("experiment_class")
    if experiment_class not in {"historical_realistic", "current_system_crash_shape"}:
        fail("SCENARIO_ONLY", "experiment_class", "An unfunded or incomplete scenario cannot admit H1 performance research.")
    if period.get("no_post_outcome_tuning") is not True:
        fail("CAUSAL_TUNING", "no_post_outcome_tuning", "Outcome-based retuning is forbidden in a frozen run.")

    refs = manifest.get("artifacts", {})
    if not isinstance(refs, dict):
        refs = {}
    source = get("protocol_hash_and_frozen_source_commits")
    timestamp(source.get("parameters_frozen_utc"), "parameters_frozen_utc")
    pins = source.get("source_pins")
    if not isinstance(pins, dict) or not pins:
        fail("SOURCE_PINS", "source_pins", "Exact source repositories and commits are required.")
    else:
        for name, pin in pins.items():
            if not isinstance(pin, dict) or set(pin) != {"repository", "commit_sha"}:
                fail("SOURCE_PINS", f"source_pins.{name}", "Exact repository and commit pair required.")
            elif not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(pin["repository"])) or not COMMIT.fullmatch(str(pin["commit_sha"])):
                fail("SOURCE_PINS", f"source_pins.{name}", "Repository and immutable 40-character Git commit required.")

    def reference(ref, field):
        if not isinstance(ref, str) or ref not in refs:
            fail("MISSING_REFERENCE", field, "Reference must name an exact artifact in this manifest.")

    def check_nested_refs(value, field):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_ref"):
                    reference(item, f"{field}.{key}")
                elif key.endswith("_refs") and isinstance(item, list):
                    for ref in item:
                        reference(ref, f"{field}.{key}")
                else:
                    check_nested_refs(item, f"{field}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_nested_refs(item, f"{field}[{index}]")
    check_nested_refs(sections, "sections")
    inputs = get("input_files_and_coverage_hashes").get("input_refs")
    if isinstance(inputs, list):
        for ref in inputs:
            reference(ref, "input_refs")
    else:
        fail("MISSING_REFERENCE", "input_refs", "Explicit input reference list required.")
    required_market_refs = {"spot_prices", "hedge_prices", "funding"}
    if not isinstance(inputs, list) or not required_market_refs.issubset(set(item for item in inputs if isinstance(item, str))):
        fail("MARKET_INPUT_ROLES", "input_refs", "Spot prices, hedge prices and funding roles must all be declared.")
    for ref in required_market_refs:
        if not isinstance(refs.get(ref), dict) or refs[ref].get("kind") != "market_data":
            fail("MARKET_INPUT_KIND", f"artifacts.{ref}", "Required market inputs cannot bypass causal coverage by relabeling their kind.")
    artifact_paths = []
    for ref, item in refs.items():
        field = f"artifacts.{ref}"
        if not isinstance(item, dict):
            fail("INVALID_ARTIFACT", field, "Artifact metadata object required.")
            continue
        kind = item.get("kind")
        expected_keys = {"path", "sha256", "kind", "captured_utc"}
        if kind == "market_data":
            expected_keys |= {"start_utc", "end_exclusive_utc"}
        if set(item) != expected_keys or kind not in {"market_data", "source", "protocol", "official_evidence", "receipt", "inventory"}:
            fail("INVALID_ARTIFACT", field, "Exact supported metadata fields and artifact kind required.")
        if not SHA256.fullmatch(str(item.get("sha256", ""))):
            fail("HASH_FORMAT", field, "Lowercase SHA256 required.")
        timestamp(item.get("captured_utc"), f"{field}.captured_utc")
        path = item.get("path")
        if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
            fail("ARTIFACT_PATH", field, "Relative contained path required.")
            continue
        if any(part.lower() in {"holdout", "sealed", "prospective"} for part in Path(path).parts):
            fail("SEALED_SOURCE_PATH", field, "Sealed holdout paths are never read by this gate.")
        if kind == "market_data":
            start = timestamp(item.get("start_utc"), f"{field}.start_utc")
            end = timestamp(item.get("end_exclusive_utc"), f"{field}.end_exclusive_utc")
            if not start or not end or start >= end or end > sealed or (observed and end > observed):
                fail("ARTIFACT_CAUSAL_WINDOW", field, "Declared market-data coverage must precede seal and cutoff.")
            if start and end and ref in required_market_refs and "evaluation_window" in windows:
                required_start = windows["evaluation_window"][0]
                if ref != "funding" and "train_window" in windows:
                    required_start = windows["train_window"][0]
                if start > required_start or end < windows["evaluation_window"][1]:
                    fail("MARKET_COVERAGE_GAP", field, "Declared input coverage must span its complete requested training/evaluation period.")
        artifact_paths.append((field, path, item.get("sha256")))

    books = get("exact_initial_books_and_all_source_owners")
    equity = number(books.get("starting_equity_quote"), "starting_equity_quote", positive=True)
    cash = books.get("cash_sources")
    cash_by_id = {}
    if isinstance(cash, list):
        for row in cash:
            if not isinstance(row, dict) or set(row) != {"source_id", "owner", "amount_quote", "unencumbered"}:
                fail("CASH_OWNERSHIP", "cash_sources", "Exact source identity, owner, amount and encumbrance required.")
                continue
            source_id = row["source_id"]
            if not isinstance(source_id, str) or not source_id.strip() or source_id in cash_by_id:
                fail("DOUBLE_CLAIM", "cash_sources", "A cash source cannot be claimed by multiple books or repeated.")
                continue
            if not isinstance(row["owner"], str) or not row["owner"].strip() or row["owner"].strip().upper() in UNKNOWN or type(row["unencumbered"]) is not bool:
                fail("CASH_OWNERSHIP", f"cash_sources.{source_id}", "Every cash row needs a known string owner and exact boolean encumbrance.")
            amount = number(row["amount_quote"], f"cash_sources.{source_id}.amount_quote")
            cash_by_id[source_id] = (row["owner"], amount, row["unencumbered"])
    else:
        fail("CASH_OWNERSHIP", "cash_sources", "Explicit source-owned cash list required.")
    known_cash = [row[1] for row in cash_by_id.values() if row[1] is not None]
    if equity is not None and sum(known_cash, Decimal("0")) > equity:
        fail("CASH_EXCEEDS_EQUITY", "cash_sources", "Declared starting cash cannot exceed consolidated starting equity.")
    budget_section = get("finite_hedge_budget_and_matched_control_allocation")
    budget = number(budget_section.get("budget_quote"), "budget_quote", positive=True)
    owner = budget_section.get("source_owner")
    funding_source = budget_section.get("cash_source_id")
    if owner != "ORDINARY_CASH" or not isinstance(funding_source, str) or any(word in funding_source.upper() for word in ("VCAP", "PARK")):
        fail("CROSS_BOOK_CLAIM", "hedge_cash_source", "Only explicit unencumbered ordinary cash can fund this H1 design.")
    funding_row = cash_by_id.get(funding_source) if isinstance(funding_source, str) else None
    if not funding_row or funding_row[0] != owner or funding_row[2] is not True or funding_row[1] is None or budget is None or funding_row[1] < budget:
        fail("UNFUNDED_COLLATERAL", "budget_quote", "Budget must fit the verified ordinary cash source.")
    if equity is not None and budget is not None and budget > equity:
        fail("UNFUNDED_COLLATERAL", "budget_quote", "Budget cannot exceed starting account equity.")
    for key in SECTION_FIELDS["finite_hedge_budget_and_matched_control_allocation"][3:]:
        if budget_section.get(key) is not True:
            fail("MATCHED_CAPITAL", key, "Matched reserve and unchanged trace feasibility must be verified.")

    spot = get("spot_control_and_native_adapter_hashes")
    if spot.get("trace_immutable") is not True or spot.get("hedge_profit_feedback_to_spot") is not False:
        fail("SPOT_CONTROL", "spot_control", "All arms use fixed PARK without hedge feedback.")
    if books.get("same_initial_units_basis_all_arms") is not True:
        fail("MATCHED_CAPITAL", "same_initial_units_basis_all_arms", "Initial owned inventory and basis must match.")
    universe = get("point_in_time_universe_and_asset_identity_hashes")
    if universe.get("membership_is_point_in_time") is not True or universe.get("maximum_spot_assets") != 50 or universe.get("excluded_spot_assets") != ["BTC", "ETH"]:
        fail("UNIVERSE_SCOPE", "universe", "Point-in-time, fifty-asset maximum and BTC/ETH spot exclusions required.")
    arms = get("all_fixed_arms_and_ratios").get("arms")
    names = [arm.get("name") for arm in arms if isinstance(arm, dict)] if isinstance(arms, list) else []
    if len(names) != 5 or set(names) != set(ARM_RATIOS):
        fail("FIXED_ARMS", "arms", "Exactly the five predeclared H1 arms required.")
    if isinstance(arms, list):
        for arm in arms:
            if not isinstance(arm, dict) or set(arm) != {"name", "ratio", "starting_equity_quote", "hedge_budget_quote", "cash_source_id", "spot_trace_ref"}:
                fail("FIXED_ARMS", "arms", "Exact arm identity, ratio, capital, budget, source and trace required.")
                continue
            ratio = number(arm["ratio"], "arm.ratio")
            if arm["name"] not in ARM_RATIOS or ratio != Decimal(ARM_RATIOS.get(arm["name"], "-1")):
                fail("FIXED_RATIO", "arm.ratio", "Ratio differs from predeclared five-arm matrix.")
            arm_equity = number(arm["starting_equity_quote"], "arm.starting_equity_quote", positive=True)
            arm_budget = number(arm["hedge_budget_quote"], "arm.hedge_budget_quote", positive=True)
            if arm_equity != equity or arm_budget != budget or arm["cash_source_id"] != funding_source or arm["spot_trace_ref"] != spot.get("spot_trace_ref"):
                fail("UNEQUAL_ARM_CAPITAL", "arms", "Every arm must use identical equity, cash reserve/source and PARK trace.")

    route = get("route_specs_costs_account_eligibility_and_collateral_recourse")
    timestamp(route.get("evidence_captured_utc"), "route.evidence_captured_utc")
    for key in ("eligibility_evidence", "api_availability", "cross_collateral_and_legal_recourse"):
        evidence = route.get(key)
        if not isinstance(evidence, dict) or set(evidence) != {"verified", "evidence_ref", "scope"} or evidence.get("verified") is not True or not isinstance(evidence.get("scope"), str) or not evidence["scope"] or evidence["scope"].upper() in UNKNOWN:
            fail("VENUE_SCOPE_UNKNOWN", f"route.{key}", "Verified account-specific scope and exact evidence reference required.")
        else:
            reference(evidence["evidence_ref"], f"route.{key}.evidence_ref")
    if route.get("underlying") != "BTC" or route.get("allowed_direction") != "SHORT_HEDGE_ONLY":
        fail("HEDGE_SCOPE", "route", "H1 is bounded BTC short hedge only.")
    if not isinstance(books.get("quote_currency"), str) or route.get("quote_currency") != books.get("quote_currency") or route.get("settlement_currency") != books.get("quote_currency"):
        fail("CURRENCY_MISMATCH", "route", "H1 cash, quote and settlement currencies must match; FX requires a separate explicit contract.")
    if experiment_class == "historical_realistic" and route.get("point_in_time_terms_verified") is not True:
        fail("HISTORICAL_TERMS", "route", "Then-effective historical terms must be verified.")
    for key in ("contract_multiplier", "minimum_quantity", "quantity_increment", "price_increment", "position_limit"):
        number(route.get(key), f"route.{key}", positive=True)
    fees = route.get("maker_taker_fee_schedule")
    if not isinstance(fees, dict) or set(fees) != {"maker_bps", "taker_bps", "as_of_utc", "evidence_ref"}:
        fail("UNKNOWN_COSTS", "route.maker_taker_fee_schedule", "Exact numeric route fee schedule and dated evidence required.")
    else:
        number(fees["maker_bps"], "fees.maker_bps")
        number(fees["taker_bps"], "fees.taker_bps")
        timestamp(fees["as_of_utc"], "fees.as_of_utc")
        reference(fees["evidence_ref"], "fees.evidence_ref")
    execution = get("execution_margin_funding_and_terminal_event_rules")
    if execution.get("funding_mode") != "EMPIRICAL_VERIFIED" or execution.get("financial_performance_claims_allowed") is not True:
        fail("SCENARIO_ONLY_FUNDING", "funding_mode", "H1 performance admission requires verified funding, not illustrative assumptions.")
    funding = route.get("funding_formula_and_series")
    if not isinstance(funding, dict) or set(funding) != {"formula_version", "series_ref", "verified"} or funding.get("verified") is not True or funding.get("series_ref") != execution.get("funding_ref"):
        fail("UNKNOWN_FUNDING", "route.funding_formula_and_series", "Exact verified funding formula and series reference required.")
    else:
        reference(funding["series_ref"], "funding.series_ref")
    if execution.get("funding_ref") != "funding":
        fail("MARKET_INPUT_ROLES", "funding_ref", "Verified funding must bind the causal funding input role.")
    model = get("causal_estimator_and_signal_state_machine")
    for key in ("lookback_bars", "return_interval_seconds", "minimum_observations", "exposure_mark_max_age_seconds", "rebalance_cadence_seconds"):
        value = number(model.get(key), f"model.{key}", positive=True)
        if value is not None and value != value.to_integral_value():
            fail("INVALID_NUMBER", key, "Whole positive count required.")
    for key in ("beta_min", "beta_max", "rebalance_tolerance_quote"):
        number(model.get(key), f"model.{key}")
    lookback = number(model.get("lookback_bars"), "model.lookback_bars", positive=True)
    observations = number(model.get("minimum_observations"), "model.minimum_observations", positive=True)
    beta_min = number(model.get("beta_min"), "model.beta_min")
    beta_max = number(model.get("beta_max"), "model.beta_max")
    if lookback is not None and observations is not None and observations > lookback:
        fail("ESTIMATOR_CONTRACT", "minimum_observations", "Minimum observations cannot exceed lookback bars.")
    if beta_min is not None and beta_max is not None and beta_min > beta_max:
        fail("ESTIMATOR_CONTRACT", "beta_bounds", "Beta lower bound cannot exceed upper bound.")
    triggers = model.get("triggers")
    if not isinstance(triggers, dict) or set(triggers) != set(TRIGGER_FIELDS):
        fail("TRIGGER_CONTRACT", "triggers", "All deterministic trigger and hysteresis fields required.")
    else:
        complete(triggers, "triggers")
        states = ["NORMAL", "RISK_OFF_WATCH", "HEDGE_ACTIVE", "CAPITULATION", "REVERSAL_WATCH", "HEDGE_EXIT"]
        if triggers.get("states") != states:
            fail("TRIGGER_CONTRACT", "triggers.states", "The six declared states in deterministic order are required.")
        expression = triggers.get("signal_expression")
        if not isinstance(expression, dict) or set(expression) != {"version", "expression", "inputs"} or not isinstance(expression.get("expression"), str) or not isinstance(expression.get("version"), str) or not isinstance(expression.get("inputs"), list) or not expression.get("inputs"):
            fail("TRIGGER_CONTRACT", "triggers.signal_expression", "Explicit versioned expression and referenced inputs required.")
        else:
            for ref in expression["inputs"]:
                reference(ref, "triggers.signal_expression.inputs")
                if not isinstance(ref, str) or not isinstance(refs.get(ref), dict) or refs[ref].get("kind") != "market_data":
                    fail("TRIGGER_INPUT_KIND", "triggers.signal_expression.inputs", "Market signal inputs require explicit causal market-data coverage.")
        for key in ("entry_thresholds", "exit_thresholds"):
            thresholds = triggers.get(key)
            if not isinstance(thresholds, dict) or not thresholds:
                fail("TRIGGER_CONTRACT", f"triggers.{key}", "Explicit named numeric thresholds required.")
            else:
                for name, value in thresholds.items():
                    number(value, f"triggers.{key}.{name}", minimum=Decimal("-Infinity"))
        frames = triggers.get("signal_timeframes_seconds")
        if not isinstance(frames, list) or not frames:
            fail("TRIGGER_CONTRACT", "triggers.signal_timeframes_seconds", "Explicit positive timeframe list required.")
        else:
            for frame in frames:
                value = number(frame, "triggers.signal_timeframes_seconds", positive=True)
                if value is not None and value != value.to_integral_value():
                    fail("TRIGGER_CONTRACT", "triggers.signal_timeframes_seconds", "Timeframe seconds must be whole numbers.")
        for key in ("confirmation_bars", "minimum_state_dwell_seconds", "cooldown_seconds"):
            value = number(triggers.get(key), f"triggers.{key}", positive=key == "confirmation_bars")
            if value is not None and value != value.to_integral_value():
                fail("TRIGGER_CONTRACT", f"triggers.{key}", "Counts and seconds must be whole numbers.")
        if triggers.get("entry_thresholds") == triggers.get("exit_thresholds"):
            fail("HYSTERESIS", "triggers", "Entry and exit conditions must differ explicitly.")
        priority = triggers.get("transition_priority")
        if not isinstance(priority, list) or not priority or any(not isinstance(item, str) or not item for item in priority) or len(priority) != len(set(str(item) for item in priority)):
            fail("TRIGGER_PRIORITY", "triggers.transition_priority", "Deterministic unique transition precedence required.")
    success = get("adverse_scenario_matrix_and_numeric_success_bounds")
    for key in ("rebound_drag_bound_quote", "minimum_liquidity_quote", "minimum_liquidation_buffer_fraction", "accounting_tolerance_quote"):
        number(success.get(key), f"success.{key}", positive=True)
    if success.get("all_scenarios_required_for_candidate") is not True:
        fail("SUCCESS_SCOPE", "success", "All declared adverse scenarios must be retained.")
    run = get("run_budget_output_locations_and_abort_rules")
    number(run.get("maximum_seconds_per_arm"), "maximum_seconds_per_arm", positive=True)
    if run.get("incomplete_is_not_complete") is not True:
        fail("INCOMPLETE_RUN", "run_budget", "Incomplete runs cannot be scored as complete.")
    preflight = get("independent_preflight_receipt")
    if preflight.get("independently_reviewed") is not True:
        fail("PREFLIGHT", "independent_preflight_receipt", "Independent contract review required.")
    if preflight.get("contract_body_sha256") != contract_body_sha256(manifest):
        fail("PREFLIGHT_BINDING", "contract_body_sha256", "Independent receipt must bind this exact canonical contract body.")

    # Metadata violations block all artifact reads, including hashing. In
    # particular, a declared sealed-price reference is never opened here.
    if not blockers:
        root = Path(artifact_root).resolve()
        for field, relative, expected_hash in artifact_paths:
            candidate = root / relative
            parts = Path(relative).parts
            if any((root.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
                fail("ARTIFACT_SYMLINK", field, "Symlinked artifacts cannot establish this contract.")
                continue
            try:
                if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
                    raise OSError("not a contained regular file")
                digest = hashlib.sha256()
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected_hash:
                    fail("HASH_MISMATCH", field, "Local artifact bytes differ from the frozen SHA256.")
            except OSError:
                fail("ARTIFACT_UNREADABLE", field, "Exact contained artifact is not readable.")
    return {
        "schema": "spectra-danger-room-gate-result-v1",
        "status": "BLOCKED" if blockers else "VALIDATED_DECLARED_CONTRACT",
        "validation_scope": "DECLARATION_AND_LOCAL_HASH_COHERENCE_ONLY",
        "approved_for_live": False,
        "approved_for_strategy_execution": False,
        "external_facts_verified": False,
        "strategy_executed": False,
        "orders_submitted": False,
        "independent_preflight_still_required": True,
        "blockers": blockers,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        def no_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate JSON key: {key}")
                result[key] = value
            return result
        manifest = json.loads(args.manifest.read_text(), object_pairs_hook=no_duplicate_keys)
        result = validate_run_manifest(manifest, args.artifact_root)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        result = {
            "schema": "spectra-danger-room-gate-result-v1", "status": "BLOCKED",
            "approved_for_live": False, "strategy_executed": False, "orders_submitted": False,
            "blockers": [{"code": "INVALID_JSON_OR_CONTRACT", "field": "$", "message": str(exc)}],
        }
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
