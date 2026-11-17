from decimal import Decimal
import unittest

from spectra_alpha2.policy import VCAPPhase
from spectra_alpha2.strategy import (
    PlannedActionType, calculate_drawdown, next_eligible_dca_step, plan_next_action,
)


class PositionStrategyTests(unittest.TestCase):
    def test_drawdown_uses_immutable_reference_price(self):
        self.assertEqual(calculate_drawdown(Decimal("100"),Decimal("92.23")),Decimal("-0.0777"))

    def test_dca_thresholds_are_sequential(self):
        first=next_eligible_dca_step(Decimal("-0.07"),set())
        self.assertEqual(first.step_id,"DCA_1")
        second=next_eligible_dca_step(Decimal("-0.07"),{"DCA_1"})
        self.assertEqual(second.step_id,"DCA_2")
        third=next_eligible_dca_step(Decimal("-0.07"),{"DCA_1","DCA_2"})
        self.assertEqual(third.step_id,"DCA_3")
        self.assertIsNone(next_eligible_dca_step(Decimal("-0.07"),{"DCA_1","DCA_2","DCA_3"}))

    def test_dca_completes_before_vcap_activation(self):
        action=plan_next_action(
            Decimal("-0.0777"),
            completed_dca_steps={"DCA_1","DCA_2"},
            completed_vcap_steps=set(),
        )
        self.assertEqual(action.action_type,PlannedActionType.DCA)
        self.assertEqual(action.step.step_id,"DCA_3")

    def test_vcap_starts_at_minus_7_77_after_dca(self):
        action=plan_next_action(
            Decimal("-0.0777"),
            completed_dca_steps={"DCA_1","DCA_2","DCA_3"},
            completed_vcap_steps=set(),
        )
        self.assertEqual(action.action_type,PlannedActionType.VCAP)
        self.assertEqual(action.step.step_id,"VCAP_FAT1_1")
        self.assertEqual(action.step.phase,VCAPPhase.FAT_1)

    def test_minus_24_overrides_all_pending_steps_without_reset(self):
        action=plan_next_action(
            Decimal("-0.24"),
            completed_dca_steps=set(),
            completed_vcap_steps=set(),
        )
        self.assertEqual(action.action_type,PlannedActionType.MANUAL_REVIEW)
        self.assertIsNone(action.step)

    def test_no_action_before_next_threshold(self):
        action=plan_next_action(
            Decimal("-0.01"),
            completed_dca_steps=set(),
            completed_vcap_steps=set(),
        )
        self.assertEqual(action.action_type,PlannedActionType.NONE)


if __name__ == "__main__":
    unittest.main()
