"""Synthetic-only missing-open lifecycle probe of the actual weekly caller."""
from pathlib import Path
import numpy as np
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_replay import source
from test_weekly_owned_adapter import config
from test_weekly_compounding_adapter import members


def test_caller_recovers_or_explicitly_reports_canceled_repair(tmp_path):
    tape=source();cfg=config(('BTC','ETH'))
    first_drop=(tape.start+600-tape.data_start)//300
    tape.array[first_drop:,:,:4]=[9,9.1,8.9,9]
    missing_open=(tape.start+1200-tape.data_start)//300
    tape.array[missing_open,:,0]=np.nan
    r=WeeklyReplay(tmp_path/'run',tape=tape,config=cfg,membership=members(cfg),
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='synthetic-only')
    try:
        for event in tape.boundaries():r.step(event)
        report=r.finish()
        repairs=[o for o in r.a.state['orders'].values() if o['kind']=='REPAIR']
        pending_rungs={c['id']:c['rung'] for c in r.a.state['cohorts'].values() if c['rung'] and c['rung']['status']=='INCOMPLETE'}
        print('CANCELED_REPAIR_CALLER',{'completed':report['completed'],
            'orders':[(o['id'],o['decision_at'],o['status']) for o in repairs],
            'unresolved_rungs':pending_rungs,'counts':report['counts']})
        assert not pending_rungs or any('INCOMPLETE_RUNG' in key for key in report['counts'])
    finally:r.close()
