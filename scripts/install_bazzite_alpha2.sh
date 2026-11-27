#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

CORE_REPO="${SPECTRA_ALPHA2_CORE_REPO:-https://github.com/andredepaulnash-cmd/spectra-finance-core.git}"
API_REPO="${SPECTRA_ALPHA2_API_REPO:-https://github.com/andredepaulnash-cmd/spectra-hummingbot-api.git}"
CORE_BRANCH="${SPECTRA_ALPHA2_CORE_BRANCH:-alpha2-paper-usd-release}"
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
UPGRADE_STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ "${SPECTRA_ALPHA2_CREATE_PREUPGRADE_BACKUPS:-false}" == "true" && -f "$INSTALL_DIR/installed-commits.txt" ]]; then
  mkdir -p "$INSTALL_DIR/backups"
  cp -p -- "$INSTALL_DIR/installed-commits.txt" "$INSTALL_DIR/backups/installed-commits-preupgrade-$UPGRADE_STAMP.txt"
  chmod 600 "$INSTALL_DIR/backups/installed-commits-preupgrade-$UPGRADE_STAMP.txt"
fi

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
backup_stopped_container_volume() {
  local container_name="$1" mount_path="$2" backup_label="$3"
  local volume_name backup_path
  volume_name="$(podman inspect --format "{{range .Mounts}}{{if eq .Destination \"$mount_path\"}}{{.Name}}{{end}}{{end}}" "$container_name" 2>/dev/null || true)"
  [[ -n "$volume_name" ]] || die "Could not identify the named volume mounted at $mount_path in $container_name; refusing an unbacked upgrade."
  podman volume exists "$volume_name" || die "Required data volume $volume_name is missing."
  backup_path="$INSTALL_DIR/backups/$backup_label-$UPGRADE_STAMP.tar"
  podman volume export "$volume_name" --output "$backup_path"
  [[ -s "$backup_path" ]] || die "Volume backup was empty: $backup_path"
  chmod 600 "$backup_path"
  info "Created offline volume backup: $backup_path"
}

if podman container exists spectra-alpha2-core 2>/dev/null; then
  # Structural safety checks are mandatory even when disk-saving backups are disabled.
  podman container exists spectra-alpha2-hummingbot-api 2>/dev/null || die "Core exists without its Hummingbot API container; refusing an incomplete upgrade."
  podman container exists spectra-alpha2-postgres 2>/dev/null || die "Core exists without its PostgreSQL container; refusing an incomplete upgrade."
  if [[ "${SPECTRA_ALPHA2_CREATE_PREUPGRADE_BACKUPS:-false}" == "true" ]]; then
    mkdir -p "$INSTALL_DIR/backups"
  running="$(podman inspect --format '{{.State.Running}}' spectra-alpha2-core 2>/dev/null || true)"
  if [[ "$running" == "true" ]]; then
    backup_name="spectra-alpha2-preupgrade-$UPGRADE_STAMP.sqlite3"
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
    # A Created/Exited container cannot run SQLite's online-backup API. Export
    # its complete stopped data volume instead, preserving the DB and any WAL.
    backup_stopped_container_volume "spectra-alpha2-core" "/data" "spectra-alpha2-core-volume-preupgrade"
  fi

  backup_stamp="$UPGRADE_STAMP"
  podman container exists spectra-alpha2-hummingbot-api 2>/dev/null || die "Core exists without its Hummingbot API container; refusing an incomplete upgrade."
  api_running="$(podman inspect --format '{{.State.Running}}' spectra-alpha2-hummingbot-api 2>/dev/null || true)"
  if [[ "$api_running" == "true" ]]; then
    if podman exec spectra-alpha2-hummingbot-api test -f /hummingbot-api/bots/data/spectra_dispatch_receipts.sqlite3; then
      receipt_name="spectra-alpha2-api-receipts-preupgrade-$backup_stamp.sqlite3"
      podman exec spectra-alpha2-hummingbot-api python - "$receipt_name" <<PYRECEIPT
import sqlite3
import sys
from pathlib import Path
target=Path("/tmp")/sys.argv[1]
with sqlite3.connect("/hummingbot-api/bots/data/spectra_dispatch_receipts.sqlite3") as source, sqlite3.connect(target) as destination:
    source.backup(destination)
target.chmod(0o600)
PYRECEIPT
      podman cp "spectra-alpha2-hummingbot-api:/tmp/$receipt_name" "$INSTALL_DIR/backups/$receipt_name"
      podman exec spectra-alpha2-hummingbot-api rm -f "/tmp/$receipt_name"
      chmod 600 "$INSTALL_DIR/backups/$receipt_name"
    fi
  elif [[ -d "$API_DIR/bots/data" ]]; then
    # The API's bots directory is a host bind mount. Preserve the complete
    # stopped data folder, including SQLite WAL/SHM files, without starting it.
    receipt_backup="$INSTALL_DIR/backups/spectra-alpha2-api-data-preupgrade-$backup_stamp.tar"
    tar -C "$API_DIR" -cf "$receipt_backup" bots/data
    [[ -s "$receipt_backup" ]] || die "API data backup was empty: $receipt_backup"
    chmod 600 "$receipt_backup"
    info "Created offline API data backup: $receipt_backup"
  fi

  podman container exists spectra-alpha2-postgres 2>/dev/null || die "Core exists without its PostgreSQL container; refusing an incomplete upgrade."
  pg_running="$(podman inspect --format "{{.State.Running}}" spectra-alpha2-postgres 2>/dev/null || true)"
  if [[ "$pg_running" == "true" ]]; then
    pg_backup="$INSTALL_DIR/backups/spectra-alpha2-postgres-preupgrade-$backup_stamp.sql"
    podman exec spectra-alpha2-postgres pg_dump -U hbot -d hummingbot_api > "$pg_backup"
    [[ -s "$pg_backup" ]] || die "PostgreSQL backup was empty: $pg_backup"
    chmod 600 "$pg_backup"
  else
    backup_stopped_container_volume "spectra-alpha2-postgres" "/var/lib/postgresql/data" "spectra-alpha2-postgres-volume-preupgrade"
  fi

  # Preserve stopped EMQX state as well. Running broker volumes remain in place
  # during the update and are not exported while the broker is writing to them.
  if podman container exists spectra-alpha2-broker 2>/dev/null; then
    broker_running="$(podman inspect --format "{{.State.Running}}" spectra-alpha2-broker 2>/dev/null || true)"
    if [[ "$broker_running" != "true" ]]; then
      for mount_path in /opt/emqx/data /opt/emqx/log /opt/emqx/etc; do
        backup_stopped_container_volume "spectra-alpha2-broker" "$mount_path" "spectra-alpha2-broker-volume-$(basename "$mount_path")-preupgrade"
      done
    fi
  fi

  # Keep existing internal credentials and signing material recoverable.
  for config_file in "$CORE_DIR/.env" "$CORE_DIR/.env.core" "$CORE_DIR/docker-compose.alpha2.yml" "$API_DIR/.env" "$API_DIR/.emqx/auth-bootstrap.csv" "$API_DIR/bots/credentials/master_account/conf_client.yml"; do
    if [[ -f "$config_file" ]]; then
      case "$config_file" in
        "$CORE_DIR/.env") backup_label="core.env" ;;
        "$CORE_DIR/.env.core") backup_label="core.env.core" ;;
        "$CORE_DIR/docker-compose.alpha2.yml") backup_label="core.docker-compose.alpha2.yml" ;;
        "$API_DIR/.env") backup_label="api.env" ;;
        "$API_DIR/.emqx/auth-bootstrap.csv") backup_label="api.auth-bootstrap.csv" ;;
        "$API_DIR/bots/credentials/master_account/conf_client.yml") backup_label="api.conf_client.yml" ;;
        *) die "Unknown configuration backup path: $config_file" ;;
      esac
      backup_config="$INSTALL_DIR/backups/$backup_label.preupgrade-$backup_stamp"
      cp -p -- "$config_file" "$backup_config"
      chmod 600 "$backup_config"
    fi
  done
  fi
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

