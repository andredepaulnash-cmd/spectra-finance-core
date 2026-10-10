"""Constructed reserve and ownership tests; no prices or exchange access."""

import unittest
import tempfile
from pathlib import Path
from decimal import Decimal, localcontext

from research.danger_room.funding_feasibility import (
    CashOwners, FundingScenario, allocate_equal_extra_cash, assess_funding,
    decimal_value, whole_contract_target,
    reconcile_saved_cash_rows,
)
from research.danger_room.funding_findings import EXPECTED_SHA256, build_findings


def constructed(**overrides):
    values = dict(btc_quote="60000", initial_margin_rate="0.20", maintenance_margin_rate="0.12",
                  adverse_rally_fraction="0.25", funding_cost_fraction_bound="0.01",
                  entry_fee_per_contract="0.15", exit_fee_per_contract="0.15",
                  liquidation_cash_buffer="10", fixed_buffer="0")
    values.update(overrides)
    return FundingScenario(**values)


class FundingFeasibilityTests(unittest.TestCase):
    def test_rejects_float_bool_nonfinite_and_magnitude(self):
        for value in (0.1, True, False, "NaN", "Infinity", "-Infinity", "1e51", "0e-1000000", "0.123" * 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal_value(value)

    def test_exact_source_precision_survives_host_decimal_context(self):
        source = "1.541959146344605609051889"
        with localcontext() as context:
            context.prec = 5
            self.assertEqual(str(decimal_value(source)), source)
            n = whole_contract_target("1200.00000000000000000000000001", "1", "60000")
        self.assertEqual(n, 2)

    def test_whole_contract_threshold_and_protection(self):
        self.assertEqual(whole_contract_target("599.99999999", "1", "60000"), 0)
        self.assertEqual(whole_contract_target("600", "1", "60000"), 1)
        self.assertEqual(whole_contract_target("1500", ".25", "60000"), 0)
        self.assertEqual(whole_contract_target("1500", ".5", "60000"), 1)
        self.assertEqual(whole_contract_target("1500", ".75", "60000"), 1)
        self.assertEqual(whole_contract_target("1500", "1", "60000"), 2)

    def test_economic_cap_prevents_round_up_overhedge(self):
        self.assertEqual(whole_contract_target("2000", "1", "60000", economic_notional_cap="599"), 0)
        self.assertEqual(whole_contract_target("2000", "1", "60000", economic_notional_cap="1200"), 2)
        self.assertEqual(whole_contract_target("2000", "1", "60000", economic_notional_cap="9999"), 3)

    def test_invalid_quantity_parameters(self):
        for args in (("-1", "1", "60000"), ("10", "-1", "60000"), ("10", "1.01", "60000"),
                     ("10", "1", "0"), ("10", "1", "60000", "0")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                whole_contract_target(*args)

    def test_missing_margin_price_and_funding_stay_unknown(self):
        result = assess_funding("1500", "1", "100000", FundingScenario())
        self.assertEqual(result["status"], "BLOCKED_UNKNOWN_INPUTS")
        self.assertIsNone(result["target_contracts"])
        self.assertEqual(result["admitted_contracts"], 0)
        self.assertIn("initial_margin_rate", result["unknown_parameters"])

    def test_off_needs_no_unknown_exchange_inputs_and_keeps_reserve(self):
        result = assess_funding("1500", "0", "300", FundingScenario())
        self.assertEqual(result["status"], "OFF")
        self.assertEqual(result["remaining_unreserved_cash"], "300")

    def test_minimum_trade_is_not_enabled_by_large_cash(self):
        result = assess_funding("195.7626187038313546713693151", "1", "100000", constructed())
        self.assertEqual(result["status"], "BELOW_ONE_CONTRACT")
        self.assertEqual(result["admitted_contracts"], 0)

    def test_exact_reserve_and_one_dollar_short(self):
        result = assess_funding("1500", "1", "287.80", constructed())
        self.assertEqual(result["admitted_contracts"], 1)
        self.assertEqual(result["per_contract_reserve"], "277.800000")
        self.assertEqual(Decimal(result["total_cash_reserved"]), Decimal("287.8"))
        self.assertEqual(Decimal(result["remaining_unreserved_cash"]), 0)
        fail = assess_funding("1500", "1", "286.80", constructed())
        self.assertEqual(fail["admitted_contracts"], 0)

    def test_all_component_buffers_are_reserved_once(self):
        result = assess_funding("1500", "1", "565.6", constructed())
        self.assertEqual(result["admitted_contracts"], 2)
        self.assertEqual(Decimal(result["total_cash_reserved"]), Decimal("565.6"))
        self.assertEqual(Decimal(result["once_only_buffers"]), Decimal("10"))
        self.assertEqual(result["status"], "CONDITIONAL_TARGET_FUNDED")

    def test_stressed_maintenance_dominates_when_needed(self):
        result = assess_funding("1500", "1", "10000", constructed(adverse_rally_fraction="2"))
        parts = result["reserve_components_per_contract"]
        self.assertEqual(Decimal(parts["selected_margin_reserve"]), Decimal("216"))
        self.assertEqual(Decimal(parts["adverse_short_loss"]), Decimal("1200"))

    def test_invalid_margin_and_costs_rejected_even_when_off(self):
        for scenario in (constructed(initial_margin_rate="0"), constructed(maintenance_margin_rate="0.3"),
                         constructed(entry_fee_per_contract="-0.1"), constructed(funding_cost_fraction_bound="1.1")):
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                assess_funding("1500", "0", "300", scenario)

    def test_venue_quantity_cap_is_distinct_from_financial_capacity(self):
        result = assess_funding("6000000", "1", "9999999", constructed(), max_contracts=2)
        self.assertEqual(result["target_contracts"], 10000)
        self.assertEqual(result["admitted_contracts"], 2)
        self.assertEqual(result["venue_capped_contracts"], 2)
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess_funding("1500", "1", "1000", constructed(), max_contracts=value)

    def test_partial_and_no_funding_conserve_cash(self):
        for budget in ("0", "9.99", "100", "300", "600"):
            result = assess_funding("1500", "1", budget, constructed())
            self.assertLessEqual(Decimal(result["total_cash_reserved"]), Decimal(budget))
            self.assertEqual(Decimal(result["total_cash_reserved"]) + Decimal(result["remaining_unreserved_cash"]), Decimal(budget))
            self.assertFalse(result["actual_historical_feasibility_proven"])
            self.assertFalse(result["approved_for_live"])

    def test_disjoint_owner_reconciliation_rejects_minted_cash(self):
        good = CashOwners("2000", "1000", "500", "500", "0")
        self.assertEqual(good.validated()["physical_cash"], Decimal("2000"))
        for owners in (CashOwners("2000", "1000", "500", "500", "100"),
                       CashOwners("2000", "1000", "500", "-1", "501")):
            with self.subTest(owners=owners), self.assertRaises(ValueError):
                owners.validated()

    def test_equal_extra_cash_does_not_reclassify_protected_owners(self):
        owners = CashOwners("2000", "1000", "500", "500", "0")
        arms = allocate_equal_extra_cash(owners, "300")
        self.assertEqual(len(arms), 5)
        for arm in arms:
            self.assertEqual(arm["physical_cash"], "2300")
            self.assertEqual(arm["park_owned"], "1000")
            self.assertEqual(arm["vcap_owned"], "500")
            self.assertEqual(arm["ordinary_owned"], "500")
            self.assertEqual(arm["hedge_owned"], "300")
            self.assertEqual(arm["extra_deposit"], "300")

    def test_existing_hedge_owner_rejects_silent_budget_substitution(self):
        with self.assertRaises(ValueError):
            allocate_equal_extra_cash(CashOwners("2000", "1000", "500", "400", "100"), "300")

    def test_row_bound_is_necessary_and_does_not_admit_unknown_event_cash(self):
        rows = [dict(timestamp="1000", cash="101", parked="100", ordinary="1", marked_inventory="20", equity="121"),
                dict(timestamp="1300", cash="100.0000001", parked="100", ordinary="0.0000001", marked_inventory="21", equity="121.0000001")]
        result = reconcile_saved_cash_rows(rows)
        self.assertEqual(result["minimum_ordinary_cash"], "1E-7")
        self.assertEqual(result["USD_cent_floor_of_static_row_upper_bound"], "0")
        self.assertEqual(result["trace_cash_admission_status"], "BLOCKED_TRACE_CASH_UNKNOWN")
        self.assertIsNone(result["admitted_historical_hedge_budget"])

    def test_row_reconciliation_and_continuity_reject_minted_missing_cash(self):
        row = dict(timestamp="1000", cash="101", parked="100", ordinary="1", marked_inventory="20", equity="121")
        for rows in ([], [{**row, "cash": "102"}], [{**row, "equity": "122"}],
                     [row, {**row, "timestamp": "1600"}], [{**row, "timestamp": True}],
                     [{key: value for key, value in row.items() if key != "ordinary"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                reconcile_saved_cash_rows(rows)

    def test_reproducer_rejects_altered_source_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "untrusted.json"
            path.write_text('{"profit": "fake"}')
            with self.assertRaisesRegex(ValueError, "already-viewed frozen input"):
                build_findings({name: path for name in EXPECTED_SHA256})

    def test_reproducer_rejects_sealed_alias_before_opening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout" / "missing_sealed_payload.json"
            alias = Path(directory) / "innocent_name.json"
            alias.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "holdout inputs are forbidden"):
                build_findings({name: alias for name in EXPECTED_SHA256})


if __name__ == "__main__":
    unittest.main()
