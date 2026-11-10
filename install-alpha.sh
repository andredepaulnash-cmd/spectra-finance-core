#!/usr/bin/env bash
# Spectra Finance Alpha installer — Linux / Bazzite
# Installs the pinned Spectra Core + Hummingbot API paper-only candidate.
set -Eeuo pipefail

CORE_REPO="https://github.com/andredepaulnash-cmd/spectra-finance-core.git"
CORE_REF="alpha/one-command-paper-demo-20261108"
API_REPO="https://github.com/andredepaulnash-cmd/spectra-hummingbot-api.git"
API_REF="main"
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
export PATH="$LOCAL_BIN:$HOME/.cargo/bin:$PATH"

say() { printf '\n\033[1;36m[SPECTRA ALPHA]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[STOP]\033[0m %s\n' "$*" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }
set_env_value() {
  local file="$1" key="$2" value="$3"
  "$CORE_DIR/.venv/bin/python" - "$file" "$key" "$value" <<'PYENV'
from pathlib import Path
import sys
path, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
replacement = f"{key}={value}"
found = False
updated = []
for line in lines:
    if line.startswith(key + "="):
        if not found:
            updated.append(replacement)
            found = True
    else:
        updated.append(line)
if not found:
    updated.append(replacement)
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PYENV
}

[[ "$(uname -s)" == Linux ]] || die "This alpha installer currently supports Linux (including Bazzite)."
[[ "$(uname -m)" =~ ^(x86_64|aarch64|arm64)$ ]] || die "Unsupported CPU architecture: $(uname -m)"
mkdir -p "$INSTALL_ROOT"

clone_or_reuse() {
  local repo="$1" ref="$2" dest="$3"
  if [[ -d "$dest/.git" ]]; then
    local remote
    remote="$(git -C "$dest" remote get-url origin)"
    [[ "$remote" == *"$(basename "$repo" .git)"* ]] || die "Existing directory $dest is a different Git repository."
    local dirty account_cfg account_cfg_backup backup_dir patch_file
    account_cfg="bots/credentials/master_account/conf_client.yml"
    account_cfg_backup=""
    backup_dir="$INSTALL_ROOT/backups/installer-overwrite-$(date -u +%Y%m%dT%H%M%SZ)-$(basename "$dest")"
    if [[ -f "$dest/$account_cfg" ]]; then
      account_cfg_backup="$(mktemp)"
      cp "$dest/$account_cfg" "$account_cfg_backup"
    fi
    dirty="$(git -C "$dest" status --porcelain --untracked-files=all | grep -v -E 'docker-compose[.]alpha[.]yml|Dockerfile[.]alpha|bots/credentials/master_account/conf_client[.]yml' || true)"
    git -C "$dest" fetch --depth 1 origin "$ref"
    # Replace tracked application code, but never run git clean or delete local data.
    # Keep a recoverable patch of tracked local edits outside the checkout.
    if [[ -n "$dirty" ]]; then
      mkdir -p "$backup_dir"
      chmod 700 "$backup_dir"
      patch_file="$backup_dir/tracked-local-changes.patch"
      git -C "$dest" diff --binary HEAD > "$patch_file"
      chmod 600 "$patch_file"
      printf '%s\\n' "$dirty" > "$backup_dir/local-change-manifest.txt"
      chmod 600 "$backup_dir/local-change-manifest.txt"
      say "Preserved tracked local edits for $dest at $patch_file"
    fi
    # Refuse only if an untracked, non-ignored user file would be displaced by
    # a path newly tracked in the incoming release. All other untracked files,
    # ignored config, credentials, and data remain in place.
    python3 - "$dest" <<'PYSAFE'
import subprocess, sys
root = sys.argv[1]
def git(*args):
    return subprocess.check_output(["git", "-C", root, *args], text=True).splitlines()
untracked = git("ls-files", "--others", "--exclude-standard")
incoming = git("ls-tree", "-r", "--name-only", "FETCH_HEAD")
conflicts = []
for local in untracked:
    for remote in incoming:
        if remote == local or remote.startswith(local.rstrip("/") + "/") or local.startswith(remote.rstrip("/") + "/"):
            conflicts.append(local)
            break
if conflicts:
    print("Untracked local paths would be displaced by the incoming release:", file=sys.stderr)
    print("\n".join(conflicts), file=sys.stderr)
    raise SystemExit(2)
PYSAFE
    git -C "$dest" reset --hard FETCH_HEAD
    if [[ -n "$account_cfg_backup" ]]; then
      mkdir -p "$dest/$(dirname "$account_cfg")"
      cp "$account_cfg_backup" "$dest/$account_cfg"
      rm -f "$account_cfg_backup"
    fi
  elif [[ -e "$dest" ]]; then
    die "$dest exists but is not a Git checkout. Move it aside or choose another SPECTRA_ALPHA_HOME."
  else
    git clone --depth 1 --single-branch --branch "$ref" "$repo" "$dest"
  fi
}

