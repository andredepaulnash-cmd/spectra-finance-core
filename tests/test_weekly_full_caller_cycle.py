"""Artificial whole-caller coverage; no manual strategy-entry or ledger mutation.

Not historical evidence and not a parameter search. The fixed path deliberately
contains a decline, a flat sale opportunity, a lower plateau, and recovery.
"""
from pathlib import Path
from dataclasses import replace
import json, math, shutil
import numpy as np
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.replay import WeeklyReplay
from research.weekly_reversal.checkpoint import save_completed_boundary, restore_checkpoint, sha
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_owned_adapter import config
from test_weekly_compounding_adapter import members


def artificial_tape():
    warmup=280; duration=18; count=(warmup+duration)*12
    prices=[]
    knots=((0.,10.),(2.,5.),(2.5,5.),(3.,3.),(6.,3.),(12.,4.4),
           (13.,4.4),(14.,3.8),(17.,5.),(18.,5.))
    for i in range(count+1):
        t=i/12-warmup
        if t<0:p=10+.04*math.sin(i/25)
        else:
            p=knots[-1][1]
            for (ta,pa),(tb,pb) in zip(knots,knots[1:]):
                if ta<=t<=tb:p=pa+(pb-pa)*(t-ta)/(tb-ta);break
        prices.append(p)
    values=np.zeros((count,1,6),dtype=np.float64)
    for i,(o,c) in enumerate(zip(prices,prices[1:])):
        values[i,0]=[o,max(o,c)+.01,min(o,c)-.01,c,1000000,100]
    start=3600+warmup*3600
    return CausalTape(values,data_start=3600,warmup_start=3600,start=start,
        end=start+duration*3600,roster=('BTC',),synthetic=True)


def scoped_pins():
    from research.weekly_reversal import replay,causal_tape,checkpoint,compounding_adapter,entry_admission,owned_cash_reversal
    from research.weekly_reversal.owned_spot import adapter,psm_bridge
    from spectra_core import next_entry_allocator,next_entry_funding_bridge,next_entry_profit_bridge,next_entry_settlement_proof
    return {str(Path(m.__file__).resolve()):sha(m.__file__) for m in
        (replay,causal_tape,checkpoint,compounding_adapter,entry_admission,owned_cash_reversal,
         adapter,psm_bridge,next_entry_allocator,next_entry_funding_bridge,next_entry_profit_bridge,next_entry_settlement_proof)}


def test_artificial_complete_psm_caller_and_cash_wait_restart(tmp_path):
    tape=artificial_tape();cfg=config(('BTC',));membership=replace(members(cfg),effective_until=tape.end+3600)
    root=tmp_path/'run'
    r=WeeklyReplay(root,tape=tape,config=cfg,membership=membership,
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='artificial-full-caller-v1')
    checkpoint=None
    try:
        for event in tape.boundaries():
            r.step(event)
            if checkpoint is None and r.references:
                checkpoint=save_completed_boundary(r,tmp_path/'checkpoint',source_pins=scoped_pins())
        continuous=r.finish();receipts=r.a._receipts();state=r.export_state()
        logs={name:(root/name).read_bytes() for name in ('events.jsonl','marks.jsonl','signals.jsonl')}
        print('COMPLETE_CALLER_COUNTS',json.dumps(continuous['counts'],sort_keys=True))
        print('COMPLETE_CALLER_ECONOMICS',str({k:continuous[k] for k in
            ('actual_fill_count','actual_fees','equity','realized_pnl','unrealized_pnl','allocation')}))
        assert continuous['completed'] and continuous['pending_orders']==[]
        assert continuous['pending_cash_reconciliation']==[]
        assert continuous['counts'].get('PSM_SELL_ADMITTED',0)>0
        assert continuous['counts'].get('PSM_REBUILD_ADMITTED',0)>0
        assert continuous['counts'].get('PSM_CYCLE_COMPLETE',0)>0
        assert checkpoint is not None
    finally:r.close()
    shutil.move(root,tmp_path/'preserved_continuous')
    restore_checkpoint(tmp_path/'checkpoint',root,expected_manifest_sha256=checkpoint['manifest_sha256'])
    restored=WeeklyReplay.resume_restored(root,tape=artificial_tape(),config=cfg,membership=membership,
        protocol_sha256='artificial-full-caller-v1')
    try:
        for event in restored.tape.boundaries():
            if event.at>restored.last_at:restored.step(event)
        result=restored.finish()
        assert restored.a._receipts()==receipts
        assert restored.export_state()==state
        assert {k:v for k,v in result.items() if k!='elapsed_seconds'}=={k:v for k,v in continuous.items() if k!='elapsed_seconds'}
        assert {name:(root/name).read_bytes() for name in logs}==logs
    finally:restored.close()
