from __future__ import annotations

import pytest

from spectra_core.market_discovery import (
    analyze_daily_history,
    build_dynamic_market_lists,
    calculate_24h_change,
    scan_kraken_universe,
)


def test_calculate_24h_change_uses_hourly_reference_and_rejects_stale_data():
    now = 1_800_000_000.0
    target = now - 86_400
    candles = [
        {"timestamp": target - 3_600, "close": 100.0},
        {"timestamp": target, "close": 105.0},
    ]

    change, reference_age = calculate_24h_change(
        candles,
        current_price=110.0,
        now=now,
    )

    assert change == pytest.approx(10.0)
    assert reference_age == pytest.approx(86_400.0)

    stale = [{"timestamp": target - 4 * 3_600, "close": 100.0}]
    assert calculate_24h_change(stale, current_price=110.0, now=now) == (None, None)


def test_dynamic_lists_keep_entry_candidates_separate_from_research_rejects():
    markets = [
        {
            "trading_pair": "AAA-USD",
            "symbol": "AAA-USD",
            "base_asset": "AAA",
            "quote_asset": "USD",
            "current_price": 110.0,
            "volume_24h_usd": 20_000_000.0,
            "volume_rank_24h": 1,
            "change_24h_pct": 4.0,
            "return_3d_pct": 2.0,
            "selection_score": 80.0,
            "eligible": True,
            "reversal_signal_count": 0,
            "governor_approved": False,
        },
        {
            "trading_pair": "BBB-USD",
            "symbol": "BBB-USD",
            "base_asset": "BBB",
            "quote_asset": "USD",
            "current_price": 90.0,
            "volume_24h_usd": 12_000_000.0,
            "volume_rank_24h": 2,
            "change_24h_pct": -5.0,
            "return_3d_pct": -9.0,
            "selection_score": 65.0,
            "eligible": False,
            "rejection_reasons": ["near_90d_low"],
            "reversal_signal_count": 2,
            "reversal_signal_reasons": ["RSI improving", "3-day return improving"],
            "governor_approved": False,
        },
        {
            "trading_pair": "CCC-USD",
            "symbol": "CCC-USD",
            "base_asset": "CCC",
            "quote_asset": "USD",
            "current_price": 50.0,
            "volume_24h_usd": 8_000_000.0,
            "volume_rank_24h": 3,
            "change_24h_pct": -2.0,
            "return_3d_pct": -3.0,
            "selection_score": 45.0,
            "eligible": False,
            "reversal_signal_count": 0,
            "governor_approved": False,
        },
    ]

    lists = build_dynamic_market_lists(markets, markets, show_n=10)

    assert [row["trading_pair"] for row in lists["mean_reversion_candidates"]] == ["AAA-USD"]
    assert {row["trading_pair"] for row in lists["mean_reversion_watchlist"]} == {
        "AAA-USD", "BBB-USD", "CCC-USD"
    }
    assert [row["trading_pair"] for row in lists["top_24h_gainers"]] == ["AAA-USD"]
    assert [row["trading_pair"] for row in lists["top_24h_losers"]] == ["BBB-USD", "CCC-USD"]
    assert [row["trading_pair"] for row in lists["top_3d_losers"]] == ["BBB-USD", "CCC-USD"]
    assert [row["trading_pair"] for row in lists["recovery_watchlist"]] == ["BBB-USD"]
    assert {row["trading_pair"] for row in lists["rejected_candidates"]} == {
        "BBB-USD", "CCC-USD"
    }
    assert all(row["capital_authorized"] is False for rows in lists.values() for row in rows)


def _daily_history(price: float, day_start: int) -> list[dict[str, float]]:
    return [
        {
            "timestamp": day_start - (90 - index) * 86_400,
            "open": price * 0.99,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 10_000.0,
        }
        for index in range(90)
    ]


