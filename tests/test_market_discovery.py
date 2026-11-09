from __future__ import annotations

import pytest

from spectra_core.market_discovery import analyze_daily_history, scan_kraken_universe


def candles(*, start=100.0, drift=0.0, count=120):
    rows = []
    price = start
    for index in range(count):
        price *= 1 + drift
        rows.append([
            index * 86_400_000,
            str(price * 0.99),
            str(price * 1.02),
            str(price * 0.98),
            str(price),
            "1000",
            (index + 1) * 86_400_000 - 1,
        ])
    return rows


def test_analyzer_returns_transparent_mean_reversion_metrics():
    history = candles(drift=0.001)
    result = analyze_daily_history(
        symbol="TESTUSDT",
        base_asset="TEST",
        current_price=float(history[-1][4]) * 0.95,
        volume_24h_usd=25_000_000,
        change_24h_pct=1.2,
        candles=history,
    )
    assert result["trading_pair"] == "TEST-USDT"
    assert result["history_days"] == 90
    assert result["volume_24h_usd"] == 25_000_000
    assert result["vwap_90d"] > 0
    assert 0 <= result["rsi_14d"] <= 100
    assert 0 <= result["selection_score"] <= 100
    assert isinstance(result["selection_reasons"], list)


def test_analyzer_rejects_persistent_downtrend_near_90d_low():
    history = candles(start=200.0, drift=-0.012)
    result = analyze_daily_history(
        symbol="FALLUSDT",
        base_asset="FALL",
        current_price=float(history[-1][4]) * 0.97,
        volume_24h_usd=50_000_000,
        change_24h_pct=-3.0,
        candles=history,
    )
    assert result["eligible"] is False
    assert any("persistent_downtrend" in reason for reason in result["rejection_reasons"])


def test_analyzer_rejects_insufficient_history():
    with pytest.raises(ValueError, match="insufficient_history"):
        analyze_daily_history(
            symbol="SHORTUSDT",
            base_asset="SHORT",
            current_price=10,
            volume_24h_usd=20_000_000,
            change_24h_pct=0,
            candles=candles(count=89),
        )


def test_analyzer_rejects_invalid_price():
    with pytest.raises(ValueError, match="invalid_current_price"):
        analyze_daily_history(
            symbol="BADUSDT",
            base_asset="BAD",
            current_price=0,
            volume_24h_usd=20_000_000,
            change_24h_pct=0,
            candles=candles(),
        )


class FakeKrakenMarketData:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    async def get_tickers(self, connector):
        assert connector == "kraken"
        return {
            "tickers": {
                "kraken": {
                    "BTC-USD": {
                        "price": 100,
                        "base_volume": 200_000,
                        "quote_volume": 20_000_000,
                        "timestamp": 1_800_000_000,
                    }
                }
            },
            "updated_at": {"kraken": 1_800_000_000},
        }

    async def get_candles(self, connector, pair, interval, max_records):
        self.calls.append((connector, pair, interval, max_records))
        return self.candles


class FakeKrakenConnectors:
    async def get_trading_rules(self, connector):
        assert connector == "kraken"
        return {
            "BTC-USD": {
                "min_order_size": "0.0001",
                "min_notional_size": "1",
                "min_order_value": "1",
                "min_price_increment": "0.01",
                "min_base_amount_increment": "0.0001",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
                "supports_market_orders": True,
            }
        }


@pytest.mark.asyncio
async def test_kraken_scan_uses_configured_connector_and_never_creates_orders():
    now = 1_800_000_000.0
    day_start = int(now // 86_400) * 86_400
    history = []
    price = 100.0
    for index in range(90):
        price *= 1.001
        history.append({
            "timestamp": day_start - (90 - index) * 86_400,
            "open": price * 0.99,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 1_000,
        })
    market_data = FakeKrakenMarketData(history)
    client = type("Client", (), {
        "market_data": market_data,
        "connectors": FakeKrakenConnectors(),
    })()

    result = await scan_kraken_universe(
        client,
        top_n=10,
        show_n=10,
        min_volume_usd=1_000,
        min_history_days=90,
        approved_pairs=("BTC-USD",),
        now=now,
    )

    assert result["source"].startswith("Kraken spot")
    assert result["governor_approved_list_loaded"] is True
    assert result["counts"]["governor_approved"] == 1
    assert result["candidates"][0]["trading_pair"] == "BTC-USD"
    assert result["candidates"][0]["governor_approved"] is True
    assert result["candidates"][0]["capital_authorized"] is False
    assert result["orders_created"] == 0
    assert market_data.calls == [("kraken", "BTC-USD", "1d", 92)]


@pytest.mark.asyncio
async def test_kraken_scan_does_not_mark_candidates_approved_without_list():
    now = 1_800_000_000.0
    day_start = int(now // 86_400) * 86_400
    history = [
        {
            "timestamp": day_start - (90 - index) * 86_400,
            "open": 100,
            "high": 102,
            "low": 98,
            "close": 100 + index * 0.1,
            "volume": 1_000,
        }
        for index in range(90)
    ]
    market_data = FakeKrakenMarketData(history)
    client = type("Client", (), {
        "market_data": market_data,
        "connectors": FakeKrakenConnectors(),
    })()

    result = await scan_kraken_universe(
        client,
        min_volume_usd=1_000,
        min_history_days=90,
        now=now,
    )

    assert result["governor_approved_list_loaded"] is False
    assert all(not row["governor_approved"] for row in result["candidates"])