# Bazzite is immutable by design. Do not install host RPMs into an rpm-ostree image.
if ! has git || ! has curl; then
  if [[ -e /run/ostree-booted ]]; then
    die "Bazzite/Atomic host detected, but git or curl is missing. Install those with the host's supported user/toolbox workflow, then rerun. No OS image changes were attempted."
  elif has apt-get; then
    sudo apt-get update
    sudo apt-get install -y git curl
  elif has dnf; then
    sudo dnf install -y git curl
  elif has pacman; then
    sudo pacman -Sy --noconfirm git curl
  elif has zypper; then
    sudo zypper --non-interactive install git curl
  else
    die "Please install git and curl, then rerun."
  fi
fi

# uv is installed in the user's home directory, never into the OS Python.
if ! has uv; then
  say "Installing uv into $LOCAL_BIN"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$LOCAL_BIN" UV_NO_MODIFY_PATH=1 sh
  export PATH="$LOCAL_BIN:$PATH"
fi
has uv || die "uv installation did not produce $LOCAL_BIN/uv"

# Node is only needed to build the bundled React dashboard.
if ! has node || ! has npm; then
  say "Installing Node.js 22 in your user account with nvm"
  export NVM_DIR="$HOME/.nvm"
  if [[ ! -s "$NVM_DIR/nvm.sh" ]]; then
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
  fi
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh"
  nvm install 22
  nvm use 22
  export PATH="$NVM_DIR/versions/node/$(nvm version 22)/bin:$PATH"
fi
has node && has npm || die "Node.js/npm are required to build the Spectra dashboard."

# Prefer Podman on Bazzite/Atomic hosts; Docker Compose is also supported.
COMPOSE_KIND=""
if has docker && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  COMPOSE_KIND="docker"
elif has podman; then
  podman info >/dev/null 2>&1 || die "Podman is installed but unavailable to this user. Enable the user's rootless Podman environment and rerun."
  if ! has podman-compose; then
    say "Installing the user-local Podman Compose provider"
    uv tool install podman-compose
    export PATH="$LOCAL_BIN:$PATH"
  fi
  has podman-compose || die "Could not install podman-compose."
  COMPOSE_KIND="podman"
else
  if [[ -e /run/ostree-booted ]]; then
    die "Bazzite requires Podman for this alpha. Podman was not found; no immutable OS changes were attempted."
  elif has apt-get; then
    sudo apt-get update && sudo apt-get install -y podman
  elif has dnf; then
    sudo dnf install -y podman
  elif has pacman; then
    sudo pacman -Sy --noconfirm podman
  elif has zypper; then
    sudo zypper --non-interactive install podman
  else
    die "Install Docker Engine + Compose or Podman, then rerun."
  fi
  has podman || die "Podman installation failed."
  uv tool install podman-compose
  COMPOSE_KIND="podman"
fi

say "Cloning the pinned Spectra Core alpha branch"
clone_or_reuse "$CORE_REPO" "$CORE_REF" "$CORE_DIR"

say "Cloning the matching public Hummingbot API branch"
clone_or_reuse "$API_REPO" "$API_REF" "$API_DIR"

