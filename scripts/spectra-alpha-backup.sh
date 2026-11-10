#!/usr/bin/env bash
# Consistent local backups of Spectra paper state only; never copies secrets/live state.
set -Eeuo pipefail
umask 077
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
CORE_ENV="$CORE_DIR/.env"
BACKUP_DIR="$INSTALL_ROOT/backups"
LOG_DIR="$INSTALL_ROOT/logs"
LOG_FILE="$LOG_DIR/backup.log"
LOCK_FILE="$LOG_DIR/backup.lock"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "[SPECTRA BACKUP] Another backup is already running."; exit 0; }
exec > >(tee -a "$LOG_FILE") 2>&1
say() { printf '[%s] [SPECTRA BACKUP] %s\n' "$(date -Is)" "$*"; }
fail() { say "FAIL: $*"; exit 1; }

[[ -f "$CORE_ENV" ]] || fail "Core environment file is missing."
STAGING="$(mktemp -d "$BACKUP_DIR/.paper-backup.XXXXXX")"
ARCHIVE="$BACKUP_DIR/spectra-paper-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
trap 'rm -rf "$STAGING"' EXIT

python3 - "$CORE_DIR" "$CORE_ENV" "$STAGING/state" <<'PY'
from pathlib import Path
import shutil
import sqlite3
import sys

core = Path(sys.argv[1]).resolve()
env_path = Path(sys.argv[2])
target = Path(sys.argv[3])
values = {}
for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")

if values.get("SPECTRA_PAPER_ONLY", "").lower() != "true":
    raise SystemExit("Backup refused: Spectra paper-only flag is not true.")
if values.get("SPECTRA_ALLOW_LIVE_EXECUTION", "").lower() != "false":
    raise SystemExit("Backup refused: live execution is not explicitly disabled.")

def resolve(raw, default):
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = core / path
    return path.resolve()

paper = resolve(values.get("SPECTRA_PAPER_STATE_DIR"), core / "data" / "spectra-paper")
live = resolve(values.get("SPECTRA_STATE_DIR"), core / "data" / "spectra")
if paper == live or paper == core / "data" / "spectra":
    raise SystemExit("Backup refused: paper state resolves to the live state directory.")
try:
    paper.relative_to(live)
except ValueError:
    try:
        live.relative_to(paper)
    except ValueError:
        pass
    else:
        raise SystemExit("Backup refused: paper and live state directories overlap.")
else:
    raise SystemExit("Backup refused: paper and live state directories overlap.")
if not paper.is_dir():
    raise SystemExit(f"Paper state directory does not exist: {paper}")

target.mkdir(parents=True, exist_ok=True)
for source in sorted(paper.rglob("*")):
    if source.is_symlink():
        raise SystemExit(f"Backup refused: symlink in paper state: {source}")
    relative = source.relative_to(paper)
    destination = target / relative
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        continue
    if not source.is_file() or source.name.endswith(("-wal", "-shm")):
        continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(destination, timeout=10)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise SystemExit(f"SQLite integrity check failed for {relative}: {result}")
        finally:
            dst.close()
            src.close()
        destination.chmod(0o600)
    else:
        shutil.copy2(source, destination)
        destination.chmod(0o600)

# Keep the validated policy and breaker snapshot; no .env, API credentials,
# private dispatch key, connector credential directory, or live-state data.
print(f"Snapshotted isolated paper state from {paper}")
PY

tar -czf "$ARCHIVE" -C "$STAGING" state
chmod 600 "$ARCHIVE"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'spectra-paper-*.tar.gz' -printf '%T@ %p\n' \
  | sort -rn | tail -n +15 | cut -d' ' -f2- | while IFS= read -r old; do
      [[ -n "$old" ]] && rm -f -- "$old"
    done
say "PASS: consistent paper-state backup created at $ARCHIVE (14 most recent retained)."
