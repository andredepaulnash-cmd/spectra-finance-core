"""Synthetic files and databases only; no historical bars or cleanup actions."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from research.weekly_reversal import checkpoint_retention as retention
from research.weekly_reversal.checkpoint_retention import (
    CheckpointArchive,
    RetentionError,
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_plan(root, **updates):
    plan = {
        "schema": 1,
        "state": "SYNTHETIC_ONLY_APPEND_ONLY",
        "workspace_root": str(root),
        "archive_root": str(root / "archive"),
        "run_directory": str(root / "run"),
        "chunk_bytes": 4096,
        "max_archive_bytes": 128 * 1024 * 1024,
        "reserve_bytes": 16 * 1024 * 1024,
        "max_checkpoint_bytes": 4 * 1024 * 1024,
        "max_generation_bytes": 1024 * 1024,
        "max_generations": 256,
    }
    plan.update(updates)
    path = root / "retention-plan.json"
    path.write_bytes(dump(plan))
    return path, sha(path.read_bytes())


def open_archive(root, **updates):
    path, digest = write_plan(root, **updates)
    return CheckpointArchive(path, expected_plan_sha256=digest, enabled=True)


def make_checkpoint(root, step=1, *, events=b'{"event":1}\n', sidecars=False):
    directory = root / ("checkpoint-" + str(step))
    (directory / "account").mkdir(parents=True)
    source = root / "fixture_source.py"
    if not source.exists():
        source.write_text("# Synthetic retention fixture, no historical input.\n")
    at = 1000 + step * 300
    for name in retention.FILES[:3]:
        with sqlite3.connect(directory / name) as db:
            db.execute("CREATE TABLE state(id INTEGER PRIMARY KEY, value TEXT)")
            db.execute(
                "INSERT INTO state VALUES(1, ?)",
                (json.dumps({"now": at if name == retention.FILES[0] else 1}),),
            )
    identity = {"synthetic": True, "fixture": "retention-test-v1"}
    caller = {
        "last_at": at,
        "steps": step,
        "protocol_sha256": "synthetic-protocol",
        "input_identity": identity,
    }
    (directory / "caller.json").write_bytes(dump(caller))
    (directory / "events.jsonl").write_bytes(events)
    (directory / "marks.jsonl").write_bytes(b'{"mark":"artificial"}\n')
    (directory / "signals.jsonl").write_bytes(b"")
    manifest = {
        "schema": 2,
        "state": "COMPLETED_BOUNDARY",
        "run_directory": str(root / "run"),
        "at": at,
        "steps": step,
        "protocol_sha256": caller["protocol_sha256"],
        "source_pins": {str(source): sha(source.read_bytes())},
        "synthetic": True,
        "input_identity": identity,
        "files": {
            name: {
                "bytes": (directory / name).stat().st_size,
                "sha256": sha((directory / name).read_bytes()),
            }
            for name in retention.FILES
        },
    }
    (directory / "manifest.json").write_bytes(dump(manifest))
    if sidecars:
        for name in retention.FILES[:3]:
            (directory / (name + "-wal")).write_bytes(b"")
            (directory / (name + "-shm")).write_bytes(b"synthetic sidecar evidence")
    return directory, sha((directory / "manifest.json").read_bytes())


def inventory(path):
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


def archive_one(store, root, step=1, **kwargs):
    source, digest = make_checkpoint(root, step, **kwargs)
    receipt = store.archive_checkpoint(
        source,
        expected_manifest_sha256=digest,
        expected_parent_sha256=getattr(store, "_test_previous", None),
    )
    store._test_previous = receipt["generation_sha256"]
    return source, receipt


def test_default_off_and_pinned_exact_plan(tmp_path):
    path, digest = write_plan(tmp_path)
    with pytest.raises(RetentionError, match="disabled"):
        CheckpointArchive(path, expected_plan_sha256=digest)
    with pytest.raises(RetentionError, match="plan identity"):
        CheckpointArchive(path, expected_plan_sha256="0" * 64, enabled=True)
    assert not (tmp_path / "archive").exists()
    for key, value in (
        ("max_generations", True),
        ("chunk_bytes", 5000),
        ("reserve_bytes", 1),
        ("state", "HISTORICAL"),
        ("unknown", 3),
    ):
        path, digest = write_plan(tmp_path, **{key: value})
        with pytest.raises(RetentionError):
            CheckpointArchive(path, expected_plan_sha256=digest, enabled=True)


def test_round_trip_exact_original_manifest_empty_files_and_sidecars(tmp_path):
    with open_archive(tmp_path) as store:
        source, receipt = archive_one(store, tmp_path, sidecars=True)
        before = inventory(source)
        restored = store.materialize(
            1, "oldest", expected_generation_sha256=receipt["generation_sha256"]
        )
        assert inventory(Path(restored["path"])) == before
        assert inventory(source) == before
        assert not (tmp_path / "run").exists()
        assert restored["checkpoint_manifest_sha256"] == sha(before["manifest.json"])
        with pytest.raises(RetentionError, match="immutable"):
            store.materialize(
                1, "oldest", expected_generation_sha256=receipt["generation_sha256"]
            )
        assert store.audit()["generations"] == 1


def test_missing_ownership_or_fork_cannot_mutate(tmp_path):
    archive = open_archive(tmp_path)
    source, digest = make_checkpoint(tmp_path)
    with pytest.raises(RetentionError, match="own"):
        archive.archive_checkpoint(source, expected_manifest_sha256=digest)
    with archive:
        original_pid = archive.pid
        archive.pid = original_pid + 1
        with pytest.raises(RetentionError, match="own"):
            archive.archive_checkpoint(source, expected_manifest_sha256=digest)
        archive.pid = original_pid
    with pytest.raises(RetentionError, match="own"):
        archive.audit()


@pytest.mark.parametrize(
    "change", ["historical", "identity", "cursor", "hash", "extra", "wal", "pin"]
)
def test_invalid_checkpoint_never_publishes(tmp_path, change):
    with open_archive(tmp_path) as store:
        source, digest = make_checkpoint(tmp_path)
        manifest = json.loads((source / "manifest.json").read_bytes())
        if change == "historical":
            manifest["synthetic"] = False
        elif change == "identity":
            manifest["input_identity"]["synthetic"] = False
        elif change == "cursor":
            manifest["steps"] = True
        elif change == "hash":
            (source / "events.jsonl").write_bytes(b"changed")
        elif change == "extra":
            (source / "unrecognized.log").write_bytes(b"sole evidence")
        elif change == "wal":
            (source / "account/paper.sqlite-wal").write_bytes(b"uncheckpointed-data")
        else:
            (tmp_path / "fixture_source.py").write_text("# Changed source\n")
        (source / "manifest.json").write_bytes(dump(manifest))
        digest = sha((source / "manifest.json").read_bytes())
        before = inventory(source)
        with pytest.raises(RetentionError):
            store.archive_checkpoint(source, expected_manifest_sha256=digest)
        assert store.audit()["generations"] == 0
        assert inventory(source) == before


def test_generation_chain_exact_hash_and_immutable_cursor(tmp_path):
    with open_archive(tmp_path) as store:
        source, first = archive_one(store, tmp_path, 1)
        _, second = archive_one(store, tmp_path, 2)
        raw = json.loads((store.root / "generations/00000002.json").read_bytes())
        assert raw["parent_sha256"] == first["generation_sha256"]
        assert raw["plan_sha256"] == store.plan_sha256
        assert raw["checkpoint_manifest_sha256"] == second["checkpoint_manifest_sha256"]
        with pytest.raises(RetentionError, match="advance"):
            store.archive_checkpoint(
                source,
                expected_manifest_sha256=first["checkpoint_manifest_sha256"],
                expected_parent_sha256=second["generation_sha256"],
            )
        with pytest.raises(RetentionError, match="Pinned generation"):
            store.materialize(
                1, "bad", expected_generation_sha256=second["generation_sha256"]
            )
        with pytest.raises(RetentionError, match="existing generation"):
            store.materialize(
                True, "bad", expected_generation_sha256=first["generation_sha256"]
            )


@pytest.mark.parametrize(
    "target",
    [
        "chunk",
        "manifest",
        "missing",
        "ledger_gap",
        "member_traversal",
        "duplicate_json",
    ],
)
def test_corruption_and_ledger_tampering_fail_closed(tmp_path, target):
    with open_archive(tmp_path) as store:
        _, receipt = archive_one(store, tmp_path)
        generation = store.root / "generations/00000001.json"
        if target == "chunk":
            blob = next((store.root / "chunks").iterdir())
            blob.chmod(0o600)
            blob.write_bytes(b"corruption")
        elif target == "manifest":
            generation.chmod(0o600)
            generation.write_bytes(b"invalid")
        elif target == "missing":
            # Move synthetic corruption evidence aside; never delete it.
            blob = next((store.root / "chunks").iterdir())
            blob.rename(tmp_path / "preserved-corrupt-chunk")
        elif target == "ledger_gap":
            generation.rename(generation.with_name("00000002.json"))
        else:
            raw = json.loads(generation.read_bytes())
            if target == "member_traversal":
                raw["files"]["../outside"] = raw["files"].pop("caller.json")
                changed = dump(raw)
            else:
                changed = b'{"schema":1,' + generation.read_bytes()[1:]
            generation.chmod(0o600)
            generation.write_bytes(changed)
        with pytest.raises((RetentionError, FileNotFoundError)):
            store.materialize(
                1, "rejected", expected_generation_sha256=receipt["generation_sha256"]
            )
        assert not (store.root / "restores/rejected").exists()


@pytest.mark.parametrize(
    "name", ["../escape", "/tmp/escape", ".", "a/b", "a\\b", "", ".partial-x"]
)
def test_restore_path_traversal_rejected(tmp_path, name):
    with open_archive(tmp_path) as store:
        _, receipt = archive_one(store, tmp_path)
        with pytest.raises(RetentionError, match="destination name"):
            store.materialize(
                1, name, expected_generation_sha256=receipt["generation_sha256"]
            )


@pytest.mark.parametrize(
    "target", ["source", "member", "archive", "chunks", "lock", "hardlink"]
)
def test_symlink_hardlink_and_lock_ownership_rejected(tmp_path, target):
    source, digest = make_checkpoint(tmp_path)
    if target == "source":
        alias = tmp_path / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        with open_archive(tmp_path) as store, pytest.raises(
            RetentionError, match="symlink"
        ):
            store.archive_checkpoint(alias, expected_manifest_sha256=digest)
        return
    if target == "archive":
        (tmp_path / "outside").mkdir()
        (tmp_path / "archive").symlink_to(
            tmp_path / "outside", target_is_directory=True
        )
        with pytest.raises(RetentionError, match="symlink"):
            open_archive(tmp_path)
        return
    with open_archive(tmp_path) as store:
        if target == "member":
            member = source / "events.jsonl"
            member.rename(tmp_path / "preserved-events")
            member.symlink_to(tmp_path / "preserved-events")
        elif target == "hardlink":
            os.link(source / "events.jsonl", tmp_path / "aliased-events")
        elif target == "chunks":
            (store.root / "chunks").rename(store.root / "preserved-chunks")
            (store.root / "chunks").symlink_to(tmp_path, target_is_directory=True)
        else:
            (store.root / "writer.lock").rename(store.root / "preserved-lock")
            (store.root / "writer.lock").write_bytes(b"")
        with pytest.raises((RetentionError, OSError)):
            store.archive_checkpoint(source, expected_manifest_sha256=digest)


def test_other_process_cannot_take_held_lock(tmp_path):
    with open_archive(tmp_path) as store:
        code = """
