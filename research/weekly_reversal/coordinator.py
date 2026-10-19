#!/usr/bin/env python3
"""Exact, default-bounded local invocation wrapper. No implicit recovery.

The caller supplies freshly readback-verified authority and a pinned invocation.
This program does not establish Library freshness or user permission, publish to
Library, update a registry, discover a checkpoint, or move an existing runtime.
The optional Python backend argument is a test seam; the CLI exposes no such flag.
"""
import argparse
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

MAX_WALL_SECONDS = 240
MAX_STEPS = 288
REF_NAMES = ('admission', 'confirmation', 'registry', 'source_pins', 'rules', 'protocol', 'packet', 'input')
REQUEST_KEYS = {'schema', 'workspace_root', 'repository_root', 'arm', 'mode',
                'output_directory', 'attempt_directory', 'checkpoint_directory', 'stop_file',
                'continuation', *REF_NAMES}
CORE_SOURCES = ('research/__init__.py', 'research/weekly_reversal/__init__.py',
                'research/weekly_reversal/configuration.py', 'research/weekly_reversal/launch_gate.py',
                'research/weekly_reversal/checkpoint.py', 'research/weekly_reversal/runner.py',
                'research/weekly_reversal/replay.py', 'research/weekly_reversal/causal_tape.py',
                'research/weekly_reversal/owned_spot/native_source_pins.json')


def _plain(value):
    if is_dataclass(value): return _plain(asdict(value))
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)): return [_plain(item) for item in value]
    return value


def _exact(left, right):
    """JSON equality without bool/int/float coercion at any depth."""
    if type(left) is not type(right): return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_exact(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_exact(a, b) for a, b in zip(left, right))
    return left == right


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise ValueError('Exact lowercase SHA-256 required')
    return value


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError('Duplicate JSON field')
        out[key] = value
    return out


def _json_bytes(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _read(path, expected):
    _sha(expected)
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Pinned bytes changed: ' + str(path))
    return _json_bytes(raw)


def _canonical(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError('Canonical absolute path required')
    path = Path(value)
    if str(path.resolve()) != value:
        raise ValueError('Path must be canonical and contain no symlink alias: ' + value)
    return path


def _within(path, root):
    return path == root or root in path.parents


def _sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path, raw):
    path = Path(path)
    temporary = path.with_name('.incomplete-' + path.name)
    if os.path.lexists(path) or os.path.lexists(temporary):
        raise ValueError('Immutable local record already exists: ' + str(path))
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temporary, path)
    temporary.unlink()
    _sync_dir(path.parent)


