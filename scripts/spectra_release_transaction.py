#!/usr/bin/env python3
"""Atomic release-pointer transaction primitive.

This is deliberately a staging primitive, not an Alpha deployment switch: the
current installer still uses fixed source directories. It is used by CI to
prove that a staged release can be activated and rolled back without deleting
the prior release. Live apply remains disabled until the install layout is
migrated to this pointer model and the host inventory is approved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("release root must not be a symlink")
    return root


def _atomic_symlink(root: Path, target: Path) -> None:
    temp = root / f".current-{uuid.uuid4().hex}"
    relative = os.path.relpath(target, root)
    try:
        os.symlink(relative, temp)
        os.replace(temp, root / "current")
    finally:
        if temp.is_symlink():
            temp.unlink()


def _write_record(path: Path, record: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".transaction-", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def activate_release(root: Path, release_id: str) -> Path:
    if not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("release id contains unsupported characters")
    root = _safe_root(root)
    releases = root / "releases"
    transactions = root / "transactions"
    if releases.is_symlink() or transactions.is_symlink():
        raise ValueError("release and transaction directories must not be symlinks")
    releases.mkdir(exist_ok=True)
    transactions.mkdir(exist_ok=True)
    candidate = releases / release_id
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"staged release does not exist as a regular directory: {release_id}")
    candidate = candidate.resolve(strict=True)
    if releases.resolve() not in candidate.parents:
        raise ValueError("staged release escaped the releases directory")

    current = root / "current"
    previous: str | None = None
    if current.is_symlink():
        previous = os.readlink(current)
        old_target = (root / previous).resolve(strict=True)
        if releases.resolve() not in old_target.parents:
            raise ValueError("current release pointer targets outside releases/")
    elif current.exists():
        raise ValueError("current release pointer exists but is not a symlink")

    transaction_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    record = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "status": "active",
        "previous_target": previous,
        "candidate_target": os.path.relpath(candidate, root),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    record_path = transactions / f"{transaction_id}.json"
    _write_record(record_path, record)
    try:
        _atomic_symlink(root, candidate)
    except Exception:
        record["status"] = "activation-failed"
        _write_record(record_path, record)
        raise
    return record_path


def rollback_release(root: Path, transaction_id: str) -> Path:
    if not _RELEASE_ID.fullmatch(transaction_id):
        raise ValueError("transaction id contains unsupported characters")
    root = _safe_root(root)
    transactions = root / "transactions"
    record_path = transactions / f"{transaction_id}.json"
    if record_path.is_symlink() or not record_path.is_file():
        raise ValueError("release transaction record does not exist")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("status") != "active":
        raise ValueError("release transaction is not active")
    current = root / "current"
    if not current.is_symlink():
        raise ValueError("current release pointer is missing or not a symlink")
    if os.readlink(current) != record.get("candidate_target"):
        raise ValueError("current release changed after this transaction; refusing stale rollback")

    previous = record.get("previous_target")
    if not previous:
        raise ValueError("transaction has no previous release to roll back to")
    previous_path = (root / previous).resolve(strict=True)
    releases = (root / "releases").resolve(strict=True)
    if releases not in previous_path.parents or not previous_path.is_dir():
        raise ValueError("previous release target is missing or outside releases/")
    _atomic_symlink(root, previous_path)
    record["status"] = "rolled-back"
    record["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    _write_record(record_path, record)
    return record_path


def self_test() -> dict[str, Any]:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="spectra-release-transaction-") as temporary:
        root = Path(temporary)
        releases = root / "releases"
        (releases / "known-good").mkdir(parents=True)
        (releases / "candidate").mkdir()
        (releases / "known-good" / "release.txt").write_text("known-good", encoding="utf-8")
        (releases / "candidate" / "release.txt").write_text("candidate", encoding="utf-8")
        os.symlink("releases/known-good", root / "current")
        record = activate_release(root, "candidate")
        after_apply = (root / "current" / "release.txt").read_text(encoding="utf-8")
        transaction_id = record.stem
        rollback_release(root, transaction_id)
        after_rollback = (root / "current" / "release.txt").read_text(encoding="utf-8")
        assert after_apply == "candidate"
        assert after_rollback == "known-good"
        assert (releases / "candidate" / "release.txt").read_text(encoding="utf-8") == "candidate"
        return {
            "status": "passed",
            "after_apply": after_apply,
            "after_rollback": after_rollback,
            "previous_release_retained": True,
            "live_installation_touched": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    test_parser = sub.add_parser("self-test", help="run an isolated apply/rollback proof")
    activate_parser = sub.add_parser("activate", help="activate a staged release pointer")
    activate_parser.add_argument("--root", type=Path, required=True)
    activate_parser.add_argument("--release", required=True)
    rollback_parser = sub.add_parser("rollback", help="roll back an active release pointer")
    rollback_parser.add_argument("--root", type=Path, required=True)
    rollback_parser.add_argument("--transaction", required=True)
    args = parser.parse_args(argv)
    if args.command == "self-test":
        print(json.dumps(self_test(), indent=2))
        return 0
    if args.command == "activate":
        print(activate_release(args.root, args.release))
        return 0
    if args.command == "rollback":
        print(rollback_release(args.root, args.transaction))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
