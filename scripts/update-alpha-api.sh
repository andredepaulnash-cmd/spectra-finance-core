#!/usr/bin/env bash
# Foreground one-command updater for Spectra Finance Alpha on Bazzite.
# It updates exact reviewed API, Core Python, dashboard, and operator files,
# preserves all state, and never removes containers, networks, or named volumes.
set -Eeuo pipefail

# The installer places user-local runtime helpers in ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
# After merge, the integration branch is the single reviewed release source.
CORE_REF="${SPECTRA_ALPHA_CORE_REF:-integration/condor-hummingbot-spectra-runtime-20261115}"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
LOG_DIR="$INSTALL_ROOT/logs"
LOG_FILE="$LOG_DIR/api-update.log"
LOCK_FILE="$LOG_DIR/api-update.lock"
COMPOSE_FILE="$API_DIR/docker-compose.alpha.yml"
API_SOURCES=("services/unified_connector_service.py" "services/market_data_service.py" "services/orders_recorder.py" "deps.py")

say() { printf '[SPECTRA API UPDATE] %s\n' "$*"; }
die() { say "STOP: $*" >&2; exit 1; }

mkdir -p "$LOG_DIR"

if [[ "$#" -ne 0 ]]; then
  die "This updater runs in the foreground and accepts no arguments."
fi

exec 9>"$LOCK_FILE"
flock -n 9 || die "Another Spectra API update is already running. Check $LOG_FILE."

RUNTIME_FILE="$INSTALL_ROOT/.container-runtime"
CONTAINER_RUNTIME=""
if [[ -f "$RUNTIME_FILE" ]]; then
  IFS= read -r CONTAINER_RUNTIME < "$RUNTIME_FILE"
fi

# Backward compatibility for pre-marker installations: identify which engine
# actually owns the existing named API stack. Never guess from installed binaries.
if [[ "$CONTAINER_RUNTIME" != "docker" && "$CONTAINER_RUNTIME" != "podman" ]]; then
  if command -v docker >/dev/null 2>&1 && docker inspect --type container --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null | grep -qx running; then
    CONTAINER_RUNTIME="docker"
  elif command -v podman >/dev/null 2>&1 && engine inspect --type container --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null | grep -qx running; then
    CONTAINER_RUNTIME="podman"
  else
    die "Container runtime is not recorded and no running Spectra API container identifies it. Rerun the installer to record the runtime safely."
  fi
  printf '%s\n' "$CONTAINER_RUNTIME" > "$RUNTIME_FILE"
  chmod 600 "$RUNTIME_FILE"
fi

command -v "$CONTAINER_RUNTIME" >/dev/null 2>&1 || die "Recorded container runtime '$CONTAINER_RUNTIME' is not installed."
engine() { "$CONTAINER_RUNTIME" "$@"; }
if [[ "$CONTAINER_RUNTIME" == "docker" ]]; then
  compose() { docker compose "$@"; }
else
  command -v uv >/dev/null 2>&1 || die "uv is required to run the rootless Podman Compose provider."
  compose() { uv tool run --from podman-compose podman-compose "$@"; }
fi
say "Using the installer's recorded container runtime: $CONTAINER_RUNTIME."
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
  ["services/orders_recorder.py"]="1c18d3768fb08654c4b51b16f8813013d8d9da78"
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
  ["services/unified_connector_service.py"]="5cb201c8b8c3a3abf958832a8f92107636850a37"
  ["services/market_data_service.py"]="95e8e31c5e9be5bb2945b80038419b88b35ba41f"
  ["services/orders_recorder.py"]="b0812c513c7a393cc825a732450c60a6617fe659"
  ["deps.py"]="254ee28a495ef2354b40b5f4118b854b28d1b67b"
)

for source in "${API_SOURCES[@]}"; do
  actual_blob="$(git rev-parse "FETCH_HEAD:${source}")"
  expected_blob="${EXPECTED_UPSTREAM_BLOBS[$source]}"
  [[ "$actual_blob" == "$expected_blob" ]] \
    || die "Fetched $source is not the reviewed API version (expected $expected_blob, got $actual_blob)."
done

git restore --source=FETCH_HEAD --worktree -- "${API_SOURCES[@]}"

# PaperTradeExchange is a Cython wrapper and does not expose _trading_pairs.
# Pin the exact API source before applying the reviewed, idempotent adapter.
PAPER_TRADE_SOURCE="services/trading_service.py"
PAPER_TRADE_EXPECTED_BLOB="975f3fe9907e47029c0f66904a01b8c741857416"
PAPER_TRADE_PATCHER="$(mktemp "$LOG_DIR/paper-trade-patcher.XXXXXX.py")"
PAPER_TRADE_PATCHER_URL="https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/$CORE_REF/scripts/patch_hummingbot_trading_service_paper_trade.py"
PAPER_TRADE_PATCHER_EXPECTED_BLOB="1f5a464e614e81cf64e5d4fe5e46b8752710a702"
curl -fsSL "$PAPER_TRADE_PATCHER_URL" -o "$PAPER_TRADE_PATCHER" \
  || die "Could not download the reviewed PaperTradeExchange compatibility patcher."
