#!/usr/bin/env python3
"""Configure the local Hummingbot account for Kraken paper trading only."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def configure(path: Path) -> None:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    paper = config.setdefault("paper_trade", {})
    paper["paper_trade_exchanges"] = ["kraken"]
    balances = paper.setdefault("paper_trade_account_balance", {})
    balances.update(
        {
            "USD": 10000.0,
            "USDT": 10000.0,
            "BTC": 1.0,
            "ETH": 20.0,
            "XRP": 10000.0,
        }
    )
    limits = config.setdefault("balance_asset_limit", {})
    limits.setdefault("kraken_paper_trade", {})
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


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
