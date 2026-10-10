import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'isolated_build_preflight', ROOT / 'scripts/spectra_isolated_build_preflight.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

class IsolatedBuildPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        receipt = json.loads((ROOT / MODULE.INPUTS_PATH).read_text())
        for name in [MODULE.INPUTS_PATH] + [x['path'] for x in receipt['files']]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.patch = self.root / 'deploy/hummingbot_core/patches/order_executor.py'

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_inputs_never_claim_build_installation_or_authority(self):
        result = MODULE.collect(self.root, 'continuumio/miniconda3@sha256:' + 'a' * 64)
        self.assertTrue(result['source_inputs_verified'])
        self.assertEqual(result['checked_source_files'], 16)
        self.assertEqual(result['build_status'], 'NOT_RUN')
        for key in ('base_image_independently_reviewed', 'daemon_and_buildkit_verified',
                    'compiled_wheel_verified_by_this_preflight', 'image_built_by_this_preflight',
                    'installed_bazzite_verified', 'approved_for_live', 'production_modified'):
            self.assertIs(result[key], False)

    def test_changed_overlay_refused(self):
        self.patch.write_bytes(self.patch.read_bytes() + b'\n# changed\n')
        self.assertFalse(MODULE.collect(self.root)['source_inputs_verified'])

    def test_changed_lock_refused(self):
        (self.root / 'spectra-stack.lock.json').write_text('{}\n')
        self.assertFalse(MODULE.collect(self.root)['source_inputs_verified'])

    def test_input_receipt_cannot_redeclare_changed_bytes(self):
        path = self.root / MODULE.INPUTS_PATH
        document = json.loads(path.read_text())
        document['files'] = []
        path.write_text(json.dumps(document))
        self.assertFalse(MODULE.collect(self.root)['source_inputs_verified'])

    def test_symlink_refused_even_when_target_contains_expected_bytes(self):
        target = self.root / 'same-bytes.py'
        self.patch.rename(target)
        self.patch.symlink_to(target)
        self.assertFalse(MODULE.collect(self.root)['source_inputs_verified'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO test requires POSIX')
    def test_fifo_refused_without_waiting_for_writer(self):
        self.patch.unlink()
        os.mkfifo(self.patch)
        self.assertFalse(MODULE.collect(self.root)['source_inputs_verified'])

    def test_missing_input_refused(self):
        self.patch.unlink()
        self.assertFalse(MODULE.collect(self.root)['source_inputs_verified'])

    def test_invalid_base_declaration_not_exported(self):
        result = MODULE.collect(self.root, 'private-value-do-not-export')
        self.assertIsNone(result['base_image_declaration'])
        self.assertNotIn('private-value-do-not-export', json.dumps(result))

if __name__ == '__main__':
    unittest.main()
