from decimal import Decimal
from types import SimpleNamespace

import pytest

from spectra_core.kraken_market_fabric import (
    collect_kraken_universe,
    normalize_kraken_tickers,
    normalize_kraken_trading_rules,
    summarize_kraken_order_book,
)


D = Decimal


def test_normalize_kraken_tickers_filters_quote_and_derives_quote_volume():
    payload = {
        "tickers": {
            "kraken": {
                "BTC-USD": {
                    "price": 100,
                    "base_volume": 12.5,
                    "quote_volume": None,
                    "timestamp": 123.0,
                },
                "ETH-EUR": {
                    "price": 50,
                    "base_volume": 2,
                    "quote_volume": 100,
                    "timestamp": 124.0,
                },
            }
        },
        "updated_at": {"kraken": 125.0},
    }

    tickers, rejected, updated_at = normalize_kraken_tickers(payload)

    assert tuple(tickers) == ("BTC-USD",)
    assert tickers["BTC-USD"].price == D("100")
    assert tickers["BTC-USD"].quote_volume_24h == D("1250.0")
    assert rejected == {}
    assert updated_at == 125.0


def test_normalize_kraken_tickers_rejects_bad_pair_without_losing_universe():
    payload = {
        "tickers": {
            "kraken": {
                "BTC-USD": {
                    "price": "101.25",
                    "base_volume": "3",
                    "quote_volume": "303.75",
                    "timestamp": 10,
                },
                "BROKEN-USD": {
                    "price": "nan",
                    "base_volume": "2",
                    "quote_volume": "5",
                    "timestamp": 10,
                },
            }
        }
    }

    tickers, rejected, updated_at = normalize_kraken_tickers(payload)

    assert set(tickers) == {"BTC-USD"}
    assert "BROKEN-USD" in rejected
    assert updated_at == 10.0


def test_normalize_kraken_trading_rules_preserves_exchange_minimums():
    rules, rejected = normalize_kraken_trading_rules(
        {
            "BTC-USD": {
                "min_order_size": "0.0001",
                "min_notional_size": "5",
                "min_order_value": "10",
                "min_price_increment": "0.1",
                "min_base_amount_increment": "0.00000001",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
                "supports_market_orders": True,
            }
        }
    )

    rule = rules["BTC-USD"]
    assert rejected == {}
    assert rule.min_order_size == D("0.0001")
    assert rule.effective_min_quote == D("10")
    assert rule.min_price_increment == D("0.1")


def test_summarize_kraken_order_book_computes_microstructure_metrics():
    book = summarize_kraken_order_book(
        "BTC-USD",
        {
            "bids": [
                {"price": "99", "amount": "2"},
                {"price": "98", "amount": "1"},
            ],
            "asks": [
                {"price": "101", "amount": "1"},
                {"price": "102", "amount": "2"},
            ],
            "timestamp": 42,
        },
    )

    assert book.best_bid == D("99")
    assert book.best_ask == D("101")
    assert book.mid_price == D("100")
    assert book.spread_quote == D("2")
    assert book.spread_bps == D("200")
    assert book.bid_depth_quote == D("296")
    assert book.ask_depth_quote == D("305")
    assert book.depth_imbalance == (D("296") - D("305")) / D("601")
    assert book.microprice == D("301") / D("3")
    assert book.microprice > book.mid_price
    assert book.microprice_edge_bps > D("0")


def test_summarize_kraken_order_book_rejects_crossed_book():
    with pytest.raises(ValueError, match="crossed or locked"):
        summarize_kraken_order_book(
            "BTC-USD",
            {
                "bids": [[101, 1]],
                "asks": [[100, 1]],
            },
        )


class FakeMarketData:
    def __init__(self):
        self.order_book_calls = []

    async def get_tickers(self, connector):
        assert connector == "kraken"
        return {
            "tickers": {
                "kraken": {
                    "BTC-USD": {
                        "price": 100,
                        "base_volume": 50,
                        "quote_volume": 5000,
                        "timestamp": 500,
                    },
                    "DOGE-USD": {
                        "price": "0.2",
                        "base_volume": 100000,
                        "quote_volume": 20000,
                        "timestamp": 500,
                    },
                    "ETH-USD": {
                        "price": 50,
                        "base_volume": 40,
                        "quote_volume": 2000,
                        "timestamp": 500,
                    },
                }
            },
            "updated_at": {"kraken": 501},
        }

    async def get_order_book(self, **kwargs):
        self.order_book_calls.append(kwargs)
        return {
            "trading_pair": kwargs["trading_pair"],
            "bids": [[99, 2]],
            "asks": [[101, 2]],
            "timestamp": 502,
        }


class FakeConnectors:
    async def get_trading_rules(self, connector):
        assert connector == "kraken"
        common = {
            "min_order_size": "1",
            "min_notional_size": "1",
            "min_order_value": "1",
            "min_price_increment": "0.01",
            "min_base_amount_increment": "0.0001",
            "min_quote_amount_increment": "0.01",
            "supports_limit_orders": True,
            "supports_market_orders": True,
        }
        return {
            "BTC-USD": common,
            "DOGE-USD": common,
            # ETH intentionally absent: ticker-only pairs are fail-closed.
        }


@pytest.mark.asyncio
async def test_collect_kraken_universe_requires_live_ticker_and_rule():
    market_data = FakeMarketData()
    client = SimpleNamespace(
        market_data=market_data,
        connectors=FakeConnectors(),
    )

    snapshot = await collect_kraken_universe(
        client,
        order_book_pairs=("BTC-USD", "ETH-USD"),
        order_book_depth=5,
    )

    assert snapshot.eligible_pairs == ("BTC-USD", "DOGE-USD")
    assert snapshot.updated_at == 501.0
    assert "ETH-USD" in snapshot.rejected
    assert snapshot.rejected["ETH-USD"] == "missing Kraken trading rule"
    assert set(snapshot.books) == {"BTC-USD"}
    assert market_data.order_book_calls == [
        {
            "connector_name": "kraken",
            "trading_pair": "BTC-USD",
            "depth": 5,
        }
    ]


@pytest.mark.asyncio
async def test_collect_kraken_universe_rejects_nonpositive_depth():
    client = SimpleNamespace(
        market_data=FakeMarketData(),
        connectors=FakeConnectors(),
    )

    with pytest.raises(ValueError, match="order_book_depth"):
        await collect_kraken_universe(client, order_book_depth=0)
