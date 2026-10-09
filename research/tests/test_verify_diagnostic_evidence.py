"""Artifact-integrity tests; the tiny fixtures contain no market results."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

from research.verify_diagnostic_evidence import MANIFEST_NAME, verify_bundle


class EvidenceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = Path(self.directory.name) / "fixture.zip"

    def fixture(self, *, wrong_digest=False, wrong_size=False, extra=False):
        payload = b"synthetic integrity fixture\n"
        spec = {
            "bytes": len(payload) + int(wrong_size),
            "sha256": "0" * 64 if wrong_digest else hashlib.sha256(payload).hexdigest(),
        }
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("fixture.txt", payload)
            archive.writestr(MANIFEST_NAME, json.dumps({"payloads": {"fixture.txt": spec}}))
            if extra:
                archive.writestr("undeclared.txt", b"extra")
        return hashlib.sha256(self.bundle.read_bytes()).hexdigest()

    def test_complete_bundle_passes_without_promotion(self):
        result = verify_bundle(self.bundle, self.fixture())
        self.assertEqual(result["payload_count"], 1)
        self.assertFalse(result["production_promotion"])
        self.assertFalse(result["historical_strategy_rerun"])

    def test_changed_bundle_pin_rejected(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, "frozen SHA"):
            verify_bundle(self.bundle, "0" * 64)

    def test_malformed_pin_rejected(self):
        with self.assertRaisesRegex(ValueError, "pin is required"):
            verify_bundle(self.bundle, "remembered result")

    def test_changed_payload_digest_rejected(self):
        pin = self.fixture(wrong_digest=True)
        with self.assertRaisesRegex(ValueError, "Payload digest"):
            verify_bundle(self.bundle, pin)

    def test_changed_payload_size_rejected(self):
        pin = self.fixture(wrong_size=True)
        with self.assertRaisesRegex(ValueError, "Payload byte"):
            verify_bundle(self.bundle, pin)

    def test_undeclared_member_rejected(self):
        pin = self.fixture(extra=True)
        with self.assertRaisesRegex(ValueError, "declared payloads differ"):
            verify_bundle(self.bundle, pin)

    def test_duplicate_members_rejected(self):
        self.fixture()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.bundle, "a") as archive:
                archive.writestr("fixture.txt", b"duplicate")
        pin = hashlib.sha256(self.bundle.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            verify_bundle(self.bundle, pin)

    def test_missing_manifest_rejected(self):
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("fixture.txt", b"no manifest")
        pin = hashlib.sha256(self.bundle.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Missing diagnostic manifest"):
            verify_bundle(self.bundle, pin)


if __name__ == "__main__":
    unittest.main()