def _atomic_json(path, value):
    _atomic_bytes(path, (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode())


def _ref(value, root):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
        raise ValueError('Exact path/hash reference required')
    path = _canonical(value['path'])
    if not _within(path, root):
        raise ValueError('Referenced artifact is outside the invocation workspace')
    _sha(value['sha256'])
    return path


def _validate_request(request):
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS or type(request['schema']) is not int or request['schema'] != 1:
        raise ValueError('Unsupported or incomplete invocation schema')
    root = _canonical(request['workspace_root'])
    repo = _canonical(request['repository_root'])
    if not root.is_dir() or not repo.is_dir() or not _within(repo, root):
        raise ValueError('Existing workspace and repository required')
    if request['mode'] not in ('FRESH', 'CONTINUE_KNOWN_SUCCESS'):
        raise ValueError('Only explicit FRESH or known-success continuation is supported')
    paths = {}
    for key in ('output_directory', 'attempt_directory', 'checkpoint_directory', 'stop_file'):
        paths[key] = _canonical(request[key])
        if not _within(paths[key], root) or _within(paths[key], repo):
            raise ValueError('Runtime/control paths must be inside workspace and outside source repository')
    control = _control_root(paths['output_directory'])
    if control == root or not _within(control, root) or _within(control, repo):
        raise ValueError('Coordinator control directory must stay strictly inside workspace and outside repository')
    for name in REF_NAMES:
        _ref(request[name], root)
    if request['mode'] == 'FRESH' and request['continuation'] is not None:
        raise ValueError('Fresh mode cannot carry continuation evidence')
    if request['mode'] == 'CONTINUE_KNOWN_SUCCESS':
        c = request['continuation']
        if not isinstance(c, dict) or set(c) != {'checkpoint_directory', 'manifest_sha256', 'prior_outcome', 'prior_segment_receipt', 'authority'}:
            raise ValueError('Explicit exact known-success continuation evidence required')
        cp = _canonical(c['checkpoint_directory'])
        if not _within(cp, root):
            raise ValueError('Checkpoint is outside workspace')
        _sha(c['manifest_sha256'])
        for key in ('prior_outcome', 'prior_segment_receipt', 'authority'):
            _ref(c[key], root)
    input_path = Path(request['input']['path'])
    other_paths = [Path(request[name]['path']) for name in REF_NAMES if name != 'input']
    if request['continuation'] is not None:
        other_paths.extend(Path(request['continuation'][key]['path']) for key in
                           ('prior_outcome', 'prior_segment_receipt', 'authority'))
    if any(path == input_path or (path.exists() and input_path.exists() and path.samefile(input_path))
           for path in other_paths):
        raise ValueError('Historical input cannot alias an authority artifact')
    for name in REF_NAMES:
        path = Path(request[name]['path'])
        if _within(path, paths['output_directory']) or _within(path, paths['attempt_directory']):
            raise ValueError('Authority/input artifacts must not be inside new runtime or attempt')
    return root, repo, paths


def _sources(request):
    root = Path(request['workspace_root'])
    repo = Path(request['repository_root'])
    ref = request['source_pins']
    raw = _read(ref['path'], ref['sha256'])
    if not isinstance(raw, dict) or not raw:
        raise ValueError('Nonempty exact source-pin map required')
    result = {}
    for name, expected in raw.items():
        if not isinstance(name, str):
            raise ValueError('Source paths must be strings')
        path = Path(name) if Path(name).is_absolute() else root / name
        path = _canonical(str(path))
        if not _within(path, root):
            raise ValueError('Frozen source path must stay inside invocation workspace')
        input_path = Path(request['input']['path'])
        if (path.suffix not in ('.py', '.md', '.json') or path == input_path
                or (path.exists() and input_path.exists() and path.samefile(input_path))):
            raise ValueError('Source map cannot contain historical input')
        if str(path) in result:
            raise ValueError('Duplicate canonical source path')
        if _digest(path) != _sha(expected):
            raise ValueError('Frozen source changed: ' + str(path))
        result[str(path)] = expected
    required = {repo / name for name in CORE_SOURCES} | {Path(__file__).resolve()}
    for package in ('research', 'spectra_core', 'mcp_servers'):
        folder = repo / package
        if not folder.is_dir():
            raise ValueError('Complete frozen package tree required: ' + package)
        required.update(folder.rglob('*.py'))
    if any(str(path.resolve()) not in result for path in required):
        raise ValueError('Source pins do not cover coordinator and full repository package trees')
    return result


class NativeBackend:
    """Lazy adapter around the frozen production helpers; never used by fake tests."""
    def __init__(self, request, pins):
        repo = Path(request['repository_root'])
        for name, module in list(sys.modules.items()):
            if name.split('.')[0] in {'research', 'spectra_core', 'mcp_servers'}:
                origin = getattr(module, '__file__', None)
                if origin:
                    raise ValueError('Native invocation requires a fresh process with no preloaded research modules: ' + name)
        # A fresh per-attempt cache location cannot contain stale executable pyc.
        # No bytecode is written. Imports compile the already verified source.
        sys.pycache_prefix = str(Path(request['attempt_directory']) / 'python_cache')
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(repo))
        self.configuration_module = importlib.import_module('research.weekly_reversal.configuration')
        self.gate = importlib.import_module('research.weekly_reversal.launch_gate')
        self.checkpoint = importlib.import_module('research.weekly_reversal.checkpoint')
        self.replay = importlib.import_module('research.weekly_reversal.replay')
        self.tape = importlib.import_module('research.weekly_reversal.causal_tape')
        self.runner = importlib.import_module('research.weekly_reversal.runner')
        for name, module in list(sys.modules.items()):
            if name.split('.')[0] in {'research', 'spectra_core', 'mcp_servers'}:
                origin = getattr(module, '__file__', None)
                if origin and str(Path(origin).resolve()) not in pins:
                    raise ValueError('Imported source escaped frozen closure: ' + name)
        c = self.configuration_module
        self.bounds = [c.WARMUP_START, c.START, c.END]
        self.input_sha256 = c.INPUT_SHA256
        self.data_start = c.DATA_START
        self.roster = c.ROSTER

    def validate(self, **kwargs): return self.gate.validate_launch_packet(**kwargs)
    def configuration(self, path, expected_sha256): return self.configuration_module.configuration(path, expected_sha256=expected_sha256)
    def membership(self, config): return self.configuration_module.membership(config)
    def arm_policy(self, arm): return self.configuration_module.arm_policy(arm)
    def load(self, path, admission):
        return self.tape.load_admitted_tape(path, expected_sha256=self.input_sha256, admission=admission,
            data_start=self.data_start, warmup_start=self.bounds[0], start=self.bounds[1], end=self.bounds[2], roster=self.roster)
    def fresh(self, directory, **kwargs): return self.replay.WeeklyReplay(directory, **kwargs)
    def resume(self, directory, **kwargs): return self.replay.WeeklyReplay.resume_restored(directory, **kwargs)
    def verify_checkpoint(self, directory, digest): return self.checkpoint.verify_checkpoint(directory, expected_manifest_sha256=digest)
    def restore(self, directory, destination, digest, recovery):
        return self.checkpoint.restore_checkpoint(directory, destination, expected_manifest_sha256=digest, confirmed_recovery=recovery)
    def run(self, replay, directory, **kwargs): return self.runner.run_completed_segment(replay, directory, **kwargs)


