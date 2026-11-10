#!/usr/bin/env bash
# Foreground one-command updater for Spectra Finance Alpha on Bazzite.
# It updates exact reviewed API, Core Python, dashboard, and operator files,
# preserves all state, and never removes containers, networks, or named volumes.
set -Eeuo pipefail

INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
CORE_REF="${SPECTRA_ALPHA_CORE_REF:-alpha/one-command-paper-demo-20261108}"
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

COMPOSE_KIND_FILE="$INSTALL_ROOT/.compose-kind"
[[ -f "$COMPOSE_KIND_FILE" ]] || die "The recorded Compose provider is missing; refusing to switch container runtimes."
COMPOSE_KIND="$(tr -d '[:space:]' < "$COMPOSE_KIND_FILE")"
case "$COMPOSE_KIND" in
  docker)
    command -v docker >/dev/null 2>&1 || die "This installation uses Docker Compose, but Docker is unavailable."
    docker compose version >/dev/null 2>&1 || die "The recorded Docker Compose provider is unavailable."
    compose() { docker compose "$@"; }
    CONTAINER_CLI="docker"
    ;;
  podman)
    command -v podman >/dev/null 2>&1 || die "This installation uses Podman, but Podman is unavailable."
    if command -v podman-compose >/dev/null 2>&1; then
      compose() { podman-compose "$@"; }
    elif podman compose version >/dev/null 2>&1; then
      compose() { podman compose "$@"; }
    else
      die "Neither podman-compose nor the Podman Compose plugin is available."
    fi
    CONTAINER_CLI="podman"
    ;;
  *)
    die "Unknown recorded Compose provider '$COMPOSE_KIND'; refusing to switch container runtimes."
    ;;
esac
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
API_UPSTREAM_COMMIT="$(git rev-parse FETCH_HEAD)"

# Reconcile known Alpha sources. Preserve local work using a three-way merge
# against the last shared Alpha baseline; a conflict stops before any source is
# changed. The Git index is never modified.
declare -A APPROVED_LEGACY_BLOBS=(
  ["services/market_data_service.py"]="132d74e8d0a182d65ab740763cab8a90bd176990"
  ["deps.py"]="fc93e8d86b5639b329fe8549873989275e60ff4e"
)
declare -A EXPECTED_UPSTREAM_BLOBS=(
  ["services/unified_connector_service.py"]="185f03c85f00d7ccb95817551f0990ccda4a2fb4"
  ["services/market_data_service.py"]="9236689f87dc32aae2478100419b9a2e6e645f73"
  ["deps.py"]="254ee28a495ef2354b40b5f4118b854b28d1b67b"
)

MERGE_TMP="$(mktemp -d "$LOG_DIR/api-source-merge.XXXXXX")"
trap 'rm -rf "$MERGE_TMP"' EXIT
UNIFIED_SOURCE="services/unified_connector_service.py"
UNIFIED_BASE_COMMIT="7dda696631cf4174cabff2cfd060358c249a88e0"
UNIFIED_BASE_BLOB="27b79133556466d6d27ec130034f28444d33f860"

for source in "${API_SOURCES[@]}"; do
  upstream_blob="$(git rev-parse "$API_UPSTREAM_COMMIT:${source}")"
  expected_blob="${EXPECTED_UPSTREAM_BLOBS[$source]}"
  [[ "$upstream_blob" == "$expected_blob" ]] \
    || die "Fetched $source is not the reviewed API version (expected $expected_blob, got $upstream_blob)."

  worktree_blob="$(git hash-object "$source")"
  if [[ "$worktree_blob" == "$upstream_blob" ]]; then
    say "$source already matches reviewed upstream."
    continue
  fi

  approved_blob="${APPROVED_LEGACY_BLOBS[$source]:-}"
  if [[ -n "$approved_blob" && "$worktree_blob" == "$approved_blob" ]]; then
    say "Recognized the exact pre-fix Alpha version of $source."
    continue
  fi

  if [[ "$source" == "$UNIFIED_SOURCE" ]]; then
    say "Preserving local $source changes while applying the reviewed upstream fix."
    git fetch --depth 1 origin "$UNIFIED_BASE_COMMIT"
    base_blob="$(git rev-parse "$UNIFIED_BASE_COMMIT:$UNIFIED_SOURCE")"
    [[ "$base_blob" == "$UNIFIED_BASE_BLOB" ]] \
      || die "The known Alpha merge base for $UNIFIED_SOURCE did not match."
    git show "$UNIFIED_BASE_COMMIT:$UNIFIED_SOURCE" > "$MERGE_TMP/unified.base"
    git show "$API_UPSTREAM_COMMIT:$UNIFIED_SOURCE" > "$MERGE_TMP/unified.upstream"
    if ! git merge-file -p "$source" "$MERGE_TMP/unified.base" "$MERGE_TMP/unified.upstream" > "$MERGE_TMP/unified.merged"; then
      die "Local $source has conflicting edits. It was preserved; no API source files were changed."
    fi
    grep -q "refusing registration" "$MERGE_TMP/unified.merged" \
      || die "Merged $source is missing the fail-closed Kraken validation."
    cp "$MERGE_TMP/unified.merged" "$MERGE_TMP/unified.install"
    continue
  fi

  die "Unrecognized local source version in $source. It was not overwritten."
