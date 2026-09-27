from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence


ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a valid decimal: {value!r}") from exc

    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and result <= ZERO:
        raise ValueError(f"{field} must be positive")
    if not positive and result < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return result


def _pair_parts(trading_pair: str) -> tuple[str, str]:
    pair = str(trading_pair).strip().upper()
    if "-" not in pair:
        raise ValueError(f"invalid Kraken pair: {trading_pair!r}")
    base, quote = pair.rsplit("-", 1)
    if not base or not quote:
        raise ValueError(f"invalid Kraken pair: {trading_pair!r}")
    return base, quote


@dataclass(frozen=True)
class KrakenTicker:
    trading_pair: str
    price: Decimal
    base_volume_24h: Decimal
    quote_volume_24h: Decimal
    timestamp: float


@dataclass(frozen=True)
class KrakenTradingRule:
    trading_pair: str
    min_order_size: Decimal
    min_notional_size: Decimal
    min_order_value: Decimal
    min_price_increment: Decimal
    min_base_amount_increment: Decimal
    min_quote_amount_increment: Decimal
    supports_limit_orders: bool
    supports_market_orders: bool

    @property
    def effective_min_quote(self) -> Decimal:
        return max(self.min_notional_size, self.min_order_value)


@dataclass(frozen=True)
class KrakenBookMetrics:
    trading_pair: str
    best_bid: Decimal
    best_ask: Decimal
    mid_price: Decimal
    spread_quote: Decimal
    spread_bps: Decimal
    bid_depth_quote: Decimal
    ask_depth_quote: Decimal
    depth_imbalance: Decimal
    microprice: Decimal
    microprice_edge_bps: Decimal
    timestamp: float


@dataclass(frozen=True)
class KrakenUniverseSnapshot:
    tickers: dict[str, KrakenTicker]
    trading_rules: dict[str, KrakenTradingRule]
    eligible_pairs: tuple[str, ...]
    rejected: dict[str, str]
    updated_at: float | None
    books: dict[str, KrakenBookMetrics]


def normalize_kraken_tickers(
    payload: Mapping[str, Any],
    *,
    quote_assets: Iterable[str] = ("USD",),
) -> tuple[dict[str, KrakenTicker], dict[str, str], float | None]:
    """Normalize the Hummingbot API Kraken ticker payload.

    The function is deliberately per-pair tolerant: one malformed market is
    rejected with a reason instead of destroying the whole universe snapshot.
    The caller still receives that rejection for observability.
    """
    quote_allowlist = {str(q).strip().upper() for q in quote_assets if str(q).strip()}
    tickers_root = payload.get("tickers")
    if not isinstance(tickers_root, Mapping):
        raise ValueError("ticker payload has no 'tickers' mapping")

    raw = tickers_root.get("kraken")
    if not isinstance(raw, Mapping):
        raise ValueError("ticker payload has no Kraken ticker mapping")

    normalized: dict[str, KrakenTicker] = {}
    rejected: dict[str, str] = {}

    for raw_pair, row in raw.items():
        pair = str(raw_pair).strip().upper()
        try:
            _, quote = _pair_parts(pair)
            if quote_allowlist and quote not in quote_allowlist:
                continue
            if not isinstance(row, Mapping):
                raise ValueError("ticker row is not a mapping")

            price = _decimal(row.get("price"), f"{pair}.price", positive=True)
            base_volume = _decimal(
                row.get("base_volume", 0),
                f"{pair}.base_volume",
            )
            quote_volume_raw = row.get("quote_volume")
            quote_volume = (
                _decimal(quote_volume_raw, f"{pair}.quote_volume")
                if quote_volume_raw is not None
                else base_volume * price
            )

            normalized[pair] = KrakenTicker(
                trading_pair=pair,
                price=price,
                base_volume_24h=base_volume,
                quote_volume_24h=quote_volume,
                timestamp=float(row.get("timestamp") or 0.0),
            )
        except Exception as exc:
            rejected[pair or str(raw_pair)] = str(exc)

    updated = payload.get("updated_at")
    updated_at: float | None = None
    if isinstance(updated, Mapping) and updated.get("kraken") is not None:
        try:
            updated_at = float(updated["kraken"])
        except (TypeError, ValueError):
            updated_at = None

    if updated_at is None and normalized:
        latest = max(t.timestamp for t in normalized.values())
        updated_at = latest or None

    return normalized, rejected, updated_at


