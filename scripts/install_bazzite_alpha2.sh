#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

CORE_REPO="${SPECTRA_ALPHA2_CORE_REPO:-https://github.com/andredepaulnash-cmd/spectra-finance-core.git}"
API_REPO="${SPECTRA_ALPHA2_API_REPO:-https://github.com/andredepaulnash-cmd/spectra-hummingbot-api.git}"
CORE_BRANCH="${SPECTRA_ALPHA2_CORE_BRANCH:-spectra-finance-alpha-2.0}"
API_BRANCH="${SPECTRA_ALPHA2_API_BRANCH:-spectra-finance-alpha-2.0}"
INSTALL_DIR="${SPECTRA_ALPHA2_HOME:-$HOME/SpectraFinance-Alpha2}"
CORE_DIR="$INSTALL_DIR/spectra-finance-core"
API_DIR="$INSTALL_DIR/spectra-hummingbot-api"
BIN_DIR="$HOME/.local/bin"
WRAPPER="$BIN_DIR/spectra-alpha2"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

for command_name in git python3 curl podman; do
  command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."
done

[[ "$INSTALL_DIR" == /* ]] || die "Installation path must be absolute."
[[ ! -L "$INSTALL_DIR" ]] || die "Refusing to use a symlink as the Alpha 2.0 installation root."
mkdir -p "$INSTALL_DIR"

AVAILABLE_KB="$(LC_ALL=C df -Pk "$HOME" | awk 'END {print $4}')"
[[ "$AVAILABLE_KB" =~ ^[0-9]+$ ]] || die "Could not determine free space on $HOME."
AVAILABLE_GIB=$((AVAILABLE_KB / 1024 / 1024))
info "Home filesystem currently has approximately ${AVAILABLE_GIB} GiB free."
info "No fixed storage minimum is assumed; container images and build cache determine actual use."

ROOTLESS="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)"
[[ "$ROOTLESS" == "true" ]] || die "Podman must be running rootless. Do not run this installer with sudo."

if podman compose version >/dev/null 2>&1; then
  COMPOSE_KIND="podman compose"
elif command -v podman-compose >/dev/null 2>&1; then
  COMPOSE_KIND="podman-compose"
else
  COMPOSE_KIND=""
fi

ensure_checkout() {
  local repository="$1" branch="$2" directory="$3" remote_url
  if [[ ! -e "$directory" ]]; then
    info "Cloning $repository ($branch)"
    git clone --branch "$branch" --depth 1 "$repository" "$directory"
    return
  fi
  [[ -d "$directory/.git" ]] || die "$directory already exists but is not a Git checkout. It was left untouched."
  remote_url="$(git -C "$directory" remote get-url origin 2>/dev/null || true)"
  [[ "$remote_url" == *"$(basename "$repository" .git)"* ]] || die "Unexpected Git origin in $directory. It was left untouched."
  [[ -z "$(git -C "$directory" status --porcelain)" ]] || die "Local source changes exist in $directory. They were preserved; review or back them up before upgrading."
  git -C "$directory" fetch origin "$branch"
  if git -C "$directory" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$directory" checkout "$branch"
    [[ -z "$(git -C "$directory" status --porcelain)" ]] || die "Checkout of $branch exposed local changes in $directory; nothing was overwritten."
    git -C "$directory" merge --ff-only FETCH_HEAD
  else
    git -C "$directory" checkout -b "$branch" FETCH_HEAD
  fi
  [[ -z "$(git -C "$directory" status --porcelain)" ]] || die "Source checkout is not clean after update; stopping without overwriting local work."
}

# Preserve the existing Alpha 2.0 ledger before changing source or rebuilding containers.
if podman container exists spectra-alpha2-core 2>/dev/null; then
  running="$(podman inspect --format '{{.State.Running}}' spectra-alpha2-core 2>/dev/null || true)"
  [[ "$running" == "true" ]] || die "An Alpha 2.0 Core container exists but is stopped. Start it and create a SQLite online backup before upgrading."
  mkdir -p "$INSTALL_DIR/backups"
  backup_name="spectra-alpha2-preupgrade-$(date +%Y%m%d-%H%M%S).sqlite3"
  podman exec spectra-alpha2-core python - "$backup_name" <<'PYBACKUP'
import sqlite3
import sys
from pathlib import Path
name=sys.argv[1]
target=Path("/data/backups")/name
target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect("/data/spectra_alpha2.sqlite3") as source, sqlite3.connect(target) as destination:
    source.backup(destination)
target.chmod(0o600)
PYBACKUP
  podman cp "spectra-alpha2-core:/data/backups/$backup_name" "$INSTALL_DIR/backups/$backup_name"
  chmod 600 "$INSTALL_DIR/backups/$backup_name"
  info "Created pre-upgrade SQLite online backup: $INSTALL_DIR/backups/$backup_name"
else
  existing_volumes="$(podman volume ls -q | grep 'spectra-alpha2' || true)"
  [[ -z "$existing_volumes" ]] || die "Alpha 2.0 data volumes exist without the Core container. Preserve/restore the ledger and create a backup before upgrading."
fi

ensure_checkout "$CORE_REPO" "$CORE_BRANCH" "$CORE_DIR"
ensure_checkout "$API_REPO" "$API_BRANCH" "$API_DIR"

CORE_SHA="$(git -C "$CORE_DIR" rev-parse HEAD)"
API_SHA="$(git -C "$API_DIR" rev-parse HEAD)"
printf 'core=%s\napi=%s\n' "$CORE_SHA" "$API_SHA" > "$INSTALL_DIR/installed-commits.txt"
chmod 600 "$INSTALL_DIR/installed-commits.txt"

info "Creating or updating the isolated host-side Python environment"
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

info "Validating isolated Alpha 2.0 Compose configuration"
compose config >/dev/null

info "Building and starting Alpha 2.0 paper services without deleting existing volumes"
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

username, password = sys.argv[1:3]
request = urllib.request.Request("http://127.0.0.1:8002/spectra/health")
request.add_header("Authorization", "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode())
deadline = time.time() + 360
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status == 200:
                print(response.read().decode())
                raise SystemExit(0)
    except Exception as exc:
        last_error = exc
    time.sleep(2)
print(f"Paper API did not become ready: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY

info "Running Core market-data and paper-safety doctor"
podman exec spectra-alpha2-core python -m spectra_alpha2 doctor

mkdir -p "$BIN_DIR"
cat > "$WRAPPER" <<'WRAPPER'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
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
    target_name="spectra-alpha2-$(date +%Y%m%d-%H%M%S).sqlite3"
    podman exec spectra-alpha2-core python - "$target_name" <<'PYBACKUP'
import sqlite3
import sys
from pathlib import Path

target = Path("/data/backups") / sys.argv[1]
target.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect("/data/spectra_alpha2.sqlite3") as source, sqlite3.connect(target) as destination:
    source.backup(destination)
target.chmod(0o600)
print(target)
PYBACKUP
    podman cp "spectra-alpha2-core:/data/backups/$target_name" "$INSTALL_DIR/backups/$target_name"
    chmod 600 "$INSTALL_DIR/backups/$target_name"
    printf 'Database backup: %s\n' "$INSTALL_DIR/backups/$target_name"
    ;;
  uninstall)
    read -r -p "Stop and remove only Alpha 2.0 containers/network while retaining named volumes and all files? [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 0
    compose down
    ;;
  *)
    echo "Usage: spectra-alpha2 {start|stop|restart|status|doctor|logs|once|backup|uninstall}" >&2
    exit 2
    ;;
esac
WRAPPER
chmod 700 "$WRAPPER"

install -m 700 "$CORE_DIR/scripts/spectra-alpha2-desktop" "$BIN_DIR/spectra-alpha2-desktop"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
install -m 644 "$CORE_DIR/packaging/linux/spectra-alpha2.svg" "$ICON_DIR/spectra-finance-alpha2.svg"
python3 - "$CORE_DIR/packaging/linux/spectra-alpha2.desktop.in" "$DESKTOP_DIR/spectra-finance-alpha2.desktop" "$BIN_DIR" <<'PYDESKTOP'
from pathlib import Path
import sys

template_path, output_path, bin_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
template = template_path.read_text(encoding="utf-8")
executable = str(bin_dir / "spectra-alpha2-desktop")
quoted = '"' + executable.replace("\\", "\\\\").replace('"', '\\"') + '"'
template = template.replace("@SPECTRA_EXEC@", quoted)
if "@SPECTRA_" in template:
    raise SystemExit("Unresolved desktop entry template variable.")
output_path.write_text(template, encoding="utf-8")
output_path.chmod(0o644)
PYDESKTOP
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

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
printf '\nCommands: %s {status|doctor|logs|once|backup|stop|start|restart}\n' "$WRAPPER"
printf 'Hummingbot API: http://127.0.0.1:8002 (authenticated)\n'
printf 'MQTT: 127.0.0.1:1884; EMQX dashboard: http://127.0.0.1:18084\n'
printf 'Paper-only flags are enforced. No exchange credentials are configured or accepted.\n'
printf 'Existing Alpha installations, data, volumes, and local source modifications were not removed.\n'
