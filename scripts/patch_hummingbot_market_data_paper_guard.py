#!/usr/bin/env python3
"""Fail-closed paper order-book guard for Hummingbot API.

A REST snapshot is not proof that the shared WebSocket order-book tracker used
by PaperTradeExchange is running. This exact-block patch makes paper order-book
reads fail closed unless the public connector and shared tracker are ready.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "SPECTRA_PAPER_ORDER_BOOK_TRACKER_REQUIRED"
START = "            # A read request must be able to initialize its own feed."
END = "            # Fallback to getting fresh order book from data source"

REPLACEMENT = '''            # SPECTRA_PAPER_ORDER_BOOK_TRACKER_REQUIRED:
            # A REST snapshot is not sufficient for PaperTradeExchange: simulated
            # fills consume the shared public WebSocket order-book tracker.
            is_paper_trade = connector_name.endswith("_paper_trade")
            tracker = getattr(connector, "order_book_tracker", None)
            tracked_books = getattr(tracker, "order_books", {}) if tracker else {}
            if trading_pair not in tracked_books:
                initialized = await self.initialize_order_book(
                    connector_name=connector_name,
                    trading_pair=trading_pair,
                    account_name=account_name,
                )
                if not initialized and is_paper_trade:
                    return {
                        "error": (
                            f"Paper order-book tracker failed to initialize for "
                            f"{connector_name}/{trading_pair}; REST fallback is not "
                            "valid evidence for simulated execution"
                        )
                    }
                if initialized:
                    connector = self._connector_service.get_best_connector_for_market(
                        connector_name, account_name
                    )
                    if not connector:
                        return {"error": f"No connector available for {connector_name} after order book initialization"}
                    tracker = getattr(connector, "order_book_tracker", None)
                    tracked_books = getattr(tracker, "order_books", {}) if tracker else {}

            if is_paper_trade:
                if tracker is None or not getattr(tracker, "ready", False):
                    return {
                        "error": (
                            f"Paper order-book tracker is not ready for "
                            f"{connector_name}/{trading_pair}"
                        )
                    }
                order_book = tracked_books.get(trading_pair)
                if order_book is None:
                    return {
                        "error": (
                            f"Paper order-book tracker has no exact pair "
                            f"{connector_name}/{trading_pair}"
                        )
                    }
                try:
                    snapshot = order_book.snapshot
                    bids = snapshot[0].head(depth)[["price", "amount"]].values.tolist()
                    asks = snapshot[1].head(depth)[["price", "amount"]].values.tolist()
                except Exception as exc:
                    return {
                        "error": (
                            f"Paper order-book tracker snapshot is unavailable for "
                            f"{connector_name}/{trading_pair}: {type(exc).__name__}"
                        )
                    }
                if not bids or not asks:
                    return {
                        "error": (
                            f"Paper order-book tracker has an empty side for "
                            f"{connector_name}/{trading_pair}"
                        )
                    }
                return {
                    "trading_pair": trading_pair,
                    "bids": bids,
                    "asks": asks,
                    "timestamp": time.time(),
                }

'''
def patch_source(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(START) != 1 or source.count(END) != 1:
        raise RuntimeError(
            "market_data_service.py does not match the exact reviewed order-book block; refusing patch"
        )
    start = source.index(START)
    end = source.index(END, start)
    if end <= start:
        raise RuntimeError("order-book guard block boundaries are invalid")
    old = source[start:end]
    required = (
        "await self.initialize_order_book(",
        "if hasattr(connector, '_orderbook_ds') and connector._orderbook_ds:",
        'return {"error": f"Order book not available',
    )
    if not all(token in source for token in required):
        raise RuntimeError("market-data service is missing expected fail-closed anchors")
    return source[:start] + REPLACEMENT + source[end:]


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: patch_hummingbot_market_data_paper_guard.py <market_data_service.py>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source)
    if patched != source:
        path.write_text(patched, encoding="utf-8")
    print(f"Applied paper order-book tracker guard to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
