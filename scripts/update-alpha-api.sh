#!/usr/bin/env bash
# Foreground updater for the Spectra Alpha paper API on Bazzite.
# It updates only the reviewed API source files, starts existing dependencies, rebuilds only the API,
# and never removes containers, networks, or named volumes.
set -Eeuo pipefail

INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
LOG_DIR="$INSTALL_ROOT/logs"
LOG_FILE="$LOG_DIR/api-update.log"
LOCK_FILE="$LOG_DIR/api-update.lock"
COMPOSE_FILE="$API_DIR/docker-compose.alpha.yml"
API_SOURCES=("services/unified_connector_service.py" "services/market_data_service.py" "deps.py")

say() { printf '[SPECTRA API UPDATE] %s\n' "$*"; }
die() { say "STOP: $*" >&2; exit 1; }

mkdir -p "$LOG_DIR"

if [[ "$#" -ne 0 ]]; then
  die "This updater runs in the foreground and accepts no arguments."
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

# Never overwrite local edits in either file this updater needs to change.
if ! git diff --quiet -- "${API_SOURCES[@]}" || ! git diff --cached --quiet -- "${API_SOURCES[@]}"; then
  die "Local edits exist in an API source file this update needs. Preserve/review them first; nothing was overwritten."
fi

# Confirm both reviewed fixes exist upstream before changing either local file.
git show "FETCH_HEAD:${API_SOURCES[0]}" | grep -q "refusing registration" \
  || die "Fetched API source does not contain the expected fail-closed Kraken fix."
git show "FETCH_HEAD:${API_SOURCES[1]}" | grep -q "A read request must be able to initialize its own feed" \
  || die "Fetched market-data service does not contain the lazy order-book initialization fix."
git show "FETCH_HEAD:${API_SOURCES[2]}" | grep -q "Bot orchestration is disabled in Spectra paper-only mode" \
  || die "Fetched API dependency does not contain the paper-only bot-status guard."

git restore --source=FETCH_HEAD --worktree -- "${API_SOURCES[@]}"

grep -q "refusing registration" "${API_SOURCES[0]}" \
  || die "The fail-closed Kraken fix was not applied locally."
grep -q "A read request must be able to initialize its own feed" "${API_SOURCES[1]}" \
  || die "The lazy order-book initialization fix was not applied locally."
grep -q "Bot orchestration is disabled in Spectra paper-only mode" "${API_SOURCES[2]}" \
  || die "The paper-only bot-status guard was not applied locally."
say "Updated only ${API_SOURCES[*]}; local credentials, configuration, and paper state were left untouched."

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
