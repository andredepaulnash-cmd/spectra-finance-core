import unittest
from decimal import Decimal as D
from dataclasses import replace
from research.danger_room.paper_hedge_sensitivity import Bar, Observation, Scenario, simulate, number


def bar(t, close='100', *, opening=None, high=None, low=None):
    opening=D(opening or close);close=D(close)
    return Bar(t,t+300,opening,D(high) if high else max(opening,close),D(low) if low else min(opening,close),close)


def observations(end=1500, *, exposure='100'):
    return [Observation(t,D('2000')-D(exposure),D(exposure),D('2000')) for t in range(300,end+1,300)]


def scenario(**changes):
    return replace(Scenario(D('1'),D('200'),D('0'),D('0'),D('0')),**changes)


class PaperSensitivityTests(unittest.TestCase):
    def test_cash_settled_short_gain_does_not_credit_sale_proceeds(self):
        bars=[bar(0),bar(300),bar(600),bar(900,'80',opening='100'),bar(1200,'80')]
        result=simulate(bars,observations(),scenario(),keep_path=True)
        self.assertEqual(D(result['gross_short_pnl']),D('20'))
        self.assertEqual(D(result['terminal_hedge_cash_marked']),D('220'))
        self.assertEqual(D(result['terminal_total_equity']),D('2220'))
        self.assertEqual(result['events'][0]['timestamp'],600)
        self.assertEqual(result['events'][0]['signal_timestamp'],300)

    def test_one_full_bar_latency_prevents_same_signal_open_execution(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(),keep_path=True)
        self.assertEqual(result['events'][0]['timestamp']-result['events'][0]['signal_timestamp'],300)
        self.assertEqual(D(result['path'][0]['short_units']),D('0'))
        self.assertEqual(D(result['path'][1]['short_units']),D('0'))
        self.assertEqual(D(result['path'][2]['short_units']),D('1'))

    def test_actual_open_is_execution_price_and_gap_variation_is_once(self):
        bars=[bar(0),bar(300),bar(600,'90',opening='95'),bar(900,'80',opening='85'),bar(1200,'80')]
        obs=observations()
        # First target1 at100 fills95 thenearns15 to80; later targetsrebalancecausally.
        result=simulate(bars,obs,scenario(),keep_path=True)
        self.assertEqual(result['events'][0]['open'],'95')
        self.assertEqual(D(result['path'][2]['hedge_cash_marked']),D('205'))
        self.assertEqual(D(result['path'][3]['hedge_cash_marked']),D('215'))

    def test_fee_and_slippage_charged_on_change_not_entire_position(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(fee_bps=D('10'),slippage_bps=D('5')))
        self.assertEqual(D(result['fees']),D('0.1'))
        self.assertEqual(D(result['slippage']),D('0.05'))
        self.assertEqual(D(result['hedge_net_pnl']),D('-0.15'))
        self.assertEqual(result['rebalance_or_liquidation_fills'],1)

    def test_declared_carry_debits_positive_short_notional_once_per_bar(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(carry_bps_per_day=D('5')))
        with __import__('decimal').localcontext() as ctx:
            ctx.prec=50
            expected=D('100')*D('5')/D('10000')*D('900')/D('86400')
            self.assertAlmostEqual(D(result['carry_cost']),expected,places=40)
        self.assertEqual(D(result['gross_short_pnl']),0)

    def test_equal_extra_reserve_off_arm_matches_spot_exactly(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(ratio=D('0'),fee_bps=D('40'),carry_bps_per_day=D('20')))
        self.assertEqual(D(result['hedge_net_pnl']),0)
        self.assertEqual(D(result['terminal_total_equity']),D('2200'))
        self.assertFalse(result['original_spot_cash_touched'])
        self.assertFalse(result['funded_original_2000_system_proven'])
        self.assertEqual(result['missing_execution_rejections'],0)
        self.assertEqual(result['unknown_signal_price_rejections'],0)
        self.assertFalse(result['terminal_pending_cancelled'])

    def test_reserve_constraint_never_borrows_spot_cash(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(reserve=D('50')))
        self.assertEqual(D(result['terminal_short_units']),0)
        self.assertGreater(result['initial_margin_denied_orders'],0)
        self.assertEqual(D(result['terminal_hedge_cash_marked']),D('50'))

    def test_zero_reserve_denies_original_account_hedge(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(reserve=D('0')))
        self.assertEqual(D(result['hedge_net_pnl']),0)
        self.assertEqual(result['rebalance_or_liquidation_fills'],0)

    def test_intrabar_high_stress_is_used_even_when_close_recovers(self):
        bars=[bar(0),bar(300),bar(600,high='200'),bar(900),bar(1200)]
        result=simulate(bars,observations(),scenario(reserve=D('100')),keep_path=True)
        self.assertEqual(result['stress_liquidations'],1)
        self.assertTrue(result['hedge_halted'])
        self.assertEqual(D(result['terminal_short_units']),0)
        self.assertEqual(D(result['terminal_hedge_cash_marked']),D('-0.2'))
        self.assertEqual(D(result['hedge_net_pnl']),D('-100.2'))
        self.assertEqual(D(result['maximum_intrabar_short_notional']),D('200'))
        self.assertEqual(result['events'][-1]['cover_price'],'200')

    def test_loss_gap_retains_negative_liability_beyond_reserve(self):
        bars=[bar(0),bar(300),bar(600),bar(900,'400'),bar(1200,'400')]
        result=simulate(bars,observations(),scenario(reserve=D('100')))
        self.assertLess(D(result['terminal_hedge_cash_marked']),D('-200'))
        self.assertLess(D(result['hedge_net_pnl']),D('-300'))

    def test_open_gap_breach_liquidates_before_queued_reduction_can_rescue_it(self):
        bars=[bar(0),bar(300),bar(600),bar(900,'400'),bar(1200,'400')]
        obs=[Observation(300,D('1900'),D('100'),D('2000'))]+[Observation(t,D('2000'),D('0'),D('2000')) for t in (600,900,1200,1500)]
        result=simulate(bars,obs,scenario(reserve=D('100')),keep_path=True)
        self.assertEqual(result['stress_liquidations'],1)
        self.assertTrue(result['hedge_halted'])
        self.assertEqual(result['events'][-1]['kind'],'OPEN_STRESS_LIQUIDATION')
        self.assertEqual(result['events'][-1]['timestamp'],900)
        self.assertEqual(D(result['terminal_hedge_cash_marked']),D('-200.4'))
        self.assertEqual(D(result['liquidation_penalties']),D('0.4'))

    def test_missing_execution_bar_rejects_without_stale_delayed_fill(self):
        bars=[bar(0),bar(300),Bar(600,900,None,None,None,None),bar(900),bar(1200)]
        result=simulate(bars,observations(),scenario(),keep_path=True)
        self.assertEqual(result['missing_execution_rejections'],1)
        self.assertEqual(result['events'][0]['kind'],'MISSING_EXECUTION_REJECTED')
        self.assertEqual(result['events'][1]['timestamp'],900)
        self.assertEqual(result['events'][1]['signal_timestamp'],600)

    def test_missing_open_position_interval_discloses_unknown_margin(self):
        bars=[bar(0),bar(300),bar(600),Bar(900,1200,None,None,None,None),bar(1200)]
        result=simulate(bars,observations(),scenario(carry_bps_per_day=D('5')))
        self.assertEqual(result['open_position_missing_btc_intervals'],1)
        self.assertFalse(result['unseen_missing_interval_liquidation_ruled_out'])
        self.assertEqual(result['unknown_signal_price_rejections'],1)
        self.assertGreater(D(result['carry_cost']),0)

    def test_future_price_changes_do_not_change_prior_orders_or_accounting(self):
        a=[bar(t) for t in range(0,1500,300)]
        b=a[:4]+[bar(1200,'120',high='130')]
        ra=simulate(a,observations(),scenario(),keep_path=True)
        rb=simulate(b,observations(),scenario(),keep_path=True)
        self.assertEqual(ra['path'][:4],rb['path'][:4])
        self.assertEqual([e for e in ra['events'] if e['timestamp']<1200],[e for e in rb['events'] if e['timestamp']<1200])

    def test_terminal_position_is_marked_without_invented_close_trade(self):
        result=simulate([bar(t) for t in range(0,1500,300)],observations(),scenario(),keep_path=True)
        self.assertEqual(D(result['terminal_short_units']),D('1'))
        self.assertEqual(len(result['events']),1)
        self.assertTrue(result['terminal_pending_cancelled'])

    def test_hourly_drawdown_uses_total_equity_and_equal_capital(self):
        obs=[Observation(300,D('1900'),D('100'),D('2000')),Observation(1500,D('1800'),D('100'),D('1900'))]
        result=simulate([bar(t) for t in range(0,1500,300)],obs,scenario(ratio=D('0')))
        self.assertAlmostEqual(D(result['hourly_sampled_max_drawdown_fraction']),D('100')/D('2200'))

    def test_inputs_reject_nan_bool_inconsistent_marks_and_missing_geometry(self):
        for value in ('NaN','Infinity',True):
            with self.assertRaises(ValueError):number(value)
        with self.assertRaises(ValueError):Observation(300,D('2000'),D('100'),D('2000')).validate()
        with self.assertRaises(ValueError):bar(0,high='90').validate()
        with self.assertRaises(ValueError):Bar(0,300,None,D('100'),None,None).validate()
        with self.assertRaises(ValueError):simulate([bar(0),bar(600)],observations(900),scenario())

    def test_scenarios_reject_invalid_ratio_rate_margin_and_latency(self):
        for kwargs in ({'ratio':D('1.01')},{'reserve':D('-1')},{'fee_bps':D('NaN')},{'maintenance_margin_fraction':D('1.1')},{'latency_seconds':0},{'latency_seconds':True}):
            with self.assertRaises(ValueError):scenario(**kwargs).validate()

    def test_missing_exact_terminal_sample_is_refused(self):
        with self.assertRaises(ValueError):simulate([bar(t) for t in range(0,1500,300)],observations(1200),scenario())


if __name__=='__main__':unittest.main()
