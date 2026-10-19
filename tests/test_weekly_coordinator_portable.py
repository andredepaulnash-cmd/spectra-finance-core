"""Independent coordinator audit: generated JSON and fake execution only.

No historical input is created or read. All fixture files are generated beneath
pytest's explicit, isolated --basetemp in this review directory. No strategy,
real replay constructor, historical loader, or registry mutation is invoked.
"""
import copy
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import os
import select
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest
import research.weekly_reversal.coordinator as candidate

ROOT = Path(os.environ.get('WEEKLY_COORDINATOR_TEST_WORKSPACE') or Path(candidate.__file__).resolve().parents[3]).resolve()
GATE_PATH = Path(candidate.__file__).resolve().with_name('launch_gate.py')
SPEC = importlib.util.spec_from_file_location('_independent_coordinator_gate', GATE_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture(scope='module')
def _generated_workspace():
    # Retain uniquely created evidence; never delete or reuse caller directories.
    return Path(tempfile.mkdtemp(prefix='.coordinator-generated-', dir=ROOT))


@pytest.fixture
def tmp_path(_generated_workspace):
    return Path(tempfile.mkdtemp(prefix='case-', dir=_generated_workspace))


def raw(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = raw(value)
    path.write_bytes(data)
    return {'path': str(path), 'sha256': digest(data)}


@dataclass
class Configuration:
    value: str = 'fixed-config'


@dataclass
class Membership:
    value: str = 'fixed-membership'


@dataclass
class Policy:
    enabled: bool = True
    bbwp_minimum: int = 50
    bearish_evidence_minimum: int = 2
    bearish_confirmation_hours: int = 2
    rsi_soft_weight: int = 1


class ArtificialReplay:
    def __init__(self, backend, directory, admission, cursor=0):
        self.backend = backend
        self.root = Path(directory)
        self.admission = admission
        self.cursor = cursor
        self.closed = False

    def close(self):
        self.backend.hit('close')
        self.closed = True


class ArtificialBackend:
    bounds = [0, 3600, 608400]
    input_sha256 = '1' * 64
    data_start = 0
    roster = ('ARTIFICIAL',)

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []
        self.callbacks = {}
        self.failures = {}
        self.result_changes = {}
        self.receipt_changes = {}
        self.replay = None
        self.tape_changes = {}
        self.terminal = False

    def hit(self, name):
        self.calls.append(name)
        if name in self.callbacks:
            self.callbacks[name]()
        if name in self.failures:
            raise self.failures[name]

    def validate(self, **kwargs):
        self.hit('validate')
        return gate.validate_launch_packet(**kwargs)

    def configuration(self, path, expected_sha256):
        self.hit('configuration')
        assert digest(Path(path).read_bytes()) == expected_sha256
        return Configuration()

    def membership(self, config):
        self.hit('membership')
        assert config == Configuration()
        return Membership()

    def arm_policy(self, arm):
        self.hit('policy')
        return Policy(bbwp_minimum=50 if arm == 'CONTROL_BBWP50_SOFT_RSI' else 20)

    def identity(self):
        return {'source_file_sha256': self.input_sha256, 'bounds': self.bounds[:], 'pairs': ['ARTIFICIAL-USD']}

    def load(self, path, admission):
        self.hit('load')
        assert not Path(path).exists(), 'Artificial input must never exist'
        values = {'synthetic': False, 'input_identity': self.identity()}
        values.update(self.tape_changes)
        return SimpleNamespace(**values)

    def fresh(self, directory, **kwargs):
        self.hit('fresh')
        path = Path(directory)
        path.mkdir()
        (path / 'runtime-evidence.json').write_bytes(raw({'preserve': True}))
        assert kwargs['signal_policy'] == self.arm_policy(self.fixture.request['arm'])
        self.replay = ArtificialReplay(self, path, kwargs['admission'])
        return self.replay

    def run(self, replay, directory, **kwargs):
        self.hit('run')
        assert kwargs == {'source_pins': self.fixture.pins, 'max_wall_seconds': 240,
                          'max_steps': 288, 'stop_file': self.fixture.request['stop_file']}
        cp = Path(directory)
        cp.mkdir()
        steps = replay.cursor + 1
        at = self.bounds[1] + (steps - 1) * 300
        if self.terminal:
            at = self.bounds[2]
            steps = (at - self.bounds[1]) // 300 + 1
        manifest = {'schema': 2, 'state': 'COMPLETED_BOUNDARY', 'run_directory': str(replay.root),
                    'synthetic': False, 'protocol_sha256': self.fixture.request['protocol']['sha256'],
                    'source_pins': self.fixture.pins, 'input_identity': self.identity(), 'at': at, 'steps': steps,
                    'files': {}}
        manifest.update(copy.deepcopy(self.result_changes))
        caller = {'schema': 2, 'admission': copy.deepcopy(replay.admission),
                  'protocol_sha256': manifest['protocol_sha256'], 'input_identity': manifest['input_identity'],
                  'synthetic': False, 'bounds': self.bounds[:], 'pairs': ['ARTIFICIAL-USD'],
                  'last_at': manifest['at'], 'steps': manifest['steps'],
                  'config': asdict(Configuration()), 'membership': asdict(Membership()),
                  'policy': asdict(self.arm_policy(self.fixture.request['arm']))}
        caller_ref = save(cp / 'caller.json', caller)
        manifest['files']['caller.json'] = {'sha256': caller_ref['sha256'], 'bytes': len(raw(caller))}
        manifest_ref = save(cp / 'manifest.json', manifest)
        receipt = {'schema': 1, 'state': 'TERMINAL_LOCAL' if self.terminal else 'BOUNDARY_LOCAL',
                   'publication_confirmed': False, 'run_id': replay.admission['run_id'],
                   'protocol_sha256': manifest['protocol_sha256'], 'synthetic': False,
                   'input_identity': manifest['input_identity'], 'before_at': None if not replay.cursor else at - 300,
                   'before_steps': replay.cursor, 'after_at': manifest['at'], 'after_steps': manifest['steps'],
                   'executed_steps': manifest['steps'] - replay.cursor,
                   'checkpoint': {'path': str(cp), 'manifest_sha256': manifest_ref['sha256'],
                                  'at': manifest['at'], 'steps': manifest['steps']},
                   'stop_reason': 'WINDOW_COMPLETE' if self.terminal else 'STEP_BOUNDARY', 'result_sha256': None}
        if self.terminal:
            result = save(replay.root / 'result.json', {'completed': True, 'synthetic': False,
                'protocol_sha256': manifest['protocol_sha256'],
                'window': {'warmup_start': self.bounds[0], 'start': self.bounds[1], 'end_exclusive': self.bounds[2]}})
            receipt['result_sha256'] = result['sha256']
        receipt.update(copy.deepcopy(self.receipt_changes))
        save(cp.with_name(cp.name + '.segment.json'), receipt)
        return receipt

    def verify_checkpoint(self, directory, expected):
        self.hit('verify_checkpoint')
        path = Path(directory) / 'manifest.json'
        assert digest(path.read_bytes()) == expected
        return json.loads(path.read_bytes())

    def restore(self, directory, destination, expected, recovery):
        self.hit('restore')
        assert recovery == {'confirmed': True, 'manifest_sha256': expected,
                            'attempt_id': self.fixture.admission['attempt_id'],
                            'registry_fence': self.fixture.admission['registry_sha256']}
        target = Path(destination)
        target.mkdir()
        shutil.copyfile(Path(directory) / 'caller.json', target / 'caller.json')
        return self.verify_checkpoint(directory, expected)

    def resume(self, directory, **kwargs):
        self.hit('resume')
        caller = json.loads((Path(directory) / 'caller.json').read_bytes())
        self.replay = ArtificialReplay(self, directory, kwargs['confirmed_admission'], caller['steps'])
        return self.replay


class Bundle:
    def __init__(self, root):
        self.root = root
        self.repo = root / 'repository'
        for name in candidate.CORE_SOURCES:
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix == '.json':
                p.write_bytes(raw({'generated_inert_source_configuration': True}))
            else:
                p.write_text('# Generated inert fixture, not production source.\n')
        for name in ('spectra_core', 'mcp_servers'):
            (self.repo / name).mkdir()
        paths = sorted(set(self.repo.rglob('*.py')) | {self.repo / name for name in candidate.CORE_SOURCES}) + [Path(candidate.__file__).resolve()]
        self.pins = {str(p): digest(p.read_bytes()) for p in paths}
        self.output = root / 'runtime'
        self.control = root / '.runtime.coordinator'
        self.attempt_id = 'artificial-attempt-1'
        self.request = {'schema': 1, 'workspace_root': str(ROOT), 'repository_root': str(self.repo),
                        'arm': 'CONTROL_BBWP50_SOFT_RSI', 'mode': 'FRESH',
                        'output_directory': str(self.output),
                        'attempt_directory': str(self.control / 'attempts' / self.attempt_id),
                        'checkpoint_directory': str(self.control / 'attempts' / self.attempt_id / 'checkpoint'),
                        'stop_file': str(root / 'STOP'), 'continuation': None}
        for name, value in [('source_pins', self.pins), ('rules', {'artificial_rules': True}),
                            ('protocol', {'fixed_protocol': True}), ('packet', {'artificial_packet': True})]:
            self.request[name] = save(root / (name + '.json'), value)
        self.request['input'] = {'path': str(root / 'ABSENT-NO-HISTORICAL-INPUT.npy'), 'sha256': '1' * 64}
        self.admission_changes = {}
        self.registry_changes = {}
        self.confirmation_changes = {}
        self.backend = ArtificialBackend(self)
        self.save()

    @property
    def attempt(self):
        return Path(self.request['attempt_directory'])

    def save(self):
        scope = {'run_id': 'artificial-run-1', 'attempt_id': self.attempt_id,
                 'arm': self.request['arm'], 'input_sha256': '1' * 64,
                 'protocol_sha256': self.request['protocol']['sha256'], 'bounds': self.backend.bounds[:],
                 'source_pins_sha256': self.request['source_pins']['sha256'],
                 'rule_sha256': self.request['rules']['sha256'], 'output_directory': str(self.output)}
        self.registry = {'historical_execution_admitted': True,
                         'old_campaign': {'consumed': 27, 'cap': 28, 'paused': True},
                         'new_campaign': {'cap': 60, 'currently_authorized_scoped_maximum': 10,
                                          'consumed': 1, 'runs': [dict(scope, state='FENCED', consumed=True)]}}
        self.registry.update(copy.deepcopy(self.registry_changes))
        self.request['registry'] = save(self.root / 'registry.json', self.registry)
        self.admission = dict(scope, historical_execution_admitted=True, confirmed_launch_fence=True,
                              registry_sha256=self.request['registry']['sha256'], coordinator=candidate._descriptor(self.request))
        self.admission.update(copy.deepcopy(self.admission_changes))
        self.request['admission'] = save(self.root / 'admission.json', self.admission)
        self.confirmation = {'state': 'DURABLE_PACKET_READBACK_CONFIRMED', 'library_file_id': 'libfile_' + 'a' * 32,
                             'library_version': 1, 'packet_sha256': self.request['packet']['sha256'],
                             'registry_sha256': self.request['registry']['sha256'],
                             'admission_sha256': self.request['admission']['sha256']}
        self.confirmation.update(copy.deepcopy(self.confirmation_changes))
        self.request['confirmation'] = save(self.root / 'confirmation.json', self.confirmation)
        self.invocation = save(self.root / 'invocation.json', self.request)
        return self

    def invoke(self):
        return candidate.execute_invocation(self.invocation['path'], self.invocation['sha256'], backend=self.backend)

    def rewrite_request(self):
        self.invocation = save(self.root / 'invocation.json', self.request)

    def outcome(self):
        return json.loads((self.attempt / 'outcome.json').read_bytes())

    def no_loader(self):
        assert 'load' not in self.backend.calls
        assert 'fresh' not in self.backend.calls
        assert 'run' not in self.backend.calls
        assert 'resume' not in self.backend.calls

    def continuation(self):
        previous = self.invoke()
        prior_cp = Path(self.request['checkpoint_directory'])
        prior_outcome = {'path': str(self.attempt / 'outcome.json'), 'sha256': digest((self.attempt / 'outcome.json').read_bytes())}
        prior_receipt = {'path': str(prior_cp.with_name(prior_cp.name + '.segment.json')),
                         'sha256': previous['segment_receipt_sha256']}
        self.output.rename(self.root / 'preserved-prior-runtime')
        previous_attempt = self.attempt_id
        self.attempt_id = 'artificial-attempt-2'
        self.request['mode'] = 'CONTINUE_KNOWN_SUCCESS'
        self.request['attempt_directory'] = str(self.control / 'attempts' / self.attempt_id)
        self.request['checkpoint_directory'] = str(self.attempt / 'checkpoint')
        proof = {'state': 'KNOWN_SUCCESS_CONTINUATION_CONFIRMED', 'prior_process_exit_code': 0,
                 'run_id': previous['run_id'], 'next_attempt_id': self.attempt_id,
                 'prior_attempt_id': previous_attempt, 'checkpoint_manifest_sha256': previous['checkpoint_manifest_sha256'],
                 'prior_outcome_sha256': prior_outcome['sha256'], 'prior_segment_receipt_sha256': prior_receipt['sha256'],
                 'output_directory': str(self.output),
                 'prior_publication': {'state': 'DURABLE_PACKET_READBACK_CONFIRMED', 'library_file_id': 'libfile_' + 'b' * 32,
                                       'library_version': 2, 'packet_sha256': '8' * 64}}
        self.request['continuation'] = {'checkpoint_directory': str(prior_cp),
                                        'manifest_sha256': previous['checkpoint_manifest_sha256'],
                                        'prior_outcome': prior_outcome, 'prior_segment_receipt': prior_receipt,
                                        'authority': save(self.root / 'recovery-authority.json', proof)}
        self.backend = ArtificialBackend(self)
        self.save()
        return self


    def repin_prior(self, *, caller_changes=None, saved_admission_changes=None,
                    manifest_changes=None, receipt_changes=None, outcome_changes=None):
        """Rebuild every synthetic hash after a semantic mutation, preserving trust-chain consistency."""
        c = self.request['continuation']
        cp = Path(c['checkpoint_directory'])
        caller = json.loads((cp / 'caller.json').read_bytes())
        caller.update(copy.deepcopy(caller_changes or {}))
        caller['admission'].update(copy.deepcopy(saved_admission_changes or {}))
        caller_ref = save(cp / 'caller.json', caller)
        manifest = json.loads((cp / 'manifest.json').read_bytes())
        manifest['files']['caller.json'] = {'sha256': caller_ref['sha256'], 'bytes': len(raw(caller))}
        manifest.update(copy.deepcopy(manifest_changes or {}))
        manifest_ref = save(cp / 'manifest.json', manifest)
        c['manifest_sha256'] = manifest_ref['sha256']
        receipt = json.loads(Path(c['prior_segment_receipt']['path']).read_bytes())
        receipt['checkpoint']['manifest_sha256'] = manifest_ref['sha256']
        receipt.update(copy.deepcopy(receipt_changes or {}))
        c['prior_segment_receipt'] = save(c['prior_segment_receipt']['path'], receipt)
        outcome = json.loads(Path(c['prior_outcome']['path']).read_bytes())
        outcome['checkpoint_manifest_sha256'] = c['manifest_sha256']
        outcome['segment_receipt_sha256'] = c['prior_segment_receipt']['sha256']
        outcome.update(copy.deepcopy(outcome_changes or {}))
        c['prior_outcome'] = save(c['prior_outcome']['path'], outcome)
        proof = json.loads(Path(c['authority']['path']).read_bytes())
        proof.update(checkpoint_manifest_sha256=c['manifest_sha256'],
                     prior_outcome_sha256=c['prior_outcome']['sha256'],
                     prior_segment_receipt_sha256=c['prior_segment_receipt']['sha256'],
                     prior_attempt_id=outcome.get('attempt_id'))
        c['authority'] = save(c['authority']['path'], proof)
        self.save()
        return self


@pytest.fixture
def bundle(tmp_path):
    return Bundle(tmp_path)


def test_exact_fresh_invocation_has_fixed_limits_readonly_authority_and_local_only_success(bundle):
    before = {name: Path(bundle.request[name]['path']).read_bytes() for name in candidate.REF_NAMES if name != 'input'}
    result = bundle.invoke()
    assert result['state'] == 'LOCAL_SUCCESS'
    assert result['publication_confirmed'] is False
    assert result['observed_process_exit'] is False
    assert result['replay_closed'] is True
    assert not result['terminal']
    assert bundle.backend.calls.index('validate') < bundle.backend.calls.index('load')
    assert bundle.backend.calls.index('configuration') < bundle.backend.calls.index('load')
    assert bundle.backend.calls.index('close') < bundle.backend.calls.index('verify_checkpoint')
    assert all(Path(bundle.request[k]['path']).read_bytes() == v for k, v in before.items())
    assert bundle.outcome() == result


@pytest.mark.parametrize('name', [x for x in candidate.REF_NAMES if x != 'input'])
def test_mutated_artifact_rejects_before_loader(bundle, name):
    path = Path(bundle.request[name]['path'])
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('field,value', [('historical_execution_admitted', False), ('confirmed_launch_fence', False),
    ('arm', 'LOOSE_BBWP20_SOFT_RSI'), ('input_sha256', '9' * 64), ('protocol_sha256', '9' * 64),
    ('bounds', [0, 7200, 608400]), ('source_pins_sha256', '9' * 64), ('rule_sha256', '9' * 64),
    ('output_directory', '/wrong'), ('attempt_id', '../unsafe'), ('coordinator', {})])
def test_mismatched_admission_rejects_before_loader(bundle, field, value):
    bundle.admission_changes[field] = value
    bundle.save()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('field,value', [('packet_sha256', '9' * 64), ('state', 'UNKNOWN'),
    ('library_version', True), ('library_file_id', 'arbitrary')])
def test_mismatched_confirmation_rejects_before_loader(bundle, field, value):
    bundle.confirmation_changes[field] = value
    bundle.save()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('kind', ['source_changed', 'source_omitted', 'empty_map', 'input_in_sources'])
def test_source_closure_fails_closed_before_loader(bundle, kind):
    source = bundle.repo / candidate.CORE_SOURCES[0]
    if kind == 'source_changed':
        source.write_text('# Changed code\n')
    else:
        if kind == 'source_omitted':
            del bundle.pins[str(source)]
        elif kind == 'empty_map':
            bundle.pins = {}
        else:
            bundle.pins[bundle.request['input']['path']] = '1' * 64
        bundle.request['source_pins'] = save(bundle.root / 'source_pins.json', bundle.pins)
        bundle.save()
    with pytest.raises((ValueError, FileNotFoundError)):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('kind', ['stop_file', 'stop_directory', 'dangling_stop_symlink', 'runtime_directory', 'runtime_file', 'dangling_runtime_symlink'])
def test_existing_stop_or_runtime_rejected_without_replacement(bundle, kind):
    path = Path(bundle.request['stop_file']) if kind.startswith('stop') or kind == 'dangling_stop_symlink' else bundle.output
    if 'symlink' in kind:
        path.symlink_to(bundle.root / 'missing-target')
    elif 'directory' in kind:
        path.mkdir()
    else:
        path.write_text('preserve')
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()
    assert os.path.lexists(path)


@pytest.mark.parametrize('mode', ['AUTO', 'RESUME', 'UNKNOWN', None])
def test_no_implicit_recovery_mode(bundle, mode):
    bundle.request['mode'] = mode
    bundle.rewrite_request()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('field,value', [('schema', True), ('extra_strategy_flag', True), ('arm', 'UNREGISTERED'),
    ('attempt_directory', '/tmp/escape'), ('input', {'path': 'relative', 'sha256': '1' * 64})])
def test_request_schema_and_scope_reject_before_loader(bundle, field, value):
    bundle.request[field] = value
    bundle.rewrite_request()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('stage', ['configuration', 'membership', 'policy', 'load', 'fresh', 'run', 'verify_checkpoint', 'close'])
def test_failure_preserves_immutable_attempt_and_never_retries(bundle, stage):
    bundle.backend.failures[stage] = RuntimeError('artificial failure at ' + stage)
    with pytest.raises(RuntimeError, match='artificial failure'):
        bundle.invoke()
    outcome = bundle.outcome()
    assert outcome['state'] == 'FAILED_UNCERTAIN'
    assert (bundle.attempt / 'invocation.json').is_file()
    snapshot = {str(p): p.read_bytes() for p in bundle.attempt.rglob('*') if p.is_file()}
    if stage in ('run', 'verify_checkpoint', 'close'):
        assert 'close' in bundle.backend.calls
        assert (bundle.output / 'runtime-evidence.json').is_file()
    calls = bundle.backend.calls.count('load')
    with pytest.raises(ValueError):
        bundle.invoke()
    assert bundle.backend.calls.count('load') == calls
    assert {str(p): p.read_bytes() for p in bundle.attempt.rglob('*') if p.is_file()} == snapshot


def test_interrupt_closes_replay_and_preserves_failure(bundle):
    bundle.backend.failures['run'] = KeyboardInterrupt('artificial interruption')
    with pytest.raises(KeyboardInterrupt):
        bundle.invoke()
    assert 'close' in bundle.backend.calls
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


@pytest.mark.parametrize('stage', ['load', 'run', 'close'])
def test_per_run_lock_stays_held_during_entire_invocation(bundle, stage):
    nested = []
    def attempt_again():
        with pytest.raises(ValueError, match='writer is already held'):
            bundle.invoke()
        nested.append(True)
    bundle.backend.callbacks[stage] = attempt_again
    bundle.invoke()
    assert nested == [True]


@pytest.mark.parametrize('kind', ['unknown_attempt', 'failed_attempt', 'empty_attempt', 'attempt_symlink'])
def test_existing_attempt_evidence_blocks_fresh_even_with_new_attempt_id(bundle, kind):
    prior = bundle.control / 'attempts' / 'previous-attempt'
    prior.parent.mkdir(parents=True)
    if kind == 'attempt_symlink':
        prior.symlink_to(bundle.root / 'missing-attempt')
    else:
        prior.mkdir()
        if kind != 'empty_attempt':
            save(prior / ('outcome.json' if kind == 'failed_attempt' else 'invocation.json'), {'state': 'FAILED_UNCERTAIN'})
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()
    assert os.path.lexists(prior)


@pytest.mark.parametrize('kind', ['source', 'authority', 'stop', 'runtime'])
def test_configuration_time_changes_rechecked_before_loader(bundle, kind):
    def change():
        if kind == 'source':
            (bundle.repo / candidate.CORE_SOURCES[0]).write_text('# changed\n')
        elif kind == 'authority':
            p = Path(bundle.request['registry']['path'])
            p.write_bytes(p.read_bytes() + b' ')
        elif kind == 'stop':
            Path(bundle.request['stop_file']).write_text('stop')
        else:
            bundle.output.mkdir()
    bundle.backend.callbacks['configuration'] = change
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


@pytest.mark.parametrize('changes', [{'synthetic': True}, {'input_identity': {'source_file_sha256': '9' * 64, 'bounds': [0, 3600, 608400]}},
                                   {'input_identity': {'source_file_sha256': '1' * 64, 'bounds': [0, 7200, 608400]}}])
def test_loader_identity_is_checked_before_constructor(bundle, changes):
    bundle.backend.tape_changes = changes
    with pytest.raises(ValueError):
        bundle.invoke()
    assert 'load' in bundle.backend.calls
    assert 'fresh' not in bundle.backend.calls
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


def test_explicit_known_success_restores_original_path_under_new_attempt(bundle):
    bundle.continuation()
    before = (bundle.root / 'preserved-prior-runtime/runtime-evidence.json').read_bytes()
    result = bundle.invoke()
    assert result['state'] == 'LOCAL_SUCCESS'
    assert result['previous_outcome_sha256'] == bundle.request['continuation']['prior_outcome']['sha256']
    assert 'fresh' not in bundle.backend.calls
    assert bundle.backend.calls.index('verify_checkpoint') < bundle.backend.calls.index('load')
    assert bundle.backend.calls.index('restore') < bundle.backend.calls.index('resume') < bundle.backend.calls.index('run')
    assert (bundle.root / 'preserved-prior-runtime/runtime-evidence.json').read_bytes() == before


@pytest.mark.parametrize('field,value', [('state', 'UNKNOWN'), ('prior_process_exit_code', 1), ('prior_process_exit_code', True),
    ('run_id', 'other-run'), ('next_attempt_id', 'wrong-attempt'), ('prior_attempt_id', 'wrong-prior'),
    ('checkpoint_manifest_sha256', '9' * 64), ('output_directory', '/wrong'), ('prior_publication', {})])
def test_continuation_proof_must_be_exact_known_success_before_loader(bundle, field, value):
    bundle.continuation()
    c = bundle.request['continuation']
    proof = json.loads(Path(c['authority']['path']).read_bytes())
    proof[field] = value
    c['authority'] = save(c['authority']['path'], proof)
    bundle.save()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('field,value', [('arm', 'LOOSE_BBWP20_SOFT_RSI'), ('policy', {'enabled': False}),
    ('config', {'value': 'changed'}), ('membership', {'value': 'changed'}), ('bounds', [0, 7200, 608400])])
def test_saved_caller_scope_and_policy_reject_before_loader(bundle, field, value):
    bundle.continuation()
    if field == 'arm':
        bundle.repin_prior(saved_admission_changes={'arm': value})
    else:
        bundle.repin_prior(caller_changes={field: value})
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('changes', [{'at': 3599}, {'at': 608700}, {'at': True}, {'steps': 0}, {'steps': True},
    {'steps': 999}, {'input_identity': {'source_file_sha256': '1' * 64, 'bounds': [0, 7200, 608400]}}])
def test_postclose_checkpoint_requires_exact_boundary_and_input_scope(bundle, changes):
    bundle.backend.result_changes = changes
    with pytest.raises(ValueError):
        bundle.invoke()
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'
    assert bundle.backend.replay.closed


@pytest.mark.parametrize('changes', [{'input_identity': {'source_file_sha256': '9' * 64}}, {'after_steps': True},
    {'checkpoint': {'path': '/wrong', 'manifest_sha256': '9' * 64}}, {'publication_confirmed': True}])
def test_postclose_receipt_requires_exact_scope_and_typed_cursor(bundle, changes):
    bundle.backend.receipt_changes = changes
    with pytest.raises((ValueError, AssertionError)):
        bundle.invoke()
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


def test_terminal_result_is_copied_exactly_and_remains_local(bundle):
    bundle.backend.bounds = [0, 3600, 7200]
    bundle.save()
    bundle.backend.terminal = True
    result = bundle.invoke()
    assert result['terminal'] is True
    assert result['publication_confirmed'] is False
    actual = (bundle.output / 'result.json').read_bytes()
    assert (bundle.attempt / 'terminal_result.json').read_bytes() == actual
    assert result['terminal_result_sha256'] == digest(actual)


def test_cli_exposes_no_strategy_or_automatic_recovery_flags():
    result = subprocess.run([sys.executable, str(Path(candidate.__file__)), '--help'], text=True, capture_output=True)
    assert result.returncode == 0
    assert '--invocation-sha256' in result.stdout
    assert '--resume' not in result.stdout
    for flag in ('--max-steps', '--arm', '--backend', '--resume'):
        with pytest.raises(SystemExit):
            candidate.main([flag, 'unapproved'])


@pytest.mark.parametrize('field,value', [('state', 'FAILED_UNCERTAIN'), ('state', 'UNKNOWN'), ('replay_closed', False),
    ('terminal', True), ('arm', 'LOOSE_BBWP20_SOFT_RSI'), ('input_sha256', '9' * 64),
    ('protocol_sha256', '9' * 64), ('bounds', [0, 7200, 608400]), ('source_pins_sha256', '9' * 64),
    ('rule_sha256', '9' * 64), ('output_directory', '/wrong')])
def test_prior_outcome_scope_and_success_are_required(bundle, field, value):
    bundle.continuation().repin_prior(outcome_changes={field: value})
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('changes', [{'run_directory': '/wrong'}, {'synthetic': True}, {'source_pins': {}},
    {'protocol_sha256': '9' * 64}, {'at': 608400}, {'at': 3599}, {'steps': True},
    {'input_identity': {'source_file_sha256': '1' * 64, 'bounds': [0, 7200, 608400]}}])
def test_prior_manifest_scope_rejected_before_loader(bundle, changes):
    bundle.continuation().repin_prior(manifest_changes=changes)
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('changes', [{'after_steps': True}, {'after_at': 3600.0}, {'before_steps': False},
    {'input_identity': {'source_file_sha256': '9' * 64}}, {'state': 'TERMINAL_LOCAL'}])
def test_prior_receipt_exact_identity_and_types_required(bundle, changes):
    bundle.continuation().repin_prior(receipt_changes=changes)
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('field,value', [('before_steps', False), ('after_steps', 1.0), ('after_at', 3600.0),
    ('executed_steps', True), ('stop_reason', 'WINDOW_COMPLETE'), ('state', 'TERMINAL_LOCAL')])
def test_new_receipt_exact_types_and_state_required(bundle, field, value):
    bundle.backend.receipt_changes = {field: value}
    with pytest.raises(ValueError):
        bundle.invoke()
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


@pytest.mark.parametrize('field,value', [('steps', True), ('steps', 1.0), ('at', 3600.0)])
def test_checkpoint_descriptor_cursor_types_required(bundle, field, value):
    def change_descriptor():
        path = Path(bundle.request['checkpoint_directory'])
        receipt_path = path.with_name(path.name + '.segment.json')
        receipt = json.loads(receipt_path.read_bytes())
        receipt['checkpoint'][field] = value
        save(receipt_path, receipt)
    original_run = bundle.backend.run
    def changed_run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        result['checkpoint'][field] = value
        path = Path(bundle.request['checkpoint_directory'])
        save(path.with_name(path.name + '.segment.json'), result)
        return result
    bundle.backend.run = changed_run
    with pytest.raises(ValueError):
        bundle.invoke()


def test_unknown_sibling_attempt_blocks_explicit_continuation(bundle):
    bundle.continuation()
    sibling = bundle.control / 'attempts' / 'interrupted-unknown'
    sibling.mkdir()
    save(sibling / 'invocation.json', {'state': 'STARTED_LOCAL'})
    with pytest.raises(ValueError, match='Unknown prior attempt'):
        bundle.invoke()
    bundle.no_loader()
    assert (sibling / 'invocation.json').is_file()


@pytest.mark.parametrize('kind', ['changed_invocation', 'symlink_invocation', 'symlink_outcome'])
def test_changed_or_aliased_local_success_evidence_blocks_continuation(bundle, kind):
    bundle.continuation()
    prior = Path(bundle.request['continuation']['prior_outcome']['path']).parent
    target = prior / ('outcome.json' if kind == 'symlink_outcome' else 'invocation.json')
    if kind == 'changed_invocation':
        target.write_bytes(target.read_bytes() + b' ')
    else:
        replacement = bundle.root / ('preserved-' + target.name)
        target.rename(replacement)
        target.symlink_to(replacement)
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


def test_exclusive_lock_is_enforced_between_processes(bundle):
    code = ('import sys; from pathlib import Path; import research.weekly_reversal.coordinator as c; '
            '\nwith c._lock(Path(sys.argv[1])):\n print("LOCKED", flush=True); sys.stdin.readline()\n')
    process = subprocess.Popen([sys.executable, '-c', code, str(bundle.control)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 10)[0], 'Lock subprocess did not start'
        assert process.stdout.readline().strip() == 'LOCKED'
        with pytest.raises(ValueError, match='writer is already held'):
            bundle.invoke()
        bundle.no_loader()
        process.stdin.write('release\n')
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        assert bundle.invoke()['state'] == 'LOCAL_SUCCESS'
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize('stage', ['load', 'run'])
def test_abrupt_process_exit_preserves_unknown_attempt_and_forbids_reuse(bundle, stage):
    code = '''
import json, os, sys
from pathlib import Path
import research.weekly_reversal.coordinator as candidate
from test_weekly_coordinator_portable import Bundle, ArtificialBackend
b = object.__new__(Bundle)
b.root = Path(sys.argv[1])
b.request = json.loads((b.root / 'invocation.json').read_bytes())
b.pins = json.loads(Path(b.request['source_pins']['path']).read_bytes())
b.admission = json.loads(Path(b.request['admission']['path']).read_bytes())
b.backend = ArtificialBackend(b)
b.backend.callbacks[sys.argv[3]] = lambda: os._exit(71)
candidate.execute_invocation(b.root / 'invocation.json', sys.argv[2], backend=b.backend)
'''
    # Pytest's in-process sys.path changes do not reach a fresh interpreter.
    # Name this helper directory explicitly without requiring a caller PYTHONPATH.
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(
        [str(Path(__file__).resolve().parent),
         str(Path(candidate.__file__).resolve().parents[2])]
        + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
    process = subprocess.run([sys.executable, '-c', code, str(bundle.root), bundle.invocation['sha256'], stage],
                             text=True, capture_output=True, timeout=15, env=env)
    assert process.returncode == 71, process.stderr
    assert (bundle.attempt / 'invocation.json').is_file()
    assert not (bundle.attempt / 'outcome.json').exists()
    if stage == 'run':
        assert (bundle.output / 'runtime-evidence.json').is_file()
        bundle.output.rename(bundle.root / 'externally-preserved-crashed-runtime')
    prior_attempt = bundle.attempt
    bundle.attempt_id = 'artificial-attempt-after-crash'
    bundle.request['attempt_directory'] = str(bundle.control / 'attempts' / bundle.attempt_id)
    bundle.request['checkpoint_directory'] = str(bundle.attempt / 'checkpoint')
    bundle.save()
    with pytest.raises(ValueError, match='Fresh mode cannot reuse'):
        bundle.invoke()
    bundle.no_loader()
    assert (prior_attempt / 'invocation.json').is_file()
    assert not (prior_attempt / 'outcome.json').exists()


@pytest.mark.parametrize('filename', ['outcome.json', '.incomplete-outcome.json'])
def test_unexpected_outcome_record_is_never_overwritten(bundle, filename):
    bytes_to_preserve = raw({'state': 'UNKNOWN', 'evidence': 'preserve exactly'})
    def inject():
        (bundle.attempt / filename).write_bytes(bytes_to_preserve)
    bundle.backend.callbacks['run'] = inject
    with pytest.raises(ValueError, match='Immutable local record'):
        bundle.invoke()
    assert (bundle.attempt / filename).read_bytes() == bytes_to_preserve
    assert bundle.backend.replay.closed


def test_run_failure_records_original_stage_after_successful_close(bundle):
    bundle.backend.failures['run'] = RuntimeError('failure while running')
    with pytest.raises(RuntimeError):
        bundle.invoke()
    assert bundle.outcome()['stage'] == 'RUNNING_SEGMENT'
    assert bundle.backend.replay.closed


@pytest.mark.parametrize('stage', ['restore', 'resume'])
def test_continuation_failure_is_preserved_and_never_retried(bundle, stage):
    bundle.continuation()
    bundle.backend.failures[stage] = RuntimeError('artificial continuation failure')
    with pytest.raises(RuntimeError):
        bundle.invoke()
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'
    assert (bundle.root / 'preserved-prior-runtime/runtime-evidence.json').exists()
    before = (bundle.attempt / 'outcome.json').read_bytes()
    if stage == 'resume':
        assert (bundle.output / 'caller.json').exists()
    with candidate._lock(bundle.control):
        pass
    with pytest.raises(ValueError):
        bundle.invoke()
    assert (bundle.attempt / 'outcome.json').read_bytes() == before


@pytest.mark.parametrize('stage', ['restore', 'resume'])
def test_per_run_lock_stays_held_during_restoration(bundle, stage):
    bundle.continuation()
    checks = []
    def reenter():
        with pytest.raises(ValueError, match='writer is already held'):
            bundle.invoke()
        checks.append(True)
    bundle.backend.callbacks[stage] = reenter
    bundle.invoke()
    assert checks == [True]


@pytest.mark.parametrize('kind', ['stop', 'runtime'])
def test_postloader_stop_or_runtime_collision_preserves_attempt_before_constructor(bundle, kind):
    def intervene():
        if kind == 'stop':
            Path(bundle.request['stop_file']).write_text('stop')
        else:
            bundle.output.mkdir()
            save(bundle.output / 'unknown.json', {'preserve': True})
    bundle.backend.callbacks['load'] = intervene
    with pytest.raises(ValueError):
        bundle.invoke()
    assert 'load' in bundle.backend.calls
    assert 'fresh' not in bundle.backend.calls
    assert 'restore' not in bundle.backend.calls
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


@pytest.mark.parametrize('field', ['attempt_directory', 'checkpoint_directory'])
def test_nondeterministic_attempt_or_checkpoint_path_rejected_before_loader(bundle, field):
    bundle.request[field] = str(bundle.root / 'wrong-path')
    bundle.save()
    with pytest.raises(ValueError, match='deterministic'):
        bundle.invoke()
    bundle.no_loader()
    assert not (bundle.root / 'wrong-path').exists()


def test_workspace_root_cannot_be_used_as_output_and_create_external_control(bundle):
    bundle.request['workspace_root'] = str(bundle.root)
    bundle.request['output_directory'] = str(bundle.root)
    bundle.output = bundle.root
    bundle.save()
    outside = bundle.root.parent / ('.' + bundle.root.name + '.coordinator')
    assert not outside.exists()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()
    assert not outside.exists(), 'Rejected request created coordinator files outside its declared workspace'


@pytest.mark.parametrize('kind', ['control_file', 'control_symlink', 'lock_symlink', 'lock_directory', 'attempts_symlink'])
def test_coordinator_path_aliases_are_never_overwritten(bundle, kind):
    if kind == 'control_file':
        bundle.control.write_text('preserve')
    elif kind == 'control_symlink':
        other = bundle.root / 'other-control'
        other.mkdir()
        bundle.control.symlink_to(other, target_is_directory=True)
    else:
        bundle.control.mkdir()
        path = bundle.control / ('attempts' if kind == 'attempts_symlink' else 'writer.lock')
        if kind == 'lock_directory':
            path.mkdir()
        else:
            target = bundle.root / 'other-target'
            if kind == 'attempts_symlink':
                target.mkdir()
            else:
                target.write_text('preserve')
            path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('name', ['invocation', 'admission', 'confirmation', 'source_pins'])
def test_duplicate_json_keys_are_rejected_before_loader(bundle, name):
    if name == 'invocation':
        target = bundle.invocation
    else:
        target = bundle.request[name]
    data = Path(target['path']).read_bytes()
    duplicate = b'{"duplicate":1,"duplicate":2,' + data[1:]
    Path(target['path']).write_bytes(duplicate)
    target['sha256'] = digest(duplicate)
    if name != 'invocation':
        bundle.rewrite_request()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('value', [True, 1.0, -1, None])
def test_request_schema_requires_exact_integer_one(bundle, value):
    bundle.request['schema'] = value
    bundle.rewrite_request()
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


def test_source_pins_cover_newly_added_repository_modules(bundle):
    (bundle.repo / 'research/new_unpinned_dependency.py').write_text('# Generated unpinned file\n')
    with pytest.raises(ValueError, match='cover coordinator'):
        bundle.invoke()
    bundle.no_loader()


def test_exact_invocation_hash_is_required(bundle):
    p = Path(bundle.invocation['path'])
    p.write_bytes(p.read_bytes() + b' ')
    with pytest.raises(ValueError, match='Pinned bytes changed'):
        bundle.invoke()
    bundle.no_loader()


def test_frozen_input_hash_is_required_without_reading_input(bundle):
    bundle.request['input']['sha256'] = '9' * 64
    bundle.save()
    with pytest.raises(ValueError, match='Input identity differs'):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('field,value', [('max_steps', 288.0), ('max_wall_seconds', 240.0)])
def test_admission_coordinator_numeric_types_must_be_exact(bundle, field, value):
    descriptor = candidate._descriptor(bundle.request)
    descriptor[field] = value
    bundle.admission_changes['coordinator'] = descriptor
    bundle.save()
    with pytest.raises(ValueError, match='exact coordinator'):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('location,value', [('caller_bounds', [False, 3600, 608400]),
    ('caller_bounds', [0.0, 3600, 608400]), ('saved_admission_bounds', [0, 3600.0, 608400]),
    ('prior_outcome_bounds', [False, 3600, 608400]), ('caller_policy_enabled', 1),
    ('caller_policy_bbwp_minimum', 50.0), ('caller_steps', True), ('caller_last_at', 3600.0)])
def test_continuation_nested_scope_types_are_exact_before_loader(bundle, location, value):
    bundle.continuation()
    if location == 'caller_bounds':
        bundle.repin_prior(caller_changes={'bounds': value})
    elif location == 'saved_admission_bounds':
        bundle.repin_prior(saved_admission_changes={'bounds': value})
    elif location == 'prior_outcome_bounds':
        bundle.repin_prior(outcome_changes={'bounds': value})
    elif location.startswith('caller_policy'):
        policy = asdict(Policy())
        policy[location.removeprefix('caller_policy_')] = value
        bundle.repin_prior(caller_changes={'policy': policy})
    else:
        bundle.repin_prior(caller_changes={location.removeprefix('caller_'): value})
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()


@pytest.mark.parametrize('bounds', [[False, 3600, 608400], [0.0, 3600, 608400], [0, 3600.0, 608400]])
def test_loader_nested_bounds_types_are_exact(bundle, bounds):
    bundle.backend.tape_changes = {'input_identity': {'source_file_sha256': '1' * 64, 'bounds': bounds}}
    with pytest.raises(ValueError):
        bundle.invoke()
    assert 'fresh' not in bundle.backend.calls
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


@pytest.mark.parametrize('bounds', [[False, 3600, 608400], [0, 3600.0, 608400]])
def test_output_nested_identity_bounds_types_are_exact(bundle, bounds):
    bundle.backend.result_changes = {'input_identity': {'source_file_sha256': '1' * 64, 'bounds': bounds}}
    with pytest.raises(ValueError):
        bundle.invoke()
    assert bundle.outcome()['state'] == 'FAILED_UNCERTAIN'


def test_source_hardlink_to_generated_input_rejected_before_hashing_input(bundle, monkeypatch):
    # Only generated JSON is used, even at the fake input pathname.
    fake_input = Path(bundle.request['input']['path'])
    generated = raw({'artificial_input_guard': True})
    fake_input.write_bytes(generated)
    alias = bundle.root / 'input-disguised-as-source.py'
    os.link(fake_input, alias)
    bundle.pins[str(alias)] = digest(generated)
    bundle.request['source_pins'] = save(bundle.root / 'source_pins.json', bundle.pins)
    bundle.admission_changes['historical_execution_admitted'] = False
    bundle.save()
    original = candidate._digest
    observed = []
    def observe(path):
        if Path(path).exists() and Path(path).samefile(fake_input):
            observed.append(str(path))
        return original(path)
    monkeypatch.setattr(candidate, '_digest', observe)
    with pytest.raises(ValueError):
        bundle.invoke()
    bundle.no_loader()
    assert observed == [], 'Historical input alias was hashed before admission validation'


def test_pinned_source_outside_declared_workspace_rejected_before_hashing(bundle, monkeypatch):
    # The narrow workspace and all sibling fixture files remain inside the review
    # area. A module __file__ seam avoids making the real candidate an exception.
    actual_candidate = Path(candidate.__file__).resolve()
    local_candidate = bundle.root / 'generated-coordinator-source.py'
    local_candidate.write_text('# Inert generated coordinator-source identity fixture.\n')
    del bundle.pins[str(actual_candidate)]
    bundle.pins[str(local_candidate)] = digest(local_candidate.read_bytes())
    monkeypatch.setattr(candidate, '__file__', str(local_candidate))
    bundle.request['workspace_root'] = str(bundle.root)
    outside = bundle.root.parent / (bundle.root.name + '-outside-source.json')
    outside.write_bytes(raw({'generated_sibling_source': True}))
    bundle.pins[str(outside)] = digest(outside.read_bytes())
    bundle.request['source_pins'] = save(bundle.root / 'source_pins.json', bundle.pins)
    bundle.save()
    observed = []
    original = candidate._digest
    def observe(path):
        if Path(path) == outside:
            observed.append(str(path))
        return original(path)
    monkeypatch.setattr(candidate, '_digest', observe)
    with pytest.raises(ValueError, match='workspace'):
        bundle.invoke()
    bundle.no_loader()
    assert observed == [], 'Out-of-workspace pinned source was hashed before rejection'


def test_required_native_source_configuration_pin_cannot_be_omitted(bundle):
    required = bundle.repo / 'research/weekly_reversal/owned_spot/native_source_pins.json'
    assert required.is_file()
    assert str(required) in bundle.pins
    del bundle.pins[str(required)]
    bundle.request['source_pins'] = save(bundle.root / 'source_pins.json', bundle.pins)
    bundle.save()
    with pytest.raises(ValueError, match='cover coordinator'):
        bundle.invoke()
    bundle.no_loader()
    assert not bundle.attempt.exists()
