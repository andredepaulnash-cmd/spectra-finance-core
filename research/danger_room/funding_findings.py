"""Reproduce funding preparation from explicit, already-viewed, SHA-pinned inputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from decimal import Decimal, localcontext
from pathlib import Path

from research.danger_room.funding_feasibility import (
    CALCULATION_PRECISION, RATIOS, FundingScenario, assess_funding,
    decimal_value, reconcile_saved_cash_rows,
)

EXPECTED_SHA256 = {
    "park_rows": "a8f2cdd216c203cef7f6801356ac78e1d1ada5fa751fd3095392d1ce599aca13",
    "june_summary": "405509794840496df929fd73d9b23ca60b1dd203e475f76dc20d8d8cf73c5e4e",
    "july_summary": "c8bcc62a68cf4dcf73ccce6f448acb03f44a73fff5c6c0fba69ebd776a5c25bd",
    "btc_spec": "1dcc9632cc349adb5d738b3a33410b8d99180374c181a7614332fec9f5170a4d",
    "capabilities": "9ee4b2129229aa640343e9a1b4eeaae13bb54b022050753a2af842253f4d345f",
}


def build_findings(paths: dict[str, Path]) -> dict:
    if set(paths) != set(EXPECTED_SHA256):
        raise ValueError("exactly the five pinned preparation sources are required")
    data = {}
    for name, path in paths.items():
        if any("holdout" in part.lower() for part in path.resolve().parts):
            raise ValueError("holdout inputs are forbidden in this preparation")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256[name]:
            raise ValueError(f"{name} differs from the already-viewed frozen input")
        data[name] = raw
    rows = list(csv.DictReader(io.StringIO(data["park_rows"].decode("utf-8"))))
    cash_audit = reconcile_saved_cash_rows(rows)
    if cash_audit["frames_checked"] != 8640 or cash_audit["first_frame_timestamp"] != 1782864300 or cash_audit["last_frame_timestamp"] != 1785456000:
        raise ValueError("saved rows do not match the frozen previously-viewed July window")
    june = json.loads(data["june_summary"])
    july = json.loads(data["july_summary"])
    specs = json.loads(data["btc_spec"], parse_float=Decimal, parse_int=Decimal)
    capabilities = json.loads(data["capabilities"], parse_float=Decimal)
    spec = specs[0]
    if decimal_value(spec["contract_size"]) != Decimal("0.01") or spec["symbol"] != "PBTCUCZ50":
        raise ValueError("frozen BTC contract spec changed")
    side_fee = capabilities["advertised_us_perpetuals"]["per_side_fee_usd_per_contract"]
    liquidation = capabilities["advertised_us_perpetuals"]["liquidation_fee_usd"]["perps_only"]
    if decimal_value(side_fee) != Decimal("0.15") or decimal_value(liquidation) != Decimal("10"):
        raise ValueError("current fee snapshot differs from audited preparation facts")
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        initial = june["ending_account_summary"]
        if decimal_value(initial["cash"]) != decimal_value(rows[0]["cash"]):
            raise ValueError("June checkpoint does not reconcile to first July cash")
        if decimal_value(july["ordinary_cash"]) != decimal_value(rows[-1]["ordinary"]):
            raise ValueError("July endpoint does not reconcile to saved cash")
        fixed_assumptions = dict(
            initial_margin_rate="0.20", maintenance_margin_rate="0.12",
            adverse_rally_fraction="0.25", funding_cost_fraction_bound="0.01",
            entry_fee_per_contract=str(side_fee), exit_fee_per_contract=str(side_fee),
            liquidation_cash_buffer=str(liquidation), fixed_buffer="0",
        )
        scenarios = []
        for spot_label, spot_value in (
            ("SAVED_JUNE_CHECKPOINT_MARKED_SPOT", initial["marked_inventory"]),
            ("SAVED_JULY_ENDPOINT_MARKED_SPOT", july["ending_marked_inventory"]),
            ("CONSTRUCTED_1500_USD_SPOT_EXPOSURE", "1500"),
        ):
            for quote in ("10000", "20000", "60000", "100000"):
                for budget in ("0", "100", "300", "600"):
                    scenario = FundingScenario(btc_quote=quote, **fixed_assumptions)
                    assessments = [assess_funding(spot_value, ratio, budget, scenario) for ratio in RATIOS]
                    e0 = decimal_value(initial["equity"]) + decimal_value(budget) if spot_label.startswith("SAVED_JUNE") else None
                    scenarios.append({
                        "spot_mark_source": spot_label,
                        "spot_mark_value": spot_value,
                        "constructed_btc_quote": quote,
                        "equal_extra_hedge_cash_per_arm": budget,
                        "common_initial_marked_equity_if_june_checkpoint": str(e0) if e0 is not None else None,
                        "capital_scope": "DISTINCT_EQUAL_EXTRA_CAPITAL_EXPERIMENT_NOT_ORIGINAL_BASELINE",
                        "same_extra_cash_in_OFF_control": True,
                        "old_spot_PARK_VCAP_balances_unchanged": True,
                        "assumed_segregation": "CONSTRUCTED_BOOK_ONLY_ACTUAL_VENUE_RECOURSE_UNKNOWN",
                        "arms": assessments,
                    })
        return {
            "schema": "spectra-dangerroom-funding-preparation-v1",
            "research_only": True,
            "approved_for_live": False,
            "input_sha256": EXPECTED_SHA256,
            "input_scopes": {
                "park_rows": "Archived PR26 control_PARK/window_accounts.csv; previously-viewed July; 8640 frames",
                "june_summary": "Published PR34 exact saved June checkpoint accounting",
                "july_summary": "Published PR34 exact saved July endpoint accounting",
                "btc_spec": "Previously-collected public contract metadata; current snapshot, not immutable historical proof",
                "capabilities": "Previously-collected official capability/fee audit; current snapshot, not actual user fee tier",
            },
            "frozen_original_start_cash": initial["cash"],
            "frozen_original_start_park_owned_cash": initial["parked_cash"],
            "frozen_original_start_ordinary_cash": initial["deployable_cash"],
            "frozen_original_start_spot_inventory_mark": initial["marked_inventory"],
            "frozen_original_start_marked_equity": initial["equity"],
            "frozen_original_end_cash": july["ending_cash"],
            "frozen_original_end_park_owned_cash": july["parked_cash"],
            "frozen_original_end_ordinary_cash": july["ordinary_cash"],
            "original_specified_hedge_allocation": "0",
            "original_design_status": "UNFUNDED_HEDGE_BOOK_NO_ALLOCATED_BUDGET",
            "ordinary_cash_is_not_proven_uncommitted_hedge_cash": True,
            "saved_row_cash_audit": cash_audit,
            "current_contract_metadata": {
                "symbol": spec["symbol"], "contract_size_BTC": str(spec["contract_size"]),
                "first_trading_day": spec["first_trading_day"],
                "current_per_side_fee_USD": str(side_fee), "current_perps_liquidation_fee_USD": str(liquidation),
                "actual_user_eligibility_proven": False,
                "actual_historical_initial_maintenance_margin_known": False,
            },
            "unresolved_frozen_historical_design_feasibility": assess_funding(initial["marked_inventory"], "1", "0", FundingScenario()),
            "constructed_common_assumptions": {
                **fixed_assumptions,
                "btc_quotes_are_invented_test_values_not_market_observations": True,
                "IM_MM_rates_are_invented_not_venue_parameters": True,
                "funding_bound_is_invented_aggregate_fraction_not_historical_funding": True,
                "finite_stress_envelope_is_not_unbounded_short_solvency_guarantee": True,
                "reserve_is_not_double_counted_spending_or_economic_loss": True,
            },
            "constructed_scenario_count": len(scenarios),
            "constructed_assessment_count": len(scenarios) * len(RATIOS),
            "constructed_scenarios": scenarios,
            "actual_hedge_performance_run": False,
            "new_item3_data_acquisition": False,
            "holdout_market_payloads_read": False,
            "production_modified": False,
            "limits": [
                "No statement that all January-June or fresh-account designs are unfundable: the January account started with $2000 cash.",
                "The minimum ordinary row is only a necessary bound for a constant carveout while preserving saved spot rows; event commitments remain unverified.",
                "A funded extra-capital experiment is a different experiment with matched E0 and cash; it cannot inherit the original account's performance claim.",
                "Internal books do not isolate venue collateral. Actual Kraken same-account recourse must be modeled before any actual feasibility approval.",
                "Saved asset values contain stale historical marks, not executable liquidation or BTC prices.",
                "No net profit, beta effectiveness, short eligibility, release readiness, or actual hedge strategy validation is claimed.",
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in EXPECTED_SHA256:
        parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    findings = build_findings({name: getattr(args, name) for name in EXPECTED_SHA256})
    args.output.write_text(json.dumps(findings, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