def _descriptor(request):
    return {key: request[key] for key in ('workspace_root', 'repository_root', 'mode', 'output_directory',
             'attempt_directory', 'checkpoint_directory', 'stop_file', 'continuation')} | {
             'input_path': request['input']['path'], 'max_wall_seconds': MAX_WALL_SECONDS, 'max_steps': MAX_STEPS}


def _authority(request, backend):
    for name in REF_NAMES:
        if name == 'input':
            continue  # Historical bytes are read only by the admitted loader.
        ref = request[name]
        if _digest(ref['path']) != ref['sha256']:
            raise ValueError('Pinned artifact changed: ' + name)
    if request['input']['sha256'] != backend.input_sha256:
        raise ValueError('Input identity differs from frozen configuration')
    a = backend.validate(admission_path=request['admission']['path'],
        expected_admission_sha256=request['admission']['sha256'], registry_path=request['registry']['path'],
        confirmation_path=request['confirmation']['path'], expected_confirmation_sha256=request['confirmation']['sha256'],
        expected_input_sha256=backend.input_sha256, expected_protocol_sha256=request['protocol']['sha256'],
        expected_bounds=backend.bounds, expected_source_pins_sha256=request['source_pins']['sha256'],
        expected_rule_sha256=request['rules']['sha256'], expected_output_directory=request['output_directory'],
        expected_arm=request['arm'])
    confirmation = _read(request['confirmation']['path'], request['confirmation']['sha256'])
    if a.get('registry_sha256') != request['registry']['sha256'] or confirmation.get('packet_sha256') != request['packet']['sha256']:
        raise ValueError('Exact packet/registry binding differs')
    if not _exact(a.get('coordinator'), _descriptor(request)):
        raise ValueError('Admission does not bind the exact coordinator invocation')
    if not isinstance(a.get('attempt_id'), str) or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,127}', a['attempt_id']) is None:
        raise ValueError('Safe exact attempt identifier required')
    return a