done

# Apply only after every source passed preflight. Local non-conflicting edits to
# unified_connector_service.py survive; the other two files use exact approved
# legacy versions or exact upstream versions.
for source in "${API_SOURCES[@]}"; do
  if [[ "$source" == "$UNIFIED_SOURCE" && -f "$MERGE_TMP/unified.install" ]]; then
    cat "$MERGE_TMP/unified.install" > "$source"
  else
    git restore --source="$API_UPSTREAM_COMMIT" --worktree -- "$source"
  fi
done

grep -q "refusing registration" "${API_SOURCES[0]}" \
  || die "The fail-closed Kraken fix was not applied locally."
grep -q "A read request must be able to initialize its own feed" "${API_SOURCES[1]}" \
  || die "The lazy order-book initialization fix was not applied locally."
grep -q "Bot orchestration is disabled in Spectra paper-only mode" "${API_SOURCES[2]}" \
  || die "The paper-only bot-status guard was not applied locally."
say "Updated only ${API_SOURCES[*]}; local credentials, configuration, and paper state were left untouched."

say "Checking the existing Postgres and EMQX containers (named volumes are preserved)."
# This is an in-place updater, not an installer: never ask Compose to recreate
# broker/database containers. Recreate attempts can collide with their own
# already-published loopback ports under some Podman Compose providers.
for container in spectra-alpha-hummingbot-postgres spectra-alpha-hummingbot-broker; do
  container_inventory="$("$CONTAINER_CLI" ps --all --format '{{.Names}}' 2>/dev/null || true)"
  if ! grep -Fq -- "$container" <<<"$container_inventory"; then
    say "Podman container inventory: $container_inventory"
    die "$container is missing from Podman inventory; refusing to recreate stateful services."
  fi
  status="$("$CONTAINER_CLI" inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
  if [[ "$status" != "running" ]]; then
    say "Starting existing $container without recreating it."
    "$CONTAINER_CLI" start "$container" >/dev/null || die "Could not start existing $container."
  else
    say "$container is already running; leaving it in place."
  fi
done

for container in spectra-alpha-hummingbot-postgres spectra-alpha-hummingbot-broker; do
  say "Waiting for $container health."
  healthy=false
  for _ in $(seq 1 90); do
    status="$("$CONTAINER_CLI" inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      healthy=true
      break
    fi
    if [[ "$status" == "unhealthy" ]]; then
      "$CONTAINER_CLI" logs --tail 80 "$container" || true
      die "$container reported unhealthy."
    fi
    sleep 2
  done
  [[ "$healthy" == true ]] || {
    "$CONTAINER_CLI" logs --tail 80 "$container" || true
    die "$container did not become healthy within 180 seconds."
  }
done

say "Building and updating only the Hummingbot API container."
compose -p spectra-alpha -f "$COMPOSE_FILE" up -d --build --no-deps hummingbot-api 9>&-

status="$("$CONTAINER_CLI" inspect --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null || true)"
[[ "$status" == "running" ]] || {
  "$CONTAINER_CLI" logs --tail 160 spectra-alpha-hummingbot-api 2>/dev/null || true
  die "API container is not running after the update."
}

say "SUCCESS: API image rebuilt and container is running."
say "Postgres and EMQX were started in place; no volumes or local credentials were deleted."

# Update exact reviewed Core UI, Python authority, route, and operator files.
# Rebuild the dashboard and restart Core only; paper execution containers stay up.
CORE_FRONTEND_SOURCE="frontend/src/components/market/OrderBook.tsx"
[[ -d "$CORE_DIR/.git" ]] || die "Core Git checkout not found at $CORE_DIR."
git -C "$CORE_DIR" fetch --depth 1 origin "$CORE_REF"
[[ -f "$CORE_DIR/frontend/package.json" ]] || die "Core frontend package.json is missing."
say "Fetching the reviewed Spectra trade-screen market-feed status component."
CORE_UPSTREAM_BLOB="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$CORE_FRONTEND_SOURCE")"
CORE_WORKTREE_BLOB="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$CORE_FRONTEND_SOURCE")"
CORE_OLD_BLOB="47e36a785b4e02a50ca8f34093727403cb692750"
CORE_NEW_BLOB="6ae7ba93d253f2127b47a301b3e33121a9f54895"
[[ "$CORE_UPSTREAM_BLOB" == "$CORE_NEW_BLOB" ]] \
  || die "Fetched trade-screen component is not the reviewed version."
