#!/usr/bin/env bash
# Read-only operational health monitor for Spectra Finance Alpha.
set -Eeuo pipefail

INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
CORE_DIR="$INSTALL_ROOT/spectra-core"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
LOG_DIR="$INSTALL_ROOT/logs"
LOG_FILE="$LOG_DIR/doctor.log"
LOCK_FILE="$LOG_DIR/doctor.lock"
API_ENV="$API_DIR/.env"
CORE_ENV="$CORE_DIR/.env"
VERIFY_BIN="$HOME/.local/bin/spectra-verify-alpha-paper-market-data"

mkdir -p "$LOG_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0
exec >>"$LOG_FILE" 2>&1

say() { printf '[%s] [SPECTRA DOCTOR] %s\n' "$(date -Is)" "$*"; }
fail() { say "FAIL: $*"; exit 1; }
ok() { say "OK: $*"; }

[[ -f "$API_ENV" && -f "$CORE_ENV" ]] || fail "Core/API configuration files are missing."
[[ -f "$INSTALL_ROOT/.compose-kind" ]] || fail "Compose runtime marker is missing."

# Parse only the fields required for checks. Never print credentials.
mapfile -t AUTH < <(python3 - "$API_ENV" <<'PY'
from pathlib import Path
import sys
values = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    values[key.strip()] = value
for key in ("USERNAME", "PASSWORD"):
    value = values.get(key, "")
    if not value:
        raise SystemExit(2)
    print(value)
PY
) || fail "API credentials could not be read safely."
[[ "${#AUTH[@]}" -eq 2 ]] || fail "API credential configuration is incomplete."

CURL_AUTH_CONFIG="$(mktemp)"
RUNTIME_JSON="$(mktemp)"
trap 'rm -f "$CURL_AUTH_CONFIG" "$RUNTIME_JSON"' EXIT
chmod 600 "$CURL_AUTH_CONFIG" "$RUNTIME_JSON"
printf 'user = "%s:%s"\n' "${AUTH[0]}" "${AUTH[1]}" > "$CURL_AUTH_CONFIG"

python3 - "$API_ENV" "$CORE_ENV" <<'PY'
from pathlib import Path
import sys

def read(path):
    result = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result

api, core = map(read, sys.argv[1:])
for label, values in (("API", api), ("Core", core)):
    if values.get("SPECTRA_PAPER_ONLY", "").lower() != "true":
        raise SystemExit(f"{label} paper-only flag is not true")
    if values.get("SPECTRA_ALLOW_LIVE_EXECUTION", "").lower() != "false":
        raise SystemExit(f"{label} live-execution flag is not false")
if api.get("SPECTRA_PAPER_CONNECTORS") != "kraken_paper_trade":
    raise SystemExit("API paper connector allowlist is not Kraken paper only")
PY
ok "paper-only and live-disabled configuration"

API_BASE="${SPECTRA_API_URL:-http://127.0.0.1:8000}"
curl -fsS --config "$CURL_AUTH_CONFIG" "$API_BASE/spectra/runtime" -o "$RUNTIME_JSON" \
  || fail "authenticated Spectra runtime endpoint is unavailable."
python3 - "$RUNTIME_JSON" <<'PY'
import json
import sys
runtime = json.load(open(sys.argv[1], encoding="utf-8"))
assert runtime.get("mode") == "PAPER_ONLY", runtime
assert runtime.get("paper_only") is True, runtime
assert runtime.get("paper_connectors_only") is True, runtime
assert runtime.get("live_dispatch_available") is False, runtime
assert any(str(item).endswith("/kraken_paper_trade") for item in runtime.get("paper_connectors", [])), runtime
PY
ok "authenticated API runtime confirms Kraken paper-only mode"

curl -fsS http://127.0.0.1:8088/overview -o /dev/null \
  || fail "Spectra dashboard is not responding on 127.0.0.1:8088."
ok "dashboard responds on loopback"

COMPOSE_KIND="$(cat "$INSTALL_ROOT/.compose-kind")"
for container in spectra-alpha-hummingbot-api spectra-alpha-hummingbot-postgres spectra-alpha-hummingbot-broker; do
  if [[ "$COMPOSE_KIND" == "docker" ]]; then
    status="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null || true)"
  elif [[ "$COMPOSE_KIND" == "podman" ]]; then
    status="$(podman inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null || true)"
  else
    fail "Unknown Compose runtime: $COMPOSE_KIND"
  fi
  [[ "$status" == running* ]] || fail "$container is not running."
  if [[ "$container" != "spectra-alpha-hummingbot-api" && "$status" == *unhealthy* ]]; then
    fail "$container health check is unhealthy."
  fi
  ok "$container: $status"
done

[[ -x "$VERIFY_BIN" ]] || fail "Read-only market-data verifier is not installed."
"$VERIFY_BIN" || fail "Kraken paper order book did not pass the read-only verification."
ok "Kraken paper market data verified"

say "PASS: operational checks completed. No orders, configuration changes, restarts, or capital mutations were performed."
