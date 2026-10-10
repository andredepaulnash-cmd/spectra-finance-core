"""Falsification of productive entry sizing and its funding boundaries."""

from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.danger_room.productive_sizing import (
    FROZEN_SIZING_PROTOCOL_SHA256, productive_entry_budget,
)


D = Decimal


def budget(equity="2000", productive="100", pair="100", physical="100"):
    return productive_entry_budget(D(equity), D(productive), D(pair), D(physical))


class ProductiveSizingTests(unittest.TestCase):
    def test_two_thousand_equity_requests_twenty_all_in(self):
        amount, diagnostic = budget()
        self.assertEqual(amount, D("20"))
        self.assertEqual(diagnostic["target_all_in"], "20.00")
        self.assertEqual(diagnostic["limiting_caps"], ["equity_fraction_target"])
        self.assertTrue(diagnostic["admitted"])

    def test_each_remaining_funding_cap_can_bind(self):
        for key in ["productive", "pair", "physical"]:
            with self.subTest(cap=key):
                amount, diagnostic = budget(**{key: "11.2345"})
                self.assertEqual(amount, D("11.2345"))
                self.assertNotEqual(diagnostic["limiting_caps"], ["equity_fraction_target"])

    def test_physical_cash_can_deny_even_with_productive_owner_funds(self):
        amount, diagnostic = budget(productive="1500", pair="100", physical="0")
        self.assertEqual(amount, D("0"))
        self.assertEqual(diagnostic["reason"], "unfunded")
        self.assertEqual(diagnostic["limiting_caps"], ["physical_spendable"])

    def test_productive_owner_cash_cannot_borrow_other_physical_cash(self):
        amount, diagnostic = budget(productive="0", physical="1500")
        self.assertEqual(amount, D("0"))
        self.assertEqual(diagnostic["limiting_caps"], ["productive_available"])

    def test_pair_authorization_exhaustion_denies(self):
        amount, diagnostic = budget(pair="0")
        self.assertEqual(amount, D("0"))
        self.assertEqual(diagnostic["limiting_caps"], ["pair_remaining"])

    def test_subminimum_target_or_any_funding_cap_is_denied(self):
        for key, value in [("equity", "499.999"), ("productive", "4.99999"),
                           ("pair", "4.99"), ("physical", "4.99")]:
            with self.subTest(cap=key):
                amount, diagnostic = budget(**{key: value})
                self.assertEqual(amount, D("0"))
                self.assertEqual(diagnostic["reason"], "below_modeled_all_in_minimum")
                self.assertLess(D(diagnostic["capped_all_in"]), D("5"))

    def test_modeled_five_dollar_boundary_remains_downstream_proposal(self):
        amount, diagnostic = budget(equity="500")
        self.assertEqual(amount, D("5"))
        self.assertTrue(diagnostic["native_and_exchange_admission_required"])
        self.assertLess(amount, D("10"))

    def test_current_marked_losses_reduce_ticket_without_realized_profit_input(self):
        first, _ = budget(equity="2000")
        lower, diagnostic = budget(equity="1500")
        self.assertEqual(first, D("20"))
        self.assertEqual(lower, D("15"))
        self.assertEqual(diagnostic["marked_equity"], "1500")

    def test_sufficient_cash_does_not_override_equity_based_target(self):
        amount, _ = budget(equity="1800", productive="99999", pair="99999", physical="99999")
        self.assertEqual(amount, D("18"))

    def test_no_previous_or_later_call_can_change_a_completed_proposal(self):
        before = budget(equity="2000")
        budget(equity="9000", productive="9000", pair="9000", physical="9000")
        self.assertEqual(budget(equity="2000"), before)
        self.assertEqual(before[0], D("20"))

    def test_exact_percentage_is_independent_of_ambient_decimal_precision(self):
        equity = D("1999.12345678901234567890123456789")
        expected = D("19.9912345678901234567890123456789")
        with localcontext() as context:
            context.prec = 3
            context.clear_flags()
            amount, diagnostic = productive_entry_budget(equity, D("100"), D("100"), D("100"))
            self.assertFalse(context.flags[next(k for k in context.flags if k.__name__ == "Inexact")])
        self.assertEqual(amount, expected)
        self.assertEqual(D(diagnostic["target_all_in"]), expected)

    def test_zero_equity_and_negative_zero_are_denied_without_fictitious_growth(self):
        for equity in ["0", "-0", "0E+5"]:
            with self.subTest(equity=equity):
                amount, diagnostic = budget(equity=equity)
                self.assertEqual(amount, D("0"))
                self.assertEqual(diagnostic["reason"], "zero_marked_equity")
                self.assertEqual(diagnostic["marked_equity"], "0")

    def test_every_money_field_requires_finite_nonnegative_decimal(self):
        valid = [D("2000"), D("100"), D("100"), D("100")]
        for index in range(4):
            for invalid in [D("-1"), D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity")]:
                with self.subTest(index=index, invalid=invalid), self.assertRaises(ValueError):
                    args = valid.copy()
                    args[index] = invalid
                    productive_entry_budget(*args)

    def test_unknown_float_boolean_and_implicit_numeric_inputs_are_rejected(self):
        valid = [D("2000"), D("100"), D("100"), D("100")]
        for index in range(4):
            for invalid in [None, 100.0, float("nan"), float("inf"), 100, True, "100"]:
                with self.subTest(index=index, invalid=invalid), self.assertRaises(TypeError):
                    args = valid.copy()
                    args[index] = invalid
                    productive_entry_budget(*args)

    def test_tied_caps_are_reported_without_exceeding_any(self):
        amount, diagnostic = budget(productive="20", pair="20", physical="20")
        self.assertEqual(amount, D("20"))
        self.assertEqual(len(diagnostic["limiting_caps"]), 4)
        self.assertTrue(all(amount <= D(value) for value in diagnostic["caps_all_in"].values()))

    def test_large_finite_values_remain_exact_without_overflow(self):
        amount, _ = productive_entry_budget(D("1E+999999"), D("1E+999999"),
                                            D("1E+999999"), D("1E+999999"))
        self.assertEqual(amount, D("1E+999997"))

    def test_diagnostics_are_finite_json_and_include_frozen_source_identity(self):
        for args in [{}, {"physical": "0"}, {"equity": "0"}, {"pair": "4.5"}]:
            amount, diagnostic = budget(**args)
            self.assertEqual(D(diagnostic["budget_all_in"]), amount)
            self.assertEqual(diagnostic["protocol_sha256"], FROZEN_SIZING_PROTOCOL_SHA256)
            self.assertEqual(json.loads(json.dumps(diagnostic, allow_nan=False)), diagnostic)

    def test_source_protocol_bytes_match_the_root_freeze(self):
        protocol = Path(__file__).resolve().parents[1] / "research/danger_room/productive_sizing_protocol.json"
        content = protocol.read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), FROZEN_SIZING_PROTOCOL_SHA256)
        declared = json.loads(content)
        self.assertEqual(declared["policy"]["new_entry_all_in_target_fraction_of_completed_marked_equity"], "0.01")
        self.assertEqual(declared["policy"]["minimum_quote"], "5")
        self.assertTrue(declared["policy"]["only_new_entries"])


if __name__ == "__main__":
    unittest.main()
