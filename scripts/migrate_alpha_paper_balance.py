#!/usr/bin/env python3
"""Safely migrate a clean Spectra paper account to the $25,000 USD baseline.

The migration refuses to touch an account with any order/trade/position history,
active orders, or non-USD holdings. It also requires the local API to prove that
paper-only mode is active and live dispatch is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


TARGET_USD_DEFAULT = Decimal("25000")


def _project_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[2]
    api_repo = root / "spectra-hummingbot-api"
    return api_repo / ".env", api_repo / "bots" / "credentials" / "master_account" / "conf_client.yml"


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"API environment file not found: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        values[key.strip()] = value
    return values


def _request_json(
    *,
    api_url: str,
    username: str,
    password: str,
    method: str,
    path: str,
    body: dict | None = None,
) -> dict:
    url = api_url.rstrip("/") + path
    payload = None if body is None else json.dumps(body).encode("utf-8")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"local API check failed for {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"local API returned an unexpected response for {path}")
    return parsed


def _response_items(payload: dict, label: str) -> list:
    for key in ("data", "items", "results", label):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    raise RuntimeError(f"local API {label} response has no recognizable item list")


def _verify_paper_runtime(
    *, api_url: str, username: str, password: str
) -> dict:
    runtime = _request_json(
        api_url=api_url,
        username=username,
        password=password,
        method="GET",
        path="/spectra/runtime",
    )
    if (
        runtime.get("mode") != "PAPER_ONLY"
        or runtime.get("paper_only") is not True
        or runtime.get("paper_connectors_only") is not True
        or runtime.get("live_dispatch_available") is not False
    ):
        raise RuntimeError(
            "refusing balance migration: local API did not prove paper-only "
            "mode with live dispatch unavailable"
        )
    connectors = runtime.get("paper_connectors", [])
    if not isinstance(connectors, list) or not any(
        str(name).endswith("/kraken_paper_trade") for name in connectors
    ):
        raise RuntimeError(
            "refusing balance migration: Kraken paper connector is not active"
        )
    return runtime


def _verify_empty_history(
    *, api_url: str, username: str, password: str, account_name: str
) -> dict[str, int]:
    filters = {"account_names": [account_name], "limit": 1}
    checks = {
        "trades": "/trading/trades",
        "orders": "/trading/orders/search",
        "active_orders": "/trading/orders/active",
        "positions": "/trading/positions",
    }
    counts: dict[str, int] = {}
    for label, endpoint in checks.items():
        payload = _request_json(
            api_url=api_url,
            username=username,
            password=password,
            method="POST",
            path=endpoint,
            body=filters,
        )
        items = _response_items(payload, label)
        counts[label] = len(items)
        if items:
            raise RuntimeError(
                f"refusing balance migration: {account_name} has existing "
                f"{label.replace('_', ' ')}; paper history is preserved"
            )
    return counts


def _load_config(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"paper account config not found: {path}")
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("paper account config must be a YAML mapping")
    paper = config.get("paper_trade")
    if not isinstance(paper, dict):
        raise ValueError("paper_trade section is missing or invalid")
    balances = paper.get("paper_trade_account_balance")
    if not isinstance(balances, dict):
        raise ValueError("paper_trade_account_balance is missing or invalid")
    return config


def _check_balances(config: dict, target_usd: Decimal) -> Decimal:
    balances = config["paper_trade"]["paper_trade_account_balance"]
    current_usd = Decimal(str(balances.get("USD", "0")))
    if not current_usd.is_finite() or current_usd < 0:
        raise ValueError("existing USD paper balance is invalid")
    non_usd: dict[str, str] = {}
    for asset, raw_amount in balances.items():
        if str(asset).strip().upper() == "USD":
            continue
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation as exc:
            raise ValueError(f"invalid paper balance for {asset}") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"invalid paper balance for {asset}")
        if amount != 0:
            non_usd[str(asset)] = str(amount)
    if non_usd:
        raise RuntimeError(
            "refusing balance migration: non-USD paper holdings exist and must "
            f"be valued/reconciled first: {non_usd}"
        )
    if current_usd > target_usd:
        raise RuntimeError(
            f"refusing to reduce existing USD balance {current_usd} to {target_usd}"
        )
    return current_usd


def _write_config_atomic(path: Path, config: dict) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.before-paper-capital-{timestamp}.bak")
    shutil.copy2(path, backup)
    mode = path.stat().st_mode & 0o777
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.chmod(mode or 0o600)
    os.replace(temporary, path)
    return backup


def migrate(
    *,
    config_path: Path,
    api_env_path: Path,
    api_url: str,
    account_name: str,
    target_usd: Decimal,
    apply: bool,
) -> int:
    if not target_usd.is_finite() or target_usd <= 0:
        raise ValueError("target USD balance must be a positive finite amount")
    env = _read_env(api_env_path)
    username = env.get("USERNAME", "")
    password = env.get("PASSWORD", "")
    if not username or not password:
        raise RuntimeError("USERNAME/PASSWORD are required in the API env file")

    _verify_paper_runtime(
        api_url=api_url, username=username, password=password
    )
    history = _verify_empty_history(
        api_url=api_url,
        username=username,
        password=password,
        account_name=account_name,
    )
    config = _load_config(config_path)
    current_usd = _check_balances(config, target_usd)

    print(
        f"Verified clean paper account {account_name}: "
        f"trades={history['trades']}, orders={history['orders']}, "
        f"active_orders={history['active_orders']}, positions={history['positions']}"
    )
    print(f"Paper USD balance: {current_usd} -> {target_usd}")

    if current_usd == target_usd:
        print("No migration required; existing $25,000 balance is already correct.")
        return 0
    if not apply:
        print("DRY RUN ONLY — no file was changed. Re-run with --apply to migrate.")
        return 0

    config["paper_trade"]["paper_trade_account_balance"]["USD"] = float(target_usd)
    backup = _write_config_atomic(config_path, config)
    print(f"Paper balance updated. Original config preserved at: {backup}")
    return 0


def main() -> int:
    default_env, default_config = _project_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-env", type=Path, default=default_env)
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--account", default="master_account")
    parser.add_argument("--target-usd", type=Decimal, default=TARGET_USD_DEFAULT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the new balance after all paper-only and clean-history checks pass",
    )
    args = parser.parse_args()
    try:
        return migrate(
            config_path=args.config,
            api_env_path=args.api_env,
            api_url=args.api_url,
            account_name=args.account,
            target_usd=args.target_usd,
            apply=args.apply,
        )
    except Exception as exc:
        print(f"PAPER BALANCE MIGRATION REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