say "Installing the reviewed Python 3.12 runtime"
uv python install 3.12
cd "$CORE_DIR"
uv sync --locked --dev --python 3.12
"$CORE_DIR/.venv/bin/python" "$CORE_DIR/scripts/configure_alpha_paper_state.py" --core-root "$CORE_DIR"

say "Building the Spectra dashboard"
cd "$CORE_DIR/frontend"
npm ci
npm run build

say "Creating local-only API credentials and paper-only configuration"
cd "$API_DIR"
mkdir -p bots/conf bots/data bots/credentials/master_account/connectors .emqx
env_value() {
  "$CORE_DIR/.venv/bin/python" - "$1" .env <<'PYVALUE'
from pathlib import Path
import sys
key, path = sys.argv[1], Path(sys.argv[2])
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith(key + "="):
        print(line.split("=", 1)[1])
        break
PYVALUE
}
if [[ -f .env ]]; then
  API_USER="$(env_value USERNAME)"
  API_PASS="$(env_value PASSWORD)"
  CONFIG_PASS="$(env_value CONFIG_PASSWORD)"
  BROKER_USER="$(env_value BROKER_USERNAME)"
  BROKER_PASS="$(env_value BROKER_PASSWORD)"
  BROKER_DASH_PASS="$(env_value BROKER_DASHBOARD_PASSWORD)"
  [[ -n "$API_USER" && -n "$API_PASS" && -n "$CONFIG_PASS" && -n "$BROKER_USER" && -n "$BROKER_PASS" && -n "$BROKER_DASH_PASS" ]] || die "Existing API .env is incomplete; refusing to replace credentials."
else
  API_USER="spectra_alpha"
  API_PASS="$(openssl rand -hex 24 2>/dev/null || $CORE_DIR/.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))')"
  CONFIG_PASS="$(openssl rand -hex 32 2>/dev/null || $CORE_DIR/.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  BROKER_USER="spectra_$($CORE_DIR/.venv/bin/python -c 'import secrets; print(secrets.token_hex(5))')"
  BROKER_PASS="$(openssl rand -hex 24 2>/dev/null || $CORE_DIR/.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))')"
  BROKER_DASH_PASS="$(openssl rand -hex 24 2>/dev/null || $CORE_DIR/.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))')"
  cat > .env <<EOF
API_BIND=127.0.0.1
DB_BIND=127.0.0.1
USERNAME=$API_USER
PASSWORD=$API_PASS
CONFIG_PASSWORD=$CONFIG_PASS
BROKER_USERNAME=$BROKER_USER
BROKER_PASSWORD=$BROKER_PASS
BROKER_DASHBOARD_PASSWORD=$BROKER_DASH_PASS
SPECTRA_PAPER_ONLY=true
SPECTRA_ALLOW_LIVE_EXECUTION=false
SPECTRA_REQUIRE_CORE_RECEIPTS=true
SPECTRA_DISPATCH_POLICY_VERSION=spectra-policy-v1
EOF
  chmod 600 .env
fi
set_env_value .env API_BIND 127.0.0.1
set_env_value .env DB_BIND 127.0.0.1
set_env_value .env SPECTRA_PAPER_ONLY true
set_env_value .env SPECTRA_PAPER_CONNECTORS kraken_paper_trade
set_env_value .env SPECTRA_ALLOW_LIVE_EXECUTION false
set_env_value .env SPECTRA_REQUIRE_CORE_RECEIPTS true
set_env_value .env SPECTRA_DISPATCH_POLICY_VERSION spectra-policy-v1
chmod 600 .env
HUMMINGBOT_MCP_DIR="$INSTALL_ROOT/hummingbot-mcp"
mkdir -p "$HUMMINGBOT_MCP_DIR"
cat > "$HUMMINGBOT_MCP_DIR/server.yml" <<EOF
name: local
url: http://127.0.0.1:8000
username: "$API_USER"
password: "$API_PASS"
EOF
chmod 600 "$HUMMINGBOT_MCP_DIR/server.yml"
chmod 700 . bots
printf 'user_id,password,is_superuser\n%s,%s,false\n' "$BROKER_USER" "$BROKER_PASS" > .emqx/auth-bootstrap.csv
chmod 700 .emqx
chmod 644 .emqx/auth-bootstrap.csv

