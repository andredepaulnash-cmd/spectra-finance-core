"""Adversarial checks for research boundaries, not manufactured profitability."""
from dataclasses import dataclass
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "research/danger_room/profitability_campaign.py"
spec = importlib.util.spec_from_file_location("profitability_campaign_under_test", PATH)
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.profile = module.RecoveryLadder("explicit-hypothesis", (
            module.RecoveryRung(D("0.1"), D("0.5")),
            module.RecoveryRung(D("0.3"), D("1"))))
        self.book = module.LadderBook({"A-USD": self.profile})
        self.book.confirmed_entry(1, "A-USD", D(100), D(20))

    def test_original_entry_anchor_survives_patch(self):
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(90))[0], D(10))
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=10)
        self.assertIsNone(self.book.requested_budget(1, "A-USD", D(75))[0])
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(70))[0], D(20))

    def test_rejected_or_unfilled_request_does_not_consume(self):
        for _ in range(3):
            self.assertEqual(self.book.requested_budget(1, "A-USD", D(80))[0], D(10))
        self.assertEqual(self.book.parents[1]["consumed"], 0)

    def test_gap_crosses_only_next_rung(self):
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(50))[0], D(10))
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=10)
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(50))[0], D(20))

    def test_unknown_profile_denies(self):
        self.assertEqual(self.book.requested_budget(1, "B-USD", D(50)), (None, "UNKNOWN_LADDER_PROFILE"))

    def test_parent_cannot_be_reused(self):
        with self.assertRaises(ValueError):
            self.book.confirmed_entry(1, "A-USD", D(100), D(20))

    def test_confirmation_requires_quantity_and_cash(self):
        for quantity, cash in [(D(0), D(10)), (D(1), D(0)), (D("NaN"), D(10))]:
            with self.assertRaises(ValueError):
                self.book.confirmed_recovery(1, "A-USD", quantity, cash, order_id=10)

    def test_exhausted_ladder_does_not_reset(self):
        for order_id in range(2):
            self.book.reserve_recovery(order_id, 1, "A-USD", D(10))
            self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=order_id)
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(50)), (None, "LADDER_EXHAUSTED"))

    def test_invalid_order_and_claimed_original_ladder_rejected(self):
        with self.assertRaises(ValueError):
            module.RecoveryLadder("claimed-original", self.profile.rungs, hypothesis=False)
        with self.assertRaises(ValueError):
            module.RecoveryLadder("unordered", tuple(reversed(self.profile.rungs)))
        with self.assertRaises(ValueError):
            module.RecoveryRung(D(1), D(1))

    def test_confirmation_cannot_invent_or_overspend_reservation(self):
        with self.assertRaises(ValueError):
            self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=10)
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        with self.assertRaises(ValueError):
            self.book.confirmed_recovery(1, "A-USD", D(1), D(11), order_id=10)
        self.assertEqual(self.book.parents[1]["consumed"], 0)
        self.book.rejected_order(10)
        self.assertEqual(self.book.parents[1]["consumed"], 0)

    def test_pending_rung_cannot_be_reserved_twice(self):
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        with self.assertRaises(ValueError):
            self.book.reserve_recovery(11, 1, "A-USD", D(10))


class SourceBoundaryTests(unittest.TestCase):
    def test_missing_and_duplicate_boundaries_rejected(self):
        for source in ["none", "mark mark"]:
            with self.assertRaises(ValueError):
                module.replace_once(source, "mark", "replacement")

    def test_passive_observer_has_one_declared_change(self):
        source = "        pending = exit_orders + partial_orders + recovery_orders + entry_orders"
        changed, receipt = module.build_campaign_body(source)
        self.assertEqual(len(receipt), 1)
        self.assertIn("dataset.observe_account(locals())", changed)

    def test_declaration_identity_and_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "protocol.json"
            data = {"research_only": True, "production_activation": False,
                    "holdout_access": False, "arms": [{"name": "control"}]}
            p.write_text(json.dumps(data))
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertEqual(module.validate_declaration(data, expected_sha256=digest, protocol_path=p, arm="control"), {"name": "control"})
            with self.assertRaises(ValueError):
                module.validate_declaration(data, expected_sha256="0" * 64, protocol_path=p, arm="control")
            for field, value in [("research_only", False), ("production_activation", True), ("holdout_access", True)]:
                bad = dict(data, **{field: value})
                with self.assertRaises(ValueError):
                    module.validate_declaration(bad, expected_sha256=digest, protocol_path=p, arm="control")

    def test_duplicate_and_missing_arm_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "protocol.json"
            data = {"research_only": True, "production_activation": False,
                    "holdout_access": False, "arms": [{"name": "control"}, {"name": "control"}]}
            p.write_text(json.dumps(data))
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                module.validate_declaration(data, expected_sha256=digest, protocol_path=p, arm="control")


class CausalGateTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.rows = np.ones((300, 1, 6), dtype="float64") * 10
        self.rows[288, 0, 3] = 11

    def test_directional_anchor_and_future_mutation(self):
        self.assertTrue(module.causal_anchor_allowed(self.rows, 0, 288))
        self.rows[289:] = 1e12
        self.assertTrue(module.causal_anchor_allowed(self.rows, 0, 288))
        self.rows[0, 0, 3] = 12
        self.assertFalse(module.causal_anchor_allowed(self.rows, 0, 288))

    def test_missing_anchor_and_insufficient_history_deny(self):
        self.rows[0, 0, 0] = self.np.nan
        self.assertFalse(module.causal_anchor_allowed(self.rows, 0, 288))
        self.assertFalse(module.causal_anchor_allowed(self.rows, 0, 287))

    def test_recovery_requires_all_actual_bars_and_rising_low(self):
        self.rows[3:6, 0, 2] = 11
        self.rows[5, 0, 3] = 12
        self.assertTrue(module.causal_recovery_allowed(self.rows, 0, 5))
        self.rows[2, 0, 0] = self.np.nan
        self.assertFalse(module.causal_recovery_allowed(self.rows, 0, 5))


if __name__ == "__main__":
    unittest.main()
