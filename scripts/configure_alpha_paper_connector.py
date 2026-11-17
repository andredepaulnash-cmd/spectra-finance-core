#!/usr/bin/env python3
"""Configure the Hummingbot master account for the keyless Kraken paper connector."""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def configure(path: Path) -> None:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config["paper_trade"] = {
        "paper_trade_exchanges": ["kraken"],
        "paper_trade_account_balance": {"USDT": 25000.0},
    }
    config["balance_asset_limit"] = {"kraken_paper_trade": {}}
    config.setdefault("global_token", {})["global_token_name"] = "USDT"
    config["global_token"]["global_token_symbol"] = "$"
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
    print("Configured only kraken_paper_trade with a simulated 25,000 USDT balance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
