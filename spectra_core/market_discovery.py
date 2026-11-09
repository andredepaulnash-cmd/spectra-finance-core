"""Public market discovery and mean-reversion candidate ranking.

This module is research-only: it reads public Binance USD-M perpetual market
data and returns ranked candidates. It does not create orders, size positions,
or authorize capital.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUTURES_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

EXCLUDED_BASES = {
    "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "USDS", "USDP",
    "PYUSD", "USDD", "FRAX", "LUSD", "UST", "USTC", "WBTC", "WETH",
    "STETH", "WSTETH", "RETH", "CBETH", "BTCB", "BTTC",
}


def _number(value: Any, field: str) -> float:
    result = float(value)
    if not (result == result and abs(result) != float("inf")):
        raise ValueError(f"{field} must be finite")
    return result


def analyze_daily_history(
    *,
    symbol: str,
    base_asset: str,
    current_price: float,
    volume_24h_usd: float,
    change_24h_pct: float,
    candles: list[list[Any]],
) -> dict[str, Any]:
    """Calculate transparent, bounded mean-reversion selection metrics."""
    rows = []
    for row in candles:
        try:
            close = _number(row[4], "close")
            high = _number(row[2], "high")
            low = _number(row[3], "low")
            volume = _number(row[5], "volume")
            if close > 0 and high >= low > 0 and volume >= 0:
                rows.append((high, low, close, volume))
        except (IndexError, TypeError, ValueError):
            continue
    if len(rows) < 90:
        raise ValueError("insufficient_history")

    highs = [r[0] for r in rows]
    lows = [r[1] for r in rows]
    closes = [r[2] for r in rows]
    volumes = [r[3] for r in rows]
    price = _number(current_price, "current_price")
    if price <= 0:
        raise ValueError("invalid_current_price")

    typical = [(h + low + close) / 3 for h, low, close, _ in rows]
    total_volume = sum(volumes)
    vwap_90d = (
        sum(v * p for v, p in zip(volumes, typical)) / total_volume
        if total_volume > 0 else sum(closes) / len(closes)
    )
    high_90d, low_90d = max(highs), min(lows)
    sma_20d = sum(closes[-20:]) / min(20, len(closes))
    return_7d = (price / closes[-8] - 1) * 100 if len(closes) >= 8 else 0.0
    return_30d = (price / closes[-31] - 1) * 100 if len(closes) >= 31 else 0.0
    drawdown_from_high = (high_90d / price - 1) * 100
    discount_to_vwap = (vwap_90d / price - 1) * 100
    range_position = ((price - low_90d) / (high_90d - low_90d) * 100) if high_90d > low_90d else 50.0
    distance_from_low = (price / low_90d - 1) * 100 if low_90d > 0 else 0.0

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = changes[-14:]
    gains = sum(max(x, 0) for x in recent) / 14
    losses = sum(max(-x, 0) for x in recent) / 14
    rsi_14d = 100.0 if losses == 0 and gains > 0 else (
        50.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
    )

    # Hard rejects target the "falling knife" cases Architect asked us to avoid.
    # These are explicit v1 guardrails, not claims of a profitable signal.
    rejection_reasons = []
    if return_30d <= -25 and price < sma_20d:
        rejection_reasons.append("persistent_downtrend: 30d loss <= -25% and below 20d average")
    if distance_from_low <= 5 and return_30d < 0:
        rejection_reasons.append("near_90d_low: within 5% of 90d low while 30d trend is negative")

    # Score rewards a meaningful discount without treating a large ATH gap alone
    # as value. Recovery, trend and RSI context contribute independently.
    discount_score = min(max(discount_to_vwap, 0.0) / 25.0, 1.0) * 35.0
    drawdown_score = 20.0 if 15.0 <= drawdown_from_high <= 65.0 else (
        10.0 if 8.0 <= drawdown_from_high <= 80.0 else 0.0
    )
    rsi_score = 15.0 if 30.0 <= rsi_14d <= 55.0 else (
        7.0 if 25.0 <= rsi_14d <= 65.0 else 0.0
    )
    recovery_score = 10.0 if return_7d > 0 else 0.0
    trend_score = 10.0 if price >= sma_20d else 0.0
    stability_score = 10.0 if return_30d > -10.0 else (
        5.0 if return_30d > -20.0 else 0.0
    )
    score = round(discount_score + drawdown_score + rsi_score + recovery_score + trend_score + stability_score, 2)

    reasons = []
    if discount_to_vwap > 0:
        reasons.append(f"{discount_to_vwap:.1f}% below 90d VWAP")
    if return_7d > 0:
        reasons.append(f"7d recovery {return_7d:+.1f}%")
    if price >= sma_20d:
        reasons.append("price above 20d average")
    if 30 <= rsi_14d <= 55:
        reasons.append(f"RSI {rsi_14d:.0f} in recovery range")
    if not reasons:
        reasons.append("no strong mean-reversion confirmation yet")

    return {
        "symbol": symbol,
        "base_asset": base_asset,
        "trading_pair": f"{base_asset}-USDT",
        "current_price": round(price, 10),
        "volume_24h_usd": round(volume_24h_usd, 2),
        "change_24h_pct": round(change_24h_pct, 2),
        "history_days": len(rows),
        "high_90d": round(high_90d, 10),
        "low_90d": round(low_90d, 10),
        "vwap_90d": round(vwap_90d, 10),
        "discount_to_vwap_pct": round(discount_to_vwap, 2),
        "drawdown_from_high_pct": round(drawdown_from_high, 2),
        "distance_from_low_pct": round(distance_from_low, 2),
        "range_position_pct": round(range_position, 2),
        "sma_20d": round(sma_20d, 10),
        "return_7d_pct": round(return_7d, 2),
        "return_30d_pct": round(return_30d, 2),
        "rsi_14d": round(rsi_14d, 2),
        "selection_score": score,
        "eligible": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
        "selection_reasons": reasons,
    }


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()


async def scan_public_universe(
    *,
    top_n: int = 80,
    show_n: int = 25,
    min_volume_usd: float = 10_000_000,
    min_history_days: int = 90,
) -> dict[str, Any]:
    """Scan the top liquid USDT perpetuals using public data only."""
    if not 1 <= top_n <= 150 or not 1 <= show_n <= 100:
        raise ValueError("top_n/show_n outside supported bounds")
    if min_volume_usd <= 0 or min_history_days < 90:
        raise ValueError("minimum volume must be positive and history at least 90 days")

    timeout = aiohttp.ClientTimeout(total=25, connect=8)
    headers = {"User-Agent": "SpectraFinance-MarketDiscovery/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        info, tickers = await asyncio.gather(
            _get_json(session, FUTURES_INFO_URL),
            _get_json(session, FUTURES_TICKER_URL),
        )

        active = {
            row.get("symbol"): row.get("baseAsset")
            for row in info.get("symbols", [])
            if row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
            and row.get("status") == "TRADING"
            and row.get("baseAsset") not in EXCLUDED_BASES
        }
        candidates = []
        for row in tickers:
            symbol = row.get("symbol")
            base = active.get(symbol)
            if not base:
                continue
            try:
                volume = _number(row.get("quoteVolume", 0), "quoteVolume")
                price = _number(row.get("lastPrice", 0), "lastPrice")
                change = _number(row.get("priceChangePercent", 0), "priceChangePercent")
            except (TypeError, ValueError):
                continue
            if volume >= min_volume_usd and price > 0:
                candidates.append({
                    "symbol": symbol, "base_asset": base, "price": price,
                    "volume": volume, "change": change,
                })
        candidates.sort(key=lambda item: item["volume"], reverse=True)
        selected = candidates[:top_n]

        semaphore = asyncio.Semaphore(8)
        async def fetch_history(item):
            async with semaphore:
                try:
                    rows = await _get_json(
                        session, FUTURES_KLINES_URL,
                        {"symbol": item["symbol"], "interval": "1d", "limit": max(365, min_history_days + 1)},
                    )
                    # Drop today's incomplete daily candle. Its live price is
                    # supplied separately by the 24h ticker.
                    now_ms = int(time.time() * 1000)
                    closed = [row for row in rows if len(row) > 6 and int(row[6]) < now_ms]
                    if len(closed) < min_history_days:
                        return (item["symbol"], None, "insufficient_history")
                    return (item["symbol"], analyze_daily_history(
                        symbol=item["symbol"], base_asset=item["base_asset"],
                        current_price=item["price"], volume_24h_usd=item["volume"],
                        change_24h_pct=item["change"], candles=closed,
                    ), None)
                except Exception as exc:
                    return (item["symbol"], None, f"market_data_error: {type(exc).__name__}")

        fetched = await asyncio.gather(*(fetch_history(item) for item in selected))
    analyzed = [row for _, row, reason in fetched if row is not None]
    rejected = [
        {"symbol": symbol, "reason": reason}
        for symbol, row, reason in fetched if row is None
    ]
    ranked = sorted(
        (row for row in analyzed if row["eligible"]),
        key=lambda row: (row["selection_score"], row["volume_24h_usd"]),
        reverse=True,
    )[:show_n]
    guardrail_rejects = sum(not row["eligible"] for row in analyzed)
    return {
        "source": "Binance USD-M public perpetual market data",
        "as_of": time.time(),
        "universe": "USDT-margined perpetual contracts",
        "criteria": {
            "top_by_24h_quote_volume": top_n,
            "minimum_24h_volume_usd": min_volume_usd,
            "minimum_history_days": min_history_days,
            "excluded_bases": sorted(EXCLUDED_BASES),
            "ranking": "discount-to-90d-VWAP + recovery/trend/RSI context",
        },
        "counts": {
            "liquid_contracts_found": len(candidates),
            "contracts_selected_for_history": len(selected),
            "analyzed": len(analyzed),
            "eligible": len(ranked),
            "rejected_by_guardrails": guardrail_rejects,
            "history_or_data_rejected": len(rejected),
        },
        "candidates": ranked,
        "rejected_data": rejected,
        "paper_only": True,
        "orders_created": 0,
    }