if [[ "${SPECTRA_ALPHA2_CREATE_PREUPGRADE_BACKUPS:-false}" == "true" ]]; then
  info "Pre-upgrade backups were explicitly enabled; existing data and configuration snapshots were created above."
else
  info "Automatic pre-upgrade backups are disabled to conserve disk space."
  info "Existing named volumes and configuration files are retained in place; no data is deleted."
fi

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
API_DIR="$INSTALL_DIR/spectra-hummingbot-api"
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
  logs)
    if [[ -n "${2:-}" ]]; then
      compose logs -f --tail=200 "$2"
    else
      compose logs -f --tail=200
    fi
    ;;
  doctor) podman exec spectra-alpha2-core python -m spectra_alpha2 doctor ;;
  once) podman exec spectra-alpha2-core python -m spectra_alpha2 run-once ;;
  update)
    exec "$CORE_DIR/scripts/install_bazzite_alpha2.sh"
    ;;
  backup)
    backup_stamp="$(date +%Y%m%d-%H%M%S)"
    backup_dir="$INSTALL_DIR/backups/manual-$backup_stamp"
    mkdir -p "$backup_dir"
    chmod 700 "$backup_dir"
    target_name="spectra-alpha2-$backup_stamp.sqlite3"
    podman exec spectra-alpha2-core python - "$target_name" <<'PYBACKUP'
