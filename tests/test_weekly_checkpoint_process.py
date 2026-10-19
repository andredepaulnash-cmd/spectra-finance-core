"""Real process-loss tests over artificial bars, with no historical execution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

from research.weekly_reversal.checkpoint import restore_checkpoint, verify_checkpoint
from research.weekly_reversal.replay import WeeklyReplay
from test_weekly_checkpoint import create
from test_weekly_replay import source
from test_weekly_owned_adapter import config
from test_weekly_compounding_adapter import members


CHILD = r'''
import json, os, sys
from pathlib import Path
from test_weekly_checkpoint import create, pins
from research.weekly_reversal.checkpoint import save_completed_boundary
base=Path(sys.argv[1]);mode=sys.argv[2]
r=create(base/'run')
for i,event in enumerate(r.tape.boundaries()):
    r.step(event)
    if i == 10:
        def crash(point):
            if point == mode: os._exit(73)
        receipt=save_completed_boundary(r,base/'checkpoint',source_pins=pins(),fault_hook=crash)
        with (base/'receipt.json').open('w') as out:
            out.write(json.dumps(receipt));out.flush();os.fsync(out.fileno())
        os._exit(74)
'''


def crash_child(base, mode):
    env=os.environ.copy()
    env['PYTHONPATH']=str(Path(__file__).parent)+os.pathsep+env.get('PYTHONPATH','')
    result=subprocess.run([sys.executable,'-c',CHILD,str(base),mode],env=env,capture_output=True,text=True,timeout=120)
    assert result.returncode==(74 if mode=='completed' else 73),result.stderr


@pytest.mark.parametrize('mode',['after_three_databases','before_atomic_publication'])
def test_actual_process_loss_during_snapshot_rejects_partial(tmp_path,mode):
    crash_child(tmp_path,mode)
    assert not (tmp_path/'checkpoint').exists()
    assert not (tmp_path/'receipt.json').exists()
    partial=list(tmp_path.glob('.incomplete-*'))
    assert len(partial)==1
    with pytest.raises(ValueError,match='Incomplete'):
        verify_checkpoint(partial[0],expected_manifest_sha256='not-confirmed')


def test_actual_process_exit_after_completed_snapshot_replays_exactly(tmp_path):
    crash_child(tmp_path,'completed')
    receipt=json.loads((tmp_path/'receipt.json').read_text())
    root=tmp_path/'run'
    shutil.move(root,tmp_path/'preserved_interrupted_writer')
    restore_checkpoint(tmp_path/'checkpoint',root,expected_manifest_sha256=receipt['manifest_sha256'])
    cfg=config(('BTC','ETH'))
    r=WeeklyReplay.resume_restored(root,tape=source(),config=cfg,membership=members(cfg),protocol_sha256='synthetic-only')
    try:
        for event in r.tape.boundaries():
            if event.at>r.last_at:r.step(event)
        resumed=r.finish();resumed_receipts=r.a._receipts();resumed_state=r.export_state()
    finally:r.close()
    logs={name:(root/name).read_bytes() for name in ('events.jsonl','marks.jsonl','signals.jsonl')}
    shutil.move(root,tmp_path/'preserved_recovered_writer')
    r=create(root)
    try:
        for event in r.tape.boundaries():r.step(event)
        continuous=r.finish()
        assert r.a._receipts()==resumed_receipts
        assert r.export_state()==resumed_state
    finally:r.close()
    assert {key:value for key,value in continuous.items() if key!='elapsed_seconds'} == {key:value for key,value in resumed.items() if key!='elapsed_seconds'}
    assert logs=={name:(root/name).read_bytes() for name in logs}