if [[ "$CORE_WORKTREE_BLOB" != "$CORE_NEW_BLOB" && "$CORE_WORKTREE_BLOB" != "$CORE_OLD_BLOB" ]]; then
  die "Unrecognized local changes in $CORE_FRONTEND_SOURCE. They were preserved; review them before updating."
fi
git -C "$CORE_DIR" restore --source="$API_UPSTREAM_COMMIT" --worktree -- "$CORE_FRONTEND_SOURCE"

# Apply the reviewed Command Center shell polish only when the local files are
# either the exact previous Alpha versions or already match this release.
CORE_SHELL_SOURCES=("frontend/src/components/layout/AppShell.tsx" "frontend/src/index.css" "frontend/src/pages/SpectraOverview.tsx" "frontend/src/lib/api.ts")
declare -A CORE_SHELL_OLD_BLOBS=(
  ["frontend/src/components/layout/AppShell.tsx"]="05426a040450d516f9080628ba56bad71168e03a"
  ["frontend/src/index.css"]="451463a274340865086b505f1d63acbd830e55a5"
  ["frontend/src/pages/SpectraOverview.tsx"]="79f74823b59409293db89f2704f3678d2f1b5fb9"
  ["frontend/src/lib/api.ts"]="dfdc7867012c37de2e0c1fc0ea1ee8cffbbccf10"
)
declare -A CORE_SHELL_NEW_BLOBS=(
  ["frontend/src/components/layout/AppShell.tsx"]="776e421f5ab1312ab97a09756bc82df8e4da0fc5"
  ["frontend/src/index.css"]="6d79e7a5111d423be6bf260a65e238c0344a4f5e"
  ["frontend/src/pages/SpectraOverview.tsx"]="dac12adcbe9337309b82e5f58cf551dc35c0c094"
  ["frontend/src/lib/api.ts"]="16d5a5adbf3af1bd4d5d2244b5d3850bc3c92494"
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
git -C "$CORE_DIR" restore --source="$API_UPSTREAM_COMMIT" --worktree -- "${CORE_SHELL_SOURCES[@]}"
say "Updated the reviewed Command Center shell and Kraken discovery UI."

# Install the reviewed paper lifecycle operator, health monitor and user services.
CORE_OPS_SOURCES=(
  "scripts/spectra"
  "scripts/spectra-alpha-doctor.sh"
  "scripts/spectra-alpha-start-api.sh"
  "scripts/spectra-alpha-core-supervisor.sh"
  "scripts/spectra-alpha-backup.sh"
  "scripts/systemd/spectra-alpha-core.service"
  "scripts/systemd/spectra-alpha-doctor.service"
  "scripts/systemd/spectra-alpha-doctor.timer"
  "scripts/systemd/spectra-alpha-backup.service"
  "scripts/systemd/spectra-alpha-backup.timer"
)
declare -A CORE_OPS_NEW_BLOBS=(
  ["scripts/spectra"]="774ae8e7912896d7266fbb06c459b9f5a48a7dc3"
  ["scripts/spectra-alpha-doctor.sh"]="96ab3e9783b61d69c80cf6efea980157f5f1ed9a"
  ["scripts/spectra-alpha-start-api.sh"]="a778ea113f445ab6a70319e400ba16d0f0c33bb3"
  ["scripts/spectra-alpha-core-supervisor.sh"]="e729bab6182fa566cda6b1f3db56f84f589ae3d2"
  ["scripts/systemd/spectra-alpha-core.service"]="9386ab538af54f03129264a7d5134420e5a622bf"
  ["scripts/systemd/spectra-alpha-doctor.service"]="054b22ea89cb8888419ec8000b5ffa0975f1b73d"
  ["scripts/systemd/spectra-alpha-doctor.timer"]="30af1b179bda4aec03105018e09b07dba1ee4b85"
  ["scripts/spectra-alpha-backup.sh"]="1320e0d1ccf552917105c85f17fb67d046d8a3fc"
  ["scripts/systemd/spectra-alpha-backup.service"]="e49bc3d235744a92d7c463e6a98d9b8b282ae08b"
  ["scripts/systemd/spectra-alpha-backup.timer"]="698a284d5663fc58e850f6ec8f0b85983ab32a6f"
)
for source in "${CORE_OPS_SOURCES[@]}"; do
  upstream_blob="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$source")"
  expected_blob="${CORE_OPS_NEW_BLOBS[$source]}"
  if [[ "$upstream_blob" != "$expected_blob" ]]; then
    die "Fetched $source is not the reviewed Alpha operations version."
  fi
  if [[ -L "$CORE_DIR/$source" ]]; then
    die "Refusing to replace symlinked local path $source."
  elif [[ -f "$CORE_DIR/$source" ]]; then
    worktree_blob="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$source")"
    if [[ "$worktree_blob" != "$expected_blob" ]]; then
      die "Unrecognized local changes in $source. They were preserved; review them before updating."
    fi
  fi