def _stop(request):
    if os.path.lexists(request['stop_file']):
        raise ValueError('Stop request present; no further execution permitted')


def _control_root(output):
    return output.parent / ('.' + output.name + '.coordinator')


@contextmanager
def _lock(control):
    if os.path.lexists(control) and (control.is_symlink() or not control.is_dir()):
        raise ValueError('Coordinator directory is not an ordinary directory')
    control.mkdir(mode=0o700, exist_ok=True)
    lock = control / 'writer.lock'
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Exclusive per-run coordinator writer is already held') from exc
        yield
    finally:
        os.close(fd)


def _receipt_cursor(receipt, bounds):
    checkpoint = receipt.get('checkpoint')
    if not isinstance(checkpoint, dict):
        raise ValueError('Exact checkpoint receipt object required')
    before, after, executed = (receipt.get(key) for key in ('before_steps', 'after_steps', 'executed_steps'))
    before_at, after_at = receipt.get('before_at'), receipt.get('after_at')
    if (any(type(value) is not int for value in (before, after, executed, after_at,
                                                checkpoint.get('at'), checkpoint.get('steps')))
            or before < 0 or after < 1 or not 1 <= executed <= MAX_STEPS
            or after - before != executed
            or not bounds[1] <= after_at <= bounds[2]
            or after_at != bounds[1] + (after - 1) * 300
            or (before == 0 and before_at is not None)
            or (before > 0 and (type(before_at) is not int or before_at != bounds[1] + (before - 1) * 300))
            or checkpoint['at'] != after_at or checkpoint['steps'] != after):
        raise ValueError('Receipt cursor and step fields must be exact integers at completed boundaries')


