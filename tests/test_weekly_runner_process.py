"""Actual process exit at bounded-runner publication stages; artificial bars only."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from research.weekly_reversal.checkpoint import verify_checkpoint


CHILD = r'''
import os, sys
from pathlib import Path
from research.weekly_reversal import runner
from test_weekly_checkpoint import create, pins
from research.weekly_reversal.checkpoint import sha
base=Path(sys.argv[1]);mode=sys.argv[2]
r=create(base/'run')
assert r.tape.synthetic is True
source_pins=pins()
source_pins[str(Path(runner.__file__).resolve())]=sha(runner.__file__)
if mode=='during_step':
    original=r._mark_completed_bar
    def fail(event):
        original(event)
        os._exit(73)
    r._mark_completed_bar=fail
elif mode=='before_receipt_link':
    def fail(source,target):
        os._exit(74)
    runner.os.link=fail
receipt=runner.run_completed_segment(r,base/'checkpoint',source_pins=source_pins,max_steps=1)
assert receipt['publication_confirmed'] is False
os._exit(75)
'''


@pytest.mark.parametrize('mode,exit_code', [('during_step',73),('before_receipt_link',74),('completed',75)])
def test_process_loss_at_runner_stages(tmp_path,mode,exit_code):
    env=os.environ.copy()
    env['PYTHONPATH']=str(Path(__file__).parent)+os.pathsep+env.get('PYTHONPATH','')
    result=subprocess.run([sys.executable,'-c',CHILD,str(tmp_path),mode],env=env,
                          capture_output=True,text=True,timeout=60)
    assert result.returncode==exit_code,result.stderr
    target=tmp_path/'checkpoint'
    receipt_path=tmp_path/'checkpoint.segment.json'
    temporary=tmp_path/'.incomplete-checkpoint.segment.json'
    if mode=='during_step':
        assert not target.exists() and not receipt_path.exists() and not temporary.exists()
    elif mode=='before_receipt_link':
        assert target.exists() and not receipt_path.exists() and temporary.exists()
        receipt=json.loads(temporary.read_text())
        assert receipt['publication_confirmed'] is False
        verify_checkpoint(target,expected_manifest_sha256=receipt['checkpoint']['manifest_sha256'])
    else:
        assert target.exists() and receipt_path.exists() and not temporary.exists()
        receipt=json.loads(receipt_path.read_text())
        assert receipt['publication_confirmed'] is False
        assert receipt['executed_steps']==1 and receipt['after_steps']==1
        verify_checkpoint(target,expected_manifest_sha256=receipt['checkpoint']['manifest_sha256'])
