"""Native adapter import/wiring only; no historical loader or replay execution."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from research.weekly_reversal import coordinator as candidate

REVIEW = Path(__file__).resolve().parent
REPO = Path(candidate.__file__).resolve().parents[2]
ROOT = Path(os.environ.get('WEEKLY_COORDINATOR_TEST_WORKSPACE', str(REPO.parent))).resolve()


@pytest.fixture(scope='module')
def _generated_workspace():
    # Retain uniquely created evidence; never delete or reuse caller directories.
    return Path(tempfile.mkdtemp(prefix='.coordinator-generated-', dir=ROOT))


@pytest.fixture
def tmp_path(_generated_workspace):
    return Path(tempfile.mkdtemp(prefix='case-', dir=_generated_workspace))


def test_native_adapter_imports_frozen_modules_and_wires_only_recording_stubs(tmp_path):
    script = r'''
import hashlib, json, sys
from pathlib import Path
import importlib.util
repo, scratch = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('_direct_coordinator_contract', repo/'research/weekly_reversal/coordinator.py')
module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
NativeBackend, _plain = module.NativeBackend, module._plain
pins = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
        for package in ('research','spectra_core','mcp_servers')
        for p in (repo / package).rglob('*.py')}
pins[str((repo/'research/weekly_reversal/owned_spot/native_source_pins.json').resolve())] = hashlib.sha256((repo/'research/weekly_reversal/owned_spot/native_source_pins.json').read_bytes()).hexdigest()
backend = NativeBackend({'repository_root':str(repo), 'attempt_directory':str(scratch/'absent-attempt')}, pins)
assert backend.bounds == [1777766400, 1780444800, 1781049600]
assert backend.input_sha256 == '53dd88099aaf140d677dfd72ef88e1caa626c9651319d194798558bca8f45972'
# Artificial exchange-rule records only.
rules = [{'asset': asset, 'status':'online', 'execution_venue':'international',
          'base_increment':'0.0001', 'minimum_base':'0.0001',
          'minimum_quote_usd':'1', 'price_tick_usd':'0.01'} for asset in backend.roster]
rule_path=scratch/'generated_rules.json';rule_path.write_text(json.dumps(rules))
config=backend.configuration(rule_path,hashlib.sha256(rule_path.read_bytes()).hexdigest())
assert config.fee_rate == __import__('decimal').Decimal('.008')
assert len(config.rules) == 15
assert len(backend.membership(config).members) == 16
assert backend.arm_policy('CONTROL_BBWP50_SOFT_RSI').bbwp_minimum == 50
assert backend.arm_policy('LOOSE_BBWP20_SOFT_RSI').bbwp_minimum == 20
# Never call production loader/checkpoint/replay/runner. Replace every callable.
calls=[]
def record(name):
    def stub(*args, **kwargs):
        calls.append((name,args,kwargs));return name
    return stub
backend.gate.validate_launch_packet=record('validate')
backend.tape.load_admitted_tape=record('load')
backend.checkpoint.verify_checkpoint=record('verify')
backend.checkpoint.restore_checkpoint=record('restore')
backend.runner.run_completed_segment=record('run')
class FakeReplay:
    def __new__(cls,*args,**kwargs): return record('fresh')(*args,**kwargs)
    resume_restored=staticmethod(record('resume'))
backend.replay.WeeklyReplay=FakeReplay
assert backend.validate(marker='authority') == 'validate'
assert backend.load('UNOPENED_GENERATED_INPUT', {'marker':'admission'}) == 'load'
assert backend.fresh('generated-output', tape='fake-tape') == 'fresh'
assert backend.resume('generated-output', tape='fake-tape') == 'resume'
assert backend.verify_checkpoint('generated-checkpoint','a'*64) == 'verify'
assert backend.restore('generated-checkpoint','generated-output','a'*64,{'confirmed':True}) == 'restore'
assert backend.run('fake-replay','generated-checkpoint',max_steps=288,max_wall_seconds=240) == 'run'
by_name={name:(args,kwargs) for name,args,kwargs in calls}
assert by_name['load'] == (('UNOPENED_GENERATED_INPUT',),dict(expected_sha256=backend.input_sha256,
 admission={'marker':'admission'},data_start=backend.data_start,warmup_start=backend.bounds[0],
 start=backend.bounds[1],end=backend.bounds[2],roster=backend.roster))
assert by_name['verify'] == (('generated-checkpoint',),{'expected_manifest_sha256':'a'*64})
assert by_name['restore'] == (('generated-checkpoint','generated-output'),
 {'expected_manifest_sha256':'a'*64,'confirmed_recovery':{'confirmed':True}})
assert not (scratch/'absent-attempt').exists()
print('Native import/configuration and record-only adapter wiring verified; no input opened or replay constructed.')
'''
    env = os.environ.copy()
    env['HOME'] = os.environ.get('HOME', str(ROOT / 'test_home'))
    env['PYTHONPATH'] = ':'.join(map(str, [REVIEW, REPO, REPO / 'tests'])) + ':' + os.environ.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-B', '-c', script, str(REPO), str(tmp_path)],
                            env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no input opened or replay constructed' in result.stdout
