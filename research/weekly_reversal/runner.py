"""Bounded execution helper for an already admitted WeeklyReplay.

This helper grants no historical authority and never resumes a run. The external
coordinator confirms the durable fence before constructing WeeklyReplay, preserves
uncertain attempts, and verifies/saves each returned snapshot before continuation.
Only completed boundaries are checkpointed. A failing boundary remains forensic
state; no snapshot of that partially changed state is published.
"""
from pathlib import Path
import hashlib
import os
import time

from research.weekly_reversal.checkpoint import save_completed_boundary, verify_source_pins
from research.weekly_reversal.replay import dumps


def _atomic_record(path, record):
    path = Path(path)
    if os.path.lexists(path):
        raise ValueError('Immutable segment receipt already exists')
    temporary = path.with_name('.incomplete-' + path.name)
    with temporary.open('x') as stream:
        stream.write(dumps(record) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    # The account writer excludes another legitimate publisher. Hard link also
    # rejects an unexpected concurrent target rather than replacing its evidence.
    os.link(temporary, path)
    temporary.unlink()
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_completed_segment(replay, checkpoint_directory, *, source_pins,
                          max_wall_seconds=240, max_steps=288, stop_file=None):
    """Execute up to a bounded number of exact consecutive completed bars.

    Runtime caps are checked only between complete boundaries. They do not interrupt
    accounting transactions or change simulated decisions. The caller still owns
    replay.close() in a finally block and external durable publication.
    """
    if (type(max_steps) is not int or max_steps < 1
            or type(max_wall_seconds) not in (int, float)
            or not 0 < max_wall_seconds <= 600):
        raise ValueError('Positive bounded step/wall limits required')
    target = Path(checkpoint_directory)
    receipt_path = target.with_name(target.name + '.segment.json')
    temporary_receipt = receipt_path.with_name('.incomplete-' + receipt_path.name)
    if any(os.path.lexists(path) for path in (target, receipt_path, temporary_receipt)):
        raise ValueError('Segment checkpoint, receipt or incomplete receipt already exists')
    if (replay.root / 'result.json').exists() or replay.last_at == replay.tape.end:
        raise ValueError('Terminal replay cannot execute another segment')
    if stop_file is not None and Path(stop_file).exists():
        raise ValueError('Stop request present before segment execution')
    verify_source_pins(source_pins)
    started = time.monotonic()
    before_at, before_steps = replay.last_at, replay.steps
    terminal = False
    stop_reason = None
    while replay.steps - before_steps < max_steps:
        if stop_file is not None and Path(stop_file).exists():
            stop_reason = 'STOP_REQUESTED'
            break
        if replay.steps > before_steps and time.monotonic() - started >= max_wall_seconds:
            stop_reason = 'WALL_BOUNDARY'
            break
        at = replay.tape.start if replay.last_at is None else replay.last_at + 300
        replay.step(replay.tape.boundary(at))
        if replay.last_at == replay.tape.end:
            # End is exclusive for fills. The existing finish path reconciles
            # cancellations and verifies ownership before any terminal claim.
            replay.finish()
            terminal = True
            stop_reason = 'WINDOW_COMPLETE'
            break
    if replay.steps == before_steps:
        raise ValueError('No completed boundary; no new checkpoint published')
    if stop_reason is None:
        stop_reason = 'STEP_BOUNDARY'
    snapshot = save_completed_boundary(replay, target, source_pins=source_pins)
    result_path = replay.root / 'result.json'
    receipt = {
        'schema': 1, 'state': 'TERMINAL_LOCAL' if terminal else 'BOUNDARY_LOCAL',
        'publication_confirmed': False, 'before_at': before_at, 'before_steps': before_steps,
        'after_at': replay.last_at, 'after_steps': replay.steps,
        'executed_steps': replay.steps - before_steps, 'stop_reason': stop_reason,
        'elapsed_seconds': time.monotonic() - started,
        'protocol_sha256': replay.protocol_sha256,
        'run_id': (replay.admission or {}).get('run_id'), 'synthetic': replay.tape.synthetic,
        'input_identity': replay.tape.input_identity, 'checkpoint': snapshot,
        'result_sha256': hashlib.sha256(result_path.read_bytes()).hexdigest() if terminal else None,
        'note': 'Local completion is not durable publication or permission to continue.'}
    _atomic_record(receipt_path, receipt)
    return receipt
