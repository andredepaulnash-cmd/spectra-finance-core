#!/usr/bin/env bash
set -Eeuo pipefail

CORE_REPO="${SPECTRA_ALPHA2_CORE_REPO:-https://github.com/andredepaulnash-cmd/spectra-finance-core.git}"
API_REPO="${SPECTRA_ALPHA2_API_REPO:-https://github.com/andredepaulnash-cmd/spectra-hummingbot-api.git}"
CORE_BRANCH="spectra-finance-alpha-2.0"
API_BRANCH="spectra-finance-alpha-2.0"
INSTALL_DIR="${SPECTRA_ALPHA2_HOME:-$HOME/SpectraFinance-Alpha2}"
CORE_DIR="$INSTALL_DIR/spectra-finance-core"
API_DIR="$INSTALL_DIR/spectra-hummingbot-api"
BIN_DIR="$HOME/.local/bin"
WRAPPER="$BIN_DIR/spectra-alpha2"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

command -v git >/dev/null || die "git is required."
command -v python3 >/dev/null || die "python3 is required."
command -v podman >/dev/null || die "Podman is required. This installer targets rootless Bazzite/Podman."
ROOTLESS="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)"
[[ "$ROOTLESS" == "true" ]] || die "Podman must be running rootless. Do not run this installer with sudo."

if podman compose version >/dev/null 2>&1; then
  COMPOSE_KIND="podman compose"
elif command -v podman-compose >/dev/null 2>&1; then
  COMPOSE_KIND="podman-compose"
else
  COMPOSE_KIND=""
fi

mkdir -p "$INSTALL_DIR"
if [[ ! -d "$CORE_DIR/.git" ]]; then
  [[ ! -e "$CORE_DIR" ]] || die "$CORE_DIR exists but is not a Git checkout. Move it aside; installer will not overwrite it."
  info "Cloning Spectra Core Alpha 2.0"
  git clone --branch "$CORE_BRANCH" --depth 1 "$CORE_REPO" "$CORE_DIR"
else
  git -C "$CORE_DIR" remote get-url origin | grep -q "spectra-finance-core" || die "Unexpected Git remote in $CORE_DIR."
  git -C "$CORE_DIR" fetch --depth 1 origin "$CORE_BRANCH"
  git -C "$CORE_DIR" checkout "$CORE_BRANCH"
  git -C "$CORE_DIR" pull --ff-only origin "$CORE_BRANCH"
fi

if [[ ! -d "$API_DIR/.git" ]]; then
  [[ ! -e "$API_DIR" ]] || die "$API_DIR exists but is not a Git checkout. Move it aside; installer will not overwrite it."
  info "Cloning isolated Hummingbot API Alpha 2.0"
  git clone --branch "$API_BRANCH" --depth 1 "$API_REPO" "$API_DIR"
else
  git -C "$API_DIR" remote get-url origin | grep -q "spectra-hummingbot-api" || die "Unexpected Git remote in $API_DIR."
  git -C "$API_DIR" fetch --depth 1 origin "$API_BRANCH"
  git -C "$API_DIR" checkout "$API_BRANCH"
  git -C "$API_DIR" pull --ff-only origin "$API_BRANCH"
fi

CORE_SHA="$(git -C "$CORE_DIR" rev-parse HEAD)"
API_SHA="$(git -C "$API_DIR" rev-parse HEAD)"
printf 'core=%s\napi=%s\n' "$CORE_SHA" "$API_SHA" > "$INSTALL_DIR/installed-commits.txt"
chmod 600 "$INSTALL_DIR/installed-commits.txt"

