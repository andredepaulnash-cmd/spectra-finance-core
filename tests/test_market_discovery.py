from __future__ import annotations

import pytest

from spectra_core.market_discovery import analyze_daily_history


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
