"""Kraken spot market discovery and mean-reversion candidate ranking.

This module reads the configured Kraken public market-data connector and
returns research candidates. It does not create orders, size positions, or
authorize capital.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from typing import Any

from .kraken_market_fabric import collect_kraken_universe

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
    change_24h_pct: float | None,
    candles: list[Any],
    quote_asset: str = "USDT",
) -> dict[str, Any]:
    """Calculate transparent, bounded mean-reversion selection metrics."""
    rows = []
    for row in candles:
        try:
            if isinstance(row, Mapping):
                close = _number(row.get("close"), "close")
                high = _number(row.get("high"), "high")
                low = _number(row.get("low"), "low")
                volume = _number(row.get("volume"), "volume")
            else:
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
    # The scan requires at least 90 completed daily candles, then evaluates a
    # consistent rolling 90-day window even when more history is available.
    rows = rows[-90:]

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
        "trading_pair": f"{base_asset}-{quote_asset.strip().upper()}",
        "current_price": round(price, 10),
        "volume_24h_usd": round(volume_24h_usd, 2),
        "change_24h_pct": None if change_24h_pct is None else round(change_24h_pct, 2),
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


def _candle_timestamp(row: Any) -> float:
    raw = row.get("timestamp") if isinstance(row, Mapping) else (row[0] if row else None)
    if raw is None:
        raise ValueError("candle_timestamp_missing")
    stamp = _number(raw, "timestamp")
    return stamp / 1000.0 if stamp > 1_000_000_000_000 else stamp


async def scan_kraken_universe(
    client: Any,
    *,
    top_n: int = 80,
    show_n: int = 25,
    min_volume_usd: float = 10_000_000,
    min_history_days: int = 90,
    quote_assets: Iterable[str] = ("USD",),
    approved_pairs: Iterable[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Rank Kraken USD spot markets using the configured Hummingbot API.

    This is discovery only. It never creates an order or reserves capital.
    If a Governor-approved pair list is supplied, candidates are annotated
    against it; without that list no candidate is marked approved for capital.
    """
    if not 1 <= top_n <= 150 or not 1 <= show_n <= 100:
        raise ValueError("top_n/show_n outside supported bounds")
    if min_volume_usd < 0 or min_history_days < 90:
        raise ValueError("minimum volume cannot be negative and history must be at least 90 days")
    timestamp = time.time() if now is None else _number(now, "now")
    day_start = int(timestamp // 86_400) * 86_400
    quote_assets = tuple(str(q).strip().upper() for q in quote_assets if str(q).strip())
    approved = (
        None
        if approved_pairs is None
        else {str(pair).strip().upper() for pair in approved_pairs if str(pair).strip()}
    )

    snapshot = await collect_kraken_universe(client, quote_assets=quote_assets)
    candidates = []
    for pair in snapshot.eligible_pairs:
        ticker = snapshot.tickers[pair]
        volume = float(ticker.quote_volume_24h)
        if volume < min_volume_usd:
            continue
        candidates.append({
            "trading_pair": pair,
            "symbol": pair,
            "base_asset": pair.rsplit("-", 1)[0],
            "quote_asset": pair.rsplit("-", 1)[1],
            "price": float(ticker.price),
            "volume": volume,
        })
    candidates.sort(key=lambda item: item["volume"], reverse=True)
    selected = candidates[:top_n]

    semaphore = asyncio.Semaphore(6)

    async def fetch_history(item: dict[str, Any]):
        async with semaphore:
            try:
                raw = await client.market_data.get_candles(
                    "kraken",
                    item["trading_pair"],
                    "1d",
                    max_records=min(1000, min_history_days + 2),
                )
                records = (
                    raw if isinstance(raw, list)
                    else raw.get("data", raw.get("candles", []))
                    if isinstance(raw, Mapping)
                    else []
                )
                completed = []
                for row in records:
                    try:
                        if _candle_timestamp(row) < day_start:
                            completed.append(row)
                    except (TypeError, ValueError, IndexError):
                        continue
                if len(completed) < min_history_days:
                    return item["trading_pair"], None, "insufficient_completed_history"
                analysis = analyze_daily_history(
                    symbol=item["symbol"],
                    base_asset=item["base_asset"],
                    quote_asset=item["quote_asset"],
                    current_price=item["price"],
                    volume_24h_usd=item["volume"],
                    change_24h_pct=None,
                    candles=completed,
                )
                analysis["governor_approved"] = (
                    approved is not None and item["trading_pair"] in approved
                )
                analysis["capital_authorized"] = False
                return item["trading_pair"], analysis, None
            except Exception as exc:
                return (
                    item["trading_pair"],
                    None,
                    f"market_data_error: {type(exc).__name__}",
                )

    fetched = await asyncio.gather(*(fetch_history(item) for item in selected))
    analyzed = [row for _, row, reason in fetched if row is not None]
    rejected = [
        {"trading_pair": pair, "reason": reason}
        for pair, row, reason in fetched if row is None
    ]
    ranked_all = sorted(
        (row for row in analyzed if row["eligible"]),
        key=lambda row: (row["selection_score"], row["volume_24h_usd"]),
        reverse=True,
    )
    ranked = ranked_all[:show_n]
    return {
        "source": "Kraken spot market data via Hummingbot API",
        "as_of": timestamp,
        "universe": "Kraken spot markets in configured quote assets",
        "criteria": {
            "top_by_24h_quote_volume": top_n,
            "minimum_24h_volume_quote": min_volume_usd,
            "minimum_completed_history_days": min_history_days,
            "quote_assets": sorted({str(q).strip().upper() for q in quote_assets}),
            "ranking": "discount-to-90d-VWAP + recovery/trend/RSI context",
        },
        "counts": {
            "eligible_kraken_markets": len(snapshot.eligible_pairs),
            "liquid_markets_found": len(candidates),
            "markets_selected_for_history": len(selected),
            "analyzed": len(analyzed),
            "eligible": len(ranked_all),
            "rejected_by_guardrails": sum(not row["eligible"] for row in analyzed),
            "history_or_data_rejected": len(rejected),
            "governor_approved": sum(bool(row["governor_approved"]) for row in ranked_all),
        },
        "candidates": ranked,
        "rejected_data": rejected,
        "governor_approved_list_loaded": approved is not None,
        "paper_only": True,
        "orders_created": 0,
    }
