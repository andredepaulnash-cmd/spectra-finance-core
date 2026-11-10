#!/usr/bin/env bash
# Safe API stack preparation for the user-level systemd Core service.
set -Eeuo pipefail
INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
ENV_FILE="$API_DIR/.env"
COMPOSE_FILE="$API_DIR/docker-compose.alpha.yml"
COMPOSE_KIND_FILE="$INSTALL_ROOT/.compose-kind"

[[ -f "$ENV_FILE" && -f "$COMPOSE_FILE" && -f "$COMPOSE_KIND_FILE" ]] || {
  echo "[SPECTRA START] Alpha API configuration is incomplete." >&2
  exit 1
}
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
values = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
if values.get("SPECTRA_PAPER_ONLY", "").lower() != "true":
    raise SystemExit("Refusing to start: SPECTRA_PAPER_ONLY must be true.")
if values.get("SPECTRA_ALLOW_LIVE_EXECUTION", "").lower() != "false":
    raise SystemExit("Refusing to start: live execution must remain disabled.")
if values.get("SPECTRA_PAPER_CONNECTORS") != "kraken_paper_trade":
    raise SystemExit("Refusing to start: paper connector allowlist is not Kraken-only.")
if not values.get("USERNAME") or not values.get("PASSWORD"):
    raise SystemExit("API credentials are missing.")
PY

COMPOSE_KIND="$(cat "$COMPOSE_KIND_FILE")"
cd "$API_DIR"
case "$COMPOSE_KIND" in
  docker) docker compose -p spectra-alpha -f "$COMPOSE_FILE" up -d ;;
  podman) podman-compose -p spectra-alpha -f "$COMPOSE_FILE" up -d ;;
  *) echo "[SPECTRA START] Unknown Compose runtime: $COMPOSE_KIND" >&2; exit 1 ;;
esac

mapfile -t AUTH < <(python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
values = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
print(values.get("USERNAME", ""))
print(values.get("PASSWORD", ""))
PY
)
[[ "${#AUTH[@]}" -eq 2 && -n "${AUTH[0]}" && -n "${AUTH[1]}" ]] || exit 1
AUTH_CONFIG="$(mktemp)"
trap 'rm -f "$AUTH_CONFIG"' EXIT
chmod 600 "$AUTH_CONFIG"
printf 'user = "%s:%s"\n' "${AUTH[0]}" "${AUTH[1]}" > "$AUTH_CONFIG"

for _ in $(seq 1 180); do
  if curl -fsS --config "$AUTH_CONFIG" http://127.0.0.1:8000/spectra/runtime -o /dev/null; then
    echo "[SPECTRA START] API stack is ready in paper-only mode."
    exit 0
  fi
  sleep 2
done
echo "[SPECTRA START] API stack did not become ready; inspect the spectra-alpha container logs." >&2
exit 1
