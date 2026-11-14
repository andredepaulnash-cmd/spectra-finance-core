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
paper = values.get("SPECTRA_PAPER_ONLY", "").lower()
live = values.get("SPECTRA_ALLOW_LIVE_EXECUTION", "").lower()
if paper not in {"true", "false"} or live not in {"true", "false"}:
    raise SystemExit("Refusing to start: execution mode flags must be explicit true/false values.")
if (paper == "true" and live != "false") or (paper == "false" and live != "true"):
    raise SystemExit("Refusing to start: live mode requires separate explicit authorization.")
connectors = [name.strip() for name in values.get("SPECTRA_PAPER_CONNECTORS", "").split(",") if name.strip()]
if paper == "true" and (not connectors or any(not name.endswith("_paper_trade") for name in connectors)):
    raise SystemExit("Refusing to start: paper mode requires an explicit paper-connector-only allowlist.")
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
RUNTIME_JSON="$(mktemp)"
trap 'rm -f "$AUTH_CONFIG" "$RUNTIME_JSON"' EXIT
chmod 600 "$AUTH_CONFIG" "$RUNTIME_JSON"
printf 'user = "%s:%s"\n' "${AUTH[0]}" "${AUTH[1]}" > "$AUTH_CONFIG"

for _ in $(seq 1 180); do
  if curl -fsS --config "$AUTH_CONFIG" http://127.0.0.1:8000/spectra/runtime -o "$RUNTIME_JSON"; then
    if python3 - "$RUNTIME_JSON" <<'PYRUNTIME'
import json
import sys
runtime = json.load(open(sys.argv[1], encoding="utf-8"))
if paper == "true":
    assert runtime.get("mode") == "PAPER_ONLY", runtime
    assert runtime.get("paper_only") is True, runtime
    assert runtime.get("paper_connectors_only") is True, runtime
    assert runtime.get("live_dispatch_available") is False, runtime
    assert any(str(item).endswith("/kraken_paper_trade") for item in runtime.get("paper_connectors", [])), runtime
else:
    assert runtime.get("paper_only") is False, runtime
    assert runtime.get("live_dispatch_available") is True, runtime
PYRUNTIME
    then
      echo "[SPECTRA START] API stack is ready; Kraken paper-only runtime is verified."
      exit 0
    fi
  fi
  sleep 2
done
echo "[SPECTRA START] API stack did not become ready; inspect the spectra-alpha container logs." >&2
exit 1
