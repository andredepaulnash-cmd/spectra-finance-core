"""Independent synthetic-only review; no historical loaders or input files."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from research.weekly_reversal import runner
from research.weekly_reversal.checkpoint import restore_checkpoint, verify_checkpoint, sha
from research.weekly_reversal.replay import WeeklyReplay
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_checkpoint import create, pins as base_pins
from test_weekly_compounding_adapter import members
from test_weekly_owned_adapter import config
from test_weekly_replay import source


@pytest.fixture
def pins():
    value = base_pins()
    value[str(Path(runner.__file__).resolve())] = sha(runner.__file__)
    return value


@contextmanager
def replay_at(path, tape=None):
    if tape is None:
        r = create(path)
    else:
        cfg = config(('BTC', 'ETH'))
        r = WeeklyReplay(path, tape=tape, config=cfg, membership=members(cfg),
                         signal_policy=SignalPolicy(enabled=True, bbwp_minimum=20),
                         protocol_sha256='synthetic-only')
    try:
        assert r.tape.synthetic is True
        yield r
    finally:
        r.close()


def check_receipt(receipt, target, r):
    assert receipt['publication_confirmed'] is False
    assert receipt['synthetic'] is True
    assert receipt['protocol_sha256'] == r.protocol_sha256
    assert receipt['input_identity'] == r.tape.input_identity
    assert json.loads(target.with_name(target.name + '.segment.json').read_text()) == receipt
    manifest = verify_checkpoint(target, expected_manifest_sha256=receipt['checkpoint']['manifest_sha256'])
    assert manifest['at'] == receipt['after_at'] == r.last_at
    assert manifest['steps'] == receipt['after_steps'] == r.steps
    assert receipt['executed_steps'] == receipt['after_steps'] - receipt['before_steps']
    return manifest


@pytest.mark.parametrize('steps', [1, 2, 11, 36])
def test_exact_finite_step_boundaries(tmp_path, pins, steps):
    target = tmp_path / 'checkpoint'
    with replay_at(tmp_path / 'run') as r:
        receipt = runner.run_completed_segment(r, target, source_pins=pins, max_steps=steps)
        assert receipt['before_at'] is None and receipt['before_steps'] == 0
        assert receipt['after_at'] == r.tape.start + (steps - 1) * 300
        assert receipt['executed_steps'] == steps
        assert receipt['stop_reason'] == 'STEP_BOUNDARY'
        assert receipt['state'] == 'BOUNDARY_LOCAL'
        assert receipt['result_sha256'] is None
        assert not (r.root / 'result.json').exists()
        check_receipt(receipt, target, r)


@pytest.mark.parametrize('steps', [37, 38, 1000])
def test_terminal_stops_exactly_once_and_hashes_result(tmp_path, pins, steps):
    target = tmp_path / 'terminal'
    with replay_at(tmp_path / 'run') as r:
        receipt = runner.run_completed_segment(r, target, source_pins=pins, max_steps=steps)
        assert receipt['executed_steps'] == 37
        assert receipt['stop_reason'] == 'WINDOW_COMPLETE'
        assert receipt['state'] == 'TERMINAL_LOCAL'
        assert receipt['result_sha256'] == sha(r.root / 'result.json')
        report = json.loads((r.root / 'result.json').read_text())
        assert report['completed'] is True
        assert report['pending_orders'] == report['pending_cash_reconciliation'] == report['pending_psm_actions'] == []
        check_receipt(receipt, target, r)
        with pytest.raises(ValueError, match='Terminal replay'):
            runner.run_completed_segment(r, tmp_path / 'after-terminal', source_pins=pins)
        assert r.steps == 37
        assert not (tmp_path / 'after-terminal').exists()


def test_terminal_cancels_pending_without_post_window_fills(tmp_path, pins):
    tape = source()
    # Artificial gaps make seed orders retry at the final pre-end boundary.
    tape.array[24:58] = np.nan
    with replay_at(tmp_path / 'run', tape=tape) as r:
        first = runner.run_completed_segment(r, tmp_path / 'penultimate', source_pins=pins, max_steps=36)
        pending_ids = [o['id'] for o in r.a._pending()]
        assert pending_ids
        before_fills = len(r.a._receipts())
        receipt = runner.run_completed_segment(r, tmp_path / 'terminal', source_pins=pins, max_steps=10)
        assert receipt['before_steps'] == first['after_steps'] == 36
        assert receipt['executed_steps'] == 1
        assert len(r.a._receipts()) == before_fills
        assert not r.a._pending()
        assert all(r.a.state['orders'][oid]['status'] == 'CANCELED' for oid in pending_ids)
        assert r.counts['WINDOW_END_CANCEL'] == 2
        check_receipt(receipt, tmp_path / 'terminal', r)


@pytest.mark.parametrize('steps,wall', [(0,240),(-1,240),(1.0,240),(True,240),(1,0),(1,-1),(1,601),(1,float('nan')),(1,float('inf')),(1,True),(1,'2')])
def test_invalid_limits_fail_before_steps(tmp_path, pins, steps, wall):
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='Positive bounded'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=steps, max_wall_seconds=wall)
        assert r.steps == 0 and r.last_at is None
        assert not (tmp_path / 'target').exists()


def test_wall_budget_finishes_first_atomic_boundary(tmp_path, pins, monkeypatch):
    ticks = iter([0.0, 100.0, 101.0])
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    with replay_at(tmp_path / 'run') as r:
        receipt = runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_wall_seconds=0.1)
        assert receipt['executed_steps'] == 1
        assert receipt['stop_reason'] == 'WALL_BOUNDARY'
        assert receipt['elapsed_seconds'] == 101
        check_receipt(receipt, tmp_path / 'target', r)


def test_stop_before_is_non_mutating(tmp_path, pins):
    stop = tmp_path / 'stop'; stop.touch()
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='Stop request present'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, stop_file=stop)
        assert r.steps == 0 and r.last_at is None
        assert not (tmp_path / 'target').exists()


def test_stop_between_boundaries_publishes_last_complete(tmp_path, pins, monkeypatch):
    stop = tmp_path / 'stop'
    with replay_at(tmp_path / 'run') as r:
        original = r.step
        def step(event):
            out = original(event)
            stop.touch()
            return out
        monkeypatch.setattr(r, 'step', step)
        receipt = runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, stop_file=stop)
        assert receipt['executed_steps'] == 1
        assert receipt['stop_reason'] == 'STOP_REQUESTED'
        check_receipt(receipt, tmp_path / 'target', r)


def test_stop_arriving_during_preflight_prevents_first_boundary(tmp_path, pins, monkeypatch):
    stop = tmp_path / 'stop'
    original = runner.verify_source_pins
    def verify(value):
        original(value)
        stop.touch()
    monkeypatch.setattr(runner, 'verify_source_pins', verify)
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='No completed boundary'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, stop_file=stop)
        assert r.steps == 0
        assert not (tmp_path / 'target').exists()


@pytest.mark.parametrize('on_step', [1, 2])
def test_partial_boundary_failure_never_publishes(tmp_path, pins, monkeypatch, on_step):
    target = tmp_path / 'target'
    with replay_at(tmp_path / 'run') as r:
        original = r._mark_completed_bar
        def fail(event):
            original(event)
            if r.steps + 1 == on_step:
                raise RuntimeError('synthetic partially mutated boundary')
        monkeypatch.setattr(r, '_mark_completed_bar', fail)
        with pytest.raises(RuntimeError, match='partially mutated'):
            runner.run_completed_segment(r, target, source_pins=pins)
        assert r.steps == on_step - 1
        assert not target.exists()
        assert not target.with_name(target.name + '.segment.json').exists()
        assert not list(tmp_path.glob('.incomplete-*'))


def test_finish_failure_never_publishes_terminal_claim(tmp_path, pins, monkeypatch):
    with replay_at(tmp_path / 'run') as r:
        def fail():
            raise RuntimeError('synthetic terminal reconciliation failure')
        monkeypatch.setattr(r, 'finish', fail)
        with pytest.raises(RuntimeError, match='terminal reconciliation'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins)
        assert r.steps == 37 and r.last_at == r.tape.end
        assert not (tmp_path / 'target').exists()
        assert not (tmp_path / 'target.segment.json').exists()
        assert not (r.root / 'result.json').exists()


@pytest.mark.parametrize('kind', ['directory','receipt'])
def test_existing_destination_or_receipt_refuses_before_steps(tmp_path, pins, kind):
    target = tmp_path / 'target'
    conflict = target if kind == 'directory' else tmp_path / 'target.segment.json'
    if kind == 'directory': conflict.mkdir()
    else: conflict.write_text('prior immutable receipt')
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='already exists'):
            runner.run_completed_segment(r, target, source_pins=pins)
        assert r.steps == 0
    if kind == 'receipt': assert conflict.read_text() == 'prior immutable receipt'


def test_tampered_source_rejects_before_steps(tmp_path, pins):
    marker = tmp_path / 'pin'; marker.write_text('original')
    pins[str(marker)] = sha(marker); marker.write_text('changed')
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='Source identity changed'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins)
        assert r.steps == 0
        assert not (tmp_path / 'target').exists()


def test_source_tamper_during_segment_prevents_checkpoint(tmp_path, pins, monkeypatch):
    marker = tmp_path / 'pin'; marker.write_text('original')
    pins[str(marker)] = sha(marker)
    with replay_at(tmp_path / 'run') as r:
        original = r.step
        def step(event):
            out = original(event); marker.write_text('changed'); return out
        monkeypatch.setattr(r, 'step', step)
        with pytest.raises(ValueError, match='Source identity changed'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=1)
        assert r.steps == 1
        assert not (tmp_path / 'target').exists()
        assert not (tmp_path / 'target.segment.json').exists()


@pytest.mark.parametrize('file', ['manifest.json','caller.json','account/paper.sqlite','account/native.sqlite','account/allocator.sqlite','events.jsonl','marks.jsonl','signals.jsonl'])
def test_tampered_checkpoint_is_rejected(tmp_path, pins, file):
    target = tmp_path / 'target'
    with replay_at(tmp_path / 'run') as r:
        receipt = runner.run_completed_segment(r, target, source_pins=pins, max_steps=2)
    path = target / file; path.write_bytes(path.read_bytes() + b'TAMPER')
    with pytest.raises(ValueError, match='identity differs|integrity differs'):
        verify_checkpoint(target, expected_manifest_sha256=receipt['checkpoint']['manifest_sha256'])


def test_segmented_restore_equals_uninterrupted_economics_state_and_logs(tmp_path, pins):
    root = tmp_path / 'run'
    with replay_at(root) as r:
        first = runner.run_completed_segment(r, tmp_path / 'first', source_pins=pins, max_steps=11)
        runner.run_completed_segment(r, tmp_path / 'continuous', source_pins=pins, max_steps=100)
        expected_result = json.loads((root / 'result.json').read_text())
        expected_state = r.export_state()
        expected_fills = r.a._receipts()
    names = ('events.jsonl','marks.jsonl','signals.jsonl')
    expected_logs = {name:(root / name).read_bytes() for name in names}
    shutil.move(root, tmp_path / 'preserved-continuous')
    restore_checkpoint(tmp_path / 'first', root, expected_manifest_sha256=first['checkpoint']['manifest_sha256'])
    cfg = config(('BTC','ETH'))
    r = WeeklyReplay.resume_restored(root, tape=source(), config=cfg, membership=members(cfg), protocol_sha256='synthetic-only')
    try:
        second = runner.run_completed_segment(r, tmp_path / 'resumed-mid', source_pins=pins, max_steps=7)
        terminal = runner.run_completed_segment(r, tmp_path / 'resumed-end', source_pins=pins, max_steps=100)
        assert second['before_steps'] == 11 and second['after_steps'] == 18
        assert terminal['before_steps'] == 18 and terminal['after_steps'] == 37
        actual = json.loads((root / 'result.json').read_text())
        assert {k:v for k,v in actual.items() if k != 'elapsed_seconds'} == {k:v for k,v in expected_result.items() if k != 'elapsed_seconds'}
        assert r.export_state() == expected_state
        assert r.a._receipts() == expected_fills
    finally:
        r.close()
    assert {name:(root / name).read_bytes() for name in names} == expected_logs


def test_receipt_publication_failure_leaves_unconfirmed_checkpoint_only(tmp_path, pins, monkeypatch):
    def fail(path, record):
        raise OSError('synthetic receipt publication failure')
    monkeypatch.setattr(runner, '_atomic_record', fail)
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(OSError, match='receipt publication failure'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=1)
        assert r.steps == 1
        assert (tmp_path / 'target/manifest.json').exists()
        assert not (tmp_path / 'target.segment.json').exists()
        with pytest.raises(ValueError, match='already exists'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=1)
        assert r.steps == 1


def test_atomic_receipt_preserves_existing_and_incomplete_evidence(tmp_path):
    target = tmp_path / 'receipt.json'
    runner._atomic_record(target, {'record': 1})
    before = target.read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        runner._atomic_record(target, {'record': 2})
    assert target.read_bytes() == before
    assert not (tmp_path / '.incomplete-receipt.json').exists()


def test_existing_result_rejects_before_steps_even_if_cursor_not_terminal(tmp_path, pins):
    with replay_at(tmp_path / 'run') as r:
        (r.root / 'result.json').write_text('uncertain prior result')
        with pytest.raises(ValueError, match='Terminal replay'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins)
        assert r.steps == 0
        assert not (tmp_path / 'target').exists()


@pytest.mark.parametrize('point', ['after_three_databases', 'before_atomic_publication'])
def test_snapshot_failure_does_not_publish_receipt(tmp_path, pins, monkeypatch, point):
    original = runner.save_completed_boundary
    def save(replay, target, *, source_pins):
        def fault(at):
            if at == point:
                raise RuntimeError('synthetic snapshot interruption')
        return original(replay, target, source_pins=source_pins, fault_hook=fault)
    monkeypatch.setattr(runner, 'save_completed_boundary', save)
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(RuntimeError, match='snapshot interruption'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=1)
        assert r.steps == 1
        assert not (tmp_path / 'target').exists()
        assert not (tmp_path / 'target.segment.json').exists()
        partial = list(tmp_path.glob('.incomplete-*'))
        assert len(partial) == 1
        with pytest.raises(ValueError, match='Incomplete'):
            verify_checkpoint(partial[0], expected_manifest_sha256='unconfirmed')


def test_concurrent_receipt_target_never_overwritten(tmp_path, monkeypatch):
    target = tmp_path / 'receipt.json'
    original = runner.os.link
    def race(source, destination):
        target.write_text('concurrent evidence')
        return original(source, destination)
    monkeypatch.setattr(runner.os, 'link', race)
    with pytest.raises(FileExistsError):
        runner._atomic_record(target, {'new': 'record'})
    assert target.read_text() == 'concurrent evidence'
    assert json.loads((tmp_path / '.incomplete-receipt.json').read_text()) == {'new': 'record'}


@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling_symlink'])
def test_orphan_receipt_temp_is_preserved_and_rejected_before_steps(tmp_path, pins, kind):
    leftover = tmp_path / '.incomplete-target.segment.json'
    if kind == 'file':
        leftover.write_text('prior partial receipt')
    elif kind == 'directory':
        leftover.mkdir()
    else:
        leftover.symlink_to(tmp_path / 'absent-temporary-target')
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='already exists'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=1)
        assert r.steps == 0 and r.last_at is None
        assert not (tmp_path / 'target').exists()
        assert not (tmp_path / 'target.segment.json').exists()
        if kind == 'file': assert leftover.read_text() == 'prior partial receipt'
        elif kind == 'directory': assert leftover.is_dir()
        else: assert leftover.is_symlink()


@pytest.mark.parametrize('name', ['target.segment.json', 'target'])
def test_dangling_destination_symlink_rejected_before_steps(tmp_path, pins, name):
    target = tmp_path / name
    target.symlink_to(tmp_path / 'absent-target')
    with replay_at(tmp_path / 'run') as r:
        with pytest.raises(ValueError, match='already exists'):
            runner.run_completed_segment(r, tmp_path / 'target', source_pins=pins, max_steps=1)
        assert r.steps == 0 and r.last_at is None
        assert target.is_symlink()
        assert not target.exists()
        assert not (tmp_path / '.incomplete-target.segment.json').exists()
        assert not (tmp_path / 'target/manifest.json').exists()