actual_patcher_blob="$(git hash-object "$PAPER_TRADE_PATCHER")"
[[ "$actual_patcher_blob" == "$PAPER_TRADE_PATCHER_EXPECTED_BLOB" ]] \
  || die "Downloaded PaperTradeExchange patcher is not the reviewed version (expected $PAPER_TRADE_PATCHER_EXPECTED_BLOB, got $actual_patcher_blob)."
python3 -m py_compile "$PAPER_TRADE_PATCHER" \
  || die "Downloaded PaperTradeExchange patcher failed Python syntax validation."
actual_paper_trade_blob="$(git rev-parse "FETCH_HEAD:$PAPER_TRADE_SOURCE")"
[[ "$actual_paper_trade_blob" == "$PAPER_TRADE_EXPECTED_BLOB" ]] \
  || die "Fetched $PAPER_TRADE_SOURCE is not the reviewed source version (expected $PAPER_TRADE_EXPECTED_BLOB, got $actual_paper_trade_blob)."
python3 "$PAPER_TRADE_PATCHER" "$API_DIR/$PAPER_TRADE_SOURCE"
rm -f "$PAPER_TRADE_PATCHER"
grep -q "SPECTRA_PAPER_TRADE_PAIR_MAP_COMPAT" "$API_DIR/$PAPER_TRADE_SOURCE" \
  || die "PaperTradeExchange pair-registration compatibility adapter was not applied."

grep -q "refusing registration" "${API_SOURCES[0]}" \
  || die "The fail-closed Kraken fix was not applied locally."
grep -q "A read request must be able to initialize its own feed" "${API_SOURCES[1]}" \
  || die "The lazy order-book initialization fix was not applied locally."
grep -q "Bot orchestration is disabled in Spectra paper-only mode" "$API_DIR/deps.py" \
  || die "The paper-only bot-status guard was not applied locally."
say "Updated only ${API_SOURCES[*]}; local credentials, configuration, and paper state were left untouched."

say "Checking existing Postgres and EMQX services before starting anything."
# Resolve running containers from Podman's structured inspect data. The
# human-readable Ports column is presentation-dependent and can omit/truncate
# mappings; relying on it caused the updater to miss the already-running EMQX.
find_running_container_for_port() {
  local port="$1" expected_name="$2" state ids id line name published
  # Inspect the deterministic Compose name directly. Podman name-filter regexes
  # differ across releases; the anchored /name/ filter missed live containers.
  state="$(engine inspect --type container --format '{{.State.Status}}' "$expected_name" 2>/dev/null || true)"
  if [[ "$state" == "running" ]]; then
    printf '%s\n' "$expected_name"
    return 0
  fi

  # Portable fallback: enumerate structured names/IDs and compare exact names.
  ids="$(engine ps --format '{{.Names}}|{{.ID}}' 2>/dev/null || true)"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    name="${line%%|*}"
    id="${line#*|}"
    if [[ "$name" == "$expected_name" ]]; then
      state="$(engine inspect --type container --format '{{.State.Status}}' "$id" 2>/dev/null || true)"
      if [[ "$state" == "running" ]]; then
        printf '%s\n' "$id"
        return 0
      fi
    fi
  done <<<"$ids"

  # Last fallback: inspect published port bindings as JSON, not human-readable
  # 'podman port' output whose formatting varies by Podman version.
  ids="$(engine ps -q 2>/dev/null || true)"
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    published="$(engine inspect --type container --format '{{json .NetworkSettings.Ports}}' "$id" 2>/dev/null || true)"
    if python3 - "$published" "$port" <<'PYPORT'
import json
import sys
try:
    bindings = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    raise SystemExit(1)
port = sys.argv[2]
for entries in bindings.values():
    for entry in entries or []:
        if entry.get("HostPort") == port and entry.get("HostIp") in ("127.0.0.1", "0.0.0.0", "::", "::1"):
            raise SystemExit(0)
raise SystemExit(1)
PYPORT
    then
      printf '%s\n' "$id"
      return 0
    fi
  done <<<"$ids"

  # Fail closed if a host listener exists but no Podman container was identified.
  if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .; then
    say "Podman running containers during failed lookup: ${ids:-<none>}" >&2
    die "Host port $port is already listening, but no running Podman container could be identified. Refusing to start a duplicate; inspect the existing service."
  fi
}
declare -A SUPPORT_IDS=()
services_to_start=()
for entry in "postgres:5432:spectra-alpha-hummingbot-postgres" "emqx:1883:spectra-alpha-hummingbot-broker"; do
  service="${entry%%:*}"
  remainder="${entry#*:}"
  port="${remainder%%:*}"
  expected_name="${remainder#*:}"
  container_id="$(find_running_container_for_port "$port" "$expected_name")"
  if [[ -n "$container_id" ]]; then
    SUPPORT_IDS["$service"]="$container_id"
    say "Found existing $service service on port $port ($container_id); leaving it untouched."
  else
    say "No running $service service found on port $port; it will be started."
    services_to_start+=("$service")
  fi
