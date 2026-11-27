"""Public Kraken market data client and gainer-first Alpha 2.0 scanner."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from datetime import datetime

import httpx

from .safety import PaperSafetySettings
from .policy import INITIAL_ENTRY_NOTIONAL


PAPER_CONNECTOR = "kraken_paper_trade"
PUBLIC_CANDLE_CONNECTOR = "kraken"
ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class MarketCandidate:
    trading_pair: str
    base_asset: str
    quote_asset: str
    last_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    quote_volume_24h: Decimal
    base_volume_24h: Decimal
    spread_fraction: Decimal
    ticker_timestamp: float
    min_order_size: Decimal
    min_notional_size: Decimal
    base_amount_increment: Decimal
    price_increment: Decimal
    change_24h_fraction: Decimal = ZERO


class HummingbotPaperDataClient:
    """Read-only public market-data client; contains no exchange credential support."""

    def __init__(
        self,
        base_url: str,
        *,
        api_username: str,
        api_password: str,
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        environment: dict[str, str] | None = None,
    ):
        env = environment if environment is not None else __import__("os").environ
        self.safety = PaperSafetySettings.from_environment(env)
        self.safety.assert_paper_mode()
        if not base_url.startswith("http://") and not base_url.startswith("https://"):
            raise ValueError("Hummingbot API base_url must be HTTP(S)")
        if not api_username or not api_password:
            raise ValueError("internal Hummingbot API authentication is required")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(api_username, api_password),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def _get(self, path: str, **kwargs: Any) -> Any:
        self.safety.assert_paper_mode()
        response = await self._client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        self.safety.assert_paper_mode()
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    async def get_tickers(self) -> dict[str, dict[str, Any]]:
        result = await self._get(
            "/market-data/tickers",
            params={"connectors": PAPER_CONNECTOR, "refresh": "true", "max_age": "30"},
        )
        return result.get("tickers", {}).get(PAPER_CONNECTOR, {})

    async def get_trading_rules(self) -> dict[str, dict[str, Any]]:
        result = await self._get(f"/connectors/{PAPER_CONNECTOR}/trading-rules")
        if not isinstance(result, dict):
            raise RuntimeError("Hummingbot trading-rules response is not an object")
        return result

    async def get_order_book(self, trading_pair: str, depth: int = 5) -> dict[str, Any]:
        return await self._post(
            "/market-data/order-book",
            {"connector_name": PAPER_CONNECTOR, "trading_pair": trading_pair, "depth": depth},
        )

    async def get_hourly_candles(self, trading_pair: str, max_records: int = 28) -> list[dict[str, Any]]:
        """Read public Kraken hourly candles; this is data-only, never an execution connector."""
        result = await self._post(
            "/market-data/candles",
            {
                "connector_name": PUBLIC_CANDLE_CONNECTOR,
                "trading_pair": trading_pair,
                "interval": "1h",
                "max_records": max_records,
            },
        )
        if not isinstance(result, list):
            raise RuntimeError("Kraken hourly candle response is not a list")
        return result

    async def close(self) -> None:
        await self._client.aclose()


class ResearchScanner:
    """Selects liquid, tight-spread 24h gainers; exposes snapshots for held positions."""

    def __init__(
        self,
        data_client: HummingbotPaperDataClient,
        *,
        min_quote_volume_24h: Decimal | None = None,
        entry_quote_assets: frozenset[str] | set[str] = frozenset(),
        max_spread_fraction: Decimal = Decimal("0.003"),
        min_change_24h_fraction: Decimal = Decimal("-Infinity"),
        max_ticker_age_seconds: int = 60,
        max_book_concurrency: int = 8,
        max_candidates: int = 250,
        change_cache_ttl_seconds: int = 300,
        clock=time.time,
    ):
        self.client = data_client
        if min_quote_volume_24h is not None and (
            not isinstance(min_quote_volume_24h, Decimal)
            or not min_quote_volume_24h.is_finite()
            or min_quote_volume_24h <= ZERO
        ):
            raise ValueError("min_quote_volume_24h must be a positive Decimal when configured")
        self.min_quote_volume_24h = min_quote_volume_24h
        self.entry_quote_assets = frozenset(asset.strip().upper() for asset in entry_quote_assets if asset.strip())
        self.max_spread_fraction = max_spread_fraction
        self.min_change_24h_fraction = min_change_24h_fraction
        self.max_ticker_age_seconds = max_ticker_age_seconds
        self.max_book_concurrency = max_book_concurrency
        self.max_candidates = max_candidates
        self.change_cache_ttl_seconds = change_cache_ttl_seconds
        self.clock = clock
        self._change_cache: dict[str, tuple[float, Decimal]] = {}
        self.last_scan_diagnostics: dict[str, Any] = {}

    async def _change_24h(self, pair: str, last_price: Decimal, now: float) -> Decimal | None:
        cached = self._change_cache.get(pair)
        if cached is not None and 0 <= now - cached[0] < self.change_cache_ttl_seconds:
            reference_close = cached[1]
            if reference_close <= ZERO or last_price <= ZERO:
                return None
            return last_price / reference_close - ONE
        try:
            candles = await self.client.get_hourly_candles(pair, max_records=28)
        except (httpx.HTTPError, RuntimeError, ValueError):
            return None
        cutoff = now - 86400
        observations: list[tuple[float, Decimal]] = []
        for candle in candles:
            try:
                raw_timestamp = candle.get("timestamp")
                if isinstance(raw_timestamp, str):
                    timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).timestamp()
                else:
                    timestamp = float(raw_timestamp)
                    if timestamp > 100_000_000_000:
                        timestamp /= 1000
                close = _decimal(candle.get("close"))
            except (TypeError, ValueError, OverflowError):
                continue
            if close > ZERO and timestamp > 0:
                observations.append((timestamp, close))
        eligible = [(ts, close) for ts, close in observations if cutoff - 7200 <= ts <= cutoff + 300]
        if not eligible:
            return None
        _, reference_close = max(eligible, key=lambda item: item[0])
        if reference_close <= ZERO or last_price <= ZERO:
            return None
        self._change_cache[pair] = (now, reference_close)
        return last_price / reference_close - ONE

    async def _candidate_from_snapshot(
        self,
        pair: str,
        ticker: dict[str, Any],
        rule: dict[str, Any],
        *,
        now: float,
        require_gainer: bool,
        rejection_sink: list[str] | None = None,
    ) -> MarketCandidate | None:
        if "-" not in pair:
            _reject(rejection_sink, "pair_malformed"); return None
        base, quote = pair.rsplit("-", 1)
        if not base or not quote or base in {"USDT", "USDC", "DAI", "TUSD", "USDE", "FDUSD"}:
            _reject(rejection_sink, "stablecoin_base"); return None
        if require_gainer and quote not in self.entry_quote_assets:
            _reject(rejection_sink, "quote_not_in_entry_quote_assets"); return None
        if not isinstance(rule, dict):
            _reject(rejection_sink, "invalid_rule"); return None
        # Initial purchases are MARKET BUY. Do not require LIMIT-order support.
        if rule.get("supports_market_orders") is False or rule.get("supports_market_orders") == 0 or str(rule.get("supports_market_orders", "")).strip().lower() in {"false", "no", "off", "0"}:
            _reject(rejection_sink, "unsupported_market_orders"); return None
        price = _decimal(ticker.get("price"))
        quote_volume = _decimal(ticker.get("quote_volume"))
        base_volume = _decimal(ticker.get("base_volume"))
        timestamp = _safe_float(ticker.get("timestamp"))
        if price <= ZERO:
            _reject(rejection_sink, "invalid_price"); return None
        if quote_volume <= ZERO:
            _reject(rejection_sink, "nonpositive_quote_volume"); return None
        if base_volume <= ZERO:
            _reject(rejection_sink, "nonpositive_base_volume"); return None
        if require_gainer and (
            self.min_quote_volume_24h is None or quote_volume < self.min_quote_volume_24h
        ):
            _reject(rejection_sink, "min_quote_volume"); return None
        if timestamp <= 0 or now - timestamp > self.max_ticker_age_seconds or timestamp > now + 5:
            _reject(rejection_sink, "missing_or_stale_ticker"); return None
        min_notional = _decimal(rule.get("min_notional_size"))
        min_order = _decimal(rule.get("min_order_size"))
        base_increment = _decimal(rule.get("min_base_amount_increment"))
        price_increment = _decimal(rule.get("min_price_increment"))
        # Kraken's TradingRule may omit min-notional; positive min_order_size is authoritative.
        if min_notional < ZERO or min_order <= ZERO or base_increment <= ZERO or price_increment <= ZERO:
            _reject(rejection_sink, "invalid_trading_rule"); return None
        if require_gainer and min_notional > INITIAL_ENTRY_NOTIONAL:
            _reject(rejection_sink, "min_notional_above_initial_entry"); return None

        change = ZERO
        if require_gainer:
            change = await self._change_24h(pair, price, now)
            if change is None:
                _reject(rejection_sink, "missing_or_stale_candles"); return None
            if change <= self.min_change_24h_fraction:
                _reject(rejection_sink, "change_below_min"); return None

        book = await self.client.get_order_book(pair, depth=5)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            _reject(rejection_sink, "empty_order_book"); return None
        bid = _decimal(bids[0].get("price"))
        ask = _decimal(asks[0].get("price"))
        if bid <= ZERO or ask <= bid:
            _reject(rejection_sink, "invalid_bid_ask"); return None
        mid = (bid + ask) / Decimal("2")
        spread = (ask - bid) / mid
        if spread > self.max_spread_fraction:
            _reject(rejection_sink, "spread_above_max"); return None
        return MarketCandidate(
            trading_pair=pair, base_asset=base, quote_asset=quote, last_price=price,
            best_bid=bid, best_ask=ask, quote_volume_24h=quote_volume, base_volume_24h=base_volume,
            spread_fraction=spread, ticker_timestamp=timestamp, min_order_size=min_order,
            min_notional_size=min_notional, base_amount_increment=base_increment,
            price_increment=price_increment, change_24h_fraction=change,
        )

    async def inspect_pair(self, trading_pair: str) -> MarketCandidate | None:
        """Get a current quality/liquidity snapshot for an existing position, even if it is down."""
        tickers, rules = await asyncio.gather(
            self.client.get_tickers(),
            self.client.get_trading_rules(),
        )
        ticker = tickers.get(trading_pair)
        rule = rules.get(trading_pair)
        if not isinstance(ticker, dict) or not isinstance(rule, dict):
            return None
        try:
            return await self._candidate_from_snapshot(
                trading_pair, ticker, rule, now=self.clock(), require_gainer=False
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            return None

    async def scan(self) -> list[MarketCandidate]:
        # Entry universe and liquidity semantics are Architect-configured, not guessed.
        if not self.entry_quote_assets or self.min_quote_volume_24h is None:
            self.last_scan_diagnostics={"result":"not_configured","candidate_count":0,"rejection_counts":{}}
            return []
        tickers, rules = await asyncio.gather(
            self.client.get_tickers(),
            self.client.get_trading_rules(),
        )
        now = self.clock()
        preliminary = []
        rejections: list[str] = []
        for pair, ticker in tickers.items():
            if not isinstance(ticker, dict):
                _reject(rejections, "malformed_ticker"); continue
            if "-" not in pair or pair.rsplit("-",1)[1] not in self.entry_quote_assets:
                _reject(rejections, "quote_filter"); continue
            rule = rules.get(pair)
            if not isinstance(rule, dict):
                _reject(rejections, "rule_missing"); continue
            if _decimal(ticker.get("quote_volume")) < self.min_quote_volume_24h:
                _reject(rejections, "preliminary_min_quote_volume"); continue
            preliminary.append((pair, ticker, rule))
        preliminary.sort(key=lambda row: _decimal(row[1].get("quote_volume")), reverse=True)
        preliminary = preliminary[: self.max_candidates]
        semaphore = asyncio.Semaphore(self.max_book_concurrency)

        async def inspect(row):
            pair, ticker, rule = row
            local: list[str] = []
            try:
                async with semaphore:
                    result = await self._candidate_from_snapshot(
                        pair, ticker, rule, now=now, require_gainer=True, rejection_sink=local
                    )
                if result is None and not local:
                    _reject(local, "scanner_error")
                rejections.extend(local)
                return result
            except (httpx.HTTPError, RuntimeError, ValueError):
                _reject(rejections, "scanner_error")
                return None

        inspected = await asyncio.gather(*(inspect(row) for row in preliminary))
        candidates = [candidate for candidate in inspected if candidate is not None]
        candidates.sort(
            key=lambda candidate: (
                candidate.change_24h_fraction,
                candidate.quote_volume_24h,
                -candidate.spread_fraction,
                candidate.trading_pair,
            ),
            reverse=True,
        )
        counts: dict[str, int] = {}
        for reason in rejections:
            counts[reason] = counts.get(reason, 0) + 1
        self.last_scan_diagnostics={
            "result":"scanned", "ticker_count":len(tickers), "rule_count":len(rules),
            "preliminary_count":len(preliminary), "candidate_count":len(candidates),
            "rejected_count":len(rejections), "rejection_counts":dict(sorted(counts.items())),
            "rejection_reasons":list(rejections), "max_candidates":self.max_candidates,
            "min_quote_volume_24h":str(self.min_quote_volume_24h),
            "entry_quote_assets":sorted(self.entry_quote_assets),
        }
        return candidates


def _reject(sink: list[str] | None, reason: str) -> None:
    if sink is not None:
        sink.append(reason)


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception:
        return ZERO
    return result if result.is_finite() else ZERO


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if result == result and abs(result) != float("inf") else 0.0
