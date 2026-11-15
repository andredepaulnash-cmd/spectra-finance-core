#!/usr/bin/env python3
"""Configure Kraken paper trading without overwriting an existing paper account.

Fresh installs may initialize a 25,000 USD-only simulated account. Upgrades
preserve existing balances, except the exact untouched legacy demo template
may be upgraded when no paper position history exists.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

try:
    from .configure_alpha_paper_state import has_paper_position_history
except ImportError:
    from configure_alpha_paper_state import has_paper_position_history


DEFAULT_INITIAL_BALANCE = 25_000.0
LEGACY_ALPHA_BALANCES = {
    "BTC": 0.0,
    "USDT": 0.0,
    "ONE": 1000.0,
    "USDQ": 1000.0,
    "TUSD": 1000.0,
    "ETH": 0.0,
    "WETH": 10.0,
    "USDC": 1000.0,
    "DAI": 1000.0,
    "USD": 1000.0,
    "XRP": 0.0,
}


def configure(
    path: Path,
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    reset_balances: bool = False,
    core_root: Path | None = None,
    upgrade_legacy_default: bool = False,
) -> None:
    if initial_balance <= 0:
        raise ValueError("initial_balance must be positive")
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    paper = config.setdefault("paper_trade", {})
    exchanges = paper.setdefault("paper_trade_exchanges", [])
    legacy_kraken_only = exchanges == ["kraken"]
    if not isinstance(exchanges, list):
        raise ValueError("paper_trade_exchanges must be a list")
    if "kraken" not in exchanges:
        exchanges.append("kraken")

    balances = paper.setdefault("paper_trade_account_balance", {})
    if not isinstance(balances, dict):
        raise ValueError("paper_trade_account_balance must be a mapping")
    if reset_balances or not balances:
        paper["paper_trade_account_balance"] = {"USD": float(initial_balance)}
    elif (
        upgrade_legacy_default
        and core_root is not None
        and legacy_kraken_only
        and balances == LEGACY_ALPHA_BALANCES
        and not has_paper_position_history(core_root)
    ):
        # Migrate only the exact untouched shipped demo balance. Any trade
        # history, custom balance, or unreadable ledger preserves current state.
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
    parser.add_argument("--core-root", type=Path)
    parser.add_argument(
        "--reset-balances",
        action="store_true",
        help="Set the simulated account to exactly the configured USD starting balance.",
    )
    parser.add_argument(
        "--upgrade-legacy-default",
        action="store_true",
        help="Upgrade only the exact untouched legacy demo balance when no position history exists.",
    )
    args = parser.parse_args()
    if not args.config.is_file():
        raise SystemExit(f"Account config does not exist: {args.config}")
    configure(
        args.config,
        initial_balance=args.initial_balance,
        reset_balances=args.reset_balances,
        core_root=None if args.core_root is None else args.core_root.expanduser().resolve(),
        upgrade_legacy_default=args.upgrade_legacy_default,
    )
    if args.reset_balances:
        message = "Initialized USD simulated equity: " + format(args.initial_balance, ",.2f")
    elif args.upgrade_legacy_default and args.core_root is not None:
        message = "Safe legacy-balance migration evaluated; existing traded/custom state preserved."
    else:
        message = "Existing simulated balances preserved."
    print("Configured Kraken paper connector; " + message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
