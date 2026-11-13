"""Core provider: a timestamped, decision-grade market snapshot for agents.

This provider is deliberately read-only. It composes Condor's existing ticker
pool and candle fetchers with Hummingbot API order-book snapshots. Missing or
stale inputs are surfaced as degraded data; they are never converted into a
healthy/flat market reading.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import threading
import time
import weakref
from datetime import datetime
from typing import Any

from condor.fetchers.market_data import fetch_historical_candles, fetch_ticker_pool

from . import register_provider
from .base import BaseProvider, ProviderResult

log = logging.getLogger(__name__)

# One shared process-local snapshot cache serves all agent ticks that use the
# same Hummingbot API server and market configuration. The lock is per event
# loop and key, so concurrent agents cannot stampede the same endpoints.
_MARKET_CACHE: dict[tuple[Any, ...], tuple[float, ProviderResult]] = {}
_MARKET_CACHE_LOCKS: dict[tuple[Any, ...], asyncio.Lock] = {}
_MARKET_CACHE_GUARD = threading.Lock()


def _summary(data: dict[str, Any]) -> str:
    lines = [
        f"Market: {data.get('connector')} {data.get('trading_pair')} | status={data.get('status')} | decision_grade={data.get('decision_grade')}",
        f"Price={data.get('price') if data.get('price') is not None else 'unavailable'} | 24h USD volume={data.get('volume_24h_usd') if data.get('volume_24h_usd') is not None else 'unavailable'}",
        f"Bid/ask={data.get('best_bid') if data.get('best_bid') is not None else 'unavailable'}/{data.get('best_ask') if data.get('best_ask') is not None else 'unavailable'} | spread={data.get('spread_bps') if data.get('spread_bps') is not None else 'unavailable'} bps",
        f"Candles={data.get('candle_count', 0)} {data.get('candle_interval')} | SMA20={data.get('sma20') if data.get('sma20') is not None else 'unavailable'} | RSI14={data.get('rsi14') if data.get('rsi14') is not None else 'unavailable'}",
        f"Data ages seconds: ticker={data.get('ticker_age_seconds')}, candles={data.get('candle_age_seconds')}, order_book={data.get('order_book_age_seconds')}",
    ]
    if not data.get("decision_grade"):
        lines.append(
            "NO-TRADE DATA GUARD: market snapshot is not decision-grade. "
            "Do not propose new exposure from this snapshot; explain the missing, stale, or failed inputs."
        )
    errors = data.get("errors") or {}
    if errors:
        lines.append("Errors: " + "; ".join(f"{key}={value}" for key, value in errors.items()))
    return "\n".join(lines)


def _refresh_cached_result(result: ProviderResult) -> ProviderResult:
    data = copy.deepcopy(result.data)
    now = time.time()
    timestamp_fields = (
        ("ticker_updated_at", "ticker_age_seconds", "ticker"),
        ("candle_updated_at", "candle_age_seconds", "candles"),
        ("order_book_updated_at", "order_book_age_seconds", "order_book"),
    )
    stale_sources: list[str] = []
    missing_timestamps: list[str] = []
    max_age = float(data.get("max_age_seconds", 180.0))
    for timestamp_key, age_key, source in timestamp_fields:
        stamp = _number(data.get(timestamp_key))
        if stamp is None:
            data[age_key] = None
            missing_timestamps.append(source)
        else:
            age = max(0.0, now - stamp)
            data[age_key] = age
            if age > max_age:
                stale_sources.append(source)
    required_missing = (
        data.get("price") is None
        or int(data.get("candle_count") or 0) <= 0
        or data.get("best_bid") is None
        or data.get("best_ask") is None
    )
    errors = data.get("errors") or {}
    if stale_sources:
        status = "STALE"
    elif required_missing or missing_timestamps or errors:
        status = "DEGRADED"
    else:
        status = "FRESH"
    data["retrieved_at"] = now
    data["stale_sources"] = stale_sources
    data["missing_timestamps"] = missing_timestamps
    data["status"] = status
    data["decision_grade"] = status == "FRESH"
    return ProviderResult(name=result.name, data=data, summary=_summary(data))


async def _shared_snapshot(
    key: tuple[Any, ...],
    requested_ttl: float,
    loader,
) -> ProviderResult:
    now = time.monotonic()
    with _MARKET_CACHE_GUARD:
        cached = _MARKET_CACHE.get(key)
        lock = _MARKET_CACHE_LOCKS.setdefault(key, asyncio.Lock())
        if cached is not None and now < cached[0]:
            return _refresh_cached_result(cached[1])

    async with lock:
        now = time.monotonic()
        with _MARKET_CACHE_GUARD:
            cached = _MARKET_CACHE.get(key)
            if cached is not None and now < cached[0]:
                return _refresh_cached_result(cached[1])

        result = await loader()
        data = result.data or {}
        if data.get("status") == "FRESH":
            ages = [
                _number(data.get(name))
                for name in ("ticker_age_seconds", "candle_age_seconds", "order_book_age_seconds")
            ]
            known_ages = [age for age in ages if age is not None]
            headroom = float(data.get("max_age_seconds", 180.0)) - max(known_ages, default=0.0)
            ttl = min(requested_ttl, max(0.0, headroom))
        else:
            # Briefly cache failures/degraded snapshots to avoid request storms,
            # but retry quickly so recovery is visible.
            ttl = min(requested_ttl, 2.0)

        if ttl > 0.25:
            with _MARKET_CACHE_GUARD:
                _MARKET_CACHE[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                expired = [cache_key for cache_key, value in _MARKET_CACHE.items() if value[0] <= time.monotonic()]
                for cache_key in expired:
                    _MARKET_CACHE.pop(cache_key, None)
                    _MARKET_CACHE_LOCKS.pop(cache_key, None)
                while len(_MARKET_CACHE) > 256:
                    oldest = next(iter(_MARKET_CACHE))
                    _MARKET_CACHE.pop(oldest, None)
                    _MARKET_CACHE_LOCKS.pop(oldest, None)
        return result


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
            return await self._execute_uncached(
                client, config, agent_id=agent_id, bot_names=bot_names, since=since
            )

        interval = str(config.get("market_candle_interval") or "1m").strip()
        records = max(20, min(int(config.get("market_candle_records", 120)), 500))
        max_age = max(5.0, float(config.get("market_data_max_age_seconds", 180)))
        depth = max(1, min(int(config.get("market_order_book_depth", 20)), 100))
        requested_ttl = max(1.0, min(float(config.get("market_data_cache_ttl_seconds", 10)), 30.0))
        client_scope = (
            getattr(client, "base_url", None)
            or getattr(client, "_base_url", None)
            or getattr(client, "api_url", None)
            or f"client:{id(client)}"
        )
        key = (
            weakref.ref(asyncio.get_running_loop()),
            str(client_scope),
            str(config.get("server_name") or "local"),
            connector,
            pair,
            interval,
            records,
            depth,
            max_age,
            requested_ttl,
        )

        async def load() -> ProviderResult:
            return await self._execute_uncached(
                client, config, agent_id=agent_id, bot_names=bot_names, since=since
            )

        return await _shared_snapshot(key, requested_ttl, load)

    async def _execute_uncached(
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
            "order_book_updated_at": book_timestamp,
            "order_book_timestamp_basis": book_timestamp_basis,
            "stale_sources": stale_sources,
            "missing_timestamps": missing_timestamps,
            "errors": errors,
        }

        return ProviderResult(name=self.name, data=data, summary=_summary(data))


register_provider(MarketIntelligenceProvider())
