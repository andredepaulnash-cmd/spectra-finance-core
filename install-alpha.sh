#!/usr/bin/env bash
# Spectra Finance Alpha installer — Linux / Bazzite
# Installs the pinned Spectra Core + Hummingbot API paper-only candidate.
set -Eeuo pipefail

CORE_REPO="https://github.com/andredepaulnash-cmd/spectra-finance-core.git"
CORE_REF="alpha/one-command-paper-demo-20261108"
API_REPO="https://github.com/andredepaulnash-cmd/spectra-hummingbot-api.git"
API_REF="security/core-signed-executor-stop-receipts-20261108"
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
export PATH="$LOCAL_BIN:$HOME/.cargo/bin:$PATH"

say() { printf '\n\033[1;36m[SPECTRA ALPHA]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[STOP]\033[0m %s\n' "$*" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

[[ "$(uname -s)" == Linux ]] || die "This alpha installer currently supports Linux (including Bazzite)."
[[ "$(uname -m)" =~ ^(x86_64|aarch64|arm64)$ ]] || die "Unsupported CPU architecture: $(uname -m)"
[[ ! -e "$INSTALL_ROOT" ]] || die "$INSTALL_ROOT already exists. This installer will not delete or overwrite an existing installation. Choose another SPECTRA_ALPHA_HOME or move the folder yourself."

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

has tmux || {
  if [[ -e /run/ostree-booted ]]; then
    die "tmux is missing. Install tmux through your Bazzite-supported package workflow, then rerun."
  elif has apt-get; then sudo apt-get install -y tmux
  elif has dnf; then sudo dnf install -y tmux
  elif has pacman; then sudo pacman -S --noconfirm tmux
  elif has zypper; then sudo zypper --non-interactive install tmux
  else die "tmux is required to manage the Condor process."; fi
}

say "Cloning the pinned Spectra Core alpha branch"
mkdir -p "$INSTALL_ROOT"
git clone --depth 1 --single-branch --branch "$CORE_REF" "$CORE_REPO" "$CORE_DIR"

say "Cloning the matching Hummingbot API branch"
if has gh && gh auth status >/dev/null 2>&1; then
  gh repo clone andredepaulnash-cmd/spectra-hummingbot-api "$API_DIR" -- --depth 1 --single-branch --branch "$API_REF"
else
  if ! git clone --depth 1 --single-branch --branch "$API_REF" "$API_REPO" "$API_DIR"; then
    die "The Hummingbot API repository is private. Authenticate this machine with GitHub (gh auth login) and grant it access to andredepaulnash-cmd/spectra-hummingbot-api, then rerun this same installer. The repository was not made public."
  fi
fi

say "Installing the reviewed Python 3.12 runtime"
uv python install 3.12
cd "$CORE_DIR"
uv sync --locked --dev --python 3.12

say "Building the Spectra dashboard"
cd "$CORE_DIR/frontend"
npm ci
npm run build

say "Creating local-only API credentials and paper-only configuration"
cd "$API_DIR"
mkdir -p bots/conf bots/data .emqx
API_USER="spectra_alpha"
API_PASS="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"
CONFIG_PASS="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
BROKER_USER="spectra_$(python3 -c 'import secrets; print(secrets.token_hex(5))')"
BROKER_PASS="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"
BROKER_DASH_PASS="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"

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
chmod 700 . bots
printf 'user_id,password,is_superuser\n%s,%s,false\n' "$BROKER_USER" "$BROKER_PASS" > .emqx/auth-bootstrap.csv
chmod 700 .emqx
chmod 644 .emqx/auth-bootstrap.csv

# Hummingbot's official paper-trade config: Kraken market data, simulated orders/balances.
cat > bots/conf/conf_client.yml <<'EOF'
paper_trade:
  paper_trade_exchanges:
    - kraken
  paper_trade_account_balance:
    USD: 10000
    USDT: 10000
    BTC: 1
    ETH: 20
    XRP: 10000
EOF

# This alpha must not hand the API container the host Docker/Podman control socket.
# Generate a private compose copy with only that mount removed; fail if source changes.
if ! grep -Fq '/var/run/docker.sock:/var/run/docker.sock' docker-compose.yml; then
  die "Expected Docker socket mount was not found in the pinned API Compose file; refusing to guess at a safe override."
fi
sed '\|/var/run/docker.sock:/var/run/docker.sock|d' docker-compose.yml > docker-compose.alpha.yml
if grep -Fq 'docker.sock' docker-compose.alpha.yml; then
  die "The alpha Compose file still contains a host container socket mount."
fi

say "Generating the Core signing key and API verification key locally"
cd "$CORE_DIR"
uv run --python 3.12 python scripts/generate_spectra_dispatch_keys.py \
  --core-env .env --api-env "$API_DIR/.env"

# Complete Core's local-mode configuration and pre-register the local API.
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
# Restore generated private signing key values; generator wrote them before this local-mode file.
uv run --python 3.12 python scripts/generate_spectra_dispatch_keys.py \
  --core-env .env --api-env "$API_DIR/.env"
chmod 600 .env config.yml

say "Building and starting the Hummingbot paper-only API stack ($COMPOSE_KIND)"
cd "$API_DIR"
if [[ "$COMPOSE_KIND" == docker ]]; then
  docker compose -f docker-compose.alpha.yml up -d --build
else
  podman-compose -f docker-compose.alpha.yml up -d --build
fi

say "Waiting for the local Hummingbot API"
for _ in $(seq 1 180); do
  if curl -fsS -u "$API_USER:$API_PASS" http://127.0.0.1:8000/openapi.json >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS -u "$API_USER:$API_PASS" http://127.0.0.1:8000/openapi.json >/dev/null \
  || die "Hummingbot API did not become ready. See: $INSTALL_ROOT/spectra-hummingbot-api logs."

say "Starting Condor and the Spectra dashboard"
cd "$CORE_DIR"
make run

cat > "$LOCAL_BIN/spectra" <<EOF
#!/usr/bin/env bash
set -e
CORE_DIR="$CORE_DIR"
API_DIR="$API_DIR"
COMPOSE_KIND="$COMPOSE_KIND"
case "${1:-status}" in
  start)
    cd "\$API_DIR"
    if [[ "\$COMPOSE_KIND" == docker ]]; then docker compose -f docker-compose.alpha.yml up -d; else podman-compose -f docker-compose.alpha.yml up -d; fi
    cd "\$CORE_DIR"
    make run
    ;;
  stop)
    cd "\$CORE_DIR"; make stop
    cd "\$API_DIR"
    if [[ "\$COMPOSE_KIND" == docker ]]; then docker compose -f docker-compose.alpha.yml down; else podman-compose -f docker-compose.alpha.yml down; fi
    ;;
  restart)
    "\$0" stop
    "\$0" start
    ;;
  status)
    cd "\$CORE_DIR"; make status
    cd "\$API_DIR"
    if [[ "\$COMPOSE_KIND" == docker ]]; then docker compose -f docker-compose.alpha.yml ps; else podman-compose -f docker-compose.alpha.yml ps; fi
    ;;
  logs)
    cd "\$CORE_DIR"; make logs
    ;;
  *)
    echo "Usage: spectra {start|stop|restart|status|logs}"
    exit 2
    ;;
esac
EOF
chmod 700 "$LOCAL_BIN/spectra"

say "Spectra Finance Alpha installation completed"
printf '\nDashboard: http://127.0.0.1:8088\n'
printf 'Paper venue: Kraken (kraken_paper_trade) — simulated balances only\n'
printf 'Live execution: DISABLED\n'
printf 'API credentials were generated locally and stored with mode 0600. They were not printed.\n'
printf 'Control: spectra status | spectra stop | spectra start | spectra logs\n'
if has xdg-open; then xdg-open http://127.0.0.1:8088 >/dev/null 2>&1 || true; fi
