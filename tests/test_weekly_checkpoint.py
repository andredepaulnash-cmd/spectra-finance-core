import json
from pathlib import Path
import shutil
import pytest
from research.weekly_reversal.checkpoint import save_completed_boundary,verify_checkpoint,restore_checkpoint,sha
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_replay import source
from test_weekly_owned_adapter import config
from test_weekly_compounding_adapter import members


def pins():
    from research.weekly_reversal import replay,checkpoint
    return {str(Path(m.__file__).resolve()):sha(m.__file__) for m in (replay,checkpoint)}


def create(root):
    cfg=config(('BTC','ETH'))
    return WeeklyReplay(root,tape=source(),config=cfg,membership=members(cfg),
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='synthetic-only')


def test_three_database_restart_is_exact_for_economics_and_four_caller_states(tmp_path):
    root=tmp_path/'run';r=create(root);receipt=None
    try:
        for i,event in enumerate(r.tape.boundaries()):
            r.step(event)
            if i==10:receipt=save_completed_boundary(r,tmp_path/'checkpoint',source_pins=pins())
        continuous=r.finish();actual_receipts=r.a._receipts()
        state=r.export_state()
    finally:r.close()
    before={name:(root/name).read_bytes() for name in ('events.jsonl','marks.jsonl','signals.jsonl')}
    shutil.move(root,tmp_path/'preserved_continuous')
    restore_checkpoint(tmp_path/'checkpoint',root,expected_manifest_sha256=receipt['manifest_sha256'])
    cfg=config(('BTC','ETH'))
    r=WeeklyReplay.resume_restored(root,tape=source(),config=cfg,membership=members(cfg),protocol_sha256='synthetic-only')
    try:
        for event in r.tape.boundaries():
            if event.at>r.last_at:r.step(event)
        resumed=r.finish()
        assert r.a._receipts()==actual_receipts
        assert r.export_state()==state
    finally:r.close()
    for key in continuous:
        if key!='elapsed_seconds':assert resumed[key]==continuous[key],key
    assert {name:(root/name).read_bytes() for name in before}==before


@pytest.mark.parametrize('boundary',['after_three_databases','before_atomic_publication'])
def test_interrupted_snapshot_never_appears_complete(tmp_path,boundary):
    r=create(tmp_path/'run')
    try:
        r.step(r.tape.boundary(r.tape.start))
        def crash(at):
            if at==boundary:raise RuntimeError('synthetic snapshot interruption')
        with pytest.raises(RuntimeError):save_completed_boundary(r,tmp_path/'checkpoint',source_pins=pins(),fault_hook=crash)
        assert not (tmp_path/'checkpoint').exists()
        partial=list(tmp_path.glob('.incomplete-*'))
        assert len(partial)==1
        with pytest.raises(ValueError,match='Incomplete'):
            verify_checkpoint(partial[0],expected_manifest_sha256='not-confirmed')
    finally:r.close()


def test_tampered_database_and_wrong_restore_path_fail_closed(tmp_path):
    r=create(tmp_path/'run')
    try:
        r.step(r.tape.boundary(r.tape.start))
        receipt=save_completed_boundary(r,tmp_path/'checkpoint',source_pins=pins())
    finally:r.close()
    with pytest.raises(ValueError,match='original run directory'):
        restore_checkpoint(tmp_path/'checkpoint',tmp_path/'clone',expected_manifest_sha256=receipt['manifest_sha256'])
    p=tmp_path/'checkpoint/account/allocator.sqlite';p.write_bytes(p.read_bytes()+b'tamper')
    with pytest.raises(ValueError,match='integrity'):
        verify_checkpoint(tmp_path/'checkpoint',expected_manifest_sha256=receipt['manifest_sha256'])
