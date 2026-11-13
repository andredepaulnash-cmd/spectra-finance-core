import json
import unittest
from pathlib import Path

from scripts.spectra_release import AUTHORITY, load_manifest, validate_document

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "spectra-release-manifest.json"


class ReleaseManifestTests(unittest.TestCase):
    def test_checked_in_release_manifest_is_valid_candidate(self):
        manifest = load_manifest(MANIFEST)
        self.assertEqual(validate_document(manifest), [])
        self.assertEqual(manifest["release_status"], "candidate")
        self.assertIs(manifest["deployment"]["paper_only_default"], True)
        self.assertIs(manifest["deployment"]["live_execution_enabled"], False)
        self.assertEqual(manifest["authority_invariant"], AUTHORITY)

    def test_manifest_requires_all_platform_components(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        del manifest["components"]["gateway"]
        self.assertTrue(any(
            "missing required components: gateway" in error
            for error in validate_document(manifest)
        ))

    def test_manifest_rejects_live_execution_or_changed_authority(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest["deployment"]["live_execution_enabled"] = True
        manifest["authority_invariant"] = "Condor can authorize capital"
        errors = validate_document(manifest)
        self.assertTrue(any("live_execution_enabled must remain false" in e for e in errors))
        self.assertTrue(any("authority_invariant" in e for e in errors))

    def test_manifest_rejects_credential_fields(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest["components"]["hummingbot_api"]["api_key"] = "must-not-exist"
        self.assertTrue(any("credential fields" in e for e in validate_document(manifest)))

    def test_approved_release_cannot_have_blockers(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest["release_status"] = "approved"
        manifest["deployment"]["ready_for_installable_release"] = True
        self.assertTrue(any(
            "cannot retain promotion blockers" in e
            for e in validate_document(manifest)
        ))


if __name__ == "__main__":
    unittest.main()
