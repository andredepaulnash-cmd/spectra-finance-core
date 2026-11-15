#!/usr/bin/env python3
"""Apply the narrow PaperTradeExchange pair-map compatibility adapter.

The Hummingbot connector is a Cython extension: its private _trading_pairs
member is not Python-visible. The supported public trading_pair property is a
mapping of exchange pair names to TradingPair records. This script changes only
the two reviewed pair-registration/cleanup sites and fails closed on drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "SPECTRA_PAPER_TRADE_DATA_CONNECTOR_PAIR_MAP_COMPAT"
PURGE_MARKER = "SPECTRA_PAPER_TRADE_DATA_CONNECTOR_PAIR_MAP_CLEANUP_COMPAT"

OLD_REGISTER = """            if trading_pair not in connector._trading_pairs:
                connector._trading_pairs.append(trading_pair)
"""
NEW_REGISTER = """            # SPECTRA_PAPER_TRADE_DATA_CONNECTOR_PAIR_MAP_COMPAT:
            # PaperTradeExchange exposes its Cython cdef dict through the
            # public trading_pair property; _trading_pairs is not Python-visible.
            if type(connector).__name__ == "PaperTradeExchange":
                paper_pair_map = getattr(connector, "trading_pair", None)
                if not isinstance(paper_pair_map, dict):
                    raise TypeError("PaperTradeExchange did not expose its trading_pair mapping")
                if trading_pair not in paper_pair_map:
                    from hummingbot.connector.exchange.paper_trade.trading_pair import TradingPair
                    base_asset, quote_asset = connector.split_trading_pair(trading_pair)
                    paper_pair_map[trading_pair] = TradingPair(trading_pair, base_asset, quote_asset)
            else:
                trading_pairs = getattr(connector, "_trading_pairs", None)
                if not isinstance(trading_pairs, list):
                    raise TypeError(f"Connector {type(connector).__name__} has no supported trading-pair list")
                if trading_pair not in trading_pairs:
                    trading_pairs.append(trading_pair)
"""
OLD_CLEANUP = '        targets = [getattr(connector, "_trading_pairs", None)]\n'
NEW_CLEANUP = """        # SPECTRA_PAPER_TRADE_DATA_CONNECTOR_PAIR_MAP_CLEANUP_COMPAT:
        # Roll back paper connector registration through its public mapping.
        if type(connector).__name__ == "PaperTradeExchange":
            paper_pair_map = getattr(connector, "trading_pair", None)
            if isinstance(paper_pair_map, dict):
                paper_pair_map.pop(trading_pair, None)
        targets = [getattr(connector, "_trading_pairs", None)]
"""


def patch_source(source: str) -> str:
    if MARKER not in source:
        if source.count(OLD_REGISTER) != 1:
            raise RuntimeError("Expected exactly one reviewed data-connector pair-registration block")
        source = source.replace(OLD_REGISTER, NEW_REGISTER, 1)
    if PURGE_MARKER not in source:
        if source.count(OLD_CLEANUP) != 1:
            raise RuntimeError("Expected exactly one reviewed connector pair-cleanup block")
        source = source.replace(OLD_CLEANUP, NEW_CLEANUP, 1)
    if MARKER not in source or PURGE_MARKER not in source:
        raise RuntimeError("PaperTradeExchange pair-map compatibility markers are incomplete")
    return source


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch_hummingbot_paper_trade_compat.py PATH", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    original = path.read_text(encoding="utf-8")
    patched = patch_source(original)
    if patched != original:
        path.write_text(patched, encoding="utf-8")
        print(f"Applied PaperTradeExchange pair-map compatibility to {path}")
    else:
        print(f"PaperTradeExchange pair-map compatibility already present in {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"paper-trade compatibility patch: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
