#!/usr/bin/env bash
# Adopt a legacy nohup Core process without starting a duplicate, then supervise
# the next Core process in the foreground for systemd restart-on-failure.
set -Eeuo pipefail
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
PYTHON="$CORE_DIR/.venv/bin/python"
MAIN="$CORE_DIR/main.py"

[[ -x "$PYTHON" && -f "$MAIN" ]] || {
  echo "[SPECTRA CORE] Core runtime is not installed." >&2
  exit 1
}

# The installer predates systemd supervision and may have started Core with nohup.
# Adopt that healthy process rather than binding the dashboard port a second time.
if curl -fsS http://127.0.0.1:8088/ >/dev/null 2>&1; then
  LEGACY_PID=""
  if [[ -f "$INSTALL_ROOT/condor.pid" ]]; then
    LEGACY_PID="$(cat "$INSTALL_ROOT/condor.pid")"
  fi
  LEGACY_COMMAND=""
  if [[ -n "$LEGACY_PID" ]] && kill -0 "$LEGACY_PID" 2>/dev/null; then
    LEGACY_COMMAND="$(ps -p "$LEGACY_PID" -o args= 2>/dev/null || true)"
  fi
  if [[ "$LEGACY_COMMAND" != *"$MAIN"* ]]; then
    echo "[SPECTRA CORE] Port 8088 responds, but no matching legacy Spectra Core PID is verified; refusing to adopt an unknown process." >&2
    exit 1
  fi
  echo "[SPECTRA CORE] Verified legacy Core PID $LEGACY_PID; adopting it until it exits."
  while kill -0 "$LEGACY_PID" 2>/dev/null && curl -fsS http://127.0.0.1:8088/ >/dev/null 2>&1; do
    sleep 5
  done
  echo "[SPECTRA CORE] Verified legacy Core process exited; starting the supervised process."
fi

cd "$CORE_DIR"
exec "$PYTHON" "$MAIN"
