"""Default-off, normal-entrypoint and real process restart acceptance."""

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from spectra_core.crash_short_model import PaperSource
from spectra_core.crash_short_paper import PaperShortStore
from spectra_core.crash_short_runtime import (
    read_paper_short_status,
    run_crash_short_paper_cycle,
)
from tests.test_crash_short_paper import (
    CRASH_DAY,
    SOURCE,
    START,
    bars,
    book,
    enter,
    seeded_database,
)


def command(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1])
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    return subprocess.run(
        [sys.executable, "-m", "spectra_core.crash_short_runtime", *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_absent_config_disabled_and_never_creates_state(tmp_path):
    state = tmp_path / "new-directory" / "paper.sqlite3"
    result = command("--state", state, "--input", tmp_path / "missing")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "DISABLED"
    assert not state.parent.exists()
    assert run_crash_short_paper_cycle()["status"] == "DISABLED"


def test_disabled_existing_book_preserves_liability(book):
    enter(book)
    before = Path(book.path).read_bytes()
    result = run_crash_short_paper_cycle(store=book)
    assert result["status"] == "DISABLED"
    assert len(result["existing_book"]["positions"]) == 2
    assert Path(book.path).read_bytes() == before


def test_live_unknown_or_client_injection_rejected_without_writes(book):
    before = Path(book.path).read_bytes()
    for mode in ("LIVE", "MARGIN", "paper", ""):
        with pytest.raises(ValueError, match="Only PAPER"):
            run_crash_short_paper_cycle(store=book, execution_mode=mode)
    with pytest.raises(TypeError):
        run_crash_short_paper_cycle(exchange_client=object())
    with pytest.raises(ValueError):
        run_crash_short_paper_cycle(enabled="true", store=book)
    assert Path(book.path).read_bytes() == before


def test_normal_callable_entrypoint_and_persistent_restart(book):
    receipt = run_crash_short_paper_cycle(
        enabled=True, store=book, timestamp=CRASH_DAY + 300, bars=bars(CRASH_DAY + 300)
    )
    with PaperShortStore(book.path, SOURCE, run_id="fixture") as restarted:
        replay = run_crash_short_paper_cycle(
            enabled=True,
            store=restarted,
            timestamp=CRASH_DAY + 300,
            bars=bars(CRASH_DAY + 300),
        )
        assert replay == receipt
        assert len(restarted.read_events("FILL")) == 2


def test_status_is_read_only_and_uninitialized_status_has_no_side_effect(
    tmp_path, book
):
    absent = tmp_path / "absent" / "paper.sqlite3"
    assert read_paper_short_status(absent)["reason"] == "NO_PAPER_BOOK"
    assert not absent.parent.exists()
    receipt = enter(book)
    before = Path(book.path).read_bytes()
    assert read_paper_short_status(book.path) == receipt
    result = command("--state", book.path, "--status")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == receipt
    assert Path(book.path).read_bytes() == before


def test_cli_replays_same_source_and_resumes_controls_only_for_new_intervals(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    state = tmp_path / "paper.sqlite3"
    lines = []
    for t in (START, START + 300, START + 600):
        lines.append(
            json.dumps(
                {
                    "timestamp": t,
                    "bars": {
                        a: {
                            k: "100" if k in ("open", "high", "low", "close") else "1"
                            for k in (
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                                "trades",
                            )
                        }
                        for a in ("BTC", "ETH")
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")
    args = (
        "--enable-paper",
        "--source",
        "synthetic",
        "--input",
        path,
        "--state",
        state,
        "--run-id",
        "cli",
    )
    first = command(*args, "--until", START + 300)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["processed_or_replayed_intervals"] == 1
    second = command(*args, "--resume", "--control", "pause")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["control"] == "PAUSE"
    before = state.read_bytes()
    third = command(*args, "--resume", "--control", "close-only")
    assert third.returncode == 0, third.stderr
    assert (
        json.loads(third.stdout)["control"] == "PAUSE"
    )  # Already committed controls cannot be rewritten.
    assert state.read_bytes() == before
    bad = command(*args, "--control", "close-only")
    assert bad.returncode == 2
    assert "conflicting" in bad.stderr
    source = PaperSource("SYNTHETIC", hashlib.sha256(path.read_bytes()).hexdigest())
    with PaperShortStore(state, source, run_id="cli") as store:
        assert len(list(store.read_receipts())) == 3


def test_frozen_cli_rejects_unknown_bytes_before_creating_state(tmp_path):
    path = tmp_path / "not-the-frozen-input.npy"
    path.write_bytes(b"synthetic")
    state = tmp_path / "paper.sqlite3"
    result = command(
        "--enable-paper",
        "--source",
        "frozen-jan-june",
        "--input",
        path,
        "--state",
        state,
    )
    assert result.returncode == 2
    assert "SHA256" in result.stderr
    assert not state.exists()


def test_new_modules_have_no_live_imports_or_dispatch_adapter():
    root = Path(__file__).resolve().parents[1] / "spectra_core"
    for path in root.glob("crash_short_*.py"):
        tree = ast.parse(path.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(
            any(
                token in name
                for token in (
                    "hummingbot",
                    "executor_create",
                    "capital_governor",
                    "kraken_authorization",
                    "vcap",
                    "urllib",
                    "requests",
                    "socket",
                )
            )
            for name in imports
        )
