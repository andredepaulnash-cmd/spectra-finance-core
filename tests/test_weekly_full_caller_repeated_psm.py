"""One-hour flat suffix after the original second reversal lands at its end.

The original fixture is preserved unchanged; no strategy parameter is altered.
"""
from dataclasses import replace
import hashlib,json
import numpy as np
from test_weekly_full_caller_cycle import artificial_tape, config, members
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy


def original_repeated_tape():
    first=artificial_tape()
    knots=((18.,5.),(20.,2.5),(20.5,2.5),(21.,1.5),(24.,1.5),(30.,2.1),(31.,2.1))
    prices=[]
    for i in range(13*12+1):
        t=18+i/12
        for (ta,pa),(tb,pb) in zip(knots,knots[1:]):
            if ta<=t<=tb:
                prices.append(pa+(pb-pa)*(t-ta)/(tb-ta));break
    rows=np.zeros((13*12,1,6),dtype=np.float64)
    for i,(o,c) in enumerate(zip(prices,prices[1:])):
        rows[i,0]=[o,max(o,c)+.01,min(o,c)-.01,c,1000000,100]
    return CausalTape(np.concatenate([first.array,rows]),data_start=first.data_start,
        warmup_start=first.warmup_start,start=first.start,end=first.end+13*3600,
        roster=('BTC',),synthetic=True)


def repeated_tape():
    first=original_repeated_tape()
    rows=np.tile(np.array([2.1,2.11,2.09,2.1,1000000,100],dtype=np.float64),(12,1,1))
    return CausalTape(np.concatenate([first.array,rows]),data_start=first.data_start,
        warmup_start=first.warmup_start,start=first.start,end=first.end+3600,
        roster=("BTC",),synthetic=True)

def test_two_actual_caller_psm_episodes_complete_with_causal_flat_tail(tmp_path):
    tape=repeated_tape();cfg=config(('BTC',));membership=replace(members(cfg),effective_until=tape.end+3600)
    r=WeeklyReplay(tmp_path/'run',tape=tape,config=cfg,membership=membership,
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='artificial-repeated-psm-flat-tail-v1')
    first_archive=None
    try:
        for event in tape.boundaries():
            r.step(event)
            meta=r.a.state['psm'].get('BTC-USD')
            if meta and meta.get('archived_episodes') and first_archive is None:
                first_archive=json.dumps(meta['archived_episodes'][0],sort_keys=True,separators=(',',':'))
        result=r.finish();archives=r.a.state['psm']['BTC-USD']['archived_episodes']
        print('REPEATED_COUNTS',json.dumps(result['counts'],sort_keys=True))
        print('REPEATED_EPISODES',json.dumps(result['psm_cycles'],sort_keys=True,default=str))
        assert result['completed'] and result['valuation_complete']
        assert result['counts'].get('PSM_CYCLE_COMPLETE',0)>=2,'Two actual complete caller episodes not covered'
        assert len(archives)>=2 and len({item['position'] for item in archives})==len(archives)
        assert json.dumps(archives[0],sort_keys=True,separators=(',',':'))==first_archive
        assert all(hashlib.sha256(item['journal'].encode()).hexdigest()==item['journal_sha256'] for item in archives)
        assert not result['pending_orders'] and not result['pending_cash_reconciliation'] and not result['pending_psm_actions']
    finally:r.close()