import sys
from research.weekly_reversal.checkpoint_retention import CheckpointArchive, RetentionError
try:
    with CheckpointArchive(sys.argv[1], expected_plan_sha256=sys.argv[2], enabled=True):
        raise SystemExit(8)
except RetentionError as exc:
    assert 'already held' in str(exc), str(exc)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(store.plan_path), store.plan_sha256],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "point",
    [
        "after_admission",
        "after_chunks",
        "before_generation_publication",
        "after_generation_publication",
    ],
)
def test_real_process_loss_preserves_old_generation_and_exact_commit(tmp_path, point):
    with open_archive(tmp_path) as store:
        source, first = archive_one(store, tmp_path, 1)
        first_bytes = inventory(source)
        plan_path, plan_hash = str(store.plan_path), store.plan_sha256
    source, digest = make_checkpoint(tmp_path, 2)
    code = """
import os, sys
from research.weekly_reversal.checkpoint_retention import CheckpointArchive
with CheckpointArchive(sys.argv[1], expected_plan_sha256=sys.argv[2], enabled=True) as store:
    store.archive_checkpoint(sys.argv[3], expected_manifest_sha256=sys.argv[4],
        expected_parent_sha256=sys.argv[6], fault_hook=lambda point: os._exit(73) if point == sys.argv[5] else None)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            plan_path,
            plan_hash,
            str(source),
            digest,
            point,
            first["generation_sha256"],
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 73, result.stderr
    with CheckpointArchive(
        plan_path, expected_plan_sha256=plan_hash, enabled=True
    ) as store:
        assert store.audit()["generations"] == (
            2 if point == "after_generation_publication" else 1
        )
        restored = store.materialize(
            1, "prior", expected_generation_sha256=first["generation_sha256"]
        )
        assert inventory(Path(restored["path"])) == first_bytes


@pytest.mark.parametrize("point", ["after_restore_files", "before_restore_publication"])
def test_interrupted_restore_remains_unpublished_and_retained(tmp_path, point):
    with open_archive(tmp_path) as store:
        source, receipt = archive_one(store, tmp_path)

        def crash(at):
            if point == at:
                raise RuntimeError("synthetic interruption")

        with pytest.raises(RuntimeError):
            store.materialize(
                1,
                "interrupted",
                expected_generation_sha256=receipt["generation_sha256"],
                fault_hook=crash,
            )
        assert not (store.root / "restores/interrupted").exists()
        partials = list((store.root / "restores").glob(".partial-*"))
        assert len(partials) == 1
        assert inventory(partials[0]) == inventory(source)
        restored = store.materialize(
            1, "retried", expected_generation_sha256=receipt["generation_sha256"]
        )
        assert inventory(Path(restored["path"])) == inventory(source)
        assert partials[0].exists()


def test_capacity_and_generation_caps_stop_without_deleting_evidence(tmp_path):
    with open_archive(tmp_path, max_generations=1) as store:
        source, first = archive_one(store, tmp_path, 1)
        before = inventory(store.root)
        source2, digest = make_checkpoint(tmp_path, 2)
        with pytest.raises(RetentionError, match="Generation limit"):
            store.archive_checkpoint(
                source2,
                expected_manifest_sha256=digest,
                expected_parent_sha256=store._test_previous,
            )
        assert inventory(store.root) == before
        assert source.exists() and source2.exists()
        assert store.materialize(
            1, "last-good", expected_generation_sha256=first["generation_sha256"]
        )


def test_budget_rejects_new_bytes_before_write_and_reserve_restores(tmp_path):
    with open_archive(tmp_path, max_archive_bytes=2 * 1024 * 1024) as store:
        _, receipt = archive_one(store, tmp_path, 1)
        source2, digest = make_checkpoint(tmp_path, 2, events=os.urandom(128 * 1024))
        before = inventory(store.root)
        with pytest.raises(RetentionError, match="disk budget"):
            store.archive_checkpoint(
                source2,
                expected_manifest_sha256=digest,
                expected_parent_sha256=store._test_previous,
            )
        assert inventory(store.root) == before
        store.materialize(
            1,
            "budget-recovery",
            expected_generation_sha256=receipt["generation_sha256"],
        )


def test_actual_free_space_reserve_checked_before_writes(tmp_path, monkeypatch):
    with open_archive(tmp_path) as store:
        source, digest = make_checkpoint(tmp_path)
        before = inventory(store.root)

        class NoSpace:
            f_bavail = 1
            f_frsize = 4096

        monkeypatch.setattr(retention.os, "fstatvfs", lambda fd: NoSpace())
        with pytest.raises(RetentionError, match="free-space reserve"):
            store.archive_checkpoint(source, expected_manifest_sha256=digest)
        assert inventory(store.root) == before


def test_failed_chunk_publication_retains_partial_and_old_restore(
    tmp_path, monkeypatch
):
    with open_archive(tmp_path) as store:
        old_source, first = archive_one(store, tmp_path, 1)
        source, digest = make_checkpoint(tmp_path, 2)
        original = retention._rename_noreplace

        def full(source_fd, source_name, destination_fd, destination):
            if len(destination) == 64:
                raise OSError(28, "synthetic ENOSPC")
            return original(source_fd, source_name, destination_fd, destination)

        monkeypatch.setattr(retention, "_rename_noreplace", full)
        with pytest.raises(OSError):
            store.archive_checkpoint(
                source,
                expected_manifest_sha256=digest,
                expected_parent_sha256=first["generation_sha256"],
            )
        monkeypatch.setattr(retention, "_rename_noreplace", original)
        partials = list((store.root / "chunks").glob(".partial-*"))
        assert partials
        assert store.audit()["generations"] == 1
        restored = store.materialize(
            1, "recover", expected_generation_sha256=first["generation_sha256"]
        )
        assert inventory(Path(restored["path"])) == inventory(old_source)
        assert all(path.exists() for path in partials)


def test_source_mutation_and_plan_or_owner_change_before_publication(tmp_path):
    for kind in ("source", "plan", "owner"):
        root = tmp_path / kind
        root.mkdir()
        with open_archive(root) as store:
            source, digest = make_checkpoint(root)

            def change(point):
                if point == "before_generation_publication":
                    if kind == "source":
                        # This must be caught by the final source revalidation.
                        (source / "events.jsonl").write_bytes(b"concurrent mutation")
                    elif kind == "plan":
                        store.plan_path.write_bytes(store.plan_raw + b" ")
                    else:
                        (store.root / "writer.lock").rename(
                            store.root / "preserved-lock"
                        )
                        (store.root / "writer.lock").write_bytes(b"")

            with pytest.raises(RetentionError):
                store.archive_checkpoint(
                    source, expected_manifest_sha256=digest, fault_hook=change
                )
            assert not (store.root / "generations/00000001.json").exists()


def test_racing_empty_restore_destination_not_overwritten(tmp_path):
    with open_archive(tmp_path) as store:
        _, receipt = archive_one(store, tmp_path)

        def race(point):
            if point == "before_restore_publication":
                (store.root / "restores/race").mkdir()

        with pytest.raises(RetentionError, match="destination appeared"):
            store.materialize(
                1,
                "race",
                expected_generation_sha256=receipt["generation_sha256"],
                fault_hook=race,
            )
        assert (store.root / "restores/race").exists()
        assert not list((store.root / "restores/race").iterdir())
        assert list((store.root / "restores").glob(".partial-*"))


def test_two_hundred_generations_measured_growth_and_exact_old_random_latest(tmp_path):
    prefix = b"".join(
        dump({"event": i, "data": "synthetic-" + str(i * 17)}) for i in range(1024)
    )
    receipts, expected, full_copy_bytes = [], {}, 0
    with open_archive(tmp_path) as store:
        for step in range(1, 201):
            prefix += dump({"generation": step})
            source, receipt = archive_one(store, tmp_path, step, events=prefix)
            receipts.append(receipt)
            snapshot = inventory(source)
            full_copy_bytes += sum(map(len, snapshot.values()))
            if step in (1, 37, 113, 200):
                expected[step] = snapshot
        files = inventory(store.root)
        archive_bytes = sum(map(len, files.values()))
        allocated_bytes = sum(
            path.stat().st_blocks * 512 for path in [store.root, *store.root.rglob("*")]
        )
        charged_before = store.audit()["charged_bytes"]
        unique_chunks = len(list((store.root / "chunks").iterdir()))
        assert archive_bytes < full_copy_bytes // 3
        assert store.audit()["charged_bytes"] <= store.plan["max_archive_bytes"]
        for step, snapshot in expected.items():
            restored = store.materialize(
                step,
                "generation_" + str(step),
                expected_generation_sha256=receipts[step - 1]["generation_sha256"],
            )
            assert inventory(Path(restored["path"])) == snapshot
        report = {
            "synthetic_generations": 200,
            "full_copy_bytes": full_copy_bytes,
            "archive_logical_bytes_before_materialization": archive_bytes,
            "archive_allocated_bytes_before_materialization": allocated_bytes,
            "archive_charged_bytes_before_materialization": charged_before,
            "unique_chunks": unique_chunks,
            "charged_bytes_after_four_materializations": store.audit()["charged_bytes"],
            "max_archive_bytes": store.plan["max_archive_bytes"],
            "reserve_bytes": store.plan["reserve_bytes"],
            "reconstructed_generations": sorted(expected),
        }
        print("RETENTION_MEASUREMENT " + json.dumps(report, sort_keys=True))


def test_explicit_predecessor_pin_detects_truncated_generation_ledger(tmp_path):
    with open_archive(tmp_path) as store:
        _, first = archive_one(store, tmp_path, 1)
        _, second = archive_one(store, tmp_path, 2)
        source, digest = make_checkpoint(tmp_path, 3)
        with pytest.raises(RetentionError, match="externally pinned predecessor"):
            store.archive_checkpoint(source, expected_manifest_sha256=digest)
        (store.root / "generations/00000002.json").rename(
            tmp_path / "preserved-generation-2.json"
        )
        with pytest.raises(RetentionError, match="externally pinned predecessor"):
            store.archive_checkpoint(
                source,
                expected_manifest_sha256=digest,
                expected_parent_sha256=second["generation_sha256"],
            )
        restored = store.materialize(
            1, "first-retained", expected_generation_sha256=first["generation_sha256"]
        )
        assert Path(restored["path"]).exists()


@pytest.mark.parametrize("change", ["changed", "missing", "symlink"])
def test_materialization_recovers_bytes_when_original_sources_are_unavailable(
    tmp_path, change
):
    with open_archive(tmp_path) as store:
        source, receipt = archive_one(store, tmp_path)
        original = inventory(source)
        pin = tmp_path / "fixture_source.py"
        if change == "changed":
            pin.write_text("# Source has changed, not eligible to execute.\n")
        else:
            pin.rename(tmp_path / "preserved-source.py")
            if change == "symlink":
                pin.symlink_to(tmp_path / "preserved-source.py")
        assert store.audit()["generations"] == 1
        result = store.materialize(
            1, "evidence_only", expected_generation_sha256=receipt["generation_sha256"]
        )
        assert inventory(Path(result["path"])) == original
        assert result["execution_authorized"] is False


def test_explicit_non_synthetic_archival_is_only_byte_preservation(tmp_path):
    # These are artificial test databases relabelled to test the archival gate;
    # this test never reads historical data or executes a historical replay.
    source, _ = make_checkpoint(tmp_path)
    caller = json.loads((source / "caller.json").read_bytes())
    caller["input_identity"]["synthetic"] = False
    (source / "caller.json").write_bytes(dump(caller))
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["synthetic"] = False
    manifest["input_identity"]["synthetic"] = False
    manifest["files"]["caller.json"] = {
        "bytes": (source / "caller.json").stat().st_size,
        "sha256": sha((source / "caller.json").read_bytes()),
    }
    (source / "manifest.json").write_bytes(dump(manifest))
    expected = sha((source / "manifest.json").read_bytes())
    path, digest = write_plan(tmp_path, state="ARCHIVE_ONLY_NO_EXECUTION")
    with pytest.raises(RetentionError, match="separate explicit opt-in"):
        CheckpointArchive(path, expected_plan_sha256=digest, enabled=True)
    assert not (tmp_path / "archive").exists()
    # Source availability/closure must not be confused with archival authority.
    (tmp_path / "fixture_source.py").rename(tmp_path / "preserved-source.py")
    with CheckpointArchive(
        path,
        expected_plan_sha256=digest,
        enabled=True,
        allow_non_synthetic_archival=True,
    ) as store:
        receipt = store.archive_checkpoint(source, expected_manifest_sha256=expected)
        result = store.materialize(
            1, "forensic_copy", expected_generation_sha256=receipt["generation_sha256"]
        )
        assert inventory(Path(result["path"])) == inventory(source)
        assert (
            receipt["synthetic"] is False and receipt["execution_authorized"] is False
        )
        assert result["synthetic"] is False and result["execution_authorized"] is False
        assert not (tmp_path / "run").exists()
    with pytest.raises(RetentionError, match="separate explicit opt-in"):
        CheckpointArchive(path, expected_plan_sha256=digest, enabled=True)


def test_non_synthetic_constructor_flag_cannot_override_synthetic_plan(tmp_path):
    path, digest = write_plan(tmp_path)
    with pytest.raises(RetentionError, match="does not match"):
        CheckpointArchive(
            path,
            expected_plan_sha256=digest,
            enabled=True,
            allow_non_synthetic_archival=True,
        )


# Regression supplied by the independent retention safety audit.
def test_manifest_bytes_hash_must_match_admitted_pin_even_when_changed_during_scan(
    tmp_path, monkeypatch
):
    with open_archive(tmp_path) as store:
        source, digest = make_checkpoint(tmp_path)
        original = (source / "manifest.json").read_bytes()
        changed = original + b"\n"
        real_member = retention._member
        from contextlib import contextmanager

        count = 0

        @contextmanager
        def racing_member(root_fd, name):
            nonlocal count
            if name == "manifest.json":
                count += 1
                # Each _checkpoint reads size, expected identity, then descriptor.
                (source / "manifest.json").write_bytes(
                    changed if count in {3, 4, 5, 7} else original
                )
            with real_member(root_fd, name) as f:
                yield f

        monkeypatch.setattr(retention, "_member", racing_member)
        with pytest.raises(RetentionError):
            store.archive_checkpoint(source, expected_manifest_sha256=digest)
        assert not (store.root / "generations/00000001.json").exists()