done
if (("${#services_to_start[@]}")); then
  compose -p spectra-alpha -f "$COMPOSE_FILE" up -d "${services_to_start[@]}" 9>&-
else
  say "Both supporting services are already running; no duplicate start requested."
fi

for entry in "postgres:5432:spectra-alpha-hummingbot-postgres" "emqx:1883:spectra-alpha-hummingbot-broker"; do
  service="${entry%%:*}"
  remainder="${entry#*:}"
  port="${remainder%%:*}"
  expected_name="${remainder#*:}"
  container_id="${SUPPORT_IDS[$service]:-}"
  if [[ -z "$container_id" ]]; then
    container_id="$(find_running_container_for_port "$port" "$expected_name")"
  fi
  [[ -n "$container_id" ]] || container_id="$expected_name"
  say "Waiting for $service health ($container_id)."
  healthy=false
  for _ in $(seq 1 90); do
    status="$(engine inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      healthy=true
      break
    fi
    if [[ "$status" == "unhealthy" ]]; then
      engine logs --tail 80 "$container_id" || true
      die "$service reported unhealthy."
    fi
    sleep 2
  done
  [[ "$healthy" == true ]] || {
    engine logs --tail 80 "$container_id" || true
    die "$service did not become healthy within 180 seconds."
  }
done

say "Building and updating only the Hummingbot API container."
compose -p spectra-alpha -f "$COMPOSE_FILE" up -d --build hummingbot-api 9>&-

