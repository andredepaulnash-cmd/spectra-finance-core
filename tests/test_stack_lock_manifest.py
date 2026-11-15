import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "spectra-stack.lock.json"


def test_stack_lock_is_explicitly_partial_until_runtime_is_pinned():
    data = json.loads(LOCK.read_text())

    assert data["schema_version"] == 1
    assert data["lock_state"] == "PARTIAL"
    assert data["deployment"]["approved_for_live"] is False
    assert data["components"]["spectra_core"]["revision"] == "SELF"

    api_revision = data["components"]["spectra_hummingbot_api"]["revision"]
    assert re.fullmatch(r"[0-9a-f]{40}", api_revision)

    hb_core = data["components"]["hummingbot_core"]
    assert hb_core["locked"] is True
    assert hb_core["revision"] == "20260922"

    blockers = " ".join(data["deployment"]["blockers"]).lower()
    assert "source commit" in blockers
    assert "mutable" in blockers
    assert "bazzite" in blockers


def test_stack_lock_preserves_capital_authority_invariant():
    data = json.loads(LOCK.read_text())
    assert (
        data["authority_invariant"]
        == "Spectra Finance orchestrates. Hummingbot executes. Only Spectra Core may authorize capital."
    )
