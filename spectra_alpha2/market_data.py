"""Public-market-data-only Kraken paper connector client and scanner."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .safety import PaperSafetySettings


PAPER_CONNECTOR = "kraken_paper_trade"
ZERO = Decimal("0")


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


class HummingbotPaperDataClient:
    """Read-only public market data client; contains no exchange credential support."""

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

    async def close(self) -> None:
        await self._client.aclose()


class ResearchScanner:
    """Filters public Kraken spot markets into a deterministic candidate universe."""

    def __init__(
        self,
        data_client: HummingbotPaperDataClient,
        *,
        min_quote_volume_24h: Decimal = Decimal("100000"),
        max_spread_fraction: Decimal = Decimal("0.003"),
        max_ticker_age_seconds: int = 60,
        max_book_concurrency: int = 8,
        max_candidates: int = 120,
        clock=time.time,
    ):
        self.client = data_client
        self.min_quote_volume_24h = min_quote_volume_24h
        self.max_spread_fraction = max_spread_fraction
        self.max_ticker_age_seconds = max_ticker_age_seconds
        self.max_book_concurrency = max_book_concurrency
        self.max_candidates = max_candidates
        self.clock = clock

    async def scan(self) -> list[MarketCandidate]:
        tickers, rules = await asyncio.gather(
            self.client.get_tickers(),
            self.client.get_trading_rules(),
        )
        now = self.clock()
        preliminary: list[tuple[str, dict, dict]] = []
        for pair, ticker in tickers.items():
            if not pair.endswith("-USDT") or "-" not in pair:
                continue
            base, quote = pair.rsplit("-", 1)
            if quote != "USDT" or not base or base in {"USDT", "USDC", "DAI", "TUSD", "USDE", "FDUSD"}:
                continue
            rule = rules.get(pair)
            if not isinstance(rule, dict) or not rule.get("supports_limit_orders", False):
                continue
            price = _decimal(ticker.get("price"))
            quote_volume = _decimal(ticker.get("quote_volume"))
            base_volume = _decimal(ticker.get("base_volume"))
            timestamp = float(ticker.get("timestamp") or 0)
            if price <= ZERO or quote_volume < self.min_quote_volume_24h or base_volume <= ZERO:
                continue
            if timestamp <= 0 or now - timestamp > self.max_ticker_age_seconds or timestamp > now + 5:
                continue
            min_notional = _decimal(rule.get("min_notional_size"))
            min_order = _decimal(rule.get("min_order_size"))
            base_increment=_decimal(rule.get("min_base_amount_increment"))
            price_increment=_decimal(rule.get("min_price_increment"))
            if min_notional > Decimal("20") or min_order <= ZERO or base_increment <= ZERO or price_increment <= ZERO:
                continue
            preliminary.append((pair,ticker,rule))
        preliminary.sort(key=lambda row: _decimal(row[1].get("quote_volume")), reverse=True)
        preliminary = preliminary[: self.max_candidates]

        semaphore = asyncio.Semaphore(self.max_book_concurrency)

        async def inspect(row: tuple[str, dict, dict]) -> MarketCandidate | None:
            pair,ticker,rule=row
            try:
                async with semaphore:
                    book=await self.client.get_order_book(pair,depth=5)
            except (httpx.HTTPError, RuntimeError, ValueError):
                return None
            bids=book.get("bids") or []
            asks=book.get("asks") or []
            if not bids or not asks:
                return None
            bid=_decimal(bids[0].get("price"))
            ask=_decimal(asks[0].get("price"))
            if bid <= ZERO or ask <= bid:
                return None
            mid=(bid+ask)/Decimal("2")
            spread=(ask-bid)/mid
            if spread > self.max_spread_fraction:
                return None
            base,quote=pair.rsplit("-",1)
            return MarketCandidate(
                trading_pair=pair,
                base_asset=base,
                quote_asset=quote,
                last_price=_decimal(ticker.get("price")),
                best_bid=bid,
                best_ask=ask,
                quote_volume_24h=_decimal(ticker.get("quote_volume")),
                base_volume_24h=_decimal(ticker.get("base_volume")),
                spread_fraction=spread,
                ticker_timestamp=float(ticker["timestamp"]),
                min_order_size=_decimal(rule.get("min_order_size")),
                min_notional_size=_decimal(rule.get("min_notional_size")),
                base_amount_increment=_decimal(rule.get("min_base_amount_increment")),
                price_increment=_decimal(rule.get("min_price_increment")),
            )

        inspected=await asyncio.gather(*(inspect(row) for row in preliminary))
        return [candidate for candidate in inspected if candidate is not None]


def _decimal(value: Any) -> Decimal:
    try:
        result=Decimal(str(value))
    except Exception:
        return ZERO
    return result if result.is_finite() else ZERO