done
# These are new reviewed files in this Alpha release and may not exist in the
# existing checkout's index yet. Read each from the verified fetched tree without
# staging or changing the local Git index.
for source in "${CORE_OPS_SOURCES[@]}"; do
  say "Installing reviewed operator file: $source"
  target="$CORE_DIR/$source"
  parent="$(dirname "$target")"
  mkdir -p "$parent"
  git -C "$CORE_DIR" show "FETCH_HEAD:$source" > "$target" || die "Could not write reviewed operator file $target."
done

# Update only the reviewed Python files that implement the paper asset authority
# and strategy-discovery path. Exact old/new blob checks preserve local edits.
CORE_PYTHON_SOURCES=(
  "spectra_core/asset_authority.py"
  "spectra_core/kraken_authorization.py"
  "spectra_core/market_discovery.py"
  "mcp_servers/hummingbot_api/tools/executor_create.py"
  "routines/mean_reversion_scanner.py"
  "condor/web/routes/market.py"
)
declare -A CORE_PYTHON_NEW_BLOBS=(
  ["spectra_core/asset_authority.py"]="5644134509e604dc5ed0df9076130608b61fe975"
  ["spectra_core/kraken_authorization.py"]="3e3d4e195422002f43818c936d9c88a740ff7c72"
  ["spectra_core/market_discovery.py"]="5222e752f46f56bc58e4acd8bdd80f940ae1ce36"
  ["mcp_servers/hummingbot_api/tools/executor_create.py"]="14b8d4a98cb1cd6ab1243f1a794b658204cb26fa"
  ["routines/mean_reversion_scanner.py"]="48282fd0a1b616b00c3715de4ee55a3c1c569fdf"
  ["condor/web/routes/market.py"]="bebac174109a7c91a33e5a0698201ffd2ac037b0"
)
declare -A CORE_PYTHON_OLD_BLOBS=(
  ["spectra_core/asset_authority.py"]=""
  ["spectra_core/kraken_authorization.py"]="f841e3689fc6821037b2a2d288a129138265b050"
  ["spectra_core/market_discovery.py"]="0254d08864eecf2f72e9371c4292f6eba9c17df6"
  ["mcp_servers/hummingbot_api/tools/executor_create.py"]="2c3fd6ab5e0372f0350eaafe4724e0f70a9c4c1a"
  ["routines/mean_reversion_scanner.py"]="218126472c0e1af749e62ef5fdc6c3db89fb9745"
  ["condor/web/routes/market.py"]="c40bf5ec4edc40e7f49005df1a8c3417c270f8a7"
)
for source in "${CORE_PYTHON_SOURCES[@]}"; do
  upstream_blob="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$source")"
  expected_blob="${CORE_PYTHON_NEW_BLOBS[$source]}"
  if [[ "$upstream_blob" != "$expected_blob" ]]; then
    die "Fetched Python source $source is not the reviewed Governor asset-authority version."
  fi
  if [[ -L "$CORE_DIR/$source" ]]; then
    die "Refusing to replace symlinked local Python source $source."
  elif [[ -f "$CORE_DIR/$source" ]]; then
    worktree_blob="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$source")"
    old_blob="${CORE_PYTHON_OLD_BLOBS[$source]}"
    if [[ "$worktree_blob" != "$expected_blob" && "$worktree_blob" != "$old_blob" ]]; then
      die "Unrecognized local changes in $source. They were preserved; review them before updating."
    fi
  elif [[ -n "${CORE_PYTHON_OLD_BLOBS[$source]}" ]]; then
    die "Expected existing Core source $source is missing; refusing an incomplete update."
  fi
done
for source in "${CORE_PYTHON_SOURCES[@]}"; do
  mkdir -p "$(dirname "$CORE_DIR/$source")"
  git -C "$CORE_DIR" show "FETCH_HEAD:$source" > "$CORE_DIR/$source"
