"""Atomic completed-boundary paper snapshots, including all three SQLite stores.

No checkpoint restores itself or resumes historical work. A coordinator must
confirm source/registry authority, preserve any uncertain attempt and hold the
exclusive writer. Partial directories or mismatched manifests are never accepted.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile

from .replay import dumps


FILES=('account/paper.sqlite','account/native.sqlite','account/allocator.sqlite',
       'caller.json','events.jsonl','marks.jsonl','signals.jsonl')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def _sync(path):
    fd=os.open(str(path),os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def verify_source_pins(pins):
    if not isinstance(pins,dict) or not pins:raise ValueError('Explicit nonempty source pins required')
    for path,expected in pins.items():
        if not Path(path).is_file() or sha(path)!=expected:
            raise ValueError('Source identity changed: '+path)


def save_completed_boundary(replay,destination,*,source_pins,fault_hook=None):
    if replay.last_at is None or replay.a.now!=replay.last_at:
        raise ValueError('Only an entirely completed caller boundary may be snapshotted')
    if replay.a.lock.closed:raise ValueError('Exclusive account writer is not held')
    verify_source_pins(source_pins)
    replay.a.audit(require_marks=False)
    replay.a.allocator.audit()
    replay.a.profit_bridge.cash_projection()
    target=Path(destination)
    if target.exists():raise ValueError('Checkpoint destination is immutable')
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix='.incomplete-',dir=target.parent))
    (temp/'account').mkdir()
    for stream in (replay.events,replay.marks,replay.signal_log):
        stream.flush();os.fsync(stream.fileno())
    for name in ('paper.sqlite','native.sqlite','allocator.sqlite'):
        with sqlite3.connect(f'file:{replay.a.root/name}?mode=ro',uri=True) as source,sqlite3.connect(temp/'account'/name) as out:
            source.backup(out)
            if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity failure')
        _sync(temp/'account'/name)
    if fault_hook:fault_hook('after_three_databases')
    state=replay.export_state()
    (temp/'caller.json').write_text(dumps(state)+'\n')
    for name in ('events.jsonl','marks.jsonl','signals.jsonl'):
        shutil.copyfile(replay.root/name,temp/name)
    manifest={'schema':2,'state':'COMPLETED_BOUNDARY','run_directory':str(replay.root.resolve()),
        'at':replay.last_at,'steps':replay.steps,'protocol_sha256':replay.protocol_sha256,
        'source_pins':source_pins,'synthetic':replay.tape.synthetic,'input_identity':replay.tape.input_identity,
        'files':{name:{'bytes':(temp/name).stat().st_size,'sha256':sha(temp/name)} for name in FILES}}
    for name in FILES:_sync(temp/name)
    (temp/'manifest.json').write_text(dumps(manifest)+'\n');_sync(temp/'manifest.json');_sync(temp/'account');_sync(temp)
    if fault_hook:fault_hook('before_atomic_publication')
    os.rename(temp,target);_sync(target.parent)
    return {'path':str(target),'manifest_sha256':sha(target/'manifest.json'),'at':replay.last_at,'steps':replay.steps}


def verify_checkpoint(directory,*,expected_manifest_sha256):
    p=Path(directory)
    if p.name.startswith('.incomplete-') or not (p/'manifest.json').is_file():raise ValueError('Incomplete snapshot cannot be accepted')
    if sha(p/'manifest.json')!=expected_manifest_sha256:raise ValueError('Checkpoint manifest identity differs')
    raw=json.loads((p/'manifest.json').read_text())
    if raw.get('schema')!=2 or raw.get('state')!='COMPLETED_BOUNDARY' or set(raw.get('files',{}))!=set(FILES):
        raise ValueError('Unsupported or incomplete checkpoint manifest')
    verify_source_pins(raw['source_pins'])
    for name,record in raw['files'].items():
        f=p/name
        if not f.is_file() or f.stat().st_size!=record['bytes'] or sha(f)!=record['sha256']:
            raise ValueError('Checkpoint file integrity differs: '+name)
    for name in ('paper.sqlite','native.sqlite','allocator.sqlite'):
        with sqlite3.connect(f'file:{p/"account"/name}?mode=ro',uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Checkpoint SQLite integrity failure')
    caller=json.loads((p/'caller.json').read_text())
    if not raw.get('input_identity') or caller.get('input_identity')!=raw['input_identity']:
        raise ValueError('Caller input identity differs from checkpoint manifest')
    if caller['last_at']!=raw['at'] or caller['steps']!=raw['steps'] or caller['protocol_sha256']!=raw['protocol_sha256']:
        raise ValueError('Caller cursor is not the published database boundary')
    with sqlite3.connect(f'file:{p/"account/paper.sqlite"}?mode=ro',uri=True) as db:
        state=json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])
    if state['now']!=raw['at']:raise ValueError('Paper clock differs from completed cursor')
    return raw


def restore_checkpoint(directory,destination,*,expected_manifest_sha256,confirmed_recovery=None):
    raw=verify_checkpoint(directory,expected_manifest_sha256=expected_manifest_sha256)
    target=Path(destination)
    if str(target.resolve())!=raw['run_directory']:
        raise ValueError('Pinned native path identity requires the original run directory')
    if target.exists():raise ValueError('Restore destination must be absent; preserve prior attempt first')
    if not raw['synthetic']:
        if (not confirmed_recovery or confirmed_recovery.get('confirmed') is not True
                or confirmed_recovery.get('manifest_sha256')!=expected_manifest_sha256
                or not confirmed_recovery.get('attempt_id') or not confirmed_recovery.get('registry_fence')):
            raise ValueError('Fresh confirmed recovery authority required before historical restoration')
    target.mkdir(parents=True)
    (target/'account').mkdir()
    for name in FILES:shutil.copyfile(Path(directory)/name,target/name)
    return raw