# Update the repository's existing master-account config without discarding its other settings.
"$CORE_DIR/.venv/bin/python" "$CORE_DIR/scripts/configure_alpha_paper_connector.py" \
  --config "$API_DIR/bots/credentials/master_account/conf_client.yml"

# This alpha must not hand the API container the host Docker/Podman control socket.
# Build a private Compose file with a deterministic Python transform; no sed program
# is used here, avoiding the broken multi-expression sed invocation in prior installs.
"$CORE_DIR/.venv/bin/python" - "$API_DIR/docker-compose.yml" "$API_DIR/docker-compose.alpha.yml" <<'PYCOMPOSE'
from pathlib import Path
import sys

source, target = map(Path, sys.argv[1:3])
text = source.read_text(encoding="utf-8")
if "/var/run/docker.sock:/var/run/docker.sock" not in text:
    raise SystemExit("Expected Docker socket mount was not found; refusing unsafe Compose generation.")

lines = text.splitlines()
out = []
in_postgres = False
skip_extra_hosts = False
skip_postgres_ports = False
for line in lines:
    if line == "  postgres:":
        in_postgres = True
    if skip_extra_hosts:
        if line.startswith("    ") and not line.startswith("      "):
            skip_extra_hosts = False
        else:
            continue
    if skip_postgres_ports:
        if line.startswith("    ") and not line.startswith("      "):
            skip_postgres_ports = False
        else:
            continue
    if line == "    extra_hosts:":
        skip_extra_hosts = True
        continue
    if in_postgres and line == "    ports:":
        skip_postgres_ports = True
        continue
    if "/var/run/docker.sock:/var/run/docker.sock" in line:
        continue
    if "host.docker.internal" in line or "host-gateway" in line:
        continue
    out.append(line)

text = "\n".join(out) + "\n"
replacements = {
    "./bots:/hummingbot-api/bots": "./bots:/hummingbot-api/bots:Z",
    "./.emqx/auth-bootstrap.csv:/opt/emqx/etc/auth-bootstrap.csv:ro": "./.emqx/auth-bootstrap.csv:/opt/emqx/etc/auth-bootstrap.csv:ro,Z",
    "./emqx/acl.conf:/opt/emqx/etc/acl.conf:ro": "./emqx/acl.conf:/opt/emqx/etc/acl.conf:ro,Z",
    "container_name: hummingbot-api": "container_name: spectra-alpha-hummingbot-api",
    "container_name: hummingbot-broker": "container_name: spectra-alpha-hummingbot-broker",
    "container_name: hummingbot-postgres": "container_name: spectra-alpha-hummingbot-postgres",
    "image: emqx/emqx:5": "image: docker.io/emqx/emqx:5.10.5",
    "image: docker.io/emqx/emqx:5": "image: docker.io/emqx/emqx:5.10.5",
    "image: postgres:": "image: docker.io/library/postgres:",
    "image: continuumio/miniconda3:": "image: docker.io/continuumio/miniconda3:",
    "dockerfile: Dockerfile": "dockerfile: Dockerfile.alpha",
}
for old, new in replacements.items():
    text = text.replace(old, new)
if any(token in text for token in ("docker.sock", "host.docker.internal", "host-gateway")):
    raise SystemExit("Generated Compose file still contains a host socket/gateway dependency.")
if "  postgres:\n" not in text or "    ports:\n" in text[text.index("  postgres:\n"):text.index("\nnetworks:\n")]:
    raise SystemExit("Postgres host port was not removed from the alpha Compose file.")
target.write_text(text, encoding="utf-8")
PYCOMPOSE
say "Generating the Core signing key and API verification key locally"
cd "$CORE_DIR"
# Complete Core's local-mode configuration and pre-register the local API.
if [[ ! -f config.yml ]]; then
CORE_API_USER="$API_USER" CORE_API_PASS="$API_PASS" uv run --python 3.12 python - <<'PY'
import os, secrets, time, yaml
from pathlib import Path

