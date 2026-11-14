#!/usr/bin/env python3
"""Configure Kraken paper trading without overwriting an existing paper account.

Fresh installs may explicitly initialize a 25,000 USD-only simulated account.
Upgrades preserve every existing simulated balance unless the operator asks
for an intentional reset after reconciling open positions.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml


DEFAULT_INITIAL_BALANCE = 25_000.0


def configure(
    path: Path,
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    reset_balances: bool = False,
) -> None:
    if initial_balance <= 0:
        raise ValueError("initial_balance must be positive")
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    paper = config.setdefault("paper_trade", {})
    exchanges = paper.setdefault("paper_trade_exchanges", [])
    if not isinstance(exchanges, list):
        raise ValueError("paper_trade_exchanges must be a list")
    if "kraken" not in exchanges:
        exchanges.append("kraken")

    balances = paper.setdefault("paper_trade_account_balance", {})
    if not isinstance(balances, dict):
        raise ValueError("paper_trade_account_balance must be a mapping")
    if reset_balances:
        # Exact starting equity: no inherited template BTC/USDT/USDC balances.
        paper["paper_trade_account_balance"] = {"USD": float(initial_balance)}
    elif not balances:
        # A non-resetting upgrade only seeds a genuinely empty account.
        paper["paper_trade_account_balance"] = {"USD": float(initial_balance)}

    limits = config.setdefault("balance_asset_limit", {})
    limits.setdefault("kraken_paper_trade", {})
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--initial-balance", type=float, default=DEFAULT_INITIAL_BALANCE)
    parser.add_argument(
        "--reset-balances",
        action="store_true",
        help="Set the simulated account to exactly the configured USD starting balance.",
    )
    args = parser.parse_args()
    if not args.config.is_file():
        raise SystemExit(f"Account config does not exist: {args.config}")
    configure(
        args.config,
        initial_balance=args.initial_balance,
        reset_balances=args.reset_balances,
    )
    if args.reset_balances:
        message = "Initialized USD simulated equity: " + format(args.initial_balance, ",.2f")
    else:
        message = "Existing simulated balances preserved."
    print("Configured Kraken paper connector; " + message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