def _known_success(request, admission, backend, pins, config, membership, policy):
    c = request['continuation']
    if c is None:
        return None
    proof = _read(c['authority']['path'], c['authority']['sha256'])
    prior = _read(c['prior_outcome']['path'], c['prior_outcome']['sha256'])
    receipt = _read(c['prior_segment_receipt']['path'], c['prior_segment_receipt']['sha256'])
    if not all(isinstance(x, dict) for x in (proof, prior, receipt)):
        raise ValueError('Known-success records must be objects')
    expected = {'run_id': admission['run_id'], 'next_attempt_id': admission['attempt_id'],
                'prior_attempt_id': prior.get('attempt_id'), 'checkpoint_manifest_sha256': c['manifest_sha256'],
                'prior_outcome_sha256': c['prior_outcome']['sha256'],
                'prior_segment_receipt_sha256': c['prior_segment_receipt']['sha256'],
                'output_directory': request['output_directory']}
    if (proof.get('state') != 'KNOWN_SUCCESS_CONTINUATION_CONFIRMED'
            or type(proof.get('prior_process_exit_code')) is not int or proof['prior_process_exit_code'] != 0
            or any(not _exact(proof.get(k), v) for k, v in expected.items())
            or not isinstance(prior.get('attempt_id'), str)
            or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,127}', prior['attempt_id']) is None
            or prior.get('attempt_id') == admission['attempt_id']):
        raise ValueError('Explicit observed-success continuation authority required')
    publication = proof.get('prior_publication', {})
    if (not isinstance(publication, dict) or publication.get('state') != 'DURABLE_PACKET_READBACK_CONFIRMED'
            or not isinstance(publication.get('library_file_id'), str)
            or re.fullmatch('libfile_[a-f0-9]{32}', publication['library_file_id']) is None
            or type(publication.get('library_version')) is not int or publication['library_version'] < 0):
        raise ValueError('Confirmed prior durable publication required')
    _sha(publication.get('packet_sha256'))
    if (prior.get('state') != 'LOCAL_SUCCESS' or prior.get('replay_closed') is not True
            or prior.get('run_id') != admission['run_id'] or prior.get('terminal') is not False
            or prior.get('segment_receipt_sha256') != c['prior_segment_receipt']['sha256']
            or prior.get('checkpoint_manifest_sha256') != c['manifest_sha256']
            or prior.get('output_directory') != request['output_directory']
            or any(not _exact(prior.get(key), admission.get(key)) for key in
                   ('arm', 'input_sha256', 'protocol_sha256', 'source_pins_sha256', 'rule_sha256', 'bounds'))):
        raise ValueError('Prior invocation was not an exact closed nonterminal success')
    if (receipt.get('state') != 'BOUNDARY_LOCAL' or receipt.get('publication_confirmed') is not False
            or receipt.get('run_id') != admission['run_id'] or receipt.get('protocol_sha256') != admission['protocol_sha256']
            or receipt.get('synthetic') is not False or receipt.get('checkpoint', {}).get('manifest_sha256') != c['manifest_sha256']):
        raise ValueError('Prior segment receipt does not bind this continuation')
    _receipt_cursor(receipt, backend.bounds)
    manifest = backend.verify_checkpoint(c['checkpoint_directory'], c['manifest_sha256'])
    identity = manifest.get('input_identity', {})
    if (manifest.get('run_directory') != request['output_directory'] or manifest.get('synthetic') is not False
            or manifest.get('protocol_sha256') != admission['protocol_sha256'] or manifest.get('source_pins') != pins
            or identity.get('source_file_sha256') != backend.input_sha256 or not _exact(identity.get('bounds'), list(backend.bounds))
            or not _exact(receipt.get('input_identity'), identity)
            or type(manifest.get('at')) is not int or not backend.bounds[1] <= manifest['at'] < backend.bounds[2]
            or type(manifest.get('steps')) is not int or manifest['steps'] < 1
            or manifest['at'] != backend.bounds[1] + (manifest['steps'] - 1) * 300
            or receipt.get('after_at') != manifest['at'] or receipt.get('after_steps') != manifest['steps']):
        raise ValueError('Checkpoint is not the exact nonterminal original-path boundary')
    caller_pin = manifest.get('files', {}).get('caller.json', {}).get('sha256')
    caller = _read(Path(c['checkpoint_directory']) / 'caller.json', caller_pin)
    saved_admission = caller.get('admission', {}) if isinstance(caller, dict) else {}
    if (not isinstance(caller, dict) or not isinstance(saved_admission, dict)
            or not _exact(caller.get('bounds'), list(backend.bounds)) or not _exact(caller.get('input_identity'), identity)
            or caller.get('protocol_sha256') != admission['protocol_sha256']
            or not _exact(caller.get('last_at'), manifest['at']) or not _exact(caller.get('steps'), manifest['steps'])
            or not _exact(caller.get('config'), _plain(config)) or not _exact(caller.get('membership'), _plain(membership))
            or not _exact(caller.get('policy'), _plain(policy))
            or saved_admission.get('attempt_id') != prior['attempt_id']
            or any(not _exact(saved_admission.get(key), admission.get(key)) for key in
                   ('run_id', 'arm', 'input_sha256', 'protocol_sha256', 'source_pins_sha256',
                    'rule_sha256', 'bounds', 'output_directory'))):
        raise ValueError('Saved caller arm, policy, configuration or admission scope differs')
    return {'proof': proof, 'prior': prior, 'manifest': manifest}


