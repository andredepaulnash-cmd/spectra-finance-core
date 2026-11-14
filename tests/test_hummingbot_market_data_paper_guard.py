from __future__ import annotations

import pytest

from scripts.patch_hummingbot_market_data_paper_guard import (
    MARKER,
    patch_source,
)


def source_fixture() -> str:
    return '''    async def get_order_book_data(self, connector_name, trading_pair, depth=10, account_name=None):
        try:
            connector = self._connector_service.get_best_connector_for_market(
                connector_name, account_name
            )
            if not connector:
                return {"error": f"No connector available for {connector_name}"}
            # A read request must be able to initialize its own feed. In particular,
            # paper-trade connectors share the base exchange's public order-book tracker,
            # which is intentionally lazy and may not contain this pair yet.
            tracker = getattr(connector, "order_book_tracker", None)
            tracked_books = getattr(tracker, "order_books", {}) if tracker else {}
            if trading_pair not in tracked_books:
                initialized = await self.initialize_order_book(
                    connector_name=connector_name,
                    trading_pair=trading_pair,
                    account_name=account_name,
                )
                if initialized:
                    connector = self._connector_service.get_best_connector_for_market(
                        connector_name, account_name
                    )
                    if not connector:
                        return {"error": f"No connector available for {connector_name} after order book initialization"}
            # Try to get from existing order book tracker
            if hasattr(connector, 'order_book_tracker') and connector.order_book_tracker:
                tracker = connector.order_book_tracker
                if trading_pair in tracker.order_books:
                    order_book = tracker.order_books[trading_pair]
                    snapshot = order_book.snapshot
            # Fallback to getting fresh order book from data source
            if hasattr(connector, '_orderbook_ds') and connector._orderbook_ds:
                orderbook_ds = connector._orderbook_ds
                order_book = await orderbook_ds.get_new_order_book(trading_pair)
                snapshot = order_book.snapshot
            return {"error": f"Order book not available for {connector_name}/{trading_pair}"}
        except Exception as e:
            return {"error": str(e)}
'''


def test_paper_market_data_requires_shared_tracker_and_has_no_rest_fallback():
    patched = patch_source(source_fixture())
    assert MARKER in patched
    assert 'is_paper_trade = connector_name.endswith("_paper_trade")' in patched
    assert 'getattr(tracker, "ready", False)' in patched
    assert 'tracked_books.get(trading_pair)' in patched
    guard = patched[patched.index(MARKER):patched.index("# Fallback to getting fresh order book from data source")]
    assert "REST fallback is not" in guard
    assert "if is_paper_trade:" in guard


def test_patch_is_idempotent():
    once = patch_source(source_fixture())
    assert patch_source(once) == once


def test_patch_fails_closed_on_unknown_upstream_source():
    with pytest.raises(RuntimeError, match="exact reviewed order-book block"):
        patch_source("async def get_order_book_data(self):\n    return {}\n")