cfg = {
    "servers": {
        "Spectra Kraken Paper": {
            "host": "127.0.0.1",
            "port": 8000,
            "username": os.environ["CORE_API_USER"],
            "password": os.environ["CORE_API_PASS"],
        }
    },
    "default_server": "Spectra Kraken Paper",
    "admin_id": 1,
    "users": {
        1: {"user_id": 1, "role": "admin", "created_at": time.time(),
            "notes": "Spectra Alpha local administrator"}
    },
    "server_access": {
        "Spectra Kraken Paper": {
            "owner_id": 1, "created_at": time.time(), "shared_with": {}
        }
    },
    "chat_defaults": {},
    "user_preferences": {},
    "web_jwt_secret": secrets.token_urlsafe(32),
    "telemetry": {},
    "sharing": {},
    "version": 1,
}
Path("config.yml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
Path("config.yml").chmod(0o600)
PY

fi
if [[ ! -f .env ]]; then
  cat > .env <<'EOF'
CONDOR_MODE=local
ADMIN_USER_ID=1
WEB_URL=http://127.0.0.1:8088
WEB_HOST=127.0.0.1
CONDOR_TELEMETRY=off
SPECTRA_PAPER_ONLY=true
SPECTRA_ALLOW_LIVE_EXECUTION=false
SPECTRA_DISPATCH_POLICY_VERSION=spectra-policy-v1
CONDOR_AGENT_SLUG=spectra
EOF
fi
set_env_value .env CONDOR_MODE local
set_env_value .env ADMIN_USER_ID 1
set_env_value .env WEB_URL http://127.0.0.1:8088
set_env_value .env WEB_HOST 127.0.0.1
set_env_value .env CONDOR_TELEMETRY off
set_env_value .env CONDOR_AGENT_SLUG spectra
set_env_value .env HUMMINGBOT_MCP_CONFIG_DIR "$INSTALL_ROOT/hummingbot-mcp"
set_env_value .env SPECTRA_PAPER_ONLY true
set_env_value .env SPECTRA_ALLOW_LIVE_EXECUTION false
set_env_value .env SPECTRA_DISPATCH_POLICY_VERSION spectra-policy-v1
# Keep the existing key pair across installer reruns; generate it only when either side is missing.
if ! grep -q '^SPECTRA_DISPATCH_PRIVATE_KEY=' .env || ! grep -q '^SPECTRA_DISPATCH_PUBLIC_KEY=' "$API_DIR/.env"; then
  uv run --python 3.12 python scripts/generate_spectra_dispatch_keys.py \
    --core-env .env --api-env "$API_DIR/.env"
fi
chmod 600 .env config.yml

# Compose uses an isolated alpha Dockerfile so Podman never has to resolve
# Docker Hub short names in a non-interactive build, and tracked source stays clean.
cp "$API_DIR/Dockerfile" "$API_DIR/Dockerfile.alpha"
"$CORE_DIR/.venv/bin/python" - "$API_DIR/Dockerfile.alpha" <<'PYDOCKER'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace("FROM continuumio/miniconda3", "FROM docker.io/continuumio/miniconda3")
path.write_text(text, encoding="utf-8")
PYDOCKER
grep -Eq '^FROM[[:space:]]+docker[.]io/continuumio/miniconda3' "$API_DIR/Dockerfile.alpha" \
  || die "Could not normalize the Hummingbot API base image for the alpha build."

say "Building and starting the Hummingbot paper-only API stack ($COMPOSE_KIND)"
cd "$API_DIR"
if [[ "$COMPOSE_KIND" == docker ]]; then
  docker compose -p spectra-alpha -f docker-compose.alpha.yml up -d --build
else
  podman-compose -p spectra-alpha -f docker-compose.alpha.yml up -d --build
fi

say "Waiting for the local Hummingbot API"
for _ in $(seq 1 180); do
  if curl -fsS -u "$API_USER:$API_PASS" http://127.0.0.1:8000/openapi.json >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS -u "$API_USER:$API_PASS" http://127.0.0.1:8000/openapi.json >/dev/null \
  || { if [[ "$COMPOSE_KIND" == docker ]]; then docker compose -p spectra-alpha -f docker-compose.alpha.yml logs --tail=120 || true; else podman-compose -p spectra-alpha -f docker-compose.alpha.yml logs --tail=120 || true; fi; die "Hummingbot API did not become ready."; }

say "Waiting for Kraken paper connector initialization"
PAPER_RUNTIME_READY=false
for _ in $(seq 1 180); do
  if curl -fsS -u "$API_USER:$API_PASS" http://127.0.0.1:8000/spectra/runtime > "$INSTALL_ROOT/api-runtime.json" 2>/dev/null; then
    if "$CORE_DIR/.venv/bin/python" - "$INSTALL_ROOT/api-runtime.json" <<'PY'
import json
import sys
from pathlib import Path
runtime = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if any(name.endswith("/kraken_paper_trade") for name in runtime.get("paper_connectors", [])) else 1)
PY
    then
      PAPER_RUNTIME_READY=true
      break
    fi
  fi
  sleep 2