done
"$CORE_DIR/.venv/bin/python" -m py_compile \
  "$CORE_DIR/spectra_core/asset_authority.py" \
  "$CORE_DIR/spectra_core/kraken_authorization.py" \
  "$CORE_DIR/spectra_core/market_discovery.py" \
  "$CORE_DIR/mcp_servers/hummingbot_api/tools/executor_create.py" \
  "$CORE_DIR/routines/mean_reversion_scanner.py" \
  "$CORE_DIR/condor/web/routes/market.py"
say "Installed and syntax-checked the reviewed paper asset authority and strategy-discovery code."

install -m 700 "$CORE_DIR/scripts/spectra" "$HOME/.local/bin/spectra"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-doctor.sh" "$HOME/.local/bin/spectra-alpha-doctor"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-start-api.sh" "$HOME/.local/bin/spectra-alpha-start-api"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-core-supervisor.sh" "$HOME/.local/bin/spectra-alpha-core-supervisor"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-backup.sh" "$HOME/.local/bin/spectra-alpha-backup"

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
    "spectra-alpha-backup.service",
    "spectra-alpha-backup.timer",
):
    text = (source / name).read_text(encoding="utf-8")
    text = text.replace("@INSTALL_ROOT@", install_root).replace("@LOCAL_BIN@", local_bin)
    unit_path = target / name
    if unit_path.exists() and unit_path.read_text(encoding="utf-8") != text:
        raise SystemExit(f"Refusing to overwrite customized user unit {unit_path}; it was preserved.")
    unit_path.write_text(text, encoding="utf-8")
    unit_path.chmod(0o600)
PYSYSTEMD
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable spectra-alpha-core.service >/dev/null
  systemctl --user enable --now spectra-alpha-doctor.timer >/dev/null
  systemctl --user enable --now spectra-alpha-backup.timer >/dev/null
  say "Enabled Core restart-on-failure at user login and the two-minute read-only health monitor."
else
  say "No active user systemd manager; operator CLI remains available without changing OS services."
fi
say "Creating the initial consistent paper-state backup."
"$HOME/.local/bin/spectra-alpha-backup"

# Prefer installer-provisioned Node 22 on Bazzite. CI and hosts with
# an existing Node runtime should not be forced through an absent NVM install.
if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  . "$HOME/.nvm/nvm.sh"
  if nvm ls 22 2>/dev/null | grep -q 'v22\.'; then
    nvm use 22 >/dev/null
  fi
fi
command -v node >/dev/null 2>&1 || die "Node.js is required to rebuild the Spectra dashboard."
command -v npm >/dev/null 2>&1 || die "npm is required to rebuild the Spectra dashboard."
cd "$CORE_DIR/frontend"
if [[ ! -f node_modules/.package-lock.json || package-lock.json -nt node_modules/.package-lock.json ]]; then
  say "Synchronizing the existing frontend dependencies."
  npm ci
fi
say "Building the updated Spectra trade screen."
npm run build
cd "$INSTALL_ROOT"

say "Restarting Spectra Core only so reviewed Python changes are loaded; Hummingbot API containers remain running."
"$HOME/.local/bin/spectra" core-restart

# Install and run the authenticated read-only verifier as the final deployment
# step. It reports tracker and underlying public-connector symbol-map state.
VERIFY_BIN="$HOME/.local/bin/spectra-verify-alpha-paper-market-data"
VERIFY_URL="https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/$CORE_REF/scripts/verify-alpha-paper-market-data.sh"
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
\\n'/, }"
    die "$container is missing. The updater will not create or replace stateful services; run the Alpha installer only if this is a new installation."
  fi
  status="$("$CONTAINER_CLI" inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
  if [[ "$status" != "running" ]]; then
    say "Starting existing $container without recreating it."
    "$CONTAINER_CLI" start "$container" >/dev/null || die "Could not start existing $container."
  else
    say "$container is already running; leaving it in place."
  fi
done

for container in spectra-alpha-hummingbot-postgres spectra-alpha-hummingbot-broker; do
  say "Waiting for $container health."
  healthy=false
  for _ in $(seq 1 90); do
    status="$("$CONTAINER_CLI" inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      healthy=true
      break
    fi
    if [[ "$status" == "unhealthy" ]]; then
      "$CONTAINER_CLI" logs --tail 80 "$container" || true
      die "$container reported unhealthy."
    fi
    sleep 2
  done
  [[ "$healthy" == true ]] || {
    "$CONTAINER_CLI" logs --tail 80 "$container" || true
    die "$container did not become healthy within 180 seconds."
  }
done

say "Building and updating only the Hummingbot API container."
compose -p spectra-alpha -f "$COMPOSE_FILE" up -d --build --no-deps hummingbot-api 9>&-