def normalize_kraken_trading_rules(
    payload: Mapping[str, Any],
    *,
    quote_assets: Iterable[str] = ("USD",),
) -> tuple[dict[str, KrakenTradingRule], dict[str, str]]:
    quote_allowlist = {str(q).strip().upper() for q in quote_assets if str(q).strip()}
    normalized: dict[str, KrakenTradingRule] = {}
    rejected: dict[str, str] = {}

    for raw_pair, row in payload.items():
        pair = str(raw_pair).strip().upper()
        if pair == "ERROR":
            continue
        try:
            _, quote = _pair_parts(pair)
            if quote_allowlist and quote not in quote_allowlist:
                continue
            if not isinstance(row, Mapping):
                raise ValueError("trading-rule row is not a mapping")
            if row.get("error"):
                raise ValueError(str(row["error"]))

            normalized[pair] = KrakenTradingRule(
                trading_pair=pair,
                min_order_size=_decimal(
                    row.get("min_order_size", 0),
                    f"{pair}.min_order_size",
                ),
                min_notional_size=_decimal(
                    row.get("min_notional_size", 0),
                    f"{pair}.min_notional_size",
                ),
                min_order_value=_decimal(
                    row.get("min_order_value", 0),
                    f"{pair}.min_order_value",
                ),
                min_price_increment=_decimal(
                    row.get("min_price_increment", 0),
                    f"{pair}.min_price_increment",
                    positive=True,
                ),
                min_base_amount_increment=_decimal(
                    row.get("min_base_amount_increment", 0),
                    f"{pair}.min_base_amount_increment",
                    positive=True,
                ),
                min_quote_amount_increment=_decimal(
                    row.get("min_quote_amount_increment", 0),
                    f"{pair}.min_quote_amount_increment",
                ),
                supports_limit_orders=bool(row.get("supports_limit_orders", False)),
                supports_market_orders=bool(row.get("supports_market_orders", False)),
            )
        except Exception as exc:
            rejected[pair or str(raw_pair)] = str(exc)

    return normalized, rejected


def _book_level(level: Any, *, side: str, index: int) -> tuple[Decimal, Decimal]:
    if isinstance(level, Mapping):
        price_raw = level.get("price")
        amount_raw = level.get("amount")
    elif isinstance(level, Sequence) and not isinstance(level, (str, bytes)) and len(level) >= 2:
        price_raw, amount_raw = level[0], level[1]
    else:
        raise ValueError(f"{side}[{index}] is not a price/amount level")

    price = _decimal(price_raw, f"{side}[{index}].price", positive=True)
    amount = _decimal(amount_raw, f"{side}[{index}].amount", positive=True)
    return price, amount


