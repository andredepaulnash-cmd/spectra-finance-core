#!/usr/bin/env python3
"""Patch the reviewed Hummingbot trading service for PaperTradeExchange.

The Cython PaperTradeExchange exposes its pair registry as the public
`trading_pair` mapping; its private `_trading_pairs` member is not a
Python-visible list. The updater verifies the upstream blob before invoking
this exact-block patcher. Unknown source drift fails closed.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "SPECTRA_PAPER_TRADE_PAIR_MAP_COMPAT"

OLD_BLOCK = """        if trading_pair not in connector._trading_pairs:
            connector._trading_pairs.append(trading_pair)
            logger.debug(f"Registered {trading_pair} with connector {type(connector).__name__}")
"""

NEW_BLOCK = """        # SPECTRA_PAPER_TRADE_PAIR_MAP_COMPAT:
        # PaperTradeExchange is a Cython extension. Its _trading_pairs member
        # is not Python-visible; use its public trading_pair mapping instead.
        if type(connector).__name__ == "PaperTradeExchange":
            paper_pair_map = getattr(connector, "trading_pair", None)
            if not isinstance(paper_pair_map, dict):
                raise TypeError("PaperTradeExchange did not expose its trading_pair mapping")
            if trading_pair not in paper_pair_map:
                from hummingbot.connector.exchange.paper_trade.trading_pair import TradingPair
                base_asset, quote_asset = connector.split_trading_pair(trading_pair)
                paper_pair_map[trading_pair] = TradingPair(trading_pair, base_asset, quote_asset)
            logger.debug(f"Registered {trading_pair} with paper connector {type(connector).__name__}")
            return

        trading_pairs = getattr(connector, "_trading_pairs", None)
        if isinstance(trading_pairs, list):
            if trading_pair not in trading_pairs:
                trading_pairs.append(trading_pair)
            logger.debug(f"Registered {trading_pair} with connector {type(connector).__name__}")
            return

        raise TypeError(
            f"Connector {type(connector).__name__} does not expose a supported "
            "trading-pair registration interface"
        )
"""


def patch_source(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(OLD_BLOCK) != 1:
        raise RuntimeError("Expected exactly one reviewed trading-pair registration block")
    patched = source.replace(OLD_BLOCK, NEW_BLOCK, 1)
    if MARKER not in patched:
        raise RuntimeError("PaperTradeExchange compatibility marker missing after patch")
    return patched


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch_hummingbot_trading_service_compat.py PATH", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    original = path.read_text(encoding="utf-8")
    patched = patch_source(original)
    if patched != original:
        path.write_text(patched, encoding="utf-8")
        print(f"Applied PaperTradeExchange trading-service compatibility to {path}")
    else:
        print(f"PaperTradeExchange trading-service compatibility already present in {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"paper-trade trading-service compatibility: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