def _ledger(control, request, admission):
    attempts = control / 'attempts'
    if not attempts.exists():
        return
    if attempts.is_symlink() or not attempts.is_dir():
        raise ValueError('Attempt ledger is not an ordinary directory')
    entries = list(attempts.iterdir())
    if request['mode'] == 'FRESH' and entries:
        raise ValueError('Fresh mode cannot reuse a prior coordinator attempt')
    outcomes = {}
    referenced = set()
    for directory in entries:
        path = directory / 'outcome.json'
        if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file():
            raise ValueError('Unknown prior attempt must be preserved and reconciled separately')
        raw = path.read_bytes()
        outcome = _json_bytes(raw)
        if (not isinstance(outcome, dict) or outcome.get('state') != 'LOCAL_SUCCESS'
                or outcome.get('run_id') != admission['run_id'] or outcome.get('replay_closed') is not True
                or outcome.get('attempt_id') != directory.name):
            raise ValueError('Failed/unknown prior attempt requires separate reconciliation')
        invocation = directory / 'invocation.json'
        if (invocation.is_symlink() or not invocation.is_file()
                or _digest(invocation) != outcome.get('invocation_record_sha256')):
            raise ValueError('Prior invocation evidence is missing or changed')
        digest = hashlib.sha256(raw).hexdigest()
        outcomes[digest] = outcome
        previous = outcome.get('previous_outcome_sha256')
        if previous is not None:
            _sha(previous)
            referenced.add(previous)
    if outcomes:
        tips = set(outcomes) - referenced
        expected = request['continuation']['prior_outcome']['sha256']
        if tips != {expected}:
            raise ValueError('Continuation does not select the latest unique known-success local outcome')


def _fresh_destinations(request, admission, control):
    expected_attempt = control / 'attempts' / admission['attempt_id']
    if (request['attempt_directory'] != str(expected_attempt)
            or request['checkpoint_directory'] != str(expected_attempt / 'checkpoint')):
        raise ValueError('Attempt/checkpoint paths must be deterministic under the per-run coordinator')
    if os.path.lexists(request['output_directory']):
        raise ValueError('Original runtime must be absent; preserve it externally before invocation')
    if os.path.lexists(expected_attempt):
        raise ValueError('Per-attempt invocation directory is immutable and already exists')
    _ledger(control, request, admission)


