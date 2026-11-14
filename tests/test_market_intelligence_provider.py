from __future__ import annotations

import asyncio
import time

import pytest

from condor.agents.providers import market_intelligence as market_module
from condor.agents.providers.base import ProviderResult
from condor.agents.providers.market_intelligence import MarketIntelligenceProvider


@pytest.fixture(autouse=True)
def clear_market_intelligence_cache():
    with market_module._MARKET_CACHE_GUARD:
        market_module._MARKET_CACHE.clear()
        market_module._MARKET_CACHE_LOCKS.clear()
    yield


class FakeMarketData:
    def __init__(
        self,
        *,
        stale: bool = False,
        fail_book: bool = False,
        omit_book_timestamp: bool = False,
        future: bool = False,
        invalid_candles: bool = False,
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
        self.calls = {"tickers": 0, "candles": 0, "order_book": 0}

    async def get_tickers(self):
        self.calls["tickers"] += 1
        return self.tickers

    async def get_candles(self, connector_name, trading_pair, interval, max_records):
        self.calls["candles"] += 1
        return [
            [
                self.stamp - (19 - index) * 60,
                98 + index * 0.1,
                101 + index * 0.1,
                97 + index * 0.1,
                0 if invalid_candles else 99 + index * 0.1,
                10 + index,
            ]
            for index in range(20)
        ]

    async def get_order_book(self, connector_name, trading_pair, depth=20):
        self.calls["order_book"] += 1
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
        self.base_url = "http://paper-api"
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



@pytest.mark.asyncio
async def test_market_snapshot_is_shared_across_agent_ticks_and_copied_per_caller():
    provider = MarketIntelligenceProvider()
    client = FakeClient()
    config = {
        "server_name": "local",
        "market_connector_name": "kraken_paper_trade",
        "market_trading_pair": "BTC-USD",
        "market_candle_interval": "1m",
        "market_candle_records": 120,
        "market_data_max_age_seconds": 180,
        "market_order_book_depth": 20,
        "market_data_cache_ttl_seconds": 10,
    }

    first, second = await asyncio.gather(
        provider.execute(client, config, agent_id="spectra-grid"),
        provider.execute(client, config, agent_id="spectra-momentum"),
    )

    assert first.data["status"] == second.data["status"] == "FRESH"
    assert client.market_data.calls == {"tickers": 1, "candles": 1, "order_book": 1}
    second.data["price"] = -1
    third = await provider.execute(client, config, agent_id="spectra-research")
    assert third.data["price"] == 100.0


@pytest.mark.asyncio
async def test_market_snapshot_cache_is_scoped_to_market_and_ttl():
    provider = MarketIntelligenceProvider()
    client = FakeClient()
    base = {
        "server_name": "local",
        "market_connector_name": "kraken_paper_trade",
        "market_trading_pair": "BTC-USD",
        "market_data_max_age_seconds": 180,
    }
    await provider.execute(client, base, agent_id="agent-a")
    await provider.execute(client, {**base, "market_trading_pair": "ETH-USD"}, agent_id="agent-b")
    await provider.execute(client, {**base, "market_data_cache_ttl_seconds": 2}, agent_id="agent-c")
    assert client.market_data.calls == {"tickers": 3, "candles": 3, "order_book": 3}


def test_cached_market_snapshot_recomputes_age_and_rejects_future_timestamps():
    now = time.time()
    future = now + 3600
    result = ProviderResult(
        name="market_intelligence",
        data={
            "connector": "kraken_paper_trade",
            "trading_pair": "BTC-USD",
            "max_age_seconds": 180,
            "ticker_updated_at": future,
            "candle_updated_at": future,
            "order_book_source_timestamp": future,
            "price": 100.0,
            "candle_count": 20,
            "best_bid": 99.0,
            "best_ask": 101.0,
            "volume_24h_usd": 1000.0,
            "candle_interval": "1m",
            "sma20": 100.0,
            "rsi14": 50.0,
            "spread_bps": 200.0,
            "ticker_age_seconds": 0.0,
            "candle_age_seconds": 0.0,
            "order_book_age_seconds": 0.0,
            "errors": {},
        },
        summary="",
    )

    refreshed = market_module._refresh_cached_result(result)

    assert refreshed.data["status"] == "DEGRADED"
    assert refreshed.data["decision_grade"] is False
    assert set(refreshed.data["future_sources"]) == {"ticker", "candles", "order_book"}


@pytest.mark.asyncio
async def test_cached_market_snapshot_summary_uses_real_line_breaks():
    provider = MarketIntelligenceProvider()
    client = FakeClient()
    config = {
        "market_connector_name": "kraken_paper_trade",
        "market_trading_pair": "BTC-USD",
        "market_data_max_age_seconds": 180,
        "market_data_cache_ttl_seconds": 10,
    }

    await provider.execute(client, config)
    cached = await provider.execute(client, config)

    assert "\n" in cached.summary
    assert "\\n" not in cached.summary


@pytest.mark.asyncio
async def test_cache_pruning_keeps_lock_registered_for_waiting_requests():
    key_a = ("market-a",)
    key_b = ("market-b",)
    expired = ProviderResult(
        name="market_intelligence",
        data={"status": "DEGRADED", "errors": {}},
        summary="",
    )
    with market_module._MARKET_CACHE_GUARD:
        market_module._MARKET_CACHE[key_a] = (time.monotonic() - 1, expired)

    loader_entered = asyncio.Event()
    release_loader = asyncio.Event()
    calls = 0

    async def slow_loader():
        nonlocal calls
        calls += 1
        loader_entered.set()
        await release_loader.wait()
        return ProviderResult(
            name="market_intelligence",
            data={"status": "DEGRADED", "errors": {}},
            summary="",
        )

    async def fast_loader():
        return ProviderResult(
            name="market_intelligence",
            data={"status": "DEGRADED", "errors": {}},
            summary="",
        )

    first = asyncio.create_task(market_module._shared_snapshot(key_a, 1, slow_loader))
    await loader_entered.wait()
    second = asyncio.create_task(market_module._shared_snapshot(key_a, 1, slow_loader))
    await asyncio.sleep(0)

    # Inserting another key prunes the expired A entry while A has a holder
    # and a waiter. Both must continue to share the original lock.
    await market_module._shared_snapshot(key_b, 1, fast_loader)
    third = asyncio.create_task(market_module._shared_snapshot(key_a, 1, slow_loader))
    await asyncio.sleep(0)

    release_loader.set()
    await asyncio.gather(first, second, third)

    assert calls == 1


@pytest.mark.asyncio
async def test_nonpositive_candle_closes_degrade_market_snapshot():
    provider = MarketIntelligenceProvider()
    result = await provider.execute(
        FakeClient(invalid_candles=True),
        {
            "market_connector_name": "kraken_paper_trade",
            "market_trading_pair": "BTC-USD",
            "market_data_max_age_seconds": 180,
        },
    )

    assert result.data["status"] == "DEGRADED"
    assert result.data["decision_grade"] is False
    assert result.data["sma20"] is None
    assert "NO-TRADE DATA GUARD" in result.summary