def summarize_kraken_order_book(
    trading_pair: str,
    payload: Mapping[str, Any],
) -> KrakenBookMetrics:
    pair = str(trading_pair).strip().upper()
    _pair_parts(pair)

    raw_bids = payload.get("bids")
    raw_asks = payload.get("asks")
    if not isinstance(raw_bids, Sequence) or isinstance(raw_bids, (str, bytes)):
        raise ValueError("order book bids are missing")
    if not isinstance(raw_asks, Sequence) or isinstance(raw_asks, (str, bytes)):
        raise ValueError("order book asks are missing")
    if not raw_bids or not raw_asks:
        raise ValueError("order book must contain both bid and ask liquidity")

    bids = [_book_level(level, side="bids", index=i) for i, level in enumerate(raw_bids)]
    asks = [_book_level(level, side="asks", index=i) for i, level in enumerate(raw_asks)]

    best_bid = max(price for price, _ in bids)
    best_ask = min(price for price, _ in asks)
    if best_bid >= best_ask:
        raise ValueError("crossed or locked order book")

    best_bid_size = sum(amount for price, amount in bids if price == best_bid)
    best_ask_size = sum(amount for price, amount in asks if price == best_ask)

    mid = (best_bid + best_ask) / Decimal("2")
    spread = best_ask - best_bid
    spread_bps = spread / mid * TEN_THOUSAND

    bid_depth_quote = sum(price * amount for price, amount in bids)
    ask_depth_quote = sum(price * amount for price, amount in asks)
    total_depth = bid_depth_quote + ask_depth_quote
    imbalance = (
        (bid_depth_quote - ask_depth_quote) / total_depth
        if total_depth > ZERO
        else ZERO
    )

    top_size = best_bid_size + best_ask_size
    microprice = (
        (best_ask * best_bid_size + best_bid * best_ask_size) / top_size
        if top_size > ZERO
        else mid
    )
    microprice_edge_bps = (microprice - mid) / mid * TEN_THOUSAND

    try:
        timestamp = float(payload.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0

    return KrakenBookMetrics(
        trading_pair=pair,
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid,
        spread_quote=spread,
        spread_bps=spread_bps,
        bid_depth_quote=bid_depth_quote,
        ask_depth_quote=ask_depth_quote,
        depth_imbalance=imbalance,
        microprice=microprice,
        microprice_edge_bps=microprice_edge_bps,
        timestamp=timestamp,
    )


async def collect_kraken_universe(
    client: Any,
    *,
    quote_assets: Iterable[str] = ("USD",),
    order_book_pairs: Iterable[str] = (),
    order_book_depth: int = 10,
) -> KrakenUniverseSnapshot:
    """Collect one canonical Kraken universe snapshot through Hummingbot API.

    Bulk ticker and trading-rule calls define the eligible universe.  Order books
    are intentionally opt-in for a small candidate set; opening hundreds of books
    just to discover the universe would waste exchange/API resources.
    """
    if order_book_depth <= 0:
        raise ValueError("order_book_depth must be positive")

    ticker_payload = await client.market_data.get_tickers("kraken")
    rules_payload = await client.connectors.get_trading_rules("kraken")

    if not isinstance(ticker_payload, Mapping):
        raise RuntimeError("Kraken ticker response is not a mapping")
    if not isinstance(rules_payload, Mapping):
        raise RuntimeError("Kraken trading-rules response is not a mapping")

    tickers, ticker_rejected, updated_at = normalize_kraken_tickers(
        ticker_payload,
        quote_assets=quote_assets,
    )
    rules, rule_rejected = normalize_kraken_trading_rules(
        rules_payload,
        quote_assets=quote_assets,
    )

    rejected = {**ticker_rejected, **rule_rejected}
    eligible: list[str] = []
    for pair in sorted(set(tickers) | set(rules)):
        if pair not in tickers:
            rejected.setdefault(pair, "missing live Kraken ticker")
            continue
        if pair not in rules:
            rejected.setdefault(pair, "missing Kraken trading rule")
            continue
        rule = rules[pair]
        if not (rule.supports_limit_orders or rule.supports_market_orders):
            rejected.setdefault(pair, "no supported spot order type")
            continue
        eligible.append(pair)

    books: dict[str, KrakenBookMetrics] = {}
    requested_books = {str(pair).strip().upper() for pair in order_book_pairs}
    for pair in sorted(requested_books):
        if pair not in eligible:
            rejected.setdefault(pair, "order book requested for ineligible pair")
            continue
        try:
            payload = await client.market_data.get_order_book(
                connector_name="kraken",
                trading_pair=pair,
                depth=order_book_depth,
            )
            if not isinstance(payload, Mapping):
                raise RuntimeError("order book response is not a mapping")
            books[pair] = summarize_kraken_order_book(pair, payload)
        except Exception as exc:
            rejected[pair] = f"order book unavailable: {exc}"

    return KrakenUniverseSnapshot(
        tickers=tickers,
        trading_rules=rules,
        eligible_pairs=tuple(eligible),
        rejected=rejected,
        updated_at=updated_at,
        books=books,
    )