info "Creating isolated host-side Python environment"
if [[ ! -x "$CORE_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$CORE_DIR/.venv"
fi
"$CORE_DIR/.venv/bin/python" -m pip install --upgrade pip
"$CORE_DIR/.venv/bin/python" -m pip install -e "$CORE_DIR"
if [[ -z "$COMPOSE_KIND" ]]; then
  "$CORE_DIR/.venv/bin/python" -m pip install podman-compose
  COMPOSE_KIND="podman-compose"
fi

info "Preparing keyless paper-only configuration"
"$CORE_DIR/.venv/bin/python" "$CORE_DIR/scripts/prepare_alpha2_environment.py" --api-repo "$API_DIR"
"$CORE_DIR/.venv/bin/python" "$CORE_DIR/scripts/build_alpha2_compose.py" --api-repo "$API_DIR" --output "$CORE_DIR/docker-compose.alpha2.yml"

compose() {
  if [[ "$COMPOSE_KIND" == "podman compose" ]]; then
    (cd "$CORE_DIR" && podman compose -p spectra-alpha2 -f docker-compose.alpha2.yml "$@")
  else
    (cd "$CORE_DIR" && "$CORE_DIR/.venv/bin/podman-compose" -p spectra-alpha2 -f docker-compose.alpha2.yml "$@")
  fi
}

info "Validating isolated Compose configuration"
compose config >/dev/null

info "Building and starting Alpha 2.0 paper services"
compose up -d --build

API_USER="$(sed -n 's/^USERNAME=//p' "$API_DIR/.env" | tail -n1)"
API_PASS="$(sed -n 's/^PASSWORD=//p' "$API_DIR/.env" | tail -n1)"
[[ -n "$API_USER" && -n "$API_PASS" ]] || die "Internal API service credentials were not generated."

info "Waiting for authenticated paper API readiness"
"$CORE_DIR/.venv/bin/python" - "$API_USER" "$API_PASS" <<'PY'
import base64
import sys
import time
import urllib.error
import urllib.request

username,password=sys.argv[1:3]
request=urllib.request.Request("http://127.0.0.1:8002/spectra/health")
request.add_header("Authorization","Basic "+base64.b64encode(f"{username}:{password}".encode()).decode())
deadline=time.time()+360
last_error=None
while time.time()<deadline:
    try:
        with urllib.request.urlopen(request,timeout=5) as response:
            if response.status==200:
                print(response.read().decode())
                raise SystemExit(0)
    except Exception as exc:
        last_error=exc
    time.sleep(2)
print(f"Paper API did not become ready: {last_error}",file=sys.stderr)
raise SystemExit(1)
PY

info "Running Core market-data and paper-safety doctor"
podman exec spectra-alpha2-core python -m spectra_alpha2 doctor

mkdir -p "$BIN_DIR"
cat > "$WRAPPER" <<'WRAPPER'
#!/usr/bin/env bash
set -Eeuo pipefail
INSTALL_DIR="${SPECTRA_ALPHA2_HOME:-$HOME/SpectraFinance-Alpha2}"
CORE_DIR="$INSTALL_DIR/spectra-finance-core"
if podman compose version >/dev/null 2>&1; then
  COMPOSE=(podman compose)
else
  COMPOSE=("$CORE_DIR/.venv/bin/podman-compose")
fi
compose() { (cd "$CORE_DIR" && "${COMPOSE[@]}" -p spectra-alpha2 -f docker-compose.alpha2.yml "$@"); }
case "${1:-status}" in
  start) compose up -d ;;
  stop) compose stop ;;
  restart) compose restart ;;
  status)
    compose ps
    podman exec spectra-alpha2-core python -m spectra_alpha2 status
    ;;
  logs) compose logs -f --tail=200 "${2:-}" ;;
  doctor) podman exec spectra-alpha2-core python -m spectra_alpha2 doctor ;;
  once) podman exec spectra-alpha2-core python -m spectra_alpha2 run-once ;;
  backup)
    mkdir -p "$INSTALL_DIR/backups"
    target="$INSTALL_DIR/backups/spectra-alpha2-$(date +%Y%m%d-%H%M%S).sqlite3"
    podman cp spectra-alpha2-core:/data/spectra_alpha2.sqlite3 "$target"
    chmod 600 "$target"
    printf 'Database backup: %s\n' "$target"
    ;;
  uninstall)
    read -r -p "Stop and remove only the Spectra Alpha 2.0 containers/network (keep named volumes and files)? [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 0
    compose down
    ;;
  *)
    echo "Usage: spectra-alpha2 {start|stop|restart|status|logs|doctor|backup|uninstall}" >&2
    exit 2
    ;;
esac
WRAPPER
chmod 700 "$WRAPPER"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/spectra-alpha2.service" <<UNIT
[Unit]
Description=Spectra Finance Alpha 2.0 paper trading stack
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart="$WRAPPER" start
ExecStop="$WRAPPER" stop
TimeoutStartSec=0

[Install]
WantedBy=default.target
UNIT
if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1; then
  systemctl --user enable spectra-alpha2.service >/dev/null 2>&1 || true
fi

info "Installation completed"
printf 'Install directory: %s\n' "$INSTALL_DIR"
printf 'Pinned source commits:\n'
cat "$INSTALL_DIR/installed-commits.txt"
printf '\nCommands: %s {status|doctor|logs|backup|stop|start}\n' "$WRAPPER"
printf 'Hummingbot API: http://127.0.0.1:8002 (authenticated)\n'
printf 'MQTT: 127.0.0.1:1884; EMQX dashboard: http://127.0.0.1:18084\n'
printf 'Paper-only flags are enforced. No exchange credentials are configured or accepted.\n'
