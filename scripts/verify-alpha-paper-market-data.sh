#!/usr/bin/env bash
# Authenticated, read-only verification of Spectra Alpha paper market data.
# Reads USERNAME/PASSWORD from the existing API .env without printing them.
# Does not add/remove trading pairs, restart services, or submit orders.
set -Eeuo pipefail

INSTALL_ROOT="${SPECTRA_ALPHA_HOME:-$HOME/SpectraFinance-Alpha}"
API_DIR="$INSTALL_ROOT/spectra-hummingbot-api"
ENV_FILE="$API_DIR/.env"
API_BASE="${SPECTRA_API_URL:-http://127.0.0.1:8000}"
CONNECTOR="${SPECTRA_PAPER_CONNECTOR:-kraken_paper_trade}"
PAIR="${SPECTRA_PAPER_PAIR:-BTC-USD}"
DEPTH="${SPECTRA_ORDER_BOOK_DEPTH:-10}"

[[ -f "$ENV_FILE" ]] || { echo "[SPECTRA VERIFY] STOP: API .env not found at $ENV_FILE" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "[SPECTRA VERIFY] STOP: python3 is required." >&2; exit 2; }

export SPECTRA_VERIFY_ENV_FILE="$ENV_FILE"
export SPECTRA_VERIFY_API_BASE="$API_BASE"
export SPECTRA_VERIFY_CONNECTOR="$CONNECTOR"
export SPECTRA_VERIFY_PAIR="$PAIR"
export SPECTRA_VERIFY_DEPTH="$DEPTH"

python3 <<'PY'
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

def read_env(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values

env = read_env(os.environ["SPECTRA_VERIFY_ENV_FILE"])
username = env.get("USERNAME", "")
password = env.get("PASSWORD", "")
if not username or not password:
    sys.exit("[SPECTRA VERIFY] STOP: USERNAME or PASSWORD is missing from the API .env; no credentials were displayed.")

base = os.environ["SPECTRA_VERIFY_API_BASE"].rstrip("/")
connector = os.environ["SPECTRA_VERIFY_CONNECTOR"]
pair = os.environ["SPECTRA_VERIFY_PAIR"]
try:
    depth = int(os.environ["SPECTRA_VERIFY_DEPTH"])
except ValueError:
    sys.exit("[SPECTRA VERIFY] STOP: order-book depth must be an integer.")
if not 1 <= depth <= 1000:
    sys.exit("[SPECTRA VERIFY] STOP: order-book depth must be between 1 and 1000.")

token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
auth = "Basic " + token

class ApiFailure(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")

def call(label, path, payload=None):
    headers = {"Accept": "application/json", "Authorization": auth}
    data = None
    method = "GET"
    if payload is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            body = response.read().decode("utf-8")
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                raise ApiFailure(response.status, "non-JSON response")
            print(f"[SPECTRA VERIFY] {label}: HTTP {response.status}")
            return result
    except urllib.error.HTTPError as exc:
        # Never print request headers or credential material.
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise ApiFailure(exc.code, detail) from None
    except ApiFailure:
        raise
    except Exception as exc:
        raise RuntimeError(f"{label}: {type(exc).__name__}: {exc}") from None

def show_diagnostics(label):
    print(f"[SPECTRA VERIFY] {label}")
    data = call("Order-book tracker diagnostics", f"/market-data/order-book/diagnostics/{connector}")
    if not isinstance(data, dict):
        print("  diagnostics response was not an object")
        return
    # This endpoint contains runtime state only; do not dump environment/configuration.
    for key in (
        "connector_type", "connector_name", "has_order_book_tracker",
        "tracker_ready", "trading_pairs", "order_books", "tasks",
        "websocket_status", "metrics",
    ):
        if key in data:
            rendered = json.dumps(data[key], sort_keys=True, default=str)
            print(f"  {key}: {rendered[:6000]}")

print("[SPECTRA VERIFY] Running authenticated, read-only paper market-data checks.")
try:
    show_diagnostics("Before order-book request")
    book = call(
        f"{connector} {pair} order book",
        "/market-data/order-book",
        {"connector_name": connector, "trading_pair": pair, "depth": depth},
    )
except ApiFailure as exc:
    print(f"[SPECTRA VERIFY] FAIL: order-book request: HTTP {exc.status}: {exc.detail}")
    try:
        show_diagnostics("After failed order-book request (failure evidence)")
    except Exception as diag_exc:
        print(f"[SPECTRA VERIFY] Could not collect follow-up diagnostics: {diag_exc}")
    sys.exit(1)
except Exception as exc:
    sys.exit(f"[SPECTRA VERIFY] FAIL: {exc}")

bids = book.get("bids") if isinstance(book, dict) else None
asks = book.get("asks") if isinstance(book, dict) else None
if not isinstance(bids, list) or not isinstance(asks, list):
    sys.exit("[SPECTRA VERIFY] FAIL: order-book response did not contain bids and asks arrays.")
print(f"  trading_pair: {book.get('trading_pair', pair)}")
print(f"  bid_levels: {len(bids)}")
print(f"  ask_levels: {len(asks)}")
if bids:
    print(f"  best_bid: {bids[0].get('price')}")
if asks:
    print(f"  best_ask: {asks[0].get('price')}")
if not bids or not asks:
    sys.exit("[SPECTRA VERIFY] INCOMPLETE: authenticated API works, but the order book has no bid or ask levels yet.")
try:
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
except (KeyError, TypeError, ValueError):
    sys.exit("[SPECTRA VERIFY] FAIL: top-of-book price levels are malformed.")
if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
    sys.exit("[SPECTRA VERIFY] FAIL: top-of-book prices are invalid or crossed.")
print("[SPECTRA VERIFY] SUCCESS: authenticated Kraken Paper order book returned valid bid/ask levels.")
print("[SPECTRA VERIFY] Read-only check complete. No orders or trading-pair changes were submitted.")
PY