status="$("$CONTAINER_CLI" inspect --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null || true)"
[[ "$status" == "running" ]] || {
  "$CONTAINER_CLI" logs --tail 160 spectra-alpha-hummingbot-api 2>/dev/null || true
  die "API container is not running after the update."
}

say "SUCCESS: API image rebuilt and container is running."
say "Postgres and EMQX were started in place; no volumes or local credentials were deleted."

# Update exact reviewed Core UI, Python authority, route, and operator files.
# Rebuild the dashboard and restart Core only; paper execution containers stay up.
CORE_FRONTEND_SOURCE="frontend/src/components/market/OrderBook.tsx"
[[ -d "$CORE_DIR/.git" ]] || die "Core Git checkout not found at $CORE_DIR."
git -C "$CORE_DIR" fetch --depth 1 origin "$CORE_REF"
[[ -f "$CORE_DIR/frontend/package.json" ]] || die "Core frontend package.json is missing."
say "Fetching the reviewed Spectra trade-screen market-feed status component."
CORE_UPSTREAM_BLOB="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$CORE_FRONTEND_SOURCE")"
CORE_WORKTREE_BLOB="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$CORE_FRONTEND_SOURCE")"
CORE_OLD_BLOB="47e36a785b4e02a50ca8f34093727403cb692750"
CORE_NEW_BLOB="6ae7ba93d253f2127b47a301b3e33121a9f54895"
[[ "$CORE_UPSTREAM_BLOB" == "$CORE_NEW_BLOB" ]] \
  || die "Fetched trade-screen component is not the reviewed version."
if [[ "$CORE_WORKTREE_BLOB" != "$CORE_NEW_BLOB" && "$CORE_WORKTREE_BLOB" != "$CORE_OLD_BLOB" ]]; then
  die "Unrecognized local changes in $CORE_FRONTEND_SOURCE. They were preserved; review them before updating."
fi
git -C "$CORE_DIR" restore --source="$API_UPSTREAM_COMMIT" --worktree -- "$CORE_FRONTEND_SOURCE"

# Apply the reviewed Command Center shell polish only when the local files are
# either the exact previous Alpha versions or already match this release.
CORE_SHELL_SOURCES=("frontend/src/components/layout/AppShell.tsx" "frontend/src/index.css" "frontend/src/pages/SpectraOverview.tsx" "frontend/src/lib/api.ts")
declare -A CORE_SHELL_OLD_BLOBS=(
  ["frontend/src/components/layout/AppShell.tsx"]="05426a040450d516f9080628ba56bad71168e03a"
  ["frontend/src/index.css"]="451463a274340865086b505f1d63acbd830e55a5"
  ["frontend/src/pages/SpectraOverview.tsx"]="79f74823b59409293db89f2704f3678d2f1b5fb9"
  ["frontend/src/lib/api.ts"]="dfdc7867012c37de2e0c1fc0ea1ee8cffbbccf10"
)
declare -A CORE_SHELL_NEW_BLOBS=(
  ["frontend/src/components/layout/AppShell.tsx"]="776e421f5ab1312ab97a09756bc82df8e4da0fc5"
  ["frontend/src/index.css"]="6d79e7a5111d423be6bf260a65e238c0344a4f5e"
  ["frontend/src/pages/SpectraOverview.tsx"]="dac12adcbe9337309b82e5f58cf551dc35c0c094"
  ["frontend/src/lib/api.ts"]="16d5a5adbf3af1bd4d5d2244b5d3850bc3c92494"
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
git -C "$CORE_DIR" restore --source="$API_UPSTREAM_COMMIT" --worktree -- "${CORE_SHELL_SOURCES[@]}"
say "Updated the reviewed Command Center shell and Kraken discovery UI."

# Install the reviewed paper lifecycle operator, health monitor and user services.
CORE_OPS_SOURCES=(
  "scripts/spectra"
  "scripts/spectra-alpha-doctor.sh"
  "scripts/spectra-alpha-start-api.sh"
  "scripts/spectra-alpha-core-supervisor.sh"
  "scripts/spectra-alpha-backup.sh"
  "scripts/systemd/spectra-alpha-core.service"
  "scripts/systemd/spectra-alpha-doctor.service"
  "scripts/systemd/spectra-alpha-doctor.timer"
  "scripts/systemd/spectra-alpha-backup.service"
  "scripts/systemd/spectra-alpha-backup.timer"
)
declare -A CORE_OPS_NEW_BLOBS=(
  ["scripts/spectra"]="774ae8e7912896d7266fbb06c459b9f5a48a7dc3"
  ["scripts/spectra-alpha-doctor.sh"]="96ab3e9783b61d69c80cf6efea980157f5f1ed9a"
  ["scripts/spectra-alpha-start-api.sh"]="a778ea113f445ab6a70319e400ba16d0f0c33bb3"
  ["scripts/spectra-alpha-core-supervisor.sh"]="e729bab6182fa566cda6b1f3db56f84f589ae3d2"
  ["scripts/systemd/spectra-alpha-core.service"]="9386ab538af54f03129264a7d5134420e5a622bf"
  ["scripts/systemd/spectra-alpha-doctor.service"]="054b22ea89cb8888419ec8000b5ffa0975f1b73d"
  ["scripts/systemd/spectra-alpha-doctor.timer"]="30af1b179bda4aec03105018e09b07dba1ee4b85"
  ["scripts/spectra-alpha-backup.sh"]="1320e0d1ccf552917105c85f17fb67d046d8a3fc"
  ["scripts/systemd/spectra-alpha-backup.service"]="e49bc3d235744a92d7c463e6a98d9b8b282ae08b"
  ["scripts/systemd/spectra-alpha-backup.timer"]="698a284d5663fc58e850f6ec8f0b85983ab32a6f"
)
for source in "${CORE_OPS_SOURCES[@]}"; do
  upstream_blob="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$source")"
  expected_blob="${CORE_OPS_NEW_BLOBS[$source]}"
  if [[ "$upstream_blob" != "$expected_blob" ]]; then
    die "Fetched $source is not the reviewed Alpha operations version."
  fi
  if [[ -L "$CORE_DIR/$source" ]]; then
    die "Refusing to replace symlinked local path $source."
  elif [[ -f "$CORE_DIR/$source" ]]; then
    worktree_blob="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$source")"
    if [[ "$worktree_blob" != "$expected_blob" ]]; then
      die "Unrecognized local changes in $source. They were preserved; review them before updating."
    fi
  fi
