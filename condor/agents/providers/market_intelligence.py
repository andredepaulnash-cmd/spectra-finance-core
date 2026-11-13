"""Core provider: a timestamped, decision-grade market snapshot for agents.

This provider is deliberately read-only. It composes Condor's existing ticker
pool and candle fetchers with Hummingbot API order-book snapshots. Missing or
stale inputs are surfaced as degraded data; they are never converted into a
healthy/flat market reading.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any

from condor.fetchers.market_data import fetch_historical_candles, fetch_ticker_pool

from . import register_provider
from .base import BaseProvider, ProviderResult

log = logging.getLogger(__name__)


def _epoch_seconds(value: Any) -> float | None:
    """Normalize an API timestamp to Unix seconds without guessing on bad data."""
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    return stamp / 1000.0 if stamp > 100_000_000_000 else stamp


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _levels(rows: Any) -> list[tuple[float, float]]:
    """Normalize order-book levels represented as [price, size] or mappings."""
    if not isinstance(rows, list):
        return []
    normalized: list[tuple[float, float]] = []
    for row in rows:
        if isinstance(row, dict):
            price = _number(row.get("price"))
            size = _number(row.get("amount", row.get("size", row.get("volume"))))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, size = _number(row[0]), _number(row[1])
        else:
            continue
        if price is not None and size is not None and price > 0 and size > 0:
            normalized.append((price, size))
    return normalized


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [right - left for left, right in zip(closes[:-1], closes[1:])]
    window = changes[-period:]
    gains = sum(max(change, 0.0) for change in window) / period
    losses = sum(max(-change, 0.0) for change in window) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


class MarketIntelligenceProvider(BaseProvider):
    """Read-only market snapshot shared by Condor's tick-provider pipeline."""

    name = "market_intelligence"
    is_core = True

    async def execute(
        self,
        client: Any,
        config: dict,
        agent_id: str = "",
        bot_names: list[str] | None = None,
        since: float = 0.0,
    ) -> ProviderResult:
        connector = str(config.get("market_connector_name") or "").strip()
        pair = str(config.get("market_trading_pair") or "").strip().upper()
        if not connector or not pair:
            return ProviderResult(
                name=self.name,
                data={"configured": False, "decision_grade": False, "status": "NOT_CONFIGURED"},
                summary=(
                    "Market Intelligence: NOT CONFIGURED. Set market_connector_name "
                    "and market_trading_pair for this strategy; do not infer a market."
                ),
            )

        interval = str(config.get("market_candle_interval") or "1m").strip()
        records = max(20, min(int(config.get("market_candle_records", 120)), 500))
        max_age = max(5.0, float(config.get("market_data_max_age_seconds", 180)))
        depth = max(1, min(int(config.get("market_order_book_depth", 20)), 100))
        now = time.time()
        errors: dict[str, str] = {}

        try:
            pool = await fetch_ticker_pool(client, strict=True)
            connector_tickers = (pool.get("connectors") or {}).get(connector) or {}
            ticker = connector_tickers.get(pair) or connector_tickers.get(pair.replace("/", "-"))
            ticker_updated = _epoch_seconds((pool.get("updated_at") or {}).get(connector))
            price = _number(ticker.get("price")) if isinstance(ticker, dict) else None
            volume_usd = _number(ticker.get("usd_volume")) if isinstance(ticker, dict) else None
            ticker_updated = ticker_updated or (
                _epoch_seconds(ticker.get("timestamp")) if isinstance(ticker, dict) else None
            )
            if price is None or price <= 0:
                errors["ticker"] = "price missing or invalid"
        except Exception as exc:
            log.warning("Market ticker snapshot failed for %s %s: %s", connector, pair, exc)
            ticker = None
            price = None
            volume_usd = None
            ticker_updated = None
            errors["ticker"] = str(exc)[:180]

        try:
            candles = await fetch_historical_candles(
                client,
                connector,
                pair,
                interval,
                start_time=None,
                limit=records,
            )
        except Exception as exc:
            log.warning("Market candle snapshot failed for %s %s: %s", connector, pair, exc)
            candles = []
            errors["candles"] = str(exc)[:180]

        try:
            book = await client.market_data.get_order_book(connector, pair, depth=depth)
            if not isinstance(book, dict):
                raise ValueError("order-book response is not a mapping")
            bids = _levels(book.get("bids"))
            asks = _levels(book.get("asks"))
            if not bids or not asks:
                raise ValueError("order book is missing valid bid or ask levels")
            best_bid = max(level[0] for level in bids)
            best_ask = min(level[0] for level in asks)
            if best_ask < best_bid:
                raise ValueError("crossed order book")
            book_timestamp = (
                _epoch_seconds(book.get("timestamp"))
                or _epoch_seconds(book.get("updated_at"))
                or _epoch_seconds(book.get("last_update_timestamp"))
            )
            book_timestamp_basis = "exchange_or_api_timestamp" if book_timestamp else "retrieved_at"
            book_timestamp = book_timestamp or now
            spread = best_ask - best_bid
            spread_bps = (spread / ((best_ask + best_bid) / 2.0)) * 10_000.0
            bid_depth_quote = sum(price_level * size for price_level, size in bids)
            ask_depth_quote = sum(price_level * size for price_level, size in asks)
        except Exception as exc:
            log.warning("Market order-book snapshot failed for %s %s: %s", connector, pair, exc)
            book = None
            bids, asks = [], []
            best_bid = best_ask = spread_bps = None
            bid_depth_quote = ask_depth_quote = None
            book_timestamp = None
            book_timestamp_basis = "unavailable"
            errors["order_book"] = str(exc)[:180]

        normalized_candles = candles[-records:] if candles else []
        closes = [float(row["close"]) for row in normalized_candles if _number(row.get("close")) is not None]
        candle_timestamp = _epoch_seconds(normalized_candles[-1].get("timestamp")) if normalized_candles else None
        candle_age = max(0.0, now - candle_timestamp) if candle_timestamp else None
        ticker_age = max(0.0, now - ticker_updated) if ticker_updated else None
        book_age = max(0.0, now - book_timestamp) if book_timestamp else None

        stale_sources = [
            name for name, age in (("ticker", ticker_age), ("candles", candle_age), ("order_book", book_age))
            if age is not None and age > max_age
        ]
        missing_timestamps = [
            name for name, stamp in (("ticker", ticker_updated), ("candles", candle_timestamp), ("order_book", book_timestamp))
            if stamp is None
        ]
        required_missing = (
            price is None or not normalized_candles or not bids or not asks
        )
        if stale_sources:
            status = "STALE"
        elif required_missing or missing_timestamps or errors:
            status = "DEGRADED"
        else:
            status = "FRESH"

        decision_grade = status == "FRESH"
        sma20 = sum(closes[-20:]) / min(20, len(closes)) if closes else None
        last_return_pct = (
            ((closes[-1] / closes[-2]) - 1.0) * 100.0
            if len(closes) >= 2 and closes[-2] != 0
            else None
        )
        rsi14 = _rsi(closes)

        data = {
            "configured": True,
            "status": status,
            "decision_grade": decision_grade,
            "connector": connector,
            "trading_pair": pair,
            "retrieved_at": now,
            "max_age_seconds": max_age,
            "price": price,
            "ticker_updated_at": ticker_updated,
            "ticker_age_seconds": ticker_age,
            "volume_24h_usd": volume_usd,
            "candle_interval": interval,
            "candle_count": len(normalized_candles),
            "candle_updated_at": candle_timestamp,
            "candle_age_seconds": candle_age,
            "candles": normalized_candles[-20:],
            "sma20": sma20,
            "rsi14": rsi14,
            "last_candle_return_pct": last_return_pct,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_bps": spread_bps,
            "bid_depth_quote": bid_depth_quote,
            "ask_depth_quote": ask_depth_quote,
            "order_book_depth": depth,
            "order_book_age_seconds": book_age,
            "order_book_timestamp_basis": book_timestamp_basis,
            "stale_sources": stale_sources,
            "missing_timestamps": missing_timestamps,
            "errors": errors,
        }

        lines = [
            f"Market: {connector} {pair} | status={status} | decision_grade={decision_grade}",
            f"Price={price if price is not None else 'unavailable'} | 24h USD volume={volume_usd if volume_usd is not None else 'unavailable'}",
            f"Bid/ask={best_bid if best_bid is not None else 'unavailable'}/{best_ask if best_ask is not None else 'unavailable'} | spread={spread_bps if spread_bps is not None else 'unavailable'} bps",
            f"Candles={len(normalized_candles)} {interval} | SMA20={sma20 if sma20 is not None else 'unavailable'} | RSI14={rsi14 if rsi14 is not None else 'unavailable'}",
            f"Data ages seconds: ticker={ticker_age}, candles={candle_age}, order_book={book_age}",
        ]
        if not decision_grade:
            lines.append(
                "NO-TRADE DATA GUARD: market snapshot is not decision-grade. "
                "Do not propose new exposure from this snapshot; explain the missing, stale, or failed inputs."
            )
        if errors:
            lines.append("Errors: " + "; ".join(f"{key}={value}" for key, value in errors.items()))
        return ProviderResult(name=self.name, data=data, summary="\n".join(lines))


register_provider(MarketIntelligenceProvider())