import sqlite3
import sys
from pathlib import Path

target = Path("/data/backups") / sys.argv[1]
target.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect("/data/spectra_alpha2.sqlite3") as source, sqlite3.connect(target) as destination:
    source.backup(destination)
target.chmod(0o600)
PYBACKUP
    podman cp "spectra-alpha2-core:/data/backups/$target_name" "$backup_dir/core.sqlite3"
    podman exec spectra-alpha2-core rm -f "/data/backups/$target_name"
    if podman exec spectra-alpha2-hummingbot-api test -f /hummingbot-api/bots/data/spectra_dispatch_receipts.sqlite3; then
      receipt_name="spectra-alpha2-receipts-$backup_stamp.sqlite3"
      podman exec spectra-alpha2-hummingbot-api python - "$receipt_name" <<'PYRECEIPT'
import sqlite3
import sys
from pathlib import Path
target=Path("/tmp")/sys.argv[1]
with sqlite3.connect("/hummingbot-api/bots/data/spectra_dispatch_receipts.sqlite3") as source, sqlite3.connect(target) as destination:
    source.backup(destination)
target.chmod(0o600)
PYRECEIPT
      podman cp "spectra-alpha2-hummingbot-api:/tmp/$receipt_name" "$backup_dir/api-receipts.sqlite3"
      podman exec spectra-alpha2-hummingbot-api rm -f "/tmp/$receipt_name"
    fi
    podman exec spectra-alpha2-postgres pg_dump -U hbot -d hummingbot_api > "$backup_dir/postgres.sql"
    for config_file in "$CORE_DIR/.env" "$CORE_DIR/.env.core" "$CORE_DIR/docker-compose.alpha2.yml" "$API_DIR/.env" "$API_DIR/.emqx/auth-bootstrap.csv" "$API_DIR/bots/credentials/master_account/conf_client.yml" "$INSTALL_DIR/installed-commits.txt"; do
      if [[ -f "$config_file" ]]; then
        case "$config_file" in
          "$CORE_DIR/.env") backup_label="core.env" ;;
          "$CORE_DIR/.env.core") backup_label="core.env.core" ;;
          "$CORE_DIR/docker-compose.alpha2.yml") backup_label="core.docker-compose.alpha2.yml" ;;
          "$API_DIR/.env") backup_label="api.env" ;;
          "$API_DIR/.emqx/auth-bootstrap.csv") backup_label="api.auth-bootstrap.csv" ;;
          "$API_DIR/bots/credentials/master_account/conf_client.yml") backup_label="api.conf_client.yml" ;;
          "$INSTALL_DIR/installed-commits.txt") backup_label="installed-commits.txt" ;;
        esac
        cp -p -- "$config_file" "$backup_dir/$backup_label"
      fi
    done
    chmod 600 "$backup_dir"/*
    printf 'Complete paper-only backup: %s\n' "$backup_dir"
    ;;
  uninstall)
    read -r -p "Stop and remove only Alpha 2.0 containers/network while retaining named volumes and all files? [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 0
    compose down
    ;;
  *)
    echo "Usage: spectra-alpha2 {start|stop|restart|status|doctor|logs|once|update|backup|uninstall}" >&2
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
printf '\nCommands: %s {status|doctor|logs|once|update|backup|stop|start|restart}\n' "$WRAPPER"
printf 'Hummingbot API: http://127.0.0.1:8002 (authenticated)\n'
printf 'MQTT: internal-only; EMQX dashboard: http://127.0.0.1:18084\n'
printf 'Paper-only flags are enforced. No exchange credentials are configured or accepted.\n'
printf 'Automatic pre-upgrade backups: disabled by default (set SPECTRA_ALPHA2_CREATE_PREUPGRADE_BACKUPS=true to opt in).\n'
printf 'Existing Alpha installations, data, volumes, and local source modifications were not removed.\n'