done
# These are new reviewed files in this Alpha release and may not exist in the
# existing checkout's index yet. Read each from the verified fetched tree without
# staging or changing the local Git index.
for source in "${CORE_OPS_SOURCES[@]}"; do
  mkdir -p "$(dirname "$CORE_DIR/$source")"
  git -C "$CORE_DIR" show "FETCH_HEAD:$source" > "$CORE_DIR/$source"
done

# Update only the reviewed Python files that implement the paper asset authority
# and strategy-discovery path. Exact old/new blob checks preserve local edits.
CORE_PYTHON_SOURCES=(
  "spectra_core/asset_authority.py"
  "spectra_core/kraken_authorization.py"
  "spectra_core/market_discovery.py"
  "mcp_servers/hummingbot_api/tools/executor_create.py"
  "routines/mean_reversion_scanner.py"
  "condor/web/routes/market.py"
)
declare -A CORE_PYTHON_NEW_BLOBS=(
  ["spectra_core/asset_authority.py"]="5644134509e604dc5ed0df9076130608b61fe975"
  ["spectra_core/kraken_authorization.py"]="3e3d4e195422002f43818c936d9c88a740ff7c72"
  ["spectra_core/market_discovery.py"]="5222e752f46f56bc58e4acd8bdd80f940ae1ce36"
  ["mcp_servers/hummingbot_api/tools/executor_create.py"]="14b8d4a98cb1cd6ab1243f1a794b658204cb26fa"
  ["routines/mean_reversion_scanner.py"]="48282fd0a1b616b00c3715de4ee55a3c1c569fdf"
  ["condor/web/routes/market.py"]="bebac174109a7c91a33e5a0698201ffd2ac037b0"
)
declare -A CORE_PYTHON_OLD_BLOBS=(
  ["spectra_core/asset_authority.py"]=""
  ["spectra_core/kraken_authorization.py"]="f841e3689fc6821037b2a2d288a129138265b050"
  ["spectra_core/market_discovery.py"]="0254d08864eecf2f72e9371c4292f6eba9c17df6"
  ["mcp_servers/hummingbot_api/tools/executor_create.py"]="2c3fd6ab5e0372f0350eaafe4724e0f70a9c4c1a"
  ["routines/mean_reversion_scanner.py"]="218126472c0e1af749e62ef5fdc6c3db89fb9745"
  ["condor/web/routes/market.py"]="c40bf5ec4edc40e7f49005df1a8c3417c270f8a7"
)
for source in "${CORE_PYTHON_SOURCES[@]}"; do
  upstream_blob="$(git -C "$CORE_DIR" rev-parse "FETCH_HEAD:$source")"
  expected_blob="${CORE_PYTHON_NEW_BLOBS[$source]}"
  if [[ "$upstream_blob" != "$expected_blob" ]]; then
    die "Fetched Python source $source is not the reviewed Governor asset-authority version."
  fi
  if [[ -L "$CORE_DIR/$source" ]]; then
    die "Refusing to replace symlinked local Python source $source."
  elif [[ -f "$CORE_DIR/$source" ]]; then
    worktree_blob="$(git -C "$CORE_DIR" hash-object "$CORE_DIR/$source")"
    old_blob="${CORE_PYTHON_OLD_BLOBS[$source]}"
    if [[ "$worktree_blob" != "$expected_blob" && "$worktree_blob" != "$old_blob" ]]; then
      die "Unrecognized local changes in $source. They were preserved; review them before updating."
    fi
  elif [[ -n "${CORE_PYTHON_OLD_BLOBS[$source]}" ]]; then
    die "Expected existing Core source $source is missing; refusing an incomplete update."
  fi
