from decimal import Decimal
import unittest

from spectra_alpha2.policy import (
    INITIAL_ENTRY_BUDGET, INITIAL_ENTRY_NOTIONAL, INITIAL_ENTRY_POOL_ACTIONS,
    MAX_OPEN_POSITIONS, MAX_ACTIVE_VCAP_SHARDS_PER_ASSET, MAX_ASSET_GROSS_NOTIONAL,
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

    def test_position_target_is_74_and_hard_ceiling_rejects_75th(self):
        self.assertEqual(MAX_OPEN_POSITIONS,74)
        self.assertTrue(can_admit_position(73))
        self.assertFalse(can_admit_position(74))
        self.assertFalse(can_admit_position(75))

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
        self.assertEqual(INITIAL_ENTRY_NOTIONAL,Decimal("20"))
        self.assertEqual(MAX_ASSET_GROSS_NOTIONAL,Decimal("1000"))
        self.assertEqual(validate_asset_exposure(Decimal("980"),Decimal("20")),Decimal("1000"))
        with self.assertRaises(ValueError):
            validate_asset_exposure(Decimal("980"),Decimal("20.01"))


if __name__ == "__main__":
    unittest.main()