done
[[ "$PAPER_RUNTIME_READY" == true ]] \
  || { if [[ "$COMPOSE_KIND" == docker ]]; then docker compose -p spectra-alpha -f docker-compose.alpha.yml logs --tail=160 || true; else podman-compose -p spectra-alpha -f docker-compose.alpha.yml logs --tail=160 || true; fi; die "Kraken paper connector did not initialize. See the API logs above."; }
"$CORE_DIR/.venv/bin/python" - "$INSTALL_ROOT/api-runtime.json" <<'PY'
import json
import sys
from pathlib import Path
runtime = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert runtime.get("mode") == "PAPER_ONLY", runtime
assert runtime.get("paper_only") is True, runtime
assert runtime.get("paper_connectors_only") is True, runtime
assert runtime.get("live_dispatch_available") is False, runtime
assert any(name.endswith("/kraken_paper_trade") for name in runtime.get("paper_connectors", [])), runtime
print("Verified: Kraken paper connector initialized; live dispatch is unavailable.")
PY

say "Initializing Kraken BTC-USD public market data for the paper adapter"
MARKET_DATA_READY=false
if curl -fsS -u "$API_USER:$API_PASS" \
  "http://127.0.0.1:8000/connectors/kraken_paper_trade/trading-rules?trading_pairs=BTC-USD" \
  > "$INSTALL_ROOT/kraken-paper-rules.json" 2>"$INSTALL_ROOT/market-data-bootstrap.log"; then
  if "$CORE_DIR/.venv/bin/python" - "$INSTALL_ROOT/kraken-paper-rules.json" <<'PY'
import json
import sys
from pathlib import Path
rules = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert "BTC-USD" in rules and rules["BTC-USD"]["min_order_size"] > 0, rules
PY
  then
    PAIR_STATUS="$(curl -sS -u "$API_USER:$API_PASS" -H "Content-Type: application/json" \
      -d '{"connector_name":"kraken_paper_trade","trading_pair":"BTC-USD","account_name":"master_account","timeout":30}' \
      -o "$INSTALL_ROOT/kraken-paper-pair.json" -w '%{http_code}' \
      http://127.0.0.1:8000/market-data/trading-pair/add || true)"
    if [[ "$PAIR_STATUS" == 200 ]] && "$CORE_DIR/.venv/bin/python" - "$INSTALL_ROOT/kraken-paper-pair.json" <<'PY'
import json
import sys
from pathlib import Path
result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result.get("success") is True, result
PY
    then
      BOOK_STATUS="$(curl -sS -u "$API_USER:$API_PASS" -H "Content-Type: application/json" \
        -d '{"connector_name":"kraken_paper_trade","trading_pair":"BTC-USD","depth":5}' \
        -o "$INSTALL_ROOT/kraken-paper-book.json" -w '%{http_code}' \
        http://127.0.0.1:8000/market-data/order-book || true)"
      if [[ "$BOOK_STATUS" == 200 ]] && "$CORE_DIR/.venv/bin/python" - "$INSTALL_ROOT/kraken-paper-book.json" <<'PY'
