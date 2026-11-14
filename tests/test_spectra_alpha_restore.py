from __future__ import annotations

import importlib.util
import sqlite3
import tarfile
from pathlib import Path

import pytest

_RESTORE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "spectra-alpha-restore.py"
_SPEC = importlib.util.spec_from_file_location("spectra_alpha_restore", _RESTORE_PATH)
_RESTORE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_RESTORE)
restore_paper_state = _RESTORE.restore_paper_state


def make_install(tmp_path: Path, *, paper_path: str | None = None, live_path: str | None = None):
    install = tmp_path / "install"
    core = install / "spectra-core"
    paper = Path(paper_path) if paper_path else core / "data" / "spectra-paper"
    live = Path(live_path) if live_path else core / "data" / "spectra"
    if not paper.is_absolute():
        paper = core / paper
    if not live.is_absolute():
        live = core / live
    paper.mkdir(parents=True)
    live.mkdir(parents=True, exist_ok=True)
    (paper / "journal.json").write_text('{"version":"old"}', encoding="utf-8")
    (live / "live.json").write_text('{"protected":true}', encoding="utf-8")
    (core / ".env").write_text(
        "SPECTRA_PAPER_ONLY=true\n"
        "SPECTRA_ALLOW_LIVE_EXECUTION=false\n"
        f"SPECTRA_PAPER_STATE_DIR={paper}\n"
        f"SPECTRA_STATE_DIR={live}\n",
        encoding="utf-8",
    )
    backup = install / "backups"
    backup.mkdir(parents=True)
    return install, core, paper, live, backup


def make_archive(path: Path, members: dict[str, bytes | None]):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
            else:
                import io
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))


def test_restore_swaps_only_paper_state_and_retains_previous_directory(tmp_path):
    install, core, paper, live, backup = make_install(tmp_path)
    archive = backup / "paper.tar.gz"
    make_archive(archive, {
        "state/": None,
        "state/journal.json": b'{"version":"restored"}',
    })

    previous = restore_paper_state(archive, install_root=install, core_dir=core)

    assert (paper / "journal.json").read_text(encoding="utf-8") == '{"version":"restored"}'
    assert (previous / "journal.json").read_text(encoding="utf-8") == '{"version":"old"}'
    assert (live / "live.json").read_text(encoding="utf-8") == '{"protected":true}'


def test_restore_rejects_archive_outside_backups(tmp_path):
    install, core, paper, live, backup = make_install(tmp_path)
    outside = tmp_path / "outside.tar.gz"
    make_archive(outside, {"state/": None, "state/file.txt": b"data"})
    with pytest.raises(ValueError, match="inside the installation backups"):
        restore_paper_state(outside, install_root=install, core_dir=core)
    assert (paper / "journal.json").exists()


def test_restore_rejects_path_traversal_before_extracting(tmp_path):
    install, core, paper, live, backup = make_install(tmp_path)
    archive = backup / "bad.tar.gz"
    make_archive(archive, {"state/": None, "state/../../escape.txt": b"bad"})
    with pytest.raises(ValueError, match="unsafe archive path"):
        restore_paper_state(archive, install_root=install, core_dir=core)
    assert not (tmp_path / "escape.txt").exists()
    assert (paper / "journal.json").exists()


def test_restore_rejects_symlink_members(tmp_path):
    install, core, paper, live, backup = make_install(tmp_path)
    archive = backup / "symlink.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("state/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)
    with pytest.raises(ValueError, match="unsupported archive member type"):
        restore_paper_state(archive, install_root=install, core_dir=core)
    assert (paper / "journal.json").exists()


def test_restore_refuses_when_paper_only_flags_are_not_safe(tmp_path):
    install, core, paper, live, backup = make_install(tmp_path)
    (core / ".env").write_text(
        "SPECTRA_PAPER_ONLY=false\nSPECTRA_ALLOW_LIVE_EXECUTION=false\n",
        encoding="utf-8",
    )
    archive = backup / "paper.tar.gz"
    make_archive(archive, {"state/": None, "state/file.txt": b"data"})
    with pytest.raises(ValueError, match="SPECTRA_PAPER_ONLY"):
        restore_paper_state(archive, install_root=install, core_dir=core)
    assert (paper / "journal.json").exists()


def test_restore_refuses_paper_and_live_state_overlap(tmp_path):
    shared = tmp_path / "install" / "spectra-core" / "data" / "shared"
    install, core, paper, live, backup = make_install(tmp_path, paper_path=str(shared), live_path=str(shared))
    archive = backup / "paper.tar.gz"
    make_archive(archive, {"state/": None, "state/file.txt": b"data"})
    with pytest.raises(ValueError, match="overlap"):
        restore_paper_state(archive, install_root=install, core_dir=core)


def test_restore_checks_sqlite_integrity_before_swap(tmp_path):
    install, core, paper, live, backup = make_install(tmp_path)
    archive = backup / "corrupt.tar.gz"
    make_archive(archive, {"state/": None, "state/broken.sqlite": b"not a database"})
    with pytest.raises(sqlite3.DatabaseError):
        restore_paper_state(archive, install_root=install, core_dir=core)
    assert (paper / "journal.json").read_text(encoding="utf-8") == '{"version":"old"}'