def _verify_result(request, admission, backend, pins, receipt, prior):
    checkpoint = Path(request['checkpoint_directory'])
    receipt_path = checkpoint.with_name(checkpoint.name + '.segment.json')
    if not isinstance(receipt, dict) or not _exact(_json_bytes(receipt_path.read_bytes()), receipt):
        raise ValueError('Returned segment receipt differs from its immutable local record')
    _receipt_cursor(receipt, backend.bounds)
    manifest_sha = receipt.get('checkpoint', {}).get('manifest_sha256')
    _sha(manifest_sha)
    manifest = backend.verify_checkpoint(checkpoint, manifest_sha)
    if (receipt.get('publication_confirmed') is not False or receipt.get('run_id') != admission['run_id']
            or receipt.get('protocol_sha256') != admission['protocol_sha256'] or receipt.get('synthetic') is not False
            or receipt.get('checkpoint', {}).get('path') != str(checkpoint)
            or manifest.get('run_directory') != request['output_directory'] or manifest.get('source_pins') != pins
            or manifest.get('protocol_sha256') != admission['protocol_sha256'] or manifest.get('synthetic') is not False
            or manifest.get('input_identity', {}).get('source_file_sha256') != backend.input_sha256
            or receipt.get('after_at') != manifest.get('at') or receipt.get('after_steps') != manifest.get('steps')):
        raise ValueError('Local checkpoint/receipt identity failed independent post-close verification')
    identity = manifest.get('input_identity', {})
    before_steps = prior['manifest']['steps'] if prior else 0
    before_at = prior['manifest']['at'] if prior else None
    after_steps, after_at = manifest.get('steps'), manifest.get('at')
    if (not _exact(identity.get('bounds'), list(backend.bounds)) or not _exact(receipt.get('input_identity'), identity)
            or type(after_steps) is not int or type(after_at) is not int
            or not backend.bounds[1] <= after_at <= backend.bounds[2]
            or after_at != backend.bounds[1] + (after_steps - 1) * 300
            or receipt.get('before_steps') != before_steps or receipt.get('before_at') != before_at
            or type(receipt.get('executed_steps')) is not int
            or not 1 <= receipt['executed_steps'] <= MAX_STEPS
            or after_steps - before_steps != receipt['executed_steps']
            or receipt.get('checkpoint', {}).get('at') != after_at
            or receipt.get('checkpoint', {}).get('steps') != after_steps):
        raise ValueError('Segment cursor, step count or input bounds are not exact')
    terminal = receipt.get('state') == 'TERMINAL_LOCAL'
    if receipt.get('state') not in {'TERMINAL_LOCAL', 'BOUNDARY_LOCAL'} or terminal != (manifest.get('at') == backend.bounds[2]):
        raise ValueError('Terminal state differs from exact final boundary')
    reason = receipt.get('stop_reason')
    if (terminal and reason != 'WINDOW_COMPLETE') or (not terminal and reason not in {'STEP_BOUNDARY', 'WALL_BOUNDARY', 'STOP_REQUESTED'}):
        raise ValueError('Segment stop reason contradicts completion state')
    terminal_sha = None
    if terminal:
        result = Path(request['output_directory']) / 'result.json'
        raw = result.read_bytes()
        terminal_sha = hashlib.sha256(raw).hexdigest()
        report = _json_bytes(raw)
        if (not isinstance(report, dict) or terminal_sha != receipt.get('result_sha256')
                or report.get('completed') is not True or report.get('synthetic') is not False
                or report.get('protocol_sha256') != admission['protocol_sha256']
                or not _exact(report.get('window'), dict(warmup_start=backend.bounds[0], start=backend.bounds[1], end_exclusive=backend.bounds[2]))):
            raise ValueError('Terminal result identity/completion differs')
        _atomic_bytes(Path(request['attempt_directory']) / 'terminal_result.json', raw)
    elif receipt.get('result_sha256') is not None:
        raise ValueError('Nonterminal receipt claims a terminal result')
    return _digest(receipt_path), manifest_sha, terminal, terminal_sha


