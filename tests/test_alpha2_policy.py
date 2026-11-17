from decimal import Decimal
import unittest

from spectra_alpha2.policy import (
    DCA_STEPS, VCAP_STEPS, MAX_OPEN_POSITIONS, MAX_ACTIVE_VCAP_SHARDS_PER_ASSET,
    MAX_ASSET_GROSS_NOTIONAL, VCAP_OUTER_BOUNDARY, VCAPPhase,
    can_admit_position, gross_sell_notional_for_net_target,
    next_eligible_vcap_step, validate_asset_exposure, validate_portfolio_policy,
    vcap_phase_for_drawdown,
)


class Alpha2PolicyTests(unittest.TestCase):
    def test_portfolio_capital_and_per_asset_schedule_reconcile(self):
        validate_portfolio_policy()
        self.assertEqual(sum((x.addition for x in DCA_STEPS), Decimal("0")), Decimal("60"))
        self.assertEqual(sum((x.addition for x in VCAP_STEPS), Decimal("0")), Decimal("920"))
        self.assertEqual(MAX_ASSET_GROSS_NOTIONAL, Decimal("1000"))

    def test_position_target_is_74_and_hard_ceiling_rejects_75th(self):
        self.assertEqual(MAX_OPEN_POSITIONS, 74)
        self.assertTrue(can_admit_position(73))
        self.assertFalse(can_admit_position(74))
        self.assertFalse(can_admit_position(75))

    def test_vcap_ladder_has_exact_thresholds_amounts_and_order(self):
        expected = [
            ("-0.0777", "20", VCAPPhase.FAT_1), ("-0.0974", "40", VCAPPhase.FAT_1),
            ("-0.1074", "80", VCAPPhase.FAT_1), ("-0.1174", "160", VCAPPhase.FAT_1),
            ("-0.1274", "30", VCAPPhase.SKINNY), ("-0.1424", "30", VCAPPhase.SKINNY),
            ("-0.1574", "40", VCAPPhase.SKINNY), ("-0.1724", "40", VCAPPhase.SKINNY),
            ("-0.1924", "160", VCAPPhase.FAT_2), ("-0.2174", "320", VCAPPhase.FAT_2),
        ]
        self.assertEqual([(str(s.drawdown), str(s.addition), s.phase) for s in VCAP_STEPS], expected)
        self.assertEqual(MAX_ACTIVE_VCAP_SHARDS_PER_ASSET, 2)

    def test_vcap_uses_sequential_steps_and_does_not_skip(self):
        self.assertIsNone(next_eligible_vcap_step(Decimal("-0.0776"), set()))
        first = next_eligible_vcap_step(Decimal("-0.15"), set())
        self.assertEqual(first.step_id, "VCAP_FAT1_1")
        completed = {first.step_id}
        second = next_eligible_vcap_step(Decimal("-0.15"), completed)
        self.assertEqual(second.step_id, "VCAP_FAT1_2")

    def test_minus_24_boundary_stops_automatic_vcap(self):
        self.assertIsNone(next_eligible_vcap_step(VCAP_OUTER_BOUNDARY, set()))
        self.assertIsNone(next_eligible_vcap_step(Decimal("-0.25"), set()))
        self.assertEqual(vcap_phase_for_drawdown(VCAP_OUTER_BOUNDARY), VCAPPhase.MANUAL_REVIEW)

    def test_skinny_and_fat2_boundaries_are_exact(self):
        self.assertEqual(vcap_phase_for_drawdown(Decimal("-0.1174")), VCAPPhase.SKINNY)
        self.assertEqual(vcap_phase_for_drawdown(Decimal("-0.1724")), VCAPPhase.FAT_2)
        self.assertEqual(vcap_phase_for_drawdown(Decimal("-0.0777")), VCAPPhase.FAT_1)

    def test_per_asset_exposure_counts_reservations_and_never_exceeds_cap(self):
        self.assertEqual(validate_asset_exposure(Decimal("980"), Decimal("20")), Decimal("1000"))
        with self.assertRaises(ValueError):
            validate_asset_exposure(Decimal("980"), Decimal("20.01"))

    def test_net_take_profit_gross_sale_accounts_for_sell_fee(self):
        gross = gross_sell_notional_for_net_target(Decimal("100.40"))
        net_after_fee = gross * Decimal("0.996")
        self.assertEqual(net_after_fee, Decimal("100.40") * Decimal("1.0174"))


if __name__ == "__main__":
    unittest.main()
