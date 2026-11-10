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
  echo "[SPECTRA CORE] Existing Core process detected; adopting its dashboard until it exits."
  while curl -fsS http://127.0.0.1:8088/ >/dev/null 2>&1; do
    sleep 5
  done
  echo "[SPECTRA CORE] Existing Core process exited; starting the supervised process."
fi

cd "$CORE_DIR"
exec "$PYTHON" "$MAIN"