import json
import sys
from pathlib import Path
book = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert book.get("trading_pair") == "BTC-USD", book
assert book.get("bids") and book.get("asks"), book
PY
      then
        PRICE_STATUS="$(curl -sS -u "$API_USER:$API_PASS" -H "Content-Type: application/json" \
          -d '{"connector_name":"kraken_paper_trade","trading_pairs":["BTC-USD"]}' \
          -o "$INSTALL_ROOT/kraken-paper-prices.json" -w '%{http_code}' \
          http://127.0.0.1:8000/market-data/prices || true)"
        if [[ "$PRICE_STATUS" == 200 ]] && "$CORE_DIR/.venv/bin/python" - "$INSTALL_ROOT/kraken-paper-prices.json" <<'PY'
import json
import sys
from pathlib import Path
result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result.get("connector") == "kraken_paper_trade", result
assert result.get("prices", {}).get("BTC-USD", 0) > 0, result
PY
        then
          MARKET_DATA_READY=true
          say "Verified: public BTC-USD order book and live public ticker are available to the paper adapter."
        else
          printf 'Public-price bootstrap failed (HTTP %s).\n' "$PRICE_STATUS" >> "$INSTALL_ROOT/market-data-bootstrap.log"
          cat "$INSTALL_ROOT/kraken-paper-prices.json" >> "$INSTALL_ROOT/market-data-bootstrap.log" 2>/dev/null || true
        fi
      else
        printf 'Order-book bootstrap failed (HTTP %s).\n' "$BOOK_STATUS" >> "$INSTALL_ROOT/market-data-bootstrap.log"
        cat "$INSTALL_ROOT/kraken-paper-book.json" >> "$INSTALL_ROOT/market-data-bootstrap.log" 2>/dev/null || true
      fi
    else
      printf 'Paper trading-pair initialization failed (HTTP %s).\n' "$PAIR_STATUS" >> "$INSTALL_ROOT/market-data-bootstrap.log"
      cat "$INSTALL_ROOT/kraken-paper-pair.json" >> "$INSTALL_ROOT/market-data-bootstrap.log" 2>/dev/null || true
    fi
  else
    printf 'Kraken BTC-USD trading rules did not pass validation.\n' >> "$INSTALL_ROOT/market-data-bootstrap.log"
  fi
else
  printf 'Kraken public trading rules could not be fetched.\n' >> "$INSTALL_ROOT/market-data-bootstrap.log"
fi
if [[ "$MARKET_DATA_READY" != true ]]; then
  say "WARNING: Kraken public market data did not initialize in this attempt."
  say "The dashboard will still launch in paper-only mode; market data will show as unavailable."
  if [[ "$COMPOSE_KIND" == docker ]]; then
    docker compose -p spectra-alpha -f "$API_DIR/docker-compose.alpha.yml" logs --tail=100 >> "$INSTALL_ROOT/market-data-bootstrap.log" 2>&1 || true
  else
    podman-compose -p spectra-alpha -f "$API_DIR/docker-compose.alpha.yml" logs --tail=100 >> "$INSTALL_ROOT/market-data-bootstrap.log" 2>&1 || true
  fi
fi
rm -f "$INSTALL_ROOT/kraken-paper-rules.json" "$INSTALL_ROOT/kraken-paper-pair.json" "$INSTALL_ROOT/kraken-paper-book.json" "$INSTALL_ROOT/kraken-paper-prices.json"

cat > "$INSTALL_ROOT/.compose-kind" <<EOF
$COMPOSE_KIND
EOF

say "Starting Spectra Intelligence and the dashboard"
if [[ -f "$INSTALL_ROOT/condor.pid" ]] && kill -0 "$(cat "$INSTALL_ROOT/condor.pid")" 2>/dev/null; then
  say "Spectra Intelligence is already running"
else
  cd "$CORE_DIR"
  nohup "$CORE_DIR/.venv/bin/python" "$CORE_DIR/main.py" > "$INSTALL_ROOT/condor.log" 2>&1 &
  echo $! > "$INSTALL_ROOT/condor.pid"
fi
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8088/ >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:8088/ >/dev/null \
  || { tail -80 "$INSTALL_ROOT/condor.log" 2>/dev/null || true; die "Spectra Intelligence did not bring up the dashboard. Its startup log is $INSTALL_ROOT/condor.log"; }
