"""Archive/recover the existing native checkpoint format using artificial bars."""

import shutil
from pathlib import Path

from test_weekly_checkpoint import create, pins
from test_weekly_checkpoint_retention import inventory, open_archive
from test_weekly_compounding_adapter import members
from test_weekly_owned_adapter import config
from test_weekly_replay import source

from research.weekly_reversal.checkpoint import (
    restore_checkpoint,
    save_completed_boundary,
)
from research.weekly_reversal.replay import WeeklyReplay


def test_native_synthetic_checkpoint_round_trip_and_resume_are_exact(tmp_path):
    replay = create(tmp_path / "run")
    try:
        for index, event in enumerate(replay.tape.boundaries()):
            replay.step(event)
            if index == 10:
                original = save_completed_boundary(
                    replay, tmp_path / "checkpoint", source_pins=pins()
                )
        continuous = replay.finish()
        continuous_receipts = replay.a._receipts()
        continuous_state = replay.export_state()
    finally:
        replay.close()
    continuous_logs = {
        name: (tmp_path / "run" / name).read_bytes()
        for name in ("events.jsonl", "marks.jsonl", "signals.jsonl")
    }
    original_bytes = inventory(tmp_path / "checkpoint")
    with open_archive(tmp_path) as store:
        archived = store.archive_checkpoint(
            tmp_path / "checkpoint",
            expected_manifest_sha256=original["manifest_sha256"],
        )
        materialized = store.materialize(
            1,
            "synthetic_recovery",
            expected_generation_sha256=archived["generation_sha256"],
        )
        assert inventory(Path(materialized["path"])) == original_bytes
        assert inventory(tmp_path / "checkpoint") == original_bytes
    shutil.move(tmp_path / "run", tmp_path / "preserved_continuous_run")
    restore_checkpoint(
        materialized["path"],
        tmp_path / "run",
        expected_manifest_sha256=original["manifest_sha256"],
    )
    cfg = config(("BTC", "ETH"))
    replay = WeeklyReplay.resume_restored(
        tmp_path / "run",
        tape=source(),
        config=cfg,
        membership=members(cfg),
        protocol_sha256="synthetic-only",
    )
    try:
        for event in replay.tape.boundaries():
            if event.at > replay.last_at:
                replay.step(event)
        recovered = replay.finish()
        assert replay.a._receipts() == continuous_receipts
        assert replay.export_state() == continuous_state
    finally:
        replay.close()
    assert {
        key: value for key, value in recovered.items() if key != "elapsed_seconds"
    } == {key: value for key, value in continuous.items() if key != "elapsed_seconds"}
    assert {
        name: (tmp_path / "run" / name).read_bytes() for name in continuous_logs
    } == continuous_logs
