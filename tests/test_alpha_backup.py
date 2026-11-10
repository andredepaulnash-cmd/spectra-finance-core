import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "spectra-alpha-backup.sh"


def prepare_install(root: Path, *, extra_env: str = ""):
    core = root / "spectra-core"
    paper = core / "data" / "spectra-paper"
    live = core / "data" / "spectra"
    paper.mkdir(parents=True)
    live.mkdir(parents=True)
    (core / ".env").write_text(
        "SPECTRA_PAPER_ONLY=true\n"
        "SPECTRA_ALLOW_LIVE_EXECUTION=false\n"
        "SPECTRA_DISPATCH_PRIVATE_KEY=never-back-up-this-key\n"
        + extra_env,
        encoding="utf-8",
    )
    db = sqlite3.connect(paper / "capital.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE paper_events (event_id INTEGER PRIMARY KEY, detail TEXT)")
    db.execute("INSERT INTO paper_events(detail) VALUES ('paper-ledger-row')")
    db.commit()
    db.close()
    (paper / "breaker_state.json").write_text('{"mode":"NORMAL"}\n')
    (live / "live-only-sentinel.txt").write_text("must-not-enter-paper-backup")
    return core, paper, live


def run_backup(root: Path):
    env = dict(os.environ)
    env["SPECTRA_ALPHA_HOME"] = str(root)
    return subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_backup_contains_consistent_paper_sqlite_but_no_live_state_or_secrets(tmp_path):
    root = tmp_path / "Spectra Alpha"
    core, paper, live = prepare_install(root)

    result = run_backup(root)

    assert result.returncode == 0, result.stdout + result.stderr
    archives = list((root / "backups").glob("spectra-paper-*.tar.gz"))
    assert len(archives) == 1

    with tarfile.open(archives[0], "r:gz") as archive:
        names = set(archive.getnames())
        assert "state/capital.sqlite3" in names
        assert "state/breaker_state.json" in names
        assert not any("live-only-sentinel" in name for name in names)
        assert not any(name.endswith(".env") for name in names)
        payload = archive.extractfile("state/capital.sqlite3")
        assert payload is not None
        db_path = tmp_path / "restored.sqlite3"
        db_path.write_bytes(payload.read())

    restored = sqlite3.connect(db_path)
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT detail FROM paper_events").fetchone()[0] == "paper-ledger-row"
    restored.close()

    archive_bytes = archives[0].read_bytes()
    assert b"never-back-up-this-key" not in archive_bytes
    assert b"must-not-enter-paper-backup" not in archive_bytes


def test_backup_refuses_paper_directory_that_resolves_to_live_state(tmp_path):
    root = tmp_path / "unsafe"
    prepare_install(root, extra_env="SPECTRA_PAPER_STATE_DIR=data/spectra\n")

    result = run_backup(root)

    assert result.returncode != 0
    assert "Backup refused" in result.stdout + result.stderr
    assert not list((root / "backups").glob("spectra-paper-*.tar.gz"))


def test_backup_refuses_when_live_execution_is_not_disabled(tmp_path):
    root = tmp_path / "unsafe-live"
    core, _, _ = prepare_install(root)
    env_file = core / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(
            "SPECTRA_ALLOW_LIVE_EXECUTION=false",
            "SPECTRA_ALLOW_LIVE_EXECUTION=true",
        ),
        encoding="utf-8",
    )

    result = run_backup(root)

    assert result.returncode != 0
    assert "Backup refused" in result.stdout + result.stderr
    assert not list((root / "backups").glob("spectra-paper-*.tar.gz"))