def execute_invocation(invocation_path, expected_sha256, *, backend=None):
    """Execute exactly one admitted segment; injected backend is for artificial tests."""
    invocation_path = Path(invocation_path)
    request = _read(invocation_path, expected_sha256)
    root, repo, paths = _validate_request(request)
    _stop(request)
    control = _control_root(paths['output_directory'])
    if not paths['output_directory'].parent.is_dir():
        raise ValueError('Original runtime parent must already exist')
    with _lock(control):
        _stop(request)
        pins = _sources(request)
        engine = backend if backend is not None else NativeBackend(request, pins)
        admission = _authority(request, engine)
        _fresh_destinations(request, admission, control)
        _stop(request)
        attempt = paths['attempt_directory']
        attempt.parent.mkdir(mode=0o700, exist_ok=True)
        attempt.mkdir(mode=0o700)  # Exclusive fresh claim; never reused or removed.
        _sync_dir(attempt.parent)
        stage = 'INVOCATION_RECORD'
        replay = None
        base = {'schema': 1, 'run_id': admission['run_id'], 'attempt_id': admission['attempt_id'],
                'invocation_sha256': expected_sha256, 'output_directory': request['output_directory'],
                'publication_confirmed': False,
                'previous_outcome_sha256': request['continuation']['prior_outcome']['sha256'] if request['continuation'] else None,
                **{key: admission[key] for key in ('arm', 'input_sha256', 'protocol_sha256', 'source_pins_sha256', 'rule_sha256', 'bounds')}}
        try:
            _atomic_json(attempt / 'invocation.json', dict(base, state='STARTED_LOCAL', request=request, started_at=time.time()))
            base['invocation_record_sha256'] = _digest(attempt / 'invocation.json')
            stage = 'CONFIGURATION'
            config = engine.configuration(request['rules']['path'], request['rules']['sha256'])
            membership = engine.membership(config)
            policy = engine.arm_policy(request['arm'])
            stage = 'VERIFYING_KNOWN_SUCCESS'
            prior = _known_success(request, admission, engine, pins, config, membership, policy)
            # Recheck exact authority, source bytes and stop after imports/configuration.
            if not _exact(_sources(request), pins) or not _exact(_authority(request, engine), admission):
                raise ValueError('Authority/source changed before loader')
            _stop(request)
            if os.path.lexists(request['output_directory']):
                raise ValueError('Runtime appeared before loader; do not overwrite it')
            stage = 'LOADING_INPUT'
            tape = engine.load(request['input']['path'], admission)
            if (tape.synthetic is not False or tape.input_identity.get('source_file_sha256') != engine.input_sha256
                    or not _exact(tape.input_identity.get('bounds'), list(engine.bounds))):
                raise ValueError('Admitted loader returned a different input identity')
            _stop(request)
            if os.path.lexists(request['output_directory']):
                raise ValueError('Runtime appeared before construction; do not overwrite it')
            try:
                if prior is None:
                    stage = 'CONSTRUCTING_FRESH_REPLAY'
                    replay = engine.fresh(request['output_directory'], tape=tape, config=config, membership=membership,
                                          signal_policy=policy, protocol_sha256=admission['protocol_sha256'], admission=admission)
                else:
                    stage = 'RESTORING_KNOWN_SUCCESS'
                    c = request['continuation']
                    recovery = {'confirmed': True, 'manifest_sha256': c['manifest_sha256'],
                                'attempt_id': admission['attempt_id'], 'registry_fence': admission['registry_sha256']}
                    engine.restore(c['checkpoint_directory'], request['output_directory'], c['manifest_sha256'], recovery)
                    stage = 'CONSTRUCTING_RESTORED_REPLAY'
                    replay = engine.resume(request['output_directory'], tape=tape, config=config, membership=membership,
                                           protocol_sha256=admission['protocol_sha256'], confirmed_admission=admission)
                stage = 'RUNNING_SEGMENT'
                receipt = engine.run(replay, request['checkpoint_directory'], source_pins=pins,
                                     max_wall_seconds=MAX_WALL_SECONDS, max_steps=MAX_STEPS, stop_file=request['stop_file'])
            finally:
                if replay is not None:
                    previous_stage = stage
                    stage = 'CLOSING_REPLAY'
                    replay.close()
                    stage = previous_stage
            stage = 'VERIFYING_LOCAL_OUTPUT'
            receipt_sha, manifest_sha, terminal, result_sha = _verify_result(request, admission, engine, pins, receipt, prior)
            outcome = dict(base, state='LOCAL_SUCCESS', replay_closed=True, terminal=terminal,
                           checkpoint_manifest_sha256=manifest_sha, segment_receipt_sha256=receipt_sha,
                           terminal_result_sha256=result_sha, finished_at=time.time(), observed_process_exit=False,
                           note='Local closed success is not observed process exit, remote publication, or continuation permission.')
            _atomic_json(attempt / 'outcome.json', outcome)
            return outcome
        except BaseException as exc:
            failure = dict(base, state='FAILED_UNCERTAIN', stage=stage, error_type=type(exc).__name__, error=str(exc),
                           traceback=traceback.format_exc(), finished_at=time.time(),
                           note='Preserve all bytes. No retry, refund, resume, or registry update is authorized.')
            try:
                _atomic_json(attempt / 'outcome.json', failure)
            except BaseException:
                # Any incomplete record is deliberately retained; missing outcome is UNKNOWN.
                pass
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invocation', required=True)
    parser.add_argument('--invocation-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        outcome = execute_invocation(args.invocation, args.invocation_sha256)
    except Exception as exc:
        print(json.dumps({'state': 'FAILED_OR_REJECTED', 'error_type': type(exc).__name__, 'error': str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
