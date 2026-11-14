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



def test_release_transaction_rejects_symlink_root(tmp_path):
    target = tmp_path / "real-root"
    target.mkdir()
    link = tmp_path / "linked-root"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="release root must not be a symlink"):
        activate_release(link, "candidate")


def test_release_rollback_rejects_symlink_transaction_directory(tmp_path):
    root = tmp_path / "release-root"
    releases = root / "releases"
    (releases / "known-good").mkdir(parents=True)
    (releases / "candidate").mkdir()
    os.symlink("releases/candidate", root / "current")

    outside = tmp_path / "outside-transactions"
    outside.mkdir()
    transaction_id = "20261113T000000Z-deadbeef"
    (outside / f"{transaction_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "transaction_id": transaction_id,
            "status": "active",
            "previous_target": "releases/known-good",
            "candidate_target": "releases/candidate",
        }),
        encoding="utf-8",
    )
    (root / "transactions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="transaction directory must not be a symlink"):
        rollback_release(root, transaction_id)