cat > "$LOCAL_BIN/spectra" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
COMPOSE_KIND="$(cat "$INSTALL_ROOT/.compose-kind")"
start_core() {
  if [[ -f "$INSTALL_ROOT/condor.pid" ]] && kill -0 "$(cat "$INSTALL_ROOT/condor.pid")" 2>/dev/null; then
    echo "Spectra Intelligence is already running."
    return 0
  fi
  cd "$CORE_DIR"
  nohup "$CORE_DIR/.venv/bin/python" "$CORE_DIR/main.py" > "$INSTALL_ROOT/condor.log" 2>&1 &
  echo $! > "$INSTALL_ROOT/condor.pid"
  for _ in $(seq 1 60); do
    curl -fsS http://127.0.0.1:8088/ >/dev/null 2>&1 && return 0
    sleep 2
  done
  tail -80 "$INSTALL_ROOT/condor.log" || true
  return 1
}
stop_core() {
  if [[ -f "$INSTALL_ROOT/condor.pid" ]]; then
    pid="$(cat "$INSTALL_ROOT/condor.pid")"
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid"
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
    fi
    rm -f "$INSTALL_ROOT/condor.pid"
  fi
}
compose() {
  cd "$API_DIR"
  if [[ "$COMPOSE_KIND" == docker ]]; then docker compose -p spectra-alpha -f docker-compose.alpha.yml "$@"; else podman-compose -p spectra-alpha -f docker-compose.alpha.yml "$@"; fi
}
case "${1:-status}" in
  start)
    compose up -d
    api_user="$("$CORE_DIR/.venv/bin/python" -c 'from pathlib import Path; print(next((x.split("=",1)[1] for x in Path("'"$API_DIR"'/.env").read_text().splitlines() if x.startswith("USERNAME=")), ""))')"
    api_pass="$("$CORE_DIR/.venv/bin/python" -c 'from pathlib import Path; print(next((x.split("=",1)[1] for x in Path("'"$API_DIR"'/.env").read_text().splitlines() if x.startswith("PASSWORD=")), ""))')"
    ready=false
    for _ in $(seq 1 180); do
      if curl -fsS -u "$api_user:$api_pass" http://127.0.0.1:8000/spectra/runtime >/dev/null 2>&1; then
        ready=true
        break
      fi
      sleep 2
    done
    [[ "$ready" == true ]] || { compose logs --tail=120; exit 1; }
    start_core
    xdg-open http://127.0.0.1:8088/overview >/dev/null 2>&1 || true
    ;;
  stop)
    stop_core
    compose down
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if [[ -f "$INSTALL_ROOT/condor.pid" ]] && kill -0 "$(cat "$INSTALL_ROOT/condor.pid")" 2>/dev/null; then
      echo "Spectra Intelligence process: running"
    else
      echo "Spectra Intelligence process: stopped"
    fi
    compose ps
    ;;
  logs)
    tail -n 100 -f "$INSTALL_ROOT/condor.log"
    ;;
  update)
    curl -fsSL "https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/alpha/one-command-paper-demo-20261108/install-alpha.sh" | bash
    "$0" restart
    ;;
  *)
    echo "Usage: spectra {start|stop|restart|status|logs|update}"
    exit 2
    ;;
esac
EOF
chmod 700 "$LOCAL_BIN/spectra"
for shell_rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [[ -f "$shell_rc" ]] && ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$shell_rc"; then
    printf '\n# Spectra Finance Alpha tools\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$shell_rc"
  fi
done

say "Spectra Finance Alpha installation completed"
printf '\nDashboard: http://127.0.0.1:8088\n'
printf 'Paper venue: Kraken (kraken_paper_trade) — simulated balances only\n'
printf 'Live execution: DISABLED\n'
printf 'API credentials were generated locally and stored with mode 0600. They were not printed.\n'
printf 'Control: spectra status | spectra stop | spectra start | spectra logs\n'
if has xdg-open; then xdg-open http://127.0.0.1:8088/overview >/dev/null 2>&1 || true; fi
