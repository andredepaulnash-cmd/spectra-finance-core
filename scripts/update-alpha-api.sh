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
  ["services/unified_connector_service.py"]="185f03c85f00d7ccb95817551f0990ccda4a2fb4"
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
CORE_NEW_BLOB="6ae7ba93d253f2127b47a301b3e33121a9f54895"
[[ "$CORE_UPSTREAM_BLOB" == "$CORE_NEW_BLOB" ]] \
  || die "Fetched trade-screen component is not the reviewed version."
if [[ "$CORE_WORKTREE_BLOB" != "$CORE_NEW_BLOB" && "$CORE_WORKTREE_BLOB" != "$CORE_OLD_BLOB" ]]; then
  die "Unrecognized local changes in $CORE_FRONTEND_SOURCE. They were preserved; review them before updating."
fi
git -C "$CORE_DIR" restore --source=FETCH_HEAD --worktree -- "$CORE_FRONTEND_SOURCE"

# Apply the reviewed Command Center shell polish only when the local files are
# either the exact previous Alpha versions or already match this release.
CORE_SHELL_SOURCES=("frontend/src/components/layout/AppShell.tsx" "frontend/src/index.css")
declare -A CORE_SHELL_OLD_BLOBS=(
  ["frontend/src/components/layout/AppShell.tsx"]="05426a040450d516f9080628ba56bad71168e03a"
  ["frontend/src/index.css"]="451463a274340865086b505f1d63acbd830e55a5"
)
declare -A CORE_SHELL_NEW_BLOBS=(
  ["frontend/src/components/layout/AppShell.tsx"]="776e421f5ab1312ab97a09756bc82df8e4da0fc5"
  ["frontend/src/index.css"]="6d79e7a5111d423be6bf260a65e238c0344a4f5e"
)
for source in "${CORE_SHELL_SOURCES[@]}"; do
  upstream_blob="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$source")"
  expected_blob="${CORE_SHELL_NEW_BLOBS[$source]}"
  if [[ "$upstream_blob" != "$expected_blob" ]]; then
    die "Fetched $source is not the reviewed Command Center version."
  fi
  worktree_blob="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$source")"
  old_blob="${CORE_SHELL_OLD_BLOBS[$source]}"
  if [[ "$worktree_blob" != "$expected_blob" && "$worktree_blob" != "$old_blob" ]]; then
    die "Unrecognized local changes in $source. They were preserved; review them before updating."
  fi
done
git -C "$CORE_DIR" restore --source=FETCH_HEAD --worktree -- "${CORE_SHELL_SOURCES[@]}"
say "Updated the reviewed Command Center shell polish without changing application behavior."

# Install the reviewed paper lifecycle operator, health monitor and user services.
CORE_OPS_SOURCES=(
  "scripts/spectra"
  "scripts/spectra-alpha-doctor.sh"
  "scripts/spectra-alpha-start-api.sh"
  "scripts/spectra-alpha-core-supervisor.sh"
  "scripts/systemd/spectra-alpha-core.service"
  "scripts/systemd/spectra-alpha-doctor.service"
  "scripts/systemd/spectra-alpha-doctor.timer"
)
declare -A CORE_OPS_NEW_BLOBS=(
  ["scripts/spectra"]="594d26cc87716409053303e6d441b7e1ad1c2ed2"
  ["scripts/spectra-alpha-doctor.sh"]="be0d38bc075b31a291f2779ce917eba8c59689f9"
  ["scripts/spectra-alpha-start-api.sh"]="a778ea113f445ab6a70319e400ba16d0f0c33bb3"
  ["scripts/spectra-alpha-core-supervisor.sh"]="e063affc6b22527f5a727bbc47f5afba4c361dff"
  ["scripts/systemd/spectra-alpha-core.service"]="9386ab538af54f03129264a7d5134420e5a622bf"
  ["scripts/systemd/spectra-alpha-doctor.service"]="054b22ea89cb8888419ec8000b5ffa0975f1b73d"
  ["scripts/systemd/spectra-alpha-doctor.timer"]="30af1b179bda4aec03105018e09b07dba1ee4b85"
)
for source in "${CORE_OPS_SOURCES[@]}"; do
  upstream_blob="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$source")"
  expected_blob="${CORE_OPS_NEW_BLOBS[$source]}"
  if [[ "$upstream_blob" != "$expected_blob" ]]; then
    die "Fetched $source is not the reviewed Alpha operations version."
  fi
  if [[ -f "$CORE_DIR/$source" ]]; then
    worktree_blob="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$source")"
    if [[ "$worktree_blob" != "$expected_blob" ]]; then
      die "Unrecognized local changes in $source. They were preserved; review them before updating."
    fi
  fi
done
git -C "$CORE_DIR" restore --source=FETCH_HEAD --worktree -- "${CORE_OPS_SOURCES[@]}"
install -m 700 "$CORE_DIR/scripts/spectra" "$HOME/.local/bin/spectra"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-doctor.sh" "$HOME/.local/bin/spectra-alpha-doctor"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-start-api.sh" "$HOME/.local/bin/spectra-alpha-start-api"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-core-supervisor.sh" "$HOME/.local/bin/spectra-alpha-core-supervisor"

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
python3 - "$CORE_DIR/scripts/systemd" "$SYSTEMD_USER_DIR" "$INSTALL_ROOT" "$HOME/.local/bin" <<'PYSYSTEMD'
from pathlib import Path
import sys
source, target, install_root, local_bin = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
for name in (
    "spectra-alpha-core.service",
    "spectra-alpha-doctor.service",
    "spectra-alpha-doctor.timer",
):
    text = (source / name).read_text(encoding="utf-8")
    text = text.replace("@INSTALL_ROOT@", install_root).replace("@LOCAL_BIN@", local_bin)
    (target / name).write_text(text, encoding="utf-8")
    (target / name).chmod(0o600)
PYSYSTEMD
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable spectra-alpha-core.service >/dev/null
  systemctl --user enable --now spectra-alpha-doctor.timer >/dev/null
  say "Enabled Core restart-on-failure at user login and the two-minute read-only health monitor."
else
  say "No active user systemd manager; operator CLI remains available without changing OS services."
fi

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
say "Running read-only operational health checks."
if ! "$HOME/.local/bin/spectra-alpha-doctor"; then
  say "The deployment completed, but one or more operational health checks need attention. See $INSTALL_ROOT/logs/doctor.log."
fi
say "Deployment, supervised-startup setup, and verification sequence complete."
