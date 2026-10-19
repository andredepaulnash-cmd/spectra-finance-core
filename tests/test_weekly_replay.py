from dataclasses import replace
from decimal import Decimal as D
import numpy as np
import pytest
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_owned_adapter import config
from test_weekly_compounding_adapter import members


def source():
    values=np.zeros((60,2,6),dtype=np.float64)
    values[:]=[10,10.1,9.9,10,1000000,100]
    return CausalTape(values,data_start=3600,warmup_start=3600,start=10800,end=21600,
        roster=('BTC','ETH'),synthetic=True)


def test_full_small_synthetic_native_pipeline(tmp_path):
    cfg=config(('BTC','ETH'));r=WeeklyReplay(tmp_path/'run',tape=source(),config=cfg,membership=members(cfg),
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='synthetic-only')
    try:
        for event in r.tape.boundaries():r.step(event)
        result=r.finish()
        assert result['completed'] and result['synthetic']
        assert result['actual_fill_count']==8
        assert result['actual_fees']>0
        assert result['total_pnl']<0
        assert result['pending_orders']==[] and result['pending_cash_reconciliation']==[]
        assert result['allocation']['event_count']==0
        assert r.steps==37
    finally:r.close()


def test_no_history_without_confirmed_admission(tmp_path):
    tape=source();tape.synthetic=False;cfg=config(('BTC','ETH'))
    with pytest.raises(ValueError,match='Confirmed historical'):
        WeeklyReplay(tmp_path/'run',tape=tape,config=cfg,membership=members(cfg),
            signal_policy=SignalPolicy(enabled=True),protocol_sha256='no-fence')
    assert not (tmp_path/'run').exists()


def test_synthetic_loose_and_control_same_seed_when_no_signal_ready(tmp_path):
    cfg=config(('BTC','ETH'));reports=[];seeds=[]
    for threshold in (50,20):
        r=WeeklyReplay(tmp_path/str(threshold),tape=source(),config=cfg,membership=members(cfg),
            signal_policy=SignalPolicy(enabled=True,bbwp_minimum=threshold),protocol_sha256='synthetic-only')
        try:
            for event in r.tape.boundaries():r.step(event)
            report=r.finish();reports.append(report['equity']);seeds.append(r.a._receipts())
        finally:r.close()
    assert reports[0]==reports[1] and seeds[0]==seeds[1]