def test_analyzer_exposes_three_day_and_reversal_context():
    history = _daily_history(100.0, 1_800_000_000 // 86_400 * 86_400)
    result = analyze_daily_history(
        symbol="AAAUSD",
        base_asset="AAA",
        current_price=102.0,
        volume_24h_usd=20_000_000,
        change_24h_pct=2.0,
        candles=history,
    )

    assert "return_3d_pct" in result
    assert "previous_return_3d_pct" in result
    assert "rsi_14d_previous" in result
    assert "rsi_14d_change" in result
    assert result["reversal_signal_count"] >= 1
    assert isinstance(result["reversal_signal_reasons"], list)
    assert 0 <= result["bbwp_percentile"] <= 100
    assert result["bbwp_state"] in {
        "COMPRESSION_TO_EXPANSION", "COMPRESSION", "EXPANSION", "NEUTRAL", "UNAVAILABLE"
    }
    assert isinstance(result["bbwp_signal_reasons"], list)


class FakeMarketData:
    def __init__(self, now: float):
        self.now = now
        self.calls: list[tuple[str, str, str, int]] = []

    async def get_tickers(self, connector: str):
        assert connector == "kraken"
        return {
            "tickers": {
                "kraken": {
                    "AAA-USD": {
                        "price": 100.0,
                        "base_volume": 200_000.0,
                        "quote_volume": 20_000_000.0,
                        "timestamp": self.now,
                    },
                    "BBB-USD": {
                        "price": 200.0,
                        "base_volume": 10_000.0,
                        "quote_volume": 2_000_000.0,
                        "timestamp": self.now,
                    },
                }
            },
            "updated_at": {"kraken": self.now},
        }

    async def get_candles(self, connector: str, pair: str, interval: str, max_records: int):
        assert connector == "kraken"
        self.calls.append((connector, pair, interval, max_records))
        price = 100.0 if pair == "AAA-USD" else 200.0
        if interval == "1h":
            reference_price = price / 1.02 if pair == "AAA-USD" else price * 1.02
            return [{
                "timestamp": self.now - 86_400 - 3_600,
                "open": reference_price,
                "high": reference_price * 1.01,
                "low": reference_price * 0.99,
                "close": reference_price,
                "volume": 1_000.0,
            }]
        day_start = int(self.now // 86_400) * 86_400
        return _daily_history(price, day_start)


class FakeConnectors:
    async def get_trading_rules(self, connector: str):
        assert connector == "kraken"
        return {
            pair: {
                "min_order_size": "0.0001",
                "min_notional_size": "1",
                "min_order_value": "1",
                "min_price_increment": "0.01",
                "min_base_amount_increment": "0.0001",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
                "supports_market_orders": True,
            }
            for pair in ("AAA-USD", "BBB-USD")
        }


@pytest.mark.asyncio
async def test_scan_expands_research_lists_without_lowering_entry_liquidity_floor():
    now = 1_800_000_000.0
    market_data = FakeMarketData(now)
    client = type("Client", (), {
        "market_data": market_data,
        "connectors": FakeConnectors(),
    })()

    result = await scan_kraken_universe(
        client,
        top_n=10,
        show_n=10,
        min_volume_usd=10_000_000,
        research_min_volume_usd=1_000_000,
        min_history_days=90,
        min_median_daily_quote_volume_usd=1_000_000,
        approved_pairs=(),
        now=now,
    )

    assert result["counts"]["liquid_markets_found"] == 2
    assert result["counts"]["entry_liquid_markets_found"] == 1
    assert result["counts"]["rejected_by_entry_liquidity"] == 1
    assert len(result["lists"]["mean_reversion_watchlist"]) == 2
    assert [row["trading_pair"] for row in result["lists"]["mean_reversion_candidates"]] == ["AAA-USD"]
    assert [row["trading_pair"] for row in result["lists"]["top_24h_gainers"]] == ["AAA-USD"]
    assert [row["trading_pair"] for row in result["lists"]["top_24h_losers"]] == ["BBB-USD"]
    assert any(
        "weak_24h_turnover" in reason
        for reason in result["rejected_candidates"][0]["rejection_reasons"]
    )
    assert result["candidates"] == result["lists"]["mean_reversion_candidates"]
    assert result["orders_created"] == 0
    assert all(row["capital_authorized"] is False for row in result["lists"]["mean_reversion_watchlist"])


def test_top_loser_reversal_candidates_are_distinct_and_keep_entry_guards():
    markets = [
        {
            "trading_pair": "REV-USD", "quote_asset": "USD",
            "current_price": 95.0, "volume_24h_usd": 15_000_000.0,
            "change_24h_pct": -4.0, "return_3d_pct": -6.0,
            "entry_liquidity_eligible": True, "eligible": True,
            "selection_score": 72.0, "reversal_signal_count": 2,
            "capital_authorized": False,
        },
        {
            "trading_pair": "NOTLOSER-USD", "quote_asset": "USD",
            "current_price": 105.0, "volume_24h_usd": 20_000_000.0,
            "change_24h_pct": 1.0, "return_3d_pct": -6.0,
            "entry_liquidity_eligible": True, "eligible": True,
            "selection_score": 75.0, "reversal_signal_count": 3,
            "capital_authorized": False,
        },
        {
            "trading_pair": "THIN-USD", "quote_asset": "USD",
            "current_price": 95.0, "volume_24h_usd": 8_000_000.0,
            "change_24h_pct": -4.0, "return_3d_pct": -6.0,
            "entry_liquidity_eligible": False, "eligible": False,
            "selection_score": 72.0, "reversal_signal_count": 3,
            "capital_authorized": False,
        },
        {
            "trading_pair": "NO-SIGNAL-USD", "quote_asset": "USD",
            "current_price": 95.0, "volume_24h_usd": 18_000_000.0,
            "change_24h_pct": -4.0, "return_3d_pct": -6.0,
            "entry_liquidity_eligible": True, "eligible": True,
            "selection_score": 72.0, "reversal_signal_count": 1,
            "capital_authorized": False,
        },
    ]
    lists = build_dynamic_market_lists(markets, markets, show_n=10)
    assert [row["trading_pair"] for row in lists["top_loser_reversal_candidates"]] == [
        "REV-USD"
    ]
    candidate = lists["top_loser_reversal_candidates"][0]
    assert candidate["reversal_pathway_eligible"] is True
    assert candidate["capital_authorized"] is False
