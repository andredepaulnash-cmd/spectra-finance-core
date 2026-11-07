"""Unauthenticated fresh Kraken book evidence for protected LIMIT dispatch.

Hummingbot API's cached book timestamp records response time, not the venue's
last update. This provider performs a bounded public venue read instead. It
has no account credentials and cannot create/cancel orders.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from time import time
from typing import Any, Awaitable, Callable

import aiohttp

from spectra_core.kraken_market_fabric import KrakenTradingRule

PUBLIC_BASE_URL = "https://api.kraken.com/0/public/"
REQUEST_TIMEOUT_SECONDS = 5.0
DEPTH_COUNT = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_READ_AGE_SECONDS = 30.0
PublicFetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class FreshVenueBook:
    pair: str
    exchange_pair: str
    best_bid: Decimal
    best_ask: Decimal
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    requested_at: float
    received_at: float
    source: str
    response_sha256: str
    venue_min_order_size: Decimal | None = None
    venue_min_cost_quote: Decimal | None = None
    venue_base_asset: str | None = None
    venue_quote_asset: str | None = None
    venue_rules_observed_at: float | None = None
    venue_rules_sha256: str | None = None

    @property
    def market_observed_at(self) -> float:
        if self.venue_rules_observed_at is None:
            return self.received_at
        return min(self.received_at, _timestamp(self.venue_rules_observed_at))


def _timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("fresh venue clock must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("fresh venue clock must be finite and positive") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError("fresh venue clock must be finite and positive")
    return number


def _positive(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"fresh venue {field} must be finite and positive")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"fresh venue {field} must be finite and positive") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"fresh venue {field} must be finite and positive")
    return number


def _asset(value: str) -> str:
    return {"XBT": "BTC", "XDG": "DOGE"}.get(value, value)


def _internal_asset(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{2,16}", value):
        raise ValueError("fresh Kraken AssetPairs lacks a valid base/quote asset")
    # Pinned Kraken connector's convert_from_exchange_symbol follows this
    # legacy four-character X/Z naming convention. Require independent wsname
    # agreement below; stripping an identifier alone never proves pair scope.
    if len(value) == 4 and value[0] in {"X", "Z"}:
        value = value[1:]
    return _asset(value)


def _venue_alias(pair: str) -> str:
    parts = pair.split("-")
    if len(parts) != 2 or not all(
        re.fullmatch(r"[A-Z0-9]{2,16}", token) for token in parts
    ):
        raise ValueError(
            "fresh venue pair must contain canonical base and quote assets"
        )
    aliases = {"BTC": "XBT", "DOGE": "XDG"}
    return "".join(aliases.get(token, token) for token in parts)


def _result(raw: Any, endpoint: str) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or raw.get("error") != []
        or not isinstance(raw.get("result"), dict)
    ):
        raise ValueError(
            f"fresh Kraken {endpoint} response is malformed or reports errors"
        )
    return raw["result"]


async def _public_fetch(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    if endpoint not in {"AssetPairs", "Depth"}:
        raise ValueError("unsupported public Kraken endpoint")
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            PUBLIC_BASE_URL + endpoint, params=params, allow_redirects=False
        ) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("fresh Kraken public response was not HTTP200")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        "fresh Kraken public response exceeded its size bound"
                    )
                chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))


async def fetch_protected_kraken_book(
    trading_rule: KrakenTradingRule,
    *,
    fetcher: PublicFetcher | None = None,
    clock: Callable[[], float] = time,
) -> FreshVenueBook:
    """Resolve the exact pair, then obtain a new public Depth response.

    AssetPairs wsname and base/quote fields prove canonical identity and the
    denomination of exact positive ordermin/costmin. An unvalidated response
    key or concatenated symbol is never accepted as ownership proof.
    A clock regression, slow request, stale read, transport error, ambiguous
    pair mapping, empty/crossed book or invalid price/size fails closed.
    Tests inject the fetcher and clock; no live venue read is needed.
    """
    if not isinstance(trading_rule, KrakenTradingRule):
        raise ValueError("fresh Kraken book requires its authoritative trading rule")
    pair = trading_rule.trading_pair.strip().upper()
    alias = _venue_alias(pair)
    canonical_pair = "-".join(_asset(token) for token in pair.split("-"))
    request = fetcher or _public_fetch
    started_at = _timestamp(clock())

    async def bounded(endpoint: str, params: dict[str, Any]):
        requested_at = _timestamp(clock())
        try:
            raw = await asyncio.wait_for(
                request(endpoint, params), timeout=REQUEST_TIMEOUT_SECONDS
            )
        except Exception as exc:
            raise ValueError("fresh Kraken venue read failed or timed out") from exc
        received_at = _timestamp(clock())
        if (
            received_at < requested_at
            or received_at - requested_at > REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "fresh Kraken request clock regressed or exceeded five seconds"
            )
        return raw, requested_at, received_at

    pair_response, _, rules_received_at = await bounded("AssetPairs", {"pair": alias})
    rows = _result(pair_response, "AssetPairs")
    matches = []
    for exchange_key, row in rows.items():
        if not isinstance(exchange_key, str) or not isinstance(row, dict):
            raise ValueError("fresh Kraken AssetPairs contains an invalid row")
        wsname, altname = row.get("wsname"), row.get("altname")
        if not isinstance(wsname, str) or not isinstance(altname, str):
            raise ValueError("fresh Kraken AssetPairs lacks canonical mapping")
        tokens = wsname.upper().split("/")
        canonical = "-".join(_asset(token) for token in tokens)
        if len(tokens) == 2 and canonical == canonical_pair:
            if not re.fullmatch(r"[A-Za-z0-9]{2,40}", altname) or not re.fullmatch(
                r"[A-Za-z0-9]{2,40}", exchange_key
            ):
                raise ValueError("fresh Kraken pair alias is not a safe spot symbol")
            matches.append((exchange_key, altname, row))
    if len(matches) != 1:
        raise ValueError("fresh Kraken pair mapping is missing or ambiguous")
    exchange_key, altname, pair_row = matches[0]
    base_asset, quote_asset = canonical_pair.split("-")
    if (
        pair_row.get("aclass_base") != "currency"
        or pair_row.get("aclass_quote") != "currency"
        or _internal_asset(pair_row.get("base")) != base_asset
        or _internal_asset(pair_row.get("quote")) != quote_asset
    ):
        raise ValueError("fresh Kraken AssetPairs base/quote or spot scope conflicts")
    minimum_base = _positive(pair_row.get("ordermin"), "ordermin")
    minimum_cost = _positive(pair_row.get("costmin"), "costmin")
    rules_digest = hashlib.sha256(
        json.dumps(pair_response, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw, requested_at, received_at = await bounded(
        "Depth", {"pair": altname, "count": DEPTH_COUNT}
    )
    result = _result(raw, "Depth")
    if len(result) != 1 or next(iter(result)) not in {exchange_key, altname}:
        raise ValueError("fresh Kraken Depth response crosses pair scope")
    depth = next(iter(result.values()))
    if not isinstance(depth, dict):
        raise ValueError("fresh Kraken Depth has no book")

    def levels(side: str) -> tuple[tuple[Decimal, Decimal], ...]:
        values = depth.get(side)
        if not isinstance(values, list) or not 1 <= len(values) <= DEPTH_COUNT:
            raise ValueError("fresh Kraken Depth must contain bounded non-empty sides")
        parsed = []
        for row in values:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                raise ValueError("fresh Kraken Depth contains an invalid price level")
            price, amount = _positive(row[0], "price"), _positive(row[1], "amount")
            _timestamp(row[2])
            parsed.append((price, amount))
        prices = [price for price, _ in parsed]
        if len(set(prices)) != len(prices) or prices != sorted(
            prices, reverse=(side == "bids")
        ):
            raise ValueError("fresh Kraken Depth levels are duplicated or unsorted")
        return tuple(parsed)

    bids, asks = levels("bids"), levels("asks")
    if bids[0][0] >= asks[0][0]:
        raise ValueError("fresh Kraken venue book is crossed or locked")
    if received_at < started_at or received_at - started_at > MAX_READ_AGE_SECONDS:
        raise ValueError("fresh Kraken venue read is stale or clock regressed")
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FreshVenueBook(
        pair,
        altname,
        bids[0][0],
        asks[0][0],
        bids,
        asks,
        requested_at,
        received_at,
        "kraken_public_rest_depth",
        digest,
        venue_min_order_size=minimum_base,
        venue_min_cost_quote=minimum_cost,
        venue_base_asset=base_asset,
        venue_quote_asset=quote_asset,
        venue_rules_observed_at=rules_received_at,
        venue_rules_sha256=rules_digest,
    )
