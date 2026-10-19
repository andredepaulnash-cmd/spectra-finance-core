"""Fixed generated continuation, solely for full caller lifecycle coverage.

No manual cohort selection, ledger mutation, signal injection, altered strategy
parameter or historical input. The original compact fixture stays immutable.
"""
from dataclasses import replace
from decimal import Decimal as D
import json
import numpy as np
from test_weekly_full_caller_cycle import artificial_tape, config, members
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy


def continuation_tape():
    first=artificial_tape()
    knots=((18.,5.),(19.,5.5),(20.,5.1),(22.,5.1),(23.,5.6),(24.,5.2),(26.,5.2))
    prices=[]
    for i in range(8*12+1):
        t=18+i/12
        for (ta,pa),(tb,pb) in zip(knots,knots[1:]):
            if ta<=t<=tb:
                prices.append(pa+(pb-pa)*(t-ta)/(tb-ta));break
    rows=np.zeros((8*12,1,6),dtype=np.float64)
    for i,(o,c) in enumerate(zip(prices,prices[1:])):
        rows[i,0]=[o,max(o,c)+.01,min(o,c)-.01,c,1000000,100]
    return CausalTape(np.concatenate([first.array,rows]),data_start=first.data_start,
        warmup_start=first.warmup_start,start=first.start,end=first.end+8*3600,
        roster=('BTC',),synthetic=True)


def test_actual_successor_profit_allocates_equally_and_funds_next_entry(tmp_path):
    tape=continuation_tape();cfg=config(('BTC',));membership=replace(members(cfg),effective_until=tape.end+3600)
    r=WeeklyReplay(tmp_path/'run',tape=tape,config=cfg,membership=membership,
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='artificial-successor-compounding-v1')
    try:
        for event in tape.boundaries():r.step(event)
        result=r.finish();a=r.a;receipts=a._receipts();allocation=a.allocator.export_research_state()
        successor_sales=[row for row in receipts if row['trade_type']=='SELL'
            and a.state['orders'][row['order_id']]['kind']=='COHORT_EXIT'
            and a.state['cohorts'][a.state['orders'][row['order_id']]['cohort']].get('origin')=='PSM_REBUILD']
        funded_entries=[row for row in receipts if row['trade_type']=='BUY'
            and a.state['orders'][row['order_id']]['kind']=='REENTRY'
            and D(a.state['orders'][row['order_id']].get('profit_budget','0'))>0]
        details={'successor_sale_ids':[row['trade_id'] for row in successor_sales],
                 'funded_entry_ids':[row['trade_id'] for row in funded_entries],
                 'allocation_events':[event['receipt'] for event in allocation['events'].values()]}
        (tmp_path/'coverage.json').write_text(json.dumps(details,indent=2,sort_keys=True)+'\n')
        print('POST_HANDBACK_COUNTS',json.dumps(result['counts'],sort_keys=True))
        print('POST_HANDBACK_COVERAGE',json.dumps(details,sort_keys=True))
        assert result['completed'] and result['valuation_complete']
        assert result['counts'].get('PSM_CYCLE_COMPLETE',0)>0
        assert successor_sales,'Actual caller-driven profitable successor disposal not covered'
        assert allocation['events'],'No actual realized-profit allocation events'
        for event in allocation['events'].values():
            receipt=event['receipt']
            assert len(receipt['grants'])==2  # One BTC allocation and the fixed VCAP allocation.
            assert all(grant['amount']==receipt['equal_share'] for grant in receipt['grants'])
            assert D(receipt['eligible_profit'])>0 and receipt['cash_created']=='0'
        assert funded_entries,'No actual filled next-entry acquisition consumed prior realized-profit cash'
        assert result['allocation']['conserved'] and D(result['allocation']['committed_profit_cash'])>0
        assert not result['pending_orders'] and not result['pending_cash_reconciliation']
    finally:r.close()
