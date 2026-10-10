import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('compile_isolated', ROOT / 'scripts/spectra_compile_isolated_core_wheel.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

class PhysicalSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'setup.py').write_text("raise RuntimeError('source-only mode must not execute setup')\n")
        (self.source / '.gitignore').write_text('*.pyc\n')
        (self.source / 'hummingbot').mkdir()
        original = b'value = 1\n'
        (self.source / 'hummingbot/one.py').write_bytes(original)
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Fixture source')
        self.head = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        replacement = b'value = 2\n'
        guard = b'guard = True\n'
        (self.source / 'hummingbot/one.py').write_bytes(replacement)
        (self.source / 'hummingbot/new_guard.py').write_bytes(guard)
        manifest = {'files': [
            {'path': 'hummingbot/one.py', 'sha256': hashlib.sha256(replacement).hexdigest(),
             'upstream_sha256': hashlib.sha256(original).hexdigest()},
            {'path': 'hummingbot/new_guard.py', 'sha256': hashlib.sha256(guard).hexdigest(),
             'upstream_sha256': None}]}
        self.manifest = self.root / 'manifest.json'
        data = json.dumps(manifest).encode()
        self.manifest.write_bytes(data)
        self.digest = hashlib.sha256(data).hexdigest()
        self.output = self.root / 'output'

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
        return subprocess.check_output(['git', '-C', str(self.source),
                                        '-c', 'core.hooksPath=/dev/null', *args],
                                       env=env, stderr=subprocess.DEVNULL).decode().strip()

    def run_check(self):
        argv = ['compile', '--core-checkout', str(self.source), '--manifest', str(self.manifest),
                '--wheel-dir', str(self.output), '--verify-source-only']
        with patch.object(MODULE, 'CORE_COMMIT', self.head), patch.object(MODULE, 'CORE_TREE', self.tree), \
             patch.object(MODULE, 'MANIFEST_SHA256', self.digest), patch.object(sys, 'argv', argv), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return MODULE.main()

    def refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_check()
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.output.exists())

    def test_source_only_checks_all_bytes_and_writes_no_output(self):
        self.assertEqual(self.run_check(), 0)
        self.assertFalse(self.output.exists())

    def test_changed_setup_hidden_by_assume_unchanged_refused(self):
        self.git('update-index', '--assume-unchanged', 'setup.py')
        (self.source / 'setup.py').write_text('unsafe_change = True\n')
        self.refused()

    def test_changed_setup_hidden_by_skip_worktree_refused(self):
        self.git('update-index', '--skip-worktree', 'setup.py')
        (self.source / 'setup.py').write_text('unsafe_change = True\n')
        self.refused()

    def test_ignored_bytecode_refused(self):
        (self.source / 'hidden.pyc').write_bytes(b'ignored input')
        self.refused()

    def test_extra_python_input_refused(self):
        (self.source / 'hummingbot/extra.py').write_text('extra = True\n')
        self.refused()

    def test_upstream_executable_mode_change_refused(self):
        (self.source / 'setup.py').chmod(0o755)
        self.refused()

    def test_symlink_in_place_of_pinned_file_refused(self):
        target = self.root / 'outside.py'
        target.write_bytes((self.source / 'setup.py').read_bytes())
        (self.source / 'setup.py').unlink()
        (self.source / 'setup.py').symlink_to(target)
        self.refused()

    def test_extra_source_symlink_directory_refused(self):
        (self.source / 'extra').symlink_to(self.root, target_is_directory=True)
        self.refused()

    def test_wrong_overlay_refused(self):
        (self.source / 'hummingbot/one.py').write_text('wrong = True\n')
        self.refused()

class WheelCheckInvocationTests(unittest.TestCase):
    def test_optimized_interpreter_cannot_claim_success(self):
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-O',
                                 str(ROOT / 'scripts/spectra_check_compiled_data_module.py'),
                                 '--manifest', '/missing-manifest', '--wheel', '/missing-wheel',
                                 '--expected-wheel-sha256', 'a' * 64],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('round_trip_passed', result.stdout)
        if sys.version_info[:2] == (3, 12):
            self.assertIn('without -O', result.stderr)

if __name__ == '__main__':
    unittest.main()
