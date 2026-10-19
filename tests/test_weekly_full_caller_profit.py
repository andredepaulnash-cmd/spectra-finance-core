"""Generated whole-caller positive-profit allocation and real next-entry path."""
from dataclasses import replace
from decimal import Decimal as D
import json,math
import numpy as np
from test_weekly_full_caller_cycle import config, members
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy


def profit_tape():
    warmup=280;duration=8;count=(warmup+duration)*12
    knots=((0.,10.),(1.,9.),(2.,9.),(3.,15.),(4.,15.),(5.,13.),(6.,14.),(8.,14.))
    prices=[]
    for i in range(count+1):
        t=i/12-warmup
        if t<0:p=10+.04*math.sin(i/25)
        else:
            for (ta,pa),(tb,pb) in zip(knots,knots[1:]):
                if ta<=t<=tb:p=pa+(pb-pa)*(t-ta)/(tb-ta);break
        prices.append(p)
    rows=np.zeros((count,1,6),dtype=np.float64)
    for i,(o,c) in enumerate(zip(prices,prices[1:])):
        rows[i,0]=[o,max(o,c)+.01,min(o,c)-.01,c,1000000,100]
    start=3600+warmup*3600
    return CausalTape(rows,data_start=3600,warmup_start=3600,start=start,
        end=start+duration*3600,roster=('BTC',),synthetic=True)


def test_real_caller_profit_is_equal_allocated_then_consumed_by_next_entry(tmp_path):
    tape=profit_tape();cfg=config(('BTC',));membership=replace(members(cfg),effective_until=tape.end+3600)
    r=WeeklyReplay(tmp_path/'run',tape=tape,config=cfg,membership=membership,
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='artificial-positive-profit-caller-v1')
    try:
        for event in tape.boundaries():r.step(event)
        result=r.finish();a=r.a;receipts=a._receipts();allocation=a.allocator.export_research_state()
        sells=[row for row in receipts if row['trade_type']=='SELL' and a.state['orders'][row['order_id']]['kind']=='COHORT_EXIT']
        buys=[row for row in receipts if row['trade_type']=='BUY' and a.state['orders'][row['order_id']]['kind']=='REENTRY'
              and D(a.state['orders'][row['order_id']].get('profit_budget','0'))>0]
        details={'native_sale_receipts':sells,'profit_funded_native_buy_receipts':buys,
                 'allocation_events':[event['receipt'] for event in allocation['events'].values()]}
        (tmp_path/'coverage.json').write_text(json.dumps(details,indent=2,sort_keys=True)+'\n')
        print('PROFIT_CALLER_COUNTS',json.dumps(result['counts'],sort_keys=True))
        print('PROFIT_CALLER_COVERAGE',json.dumps(details,sort_keys=True))
        assert result['completed'] and result['valuation_complete'] and sells
        assert allocation['events'],'Actual positive realized profit not covered'
        for event in allocation['events'].values():
            receipt=event['receipt']
            assert len(receipt['grants'])==2
            assert all(grant['amount']==receipt['equal_share'] for grant in receipt['grants'])
            assert D(receipt['eligible_profit'])>0 and receipt['cash_created']=='0'
        assert buys,'Actual filled next entry did not consume prior settled profit'
        assert result['allocation']['conserved'] and D(result['allocation']['committed_profit_cash'])>0
        assert not result['pending_orders'] and not result['pending_cash_reconciliation']
    finally:r.close()
