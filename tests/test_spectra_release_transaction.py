from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.spectra_release_transaction import (
    activate_release,
    rollback_release,
    self_test,
)


def test_isolated_release_apply_and_rollback_cycle():
    result = self_test()
    assert result == {
        "status": "passed",
        "after_apply": "candidate",
        "after_rollback": "known-good",
        "previous_release_retained": True,
        "live_installation_touched": False,
    }


def test_release_rollback_refuses_if_current_pointer_moved(tmp_path):
    root = tmp_path / "release-root"
    releases = root / "releases"
    (releases / "known-good").mkdir(parents=True)
    (releases / "candidate").mkdir()
    (releases / "later").mkdir()
    os.symlink("releases/known-good", root / "current")
    record = activate_release(root, "candidate")
    (root / "current").unlink()
    os.symlink("releases/later", root / "current")

    with pytest.raises(ValueError, match="changed after this transaction"):
        rollback_release(root, record.stem)


def test_release_transaction_rejects_path_traversal_and_missing_candidate(tmp_path):
    root = tmp_path / "release-root"
    (root / "releases").mkdir(parents=True)
    with pytest.raises(ValueError, match="unsupported characters"):
        activate_release(root, "../candidate")
    with pytest.raises(ValueError, match="does not exist"):
        activate_release(root, "missing")


def test_release_transaction_record_tracks_rollback(tmp_path):
    root = tmp_path / "release-root"
    releases = root / "releases"
    (releases / "known-good").mkdir(parents=True)
    (releases / "candidate").mkdir()
    os.symlink("releases/known-good", root / "current")
    record_path = activate_release(root, "candidate")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "active"
    rollback_release(root, record["transaction_id"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "rolled-back"
    assert (root / "current").resolve() == (releases / "known-good").resolve()
