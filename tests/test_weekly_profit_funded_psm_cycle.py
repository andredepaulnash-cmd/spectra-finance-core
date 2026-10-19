"""Synthetic native integration probe using unchanged frozen capital limits."""
from pathlib import Path
from decimal import Decimal as D
from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.model import CompletedBar, ObservedOpen, CapitalBlocked
from test_weekly_owned_adapter import config, seed
from test_weekly_compounding_adapter import members, profitable_sale, reentry_boundary


def test_native_psm_after_actual_profit_funded_entries(tmp_path):
    cfg=config(('BTC',));pair='BTC-USD'
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=1000,membership=members(cfg),enabled=True) as a:
        def mark(at,price):
            a.observe(CompletedBar(pair,at-300,at,D(price),D('1000000'),at),now=at)
            a.refresh_accounting_all(now=at)
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        entry=a.request_reentry(pair+':seed:01',now=36000)
        mark(36300,'11');a.observe_open(entry['id'],ObservedOpen(D('11'),36300,36300))
        a.refresh_accounting_all(now=36300);a.sync_profit_cash(now=36300)
        mark(36600,'11');a.sync_profit_cash(now=36600)
        at=36900;blocked=False
        for rung in range(1,8):
            mark(at,'1')
            evidence=a.repair_feasibility(pair,rung=rung,now=at)
            print('REPAIR',rung,evidence['reason_code'],evidence['target'])
            try:order=a.request_repair(pair,rung=rung,now=at)
            except CapitalBlocked:
                blocked=True;break
            mark(at+300,'1');a.observe_open(order['id'],ObservedOpen(D('1'),at+300,at+300))
            a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300)
            mark(at+600,'1');a.sync_profit_cash(now=at+600)
            at+=900
        assert blocked
        a.psm.start(pair,decision_ref='synthetic-due-capital-block',falling_knife=True,
            repair_eligible=False,policy_ref='synthetic-only',now=at)
        a.psm.request_sell(pair,now=at)
        mark(at+300,'1');a.psm.observe_open(pair,ObservedOpen(D('1'),at+300,at+300))
        a.refresh_accounting_all(now=at+300)
        print('ACTUAL_PSM_AFTER_COMPOUNDING',{'reentry_pool':entry['pool'],
            'psm_sale_fills':sum(a.state['orders'][r['order_id']]['kind']=='PSM_EXIT' for r in a._receipts()),
            'current_capital':str(a.capital_commitments())})
        a.sync_profit_cash(now=at+300)
        assert a.profit_bridge.cash_projection().new_spending_allowed
