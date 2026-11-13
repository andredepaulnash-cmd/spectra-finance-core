#!/usr/bin/env python3
"""Safely restore an isolated Spectra paper-state archive.

Only archives created under the installation's backups directory are accepted.
The restore is staged beside the target, validates every archive member and
SQLite database, and atomically swaps the paper-state directory. The previous
state is retained beside it for manual rollback. Credentials and live state are
never restored.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolved_configured_path(raw: str | None, default: Path, base: Path) -> Path:
    candidate = Path(raw).expanduser() if raw else default
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_archive_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = member.name
    if not name or "\\" in name:
        raise ValueError("archive contains an empty or non-portable path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive path: {name!r}")
    if not path.parts or path.parts[0] != "state":
        raise ValueError(f"archive member is outside the paper-state root: {name!r}")
    if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
        raise ValueError(f"unsupported archive member type: {name!r}")
    if any(part.lower() in {"credentials", "secrets"} for part in path.parts):
        raise ValueError(f"credential-like path is not permitted in paper-state archive: {name!r}")
    if path.name == ".env":
        raise ValueError("environment files are not permitted in paper-state archives")
    return path


def _extract_verified(archive: Path, destination: Path) -> None:
    seen: set[str] = set()
    file_count = 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise ValueError("paper-state archive is empty")
        validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member in members:
            relative = _validate_archive_member(member)
            if member.name in seen:
                raise ValueError(f"duplicate archive member: {member.name!r}")
            seen.add(member.name)
            validated.append((member, relative))

        for member, relative in validated:
            parts = relative.parts[1:]
            if not parts:
                if not member.isdir():
                    raise ValueError("archive state root must be a directory")
                destination.mkdir(parents=True, exist_ok=True)
                continue
            target = destination.joinpath(*parts)
            if target.resolve().is_relative_to(destination.resolve()) is False:
                raise ValueError(f"archive path escapes staging directory: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"could not read archive member: {member.name!r}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
            file_count += 1

    if file_count == 0:
        raise ValueError("paper-state archive contains no files")


def _verify_sqlite_files(state_dir: Path) -> None:
    for path in state_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"restored paper state contains a symlink: {path}")
        if path.is_file() and path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            connection = sqlite3.connect(path)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise ValueError(f"SQLite integrity check failed: {path}")
            finally:
                connection.close()


def restore_paper_state(
    archive_path: Path,
    *,
    install_root: Path,
    core_dir: Path,
    env_path: Path | None = None,
) -> Path:
    install_root = install_root.expanduser().resolve()
    core_dir = core_dir.expanduser().resolve()
    env_path = env_path or (core_dir / ".env")
    if not env_path.is_file() or env_path.is_symlink():
        raise ValueError("Core .env is missing or is a symlink")

    values = read_env(env_path)
    if values.get("SPECTRA_PAPER_ONLY", "").lower() != "true":
        raise ValueError("restore refused: SPECTRA_PAPER_ONLY must be true")
    if values.get("SPECTRA_ALLOW_LIVE_EXECUTION", "").lower() != "false":
        raise ValueError("restore refused: live execution must be explicitly disabled")

    backup_root = (install_root / "backups").resolve()
    archive = archive_path.expanduser()
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("restore archive must be a regular, non-symlink file")
    archive = archive.resolve(strict=True)
    if archive == backup_root or backup_root not in archive.parents:
        raise ValueError("restore archive must be inside the installation backups directory")

    paper_raw = Path(values.get("SPECTRA_PAPER_STATE_DIR") or (core_dir / "data" / "spectra-paper")).expanduser()
    live_raw = Path(values.get("SPECTRA_STATE_DIR") or (core_dir / "data" / "spectra")).expanduser()
    if not paper_raw.is_absolute():
        paper_raw = core_dir / paper_raw
    if not live_raw.is_absolute():
        live_raw = core_dir / live_raw
    if paper_raw.is_symlink() or live_raw.is_symlink():
        raise ValueError("paper or live state directory must not be a symlink")
    paper = paper_raw.resolve()
    live = live_raw.resolve()

    if not paper.is_dir():
        raise ValueError(f"paper state directory does not exist: {paper}")
    if _overlap(paper, live):
        raise ValueError("paper and live state directories overlap")
    if _overlap(paper, backup_root):
        raise ValueError("paper state and backup directories overlap")

    stage_root = Path(tempfile.mkdtemp(prefix=".spectra-restore-", dir=paper.parent))
    staged_state = stage_root / "state"
    previous = paper.parent / f"{paper.name}.pre-restore-{uuid.uuid4().hex[:12]}"
    swapped_old = False
    try:
        _extract_verified(archive, staged_state)
        _verify_sqlite_files(staged_state)
        os.replace(paper, previous)
        swapped_old = True
        try:
            os.replace(staged_state, paper)
        except Exception:
            os.replace(previous, paper)
            swapped_old = False
            raise
        return previous
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)
        if swapped_old and not paper.exists() and previous.exists():
            os.replace(previous, paper)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    core_dir = args.install_root.expanduser() / "spectra-core"
    try:
        previous = restore_paper_state(
            args.archive,
            install_root=args.install_root,
            core_dir=core_dir,
        )
    except (OSError, ValueError, tarfile.TarError, sqlite3.DatabaseError) as exc:
        parser.exit(2, f"spectra paper restore: REFUSED: {exc}\n")
    print("Paper-state restore completed.")
    print(f"Previous paper state retained at: {previous}")
    print("Spectra Core was not restarted; review the restored state, then run: spectra core-restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
