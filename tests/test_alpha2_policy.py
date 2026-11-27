from decimal import Decimal
import unittest

from spectra_alpha2.policy import (
    INITIAL_ENTRY_BUDGET, INITIAL_ENTRY_NOTIONAL, INITIAL_ENTRY_POOL_ACTIONS,
    MAX_OPEN_POSITIONS, INITIAL_TARGET_POSITIONS, MAX_NEW_POSITIONS_PER_CYCLE,
    MAX_ACTIVE_VCAP_SHARDS_PER_ASSET, MAX_ASSET_GROSS_NOTIONAL,
    PORTFOLIO_EQUITY, VCAP_OUTER_BOUNDARY, VCAP_RECOVERY_POOL,
    NET_TAKE_PROFIT_RATE, gross_sell_notional_for_net_target,
    can_admit_position, validate_asset_exposure, validate_portfolio_policy,
)


class Alpha2PolicyTests(unittest.TestCase):
    def test_portfolio_pools_reconcile_and_entry_actions_share_initial_pool(self):
        validate_portfolio_policy()
        self.assertEqual(PORTFOLIO_EQUITY,Decimal("25000"))
        self.assertEqual(INITIAL_ENTRY_BUDGET,Decimal("6250"))
        self.assertEqual(VCAP_RECOVERY_POOL,Decimal("18750"))
        self.assertEqual(INITIAL_ENTRY_BUDGET+VCAP_RECOVERY_POOL,PORTFOLIO_EQUITY)
        self.assertEqual(INITIAL_ENTRY_POOL_ACTIONS,frozenset({"ENTRY","PRESS","ROTATION"}))

    def test_initial_target_is_50_hard_ceiling_is_74_rejects_75th(self):
        # Phase 2: hard concurrent ceiling 74; initial operating target 50 (not a spending mandate).
        self.assertEqual(MAX_OPEN_POSITIONS,74)
        self.assertEqual(INITIAL_TARGET_POSITIONS,50)
        self.assertEqual(MAX_NEW_POSITIONS_PER_CYCLE,10)
        # Configured target 50 valid; target 74 valid; target 75 rejected (see config tests).
        self.assertTrue(can_admit_position(73))
        self.assertFalse(can_admit_position(74))
        self.assertFalse(can_admit_position(75))
        # 74th concurrent reservation may succeed; 75th is rejected.
        self.assertTrue(can_admit_position(MAX_OPEN_POSITIONS-1))
        self.assertFalse(can_admit_position(MAX_OPEN_POSITIONS))

    def test_vcap_has_two_shard_concurrency_ceiling_but_no_unapproved_ladder(self):
        self.assertEqual(MAX_ACTIVE_VCAP_SHARDS_PER_ASSET,2)
        self.assertEqual(VCAP_OUTER_BOUNDARY,Decimal("-0.24"))

    def test_net_take_profit_is_after_confirmed_buy_and_sell_fee_accounting(self):
        self.assertEqual(NET_TAKE_PROFIT_RATE,Decimal("0.0174"))
        basis=Decimal("100.40")
        gross=gross_sell_notional_for_net_target(basis)
        net=gross*(Decimal("1")-Decimal("0.004"))
        self.assertGreaterEqual(net,basis*Decimal("1.0174"))

    def test_initial_entry_and_per_asset_cap(self):
        self.assertEqual(INITIAL_ENTRY_NOTIONAL,Decimal("30"))
        self.assertEqual(MAX_ASSET_GROSS_NOTIONAL,Decimal("1000"))
        self.assertEqual(validate_asset_exposure(Decimal("970"),Decimal("30")),Decimal("1000"))
        with self.assertRaises(ValueError):
            validate_asset_exposure(Decimal("970"),Decimal("30.01"))


if __name__ == "__main__":
    unittest.main()
