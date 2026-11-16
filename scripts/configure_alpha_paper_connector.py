#!/usr/bin/env python3
"""Configure the local Hummingbot account for Kraken paper trading only."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

PAPER_INITIAL_USD_BALANCE = 25000.0


def configure(path: Path) -> None:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    paper = config.setdefault("paper_trade", {})
    exchanges = paper.setdefault("paper_trade_exchanges", [])
    if not isinstance(exchanges, list):
        raise ValueError("paper_trade_exchanges must be a YAML list")
    if "kraken" not in exchanges:
        exchanges.append("kraken")

    balances = paper.setdefault("paper_trade_account_balance", {})
    if not isinstance(balances, dict):
        raise ValueError("paper_trade_account_balance must be a YAML mapping")
    # Seed only missing assets. Re-running setup must never reset an existing
    # simulated balance or erase paper-trading progress.
    defaults = {
        "USD": PAPER_INITIAL_USD_BALANCE,
        "USDT": 0.0,
        "BTC": 0.0,
        "ETH": 0.0,
        "XRP": 0.0,
    }
    for asset, amount in defaults.items():
        balances.setdefault(asset, amount)

    limits = config.setdefault("balance_asset_limit", {})
    limits.setdefault("kraken_paper_trade", {})
    mode = path.stat().st_mode & 0o777
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.chmod(mode or 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if not args.config.is_file():
        raise SystemExit(f"Account config does not exist: {args.config}")
    configure(args.config)
    print("Configured Kraken paper connector with simulated balances.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
