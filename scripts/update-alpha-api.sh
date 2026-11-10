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

# Reconcile only source states that are known and reviewed.
# The working tree is what the API image builds from. Do not compare the Git index:
# a stale staged blob must not block an already-correct worktree, and this updater
# deliberately never changes the index.
#
# These two blob IDs are the exact older paper-alpha files found on the existing
# Bazzite installation. They differ from API main only by the two fixes below:
#   market_data_service.py: missing lazy order-book initialization
#   deps.py: missing paper-only 503 guard for disabled bot orchestration
# Any other local source version is preserved and causes a safe stop.
declare -A APPROVED_LEGACY_BLOBS=(
  ["services/unified_connector_service.py"]="27b79133556466d6d27ec130034f28444d33f860"
  ["services/market_data_service.py"]="132d74e8d0a182d65ab740763cab8a90bd176990"
  ["deps.py"]="fc93e8d86b5639b329fe8549873989275e60ff4e"
)

for source in "${API_SOURCES[@]}"; do
  upstream_blob="$(git rev-parse "FETCH_HEAD:${source}")"
  worktree_blob="$(git hash-object "${source}")"
  if [[ "$worktree_blob" == "$upstream_blob" ]]; then
    say "$source already matches reviewed upstream."
    continue
  fi

  approved_blob="${APPROVED_LEGACY_BLOBS[$source]:-}"
  if [[ -n "$approved_blob" && "$worktree_blob" == "$approved_blob" ]]; then
    say "Recognized the exact pre-fix Alpha version of $source; applying its reviewed upstream fix."
    continue
  fi

  die "Unrecognized local source version in $source. It was not overwritten; preserve/review it before updating."
done

# Verify the exact reviewed API source blobs. This is deterministic and avoids
# shell text-search, pager, locale, and pipefail behavior entirely.
declare -A EXPECTED_UPSTREAM_BLOBS=(
  ["services/unified_connector_service.py"]="dec6e2e7e3a6e4016dae05c5575c42a35633fb0c"
  ["services/market_data_service.py"]="9236689f87dc32aae2478100419b9a2e6e645f73"
  ["deps.py"]="254ee28a495ef2354b40b5f4118b854b28d1b67b"
)

for source in "${API_SOURCES[@]}"; do
  actual_blob="$(git rev-parse "FETCH_HEAD:${source}")"
  expected_blob="${EXPECTED_UPSTREAM_BLOBS[$source]}"
  [[ "$actual_blob" == "$expected_blob" ]] \
    || die "Fetched $source is not the reviewed API version (expected $expected_blob, got $actual_blob)."
done

git restore --source=FETCH_HEAD --worktree -- "${API_SOURCES[@]}"

grep -q "refusing registration" "${API_SOURCES[0]}" \
  || die "The fail-closed Kraken fix was not applied locally."
grep -q "A read request must be able to initialize its own feed" "${API_SOURCES[1]}" \
  || die "The lazy order-book initialization fix was not applied locally."
grep -q "Bot orchestration is disabled in Spectra paper-only mode" "${API_SOURCES[2]}" \
  || die "The paper-only bot-status guard was not applied locally."
say "Updated only ${API_SOURCES[*]}; local credentials, configuration, and paper state were left untouched."

say "Starting the existing Postgres and EMQX services (named volumes are preserved)."
podman-compose -p spectra-alpha -f "$COMPOSE_FILE" up -d postgres emqx 9>&-

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
podman-compose -p spectra-alpha -f "$COMPOSE_FILE" up -d --build hummingbot-api 9>&-

status="$(podman inspect --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null || true)"
[[ "$status" == "running" ]] || {
  podman logs --tail 160 spectra-alpha-hummingbot-api 2>/dev/null || true
  die "API container is not running after the update."
}

say "SUCCESS: API image rebuilt and container is running."
say "Postgres and EMQX were started in place; no volumes or local credentials were deleted."

# Update only the reviewed OrderBook component in the existing Core checkout,
# then rebuild the static frontend. The running web process serves dist files
# from disk, so no Core restart or user-data migration is required.
CORE_DIR="$INSTALL_ROOT/spectra-core"
CORE_FRONTEND_SOURCE="frontend/src/components/market/OrderBook.tsx"
[[ -d "$CORE_DIR/.git" ]] || die "Core Git checkout not found at $CORE_DIR."
[[ -f "$CORE_DIR/frontend/package.json" ]] || die "Core frontend package.json is missing."
say "Fetching the reviewed Spectra trade-screen market-feed status component."
git -C "$CORE_DIR" fetch --depth 1 origin alpha/one-command-paper-demo-20261108
CORE_UPSTREAM_BLOB="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$CORE_FRONTEND_SOURCE")"
CORE_WORKTREE_BLOB="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$CORE_FRONTEND_SOURCE")"
CORE_OLD_BLOB="47e36a785b4e02a50ca8f34093727403cb692750"
CORE_NEW_BLOB="7e089fad16334dee3ca0f991c1f2b6bef783ff7a"
[[ "$CORE_UPSTREAM_BLOB" == "$CORE_NEW_BLOB" ]] \
  || die "Fetched trade-screen component is not the reviewed version."
if [[ "$CORE_WORKTREE_BLOB" != "$CORE_NEW_BLOB" && "$CORE_WORKTREE_BLOB" != "$CORE_OLD_BLOB" ]]; then
  die "Unrecognized local changes in $CORE_FRONTEND_SOURCE. They were preserved; review them before updating."
fi
git -C "$CORE_DIR" restore --source=FETCH_HEAD --worktree -- "$CORE_FRONTEND_SOURCE"

if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  . "$HOME/.nvm/nvm.sh"
  nvm use 22 >/dev/null
fi
command -v npm >/dev/null 2>&1 || die "npm is required to rebuild the Spectra dashboard."
cd "$CORE_DIR/frontend"
if [[ ! -f node_modules/.package-lock.json || package-lock.json -nt node_modules/.package-lock.json ]]; then
  say "Synchronizing the existing frontend dependencies."
  npm ci
fi
say "Building the updated Spectra trade screen."
npm run build
cd "$INSTALL_ROOT"

# Install and run the authenticated read-only verifier as the final deployment
# step. It reports tracker and underlying public-connector symbol-map state.
VERIFY_BIN="$HOME/.local/bin/spectra-verify-alpha-paper-market-data"
VERIFY_URL="https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/alpha/one-command-paper-demo-20261108/scripts/verify-alpha-paper-market-data.sh"
mkdir -p "$(dirname "$VERIFY_BIN")"
say "Installing the read-only Kraken paper market-data verifier."
curl -fsSL "$VERIFY_URL" -o "$VERIFY_BIN"
chmod 700 "$VERIFY_BIN"
bash -n "$VERIFY_BIN" || die "The downloaded paper market-data verifier failed shell syntax validation."
say "Running authenticated, read-only Kraken paper market-data verification."
if ! "$VERIFY_BIN"; then
  say "API deployment succeeded, but Kraken paper market data is not yet verified. Review the diagnostics above."
fi
say "Deployment and verification sequence complete."
