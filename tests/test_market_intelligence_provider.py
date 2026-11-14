from __future__ import annotations

import time

import pytest

from condor.agents.providers.market_intelligence import MarketIntelligenceProvider


class FakeMarketData:
    def __init__(
        self,
        *,
        stale: bool = False,
        fail_book: bool = False,
        omit_book_timestamp: bool = False,
        future: bool = False,
    ):
        now = time.time()
        stamp = now - 600 if stale else now + 3600 if future else now
        self.tickers = {
            "tickers": {
                "kraken_paper_trade": {
                    "BTC-USD": {
                        "price": 100.0,
                        "base_volume": 10.0,
                        "quote_volume": 1000.0,
                        "timestamp": stamp,
                    }
                }
            },
            "updated_at": {"kraken_paper_trade": stamp},
        }
        self.stamp = stamp
        self.fail_book = fail_book
        self.omit_book_timestamp = omit_book_timestamp

    async def get_tickers(self):
        return self.tickers

    async def get_candles(self, connector_name, trading_pair, interval, max_records):
        return [
            [
                self.stamp - (19 - index) * 60,
                98 + index * 0.1,
                101 + index * 0.1,
                97 + index * 0.1,
                99 + index * 0.1,
                10 + index,
            ]
            for index in range(20)
        ]

    async def get_order_book(self, connector_name, trading_pair, depth=20):
        if self.fail_book:
            raise RuntimeError("order-book feed unavailable")
        result = {
            "bids": [[99.0, 2.0], [98.0, 3.0]],
            "asks": [[101.0, 1.5], [102.0, 2.5]],
        }
        if not self.omit_book_timestamp:
            result["timestamp"] = self.stamp
        return result


class FakeClient:
    def __init__(self, **kwargs):
        self.market_data = FakeMarketData(**kwargs)


@pytest.mark.asyncio
async def test_market_intelligence_provider_returns_fresh_snapshot():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(
        FakeClient(),
        {
            "market_connector_name": "kraken_paper_trade",
            "market_trading_pair": "BTC-USD",
            "market_candle_interval": "1m",
            "market_candle_records": 120,
            "market_data_max_age_seconds": 180,
            "market_order_book_depth": 20,
        },
    )

    assert result.data["status"] == "FRESH"
    assert result.data["decision_grade"] is True
    assert result.data["price"] == 100.0
    assert result.data["best_bid"] == 99.0
    assert result.data["best_ask"] == 101.0
    assert result.data["spread_bps"] == pytest.approx(200.0)
    assert result.data["bid_depth_quote"] == pytest.approx(492.0)
    assert result.data["ask_depth_quote"] == pytest.approx(406.5)
    assert result.data["sma20"] is not None
    assert "NO-TRADE DATA GUARD" not in result.summary


@pytest.mark.asyncio
async def test_stale_market_inputs_are_never_decision_grade():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(
        FakeClient(stale=True),
        {
            "market_connector_name": "kraken_paper_trade",
            "market_trading_pair": "BTC-USD",
            "market_data_max_age_seconds": 180,
        },
    )

    assert result.data["status"] == "STALE"
    assert result.data["decision_grade"] is False
    assert {"ticker", "candles", "order_book"}.issubset(result.data["stale_sources"])
    assert "NO-TRADE DATA GUARD" in result.summary


@pytest.mark.asyncio
async def test_missing_order_book_degrades_snapshot_and_blocks_decision():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(
        FakeClient(fail_book=True),
        {
            "market_connector_name": "kraken_paper_trade",
            "market_trading_pair": "BTC-USD",
        },
    )

    assert result.data["status"] == "DEGRADED"
    assert result.data["decision_grade"] is False
    assert "order_book" in result.data["errors"]
    assert "NO-TRADE DATA GUARD" in result.summary


@pytest.mark.asyncio
async def test_market_intelligence_is_explicitly_disabled_without_market_config():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(FakeClient(), {})

    assert result.data["configured"] is False
    assert result.data["status"] == "NOT_CONFIGURED"
    assert result.data["decision_grade"] is False
    assert "do not infer a market" in result.summary

@pytest.mark.asyncio
async def test_missing_order_book_source_timestamp_is_not_decision_grade():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(
        FakeClient(omit_book_timestamp=True),
        {
            "market_connector_name": "kraken_paper_trade",
            "market_trading_pair": "BTC-USD",
            "market_data_max_age_seconds": 180,
        },
    )

    assert result.data["status"] == "DEGRADED"
    assert result.data["decision_grade"] is False
    assert "order_book" in result.data["missing_timestamps"]
    assert result.data["order_book_timestamp_basis"] == "retrieved_at"


@pytest.mark.asyncio
async def test_future_market_timestamps_are_not_treated_as_fresh():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(
        FakeClient(future=True),
        {
            "market_connector_name": "kraken_paper_trade",
            "market_trading_pair": "BTC-USD",
            "market_data_max_age_seconds": 180,
        },
    )

    assert result.data["status"] == "DEGRADED"
    assert result.data["decision_grade"] is False
    assert {"ticker", "candles", "order_book"}.issubset(result.data["future_sources"])
