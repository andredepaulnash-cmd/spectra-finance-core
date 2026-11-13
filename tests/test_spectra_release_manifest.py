import json
from pathlib import Path

from scripts.spectra_release import AUTHORITY, load_manifest, validate_document

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "spectra-release-manifest.json"


def test_checked_in_release_manifest_is_valid_candidate():
    manifest = load_manifest(MANIFEST)
    assert validate_document(manifest) == []
    assert manifest["release_status"] == "candidate"
    assert manifest["deployment"]["paper_only_default"] is True
    assert manifest["deployment"]["live_execution_enabled"] is False
    assert manifest["authority_invariant"] == AUTHORITY


def test_manifest_requires_all_platform_components():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    del manifest["components"]["gateway"]
    assert any("missing required components: gateway" in e for e in validate_document(manifest))


def test_manifest_rejects_live_execution_or_changed_authority():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["deployment"]["live_execution_enabled"] = True
    manifest["authority_invariant"] = "Condor can authorize capital"
    errors = validate_document(manifest)
    assert any("live_execution_enabled must remain false" in e for e in errors)
    assert any("authority_invariant" in e for e in errors)


def test_manifest_rejects_credential_fields():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["components"]["hummingbot_api"]["api_key"] = "must-not-exist"
    assert any("credential fields" in e for e in validate_document(manifest))


def test_approved_release_cannot_have_blockers():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["release_status"] = "approved"
    manifest["deployment"]["ready_for_installable_release"] = True
    assert any("cannot retain promotion blockers" in e for e in validate_document(manifest))