status="$(engine inspect --format '{{.State.Status}}' spectra-alpha-hummingbot-api 2>/dev/null || true)"
[[ "$status" == "running" ]] || {
  engine logs --tail 160 spectra-alpha-hummingbot-api 2>/dev/null || true
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
git -C "$CORE_DIR" restore --source=FETCH_HEAD --worktree -- "$CORE_FRONTEND_SOURCE"

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
  ["frontend/src/pages/SpectraOverview.tsx"]="31a9fe01decc24a7067da27e575552186dc98988"
  ["frontend/src/lib/api.ts"]="93d2e82aa4f1e9905f95f66c52842a53f81ca0af"
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
  ["scripts/spectra"]="f716e1b6abccc775270fb9c5480d57091e2c202e"
  ["scripts/spectra-alpha-doctor.sh"]="13185d2fe40175f2541288e27b6ecb816dcc26e7"
  ["scripts/spectra-alpha-start-api.sh"]="a778ea113f445ab6a70319e400ba16d0f0c33bb3"
  ["scripts/spectra-alpha-core-supervisor.sh"]="e729bab6182fa566cda6b1f3db56f84f589ae3d2"
  ["scripts/systemd/spectra-alpha-core.service"]="9386ab538af54f03129264a7d5134420e5a622bf"
  ["scripts/systemd/spectra-alpha-doctor.service"]="054b22ea89cb8888419ec8000b5ffa0975f1b73d"
  ["scripts/systemd/spectra-alpha-doctor.timer"]="30af1b179bda4aec03105018e09b07dba1ee4b85"
  ["scripts/spectra-alpha-backup.sh"]="1320e0d1ccf552917105c85f17fb67d046d8a3fc"
  ["scripts/systemd/spectra-alpha-backup.service"]="e49bc3d235744a92d7c463e6a98d9b8b282ae08b"
  ["scripts/systemd/spectra-alpha-backup.timer"]="698a284d5663fc58e850f6ec8f0b85983ab32a6f"
)
# Exact previously shipped Alpha launcher version; safe to replace with the
# reviewed current launcher during this update.
declare -A CORE_OPS_OLD_BLOBS=(
  ["scripts/spectra"]="774ae8e7912896d7266fbb06c459b9f5a48a7dc3"
  ["scripts/spectra-alpha-doctor.sh"]="96ab3e9783b61d69c80cf6efea980157f5f1ed9a"
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
    previous_blob="${CORE_OPS_OLD_BLOBS[$source]:-}"
    if [[ "$worktree_blob" != "$expected_blob" && "$worktree_blob" != "$previous_blob" ]]; then
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
  "spectra_core/portfolio_supervisor.py"
  "mcp_servers/hummingbot_api/tools/executor_create.py"
  "mcp_servers/hummingbot_api/tools/runtime_scheduler.py"
  "spectra_core/execution_policy.py"
  "spectra_core/capital_governor.py"
  "spectra_core/economic_position.py"
  "routines/mean_reversion_scanner.py"
  "condor/web/routes/market.py"
)
declare -A CORE_PYTHON_NEW_BLOBS=(
  ["spectra_core/asset_authority.py"]="26b65d413e9dffde2464e20945b3e7755c6491e6"
  ["spectra_core/kraken_authorization.py"]="199bd1d7c4ef0940e8531c9f020f21c7a2ba43e6"
  ["spectra_core/market_discovery.py"]="43191679ac7027354406d32ec312a5f3dadb433e"
  ["spectra_core/portfolio_supervisor.py"]="148cb2516cf029f84d5e8d82b4ea623891323149"
  ["mcp_servers/hummingbot_api/tools/executor_create.py"]="274c68d8dd7a6e7419f018bed21305c89eaaaf0e"
  ["mcp_servers/hummingbot_api/tools/runtime_scheduler.py"]="3481b486fc820eef7fff9381212dfa8e72627efd"
  ["spectra_core/execution_policy.py"]="1e37a51dea7a0634a5c1a2e89dd5c9ff08c44592"
  ["spectra_core/capital_governor.py"]="feaf6eeb85afc4f8c43e20fe6f34ffa4540389ac"
  ["spectra_core/economic_position.py"]="e7fbbdf56cead43c186fa8be923795442fa0e289"
  ["routines/mean_reversion_scanner.py"]="48282fd0a1b616b00c3715de4ee55a3c1c569fdf"
  ["condor/web/routes/market.py"]="bebac174109a7c91a33e5a0698201ffd2ac037b0"
)
declare -A CORE_PYTHON_OLD_BLOBS=(
  ["spectra_core/asset_authority.py"]=""
  ["spectra_core/kraken_authorization.py"]="3e3d4e195422002f43818c936d9c88a740ff7c72"
  ["spectra_core/market_discovery.py"]="5222e752f46f56bc58e4acd8bdd80f940ae1ce36"
  ["spectra_core/portfolio_supervisor.py"]="1200fa1698bf3dcb58a9c8ae9b315babb7cfedab"
  ["mcp_servers/hummingbot_api/tools/executor_create.py"]="14b8d4a98cb1cd6ab1243f1a794b658204cb26fa"
  ["mcp_servers/hummingbot_api/tools/runtime_scheduler.py"]="1709bebcbefc326c19c9d403857cff937144f900"
  ["spectra_core/execution_policy.py"]="403f0e7d229d36231a1fdd88f21eb809aacae2e8"
  ["spectra_core/capital_governor.py"]="d100fc52c8bab80aebf49e1b7a51da21d4d1750e"
  ["spectra_core/economic_position.py"]="3d63e76d57081c5a9b232d3966b7ef20f975ca8a"
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
    # Recognize both the immediately previous reviewed integration blob and
    # the older Alpha-installed MCP source; never overwrite unknown edits.
    if [[ "$source" == "mcp_servers/hummingbot_api/tools/executor_create.py" \
      && "$worktree_blob" == "2c3fd6ab5e0372f0350eaafe4724e0f70a9c4c1a" ]]; then
      old_blob="$worktree_blob"
    fi
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
  "$CORE_DIR/spectra_core/portfolio_supervisor.py" \
  "$CORE_DIR/mcp_servers/hummingbot_api/tools/executor_create.py" \
  "$CORE_DIR/mcp_servers/hummingbot_api/tools/runtime_scheduler.py" \
  "$CORE_DIR/spectra_core/execution_policy.py" \
  "$CORE_DIR/spectra_core/capital_governor.py" \
  "$CORE_DIR/spectra_core/economic_position.py" \
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

if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  . "$HOME/.nvm/nvm.sh"
  current_node="$(node --version 2>/dev/null || true)"
  if [[ "$current_node" =~ ^v22\. ]]; then
    : # CI/setup-node or the user's PATH already provides the reviewed Node 22 runtime.
  elif nvm ls 22 >/dev/null 2>&1; then
    nvm use 22 >/dev/null
  else
    nvm install 22 >/dev/null
    nvm use 22 >/dev/null
  fi
fi
command -v npm >/dev/null 2>&1 || die "npm is required to rebuild the Spectra dashboard."
node_major="$(node --version 2>/dev/null || true)"
[[ "$node_major" =~ ^v22\. ]] || die "Node.js 22 is required for the Spectra dashboard build; found ${node_major:-unavailable}."
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