done
for source in "${CORE_PYTHON_SOURCES[@]}"; do
  mkdir -p "$(dirname "$CORE_DIR/$source")"
  git -C "$CORE_DIR" show "FETCH_HEAD:$source" > "$CORE_DIR/$source"
done
"$CORE_DIR/.venv/bin/python" -m py_compile \
  "$CORE_DIR/spectra_core/asset_authority.py" \
  "$CORE_DIR/spectra_core/kraken_authorization.py" \
  "$CORE_DIR/spectra_core/market_discovery.py" \
  "$CORE_DIR/mcp_servers/hummingbot_api/tools/executor_create.py" \
  "$CORE_DIR/routines/mean_reversion_scanner.py" \
  "$CORE_DIR/condor/web/routes/market.py"
say "Installed and syntax-checked the reviewed paper asset authority and strategy-discovery code."

install -m 700 "$CORE_DIR/scripts/spectra" "$HOME/.local/bin/spectra"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-doctor.sh" "$HOME/.local/bin/spectra-alpha-doctor"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-start-api.sh" "$HOME/.local/bin/spectra-alpha-start-api"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-core-supervisor.sh" "$HOME/.local/bin/spectra-alpha-core-supervisor"
install -m 700 "$CORE_DIR/scripts/spectra-alpha-backup.sh" "$HOME/.local/bin/spectra-alpha-backup"

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
    "spectra-alpha-backup.service",
    "spectra-alpha-backup.timer",
):
    text = (source / name).read_text(encoding="utf-8")
    text = text.replace("@INSTALL_ROOT@", install_root).replace("@LOCAL_BIN@", local_bin)
    unit_path = target / name
    if unit_path.exists() and unit_path.read_text(encoding="utf-8") != text:
        raise SystemExit(f"Refusing to overwrite customized user unit {unit_path}; it was preserved.")
    unit_path.write_text(text, encoding="utf-8")
    unit_path.chmod(0o600)
PYSYSTEMD
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable spectra-alpha-core.service >/dev/null
  systemctl --user enable --now spectra-alpha-doctor.timer >/dev/null
  systemctl --user enable --now spectra-alpha-backup.timer >/dev/null
  say "Enabled Core restart-on-failure at user login and the two-minute read-only health monitor."
else
  say "No active user systemd manager; operator CLI remains available without changing OS services."
fi
say "Creating the initial consistent paper-state backup."
"$HOME/.local/bin/spectra-alpha-backup"

# Prefer installer-provisioned Node 22 on Bazzite. CI and hosts with
# an existing Node runtime should not be forced through an absent NVM install.
if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  . "$HOME/.nvm/nvm.sh"
  if nvm ls 22 2>/dev/null | grep -q 'v22\.'; then
    nvm use 22 >/dev/null
  fi
fi
command -v node >/dev/null 2>&1 || die "Node.js is required to rebuild the Spectra dashboard."
command -v npm >/dev/null 2>&1 || die "npm is required to rebuild the Spectra dashboard."
cd "$CORE_DIR/frontend"
if [[ ! -f node_modules/.package-lock.json || package-lock.json -nt node_modules/.package-lock.json ]]; then
  say "Synchronizing the existing frontend dependencies."
  npm ci
fi
say "Building the updated Spectra trade screen."
npm run build
cd "$INSTALL_ROOT"

say "Restarting Spectra Core only so reviewed Python changes are loaded; Hummingbot API containers remain running."
"$HOME/.local/bin/spectra" core-restart

# Install and run the authenticated read-only verifier as the final deployment
# step. It reports tracker and underlying public-connector symbol-map state.
VERIFY_BIN="$HOME/.local/bin/spectra-verify-alpha-paper-market-data"
VERIFY_URL="https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/$CORE_REF/scripts/verify-alpha-paper-market-data.sh"
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
