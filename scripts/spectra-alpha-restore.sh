#!/usr/bin/env bash
# Restore an isolated paper-state archive after an explicit operator confirmation.
set -Eeuo pipefail
umask 077
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
LOCAL_BIN="$HOME/.local/bin"

if [[ "$#" -ne 2 || "$2" != "--confirm" ]]; then
  echo "Usage: spectra-alpha-restore <archive-under-backups/> --confirm" >&2
  exit 2
fi
[[ -f "$CORE_DIR/.env" ]] || { echo "Core .env is missing; refusing restore." >&2; exit 1; }
[[ -x "$LOCAL_BIN/spectra-alpha-backup" ]] || { echo "Paper backup command is unavailable; refusing restore." >&2; exit 1; }
[[ -x "$CORE_DIR/.venv/bin/python" ]] || { echo "Spectra Core Python environment is unavailable; refusing restore." >&2; exit 1; }
[[ -f "$CORE_DIR/scripts/spectra-alpha-restore.py" ]] || { echo "Reviewed restore implementation is missing." >&2; exit 1; }

echo "[SPECTRA RESTORE] Creating a fresh backup of the current paper state first."
"$LOCAL_BIN/spectra-alpha-backup"
echo "[SPECTRA RESTORE] Validating and atomically restoring the selected paper archive."
"$CORE_DIR/.venv/bin/python" "$CORE_DIR/scripts/spectra-alpha-restore.py" \
  --install-root "$INSTALL_ROOT" --archive "$1"
