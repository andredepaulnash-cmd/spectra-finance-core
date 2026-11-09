#!/usr/bin/env bash
# Resumable, terminal-independent updater for the Spectra Alpha paper API on Bazzite.
# It updates one API source file, starts existing dependencies, rebuilds only the API,
# and never removes containers, networks, or named volumes.
set -Eeuo pipefail

INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
LOG_DIR="$INSTALL_ROOT/logs"
LOG_FILE="$LOG_DIR/api-update.log"
LOCK_FILE="$LOG_DIR/api-update.lock"
COMPOSE_FILE="$API_DIR/docker-compose.alpha.yml"
API_SOURCE="services/unified_connector_service.py"

say() { printf '[SPECTRA API UPDATE] %s\n' "$*"; }
die() { say "STOP: $*" >&2; exit 1; }

mkdir -p "$LOG_DIR"

if [[ "${1:-}" != "--run" ]]; then
  if [[ "${1:-}" != "" ]]; then
    die "Usage: $0 [--run]"
  fi
  nohup "$0" --run >>"$LOG_FILE" 2>&1 </dev/null &
  printf 'Update started in the background.\nLog: %s\nPID: %s\n' "$LOG_FILE" "$!"
  printf 'Check progress with: tail -n 40 "%s"\n' "$LOG_FILE"
  exit 0
fi

exec 9>"$LOCK_FILE"
flock -n 9 || die "Another Spectra API update is already running. Check $LOG_FILE."

command -v podman >/dev/null 2>&1 || die "Podman is not installed."
command -v podman-compose >/dev/null 2>&1 || die "podman-compose is not installed."
command -v git >/dev/null 2>&1 || die "Git is not installed."
command -v curl >/dev/null 2>&1 || die "curl is not installed."
[[ -d "$API_DIR/.git" ]] || die "API Git checkout not found at $API_DIR."
[[ -f "$API_DIR/.env" ]] || die "API .env is missing; refusing to generate or replace credentials."
[[ -f "$COMPOSE_FILE" ]] || die "Alpha Compose file is missing: $COMPOSE_FILE"
[[ -f "$API_DIR/Dockerfile.alpha" ]] || die "Alpha Dockerfile is missing: $API_DIR/Dockerfile.alpha"

# Verify paper-only safeguards without displaying or exporting any credentials.
python3 - "$API_DIR/.env" <<'PY'
from pathlib import Path
import sys
values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\\\"").strip("'")
required = {
    "SPECTRA_PAPER_ONLY": "true",
    "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
    "SPECTRA_PAPER_CONNECTORS": "kraken_paper_trade",
}
wrong = [key for key, expected in required.items() if values.get(key, "").lower() != expected]
if wrong:
    raise SystemExit("Paper-safety preflight failed for: " + ", ".join(wrong))
print("Paper-only configuration verified; live execution remains disabled.")
PY

cd "$API_DIR"
say "Fetching the merged API main branch."
git fetch --depth 1 origin main

# Confirm the upstream file contains the fail-closed symbol-map behavior before applying it.
git show "FETCH_HEAD:$API_SOURCE" | grep -q "refusing registration" \
  || die "Fetched API source does not contain the expected fail-closed Kraken fix."
git restore --source=FETCH_HEAD --worktree -- "$API_SOURCE"

grep -q "refusing registration" "$API_SOURCE" \
  || die "The fail-closed Kraken fix was not applied locally."
say "Updated only $API_SOURCE; local credentials and bot configuration were left untouched."

say "Starting the existing Postgres and EMQX services (named volumes are preserved)."
podman-compose -p spectra-alpha -f "$COMPOSE_FILE" up -d postgres emqx

for container in spectra-alpha-hummingbot-postgres spectra-alpha-hummingbot-broker; do
  say "Waiting for $container health."
  healthy=false
  for _ in $(seq 1 90); do
    status="$(podman inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      healthy=true
      break
    fi
    if [[ "$status" == "unhealthy" ]]; then
      podman logs --tail 80 "$container" || true
      die "$container reported unhealthy."
    fi
    sleep 2
  done
  [[ "$healthy" == true ]] || {
    podman logs --tail 80 "$container" || true
    die "$container did not become healthy within 180 seconds."
  }
done

say "Building and updating only the Hummingbot API container."
podman-compose -p spectra-alpha -f "$COMPOSE_FILE" up -d --build hummingbot-api

status="$(podman inspect --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null || true)"
[[ "$status" == "running" ]] || {
  podman logs --tail 160 spectra-alpha-hummingbot-api 2>/dev/null || true
  die "API container is not running after the update."
}

say "SUCCESS: API image rebuilt and container is running."
say "Postgres and EMQX were started in place; no volumes or local credentials were deleted."
say "Next: verify the API and Kraken paper order book from the local dashboard."
