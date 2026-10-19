"""Artificial software fixture, never historical evidence or strategy selection."""
from decimal import Decimal as D
import math
import numpy as np
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_owned_adapter import config
from test_weekly_compounding_adapter import members


def cycle_source():
    warmup_hours=280;hours=36;count=(warmup_hours+hours)*12
    values=np.zeros((count,2,6),dtype=np.float64)
    prices=[]
    for i in range(count+1):
        t=(i-warmup_hours*12)/12
        if t<0:p=10+.04*math.sin(i/25)
        elif t<18:p=10-4*t/18
        else:p=6+4*min(t-18,18)/18
        prices.append(p)
    for i in range(count):
        o,c=prices[i],prices[i+1]
        values[i]=[o,max(o,c)+.01,min(o,c)-.01,c,1000000,100]
    start=3600+warmup_hours*3600
    return CausalTape(values,data_start=3600,warmup_start=3600,start=start,end=start+hours*3600,
        roster=('BTC','ETH'),synthetic=True)


def test_artificial_fall_and_rebound_pipeline(tmp_path):
    cfg=config(('BTC','ETH'));source=cycle_source()
    snapshot=members(cfg)
    from dataclasses import replace
    snapshot=replace(snapshot,effective_until=source.end+3600)
    r=WeeklyReplay(tmp_path/'run',tape=source,config=cfg,membership=snapshot,
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='artificial-software-fixture')
    try:
        for event in source.boundaries():r.step(event)
        result=r.finish()
        assert result['completed']
        assert result['allocation']['conserved']
        assert result['pending_orders']==[]
        assert any(k.startswith('SIGNAL:signal_eligible:') for k in result['counts'])
        # Coverage is reported from actual events, without demanding a profit.
        print('SYNTHETIC_COVERAGE', {k:v for k,v in result['counts'].items() if k in
            {'PSM_SELL_ADMITTED','PSM_REBUILD_ADMITTED','PSM_CYCLE_COMPLETE','REPAIR_ADMITTED','NEXT_ENTRY_ADMITTED'}})
        assert result['counts'].get('REPAIR_ADMITTED',0)>0
    finally:r.close()
