"""Price and cash bounds for protected, single-parcel Kraken spot buys.

The supported backend primitive is a fixed-price OrderExecutor LIMIT order.
Native DCA/GRID executors can autonomously close with MARKET orders, so this
adapter executes one explicitly funded parcel and leaves subsequent lifecycle
decisions to Spectra. It does not add IOC or time-in-force fields: the inspected
Hummingbot/Kraken transport does not expose them. LIMIT orders may rest as GTC.

This pure adapter does not attest an installed backend, authorize capital, or
submit orders. Deployment must separately verify its pinned backend semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    Inexact,
    Rounded,
    localcontext,
)
from functools import wraps
from math import isfinite
from typing import Any

from .kraken_market_fabric import KrakenTradingRule

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
MAX_MARKET_AGE_SECONDS = 30.0


def _financial_precision(function):
    @wraps(function)
    def bounded_context(*args, **kwargs):
        # Inputs are bounded below. This precision exceeds every exact product
        # and preserves integer-side rounding even near a venue step boundary.
        # It is isolated from process-wide precision/rounding/trap settings.
        with localcontext() as context:
            context.prec = 2048
            context.rounding = ROUND_HALF_EVEN
            context.traps[Inexact] = False
            context.traps[Rounded] = False
            return function(*args, **kwargs)

    return bounded_context


@dataclass(frozen=True)
class BoundedBuyOrder:
    executor_config: dict[str, Any]
    authorized_quote_budget: Decimal
    reference_price: Decimal
    price_cap: Decimal
    limit_price: Decimal
    amount_base: Decimal
    max_principal_quote: Decimal
    max_fee_quote: Decimal
    max_total_quote: Decimal


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite() or result < ZERO or (positive and result == ZERO):
        raise ValueError(
            f"{field} must be finite and {'positive' if positive else 'non-negative'}"
        )
    parts = result.as_tuple()
    if len(parts.digits) > 128 or abs(parts.exponent) > 128:
        raise ValueError(f"{field} exceeds the supported exact decimal geometry")
    return result


def _timestamp(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite positive timestamp")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive timestamp") from exc
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a finite positive timestamp")
    return result


def _parcel_geometry(
    executor_type: str, config: dict[str, Any]
) -> tuple[Decimal, Decimal, Decimal | None]:
    if executor_type == "dca_executor":
        amounts, prices = config.get("amounts_quote"), config.get("prices")
        if (
            not isinstance(amounts, (tuple, list))
            or not isinstance(prices, (tuple, list))
            or len(amounts) != 1
            or len(prices) != 1
        ):
            raise ValueError("protected DCA buy requires exactly one funded parcel")
        return (
            _decimal(amounts[0], "amounts_quote[0]", positive=True),
            _decimal(prices[0], "prices[0]", positive=True),
            None,
        )
    if executor_type == "grid_executor":
        budget = _decimal(
            config.get("total_amount_quote"), "total_amount_quote", positive=True
        )
        child = _decimal(
            config.get("min_order_amount_quote"),
            "min_order_amount_quote",
            positive=True,
        )
        if (
            child != budget
            or isinstance(config.get("max_open_orders"), bool)
            or config.get("max_open_orders") != 1
        ):
            raise ValueError(
                "protected GRID buy requires one explicit fully funded parcel"
            )
        batch = config.get("max_orders_per_batch")
        if batch is not None and (isinstance(batch, bool) or batch != 1):
            raise ValueError(
                "protected GRID buy cannot contain multiple parcels per batch"
            )
        start = _decimal(config.get("start_price"), "start_price", positive=True)
        end = _decimal(config.get("end_price"), "end_price", positive=True)
        if start >= end:
            raise ValueError("protected GRID price range is invalid")
        return budget, (start + end) / Decimal("2"), end
    if executor_type == "order_executor":
        price = _decimal(config.get("price"), "price", positive=True)
        amount = _decimal(config.get("amount"), "amount", positive=True)
        return amount * price, price, price
    raise ValueError(
        "protected buy supports only single-parcel DCA, GRID or order executors"
    )


@_financial_precision
def build_protected_buy_limit_config(
    executor_type: str,
    config: dict[str, Any],
    *,
    trading_rule: KrakenTradingRule,
    best_ask: Decimal,
    market_observed_at: float,
    now: float,
    max_price_slippage_bps: Decimal,
    fee_allowance_bps: Decimal,
) -> BoundedBuyOrder:
    """Convert a protected BUY parcel into a bounded, marketable LIMIT config.

    The native quote budget remains the immutable capital authorization Q.
    The order's maximum principal plus explicit fee allowance stays within Q;
    callers must commit the original budget, not the smaller amount*price.
    Any separate governor fee headroom remains reserved until exact terminal
    reconciliation permits its release. No additional capital is authorized.

    Reference price comes from the original parcel, never the fresh ask. Tick
    rounding may raise the ask only within that immutable price/slippage cap.
    Marketability is measured at observation time; execution is not guaranteed.
    """
    if not isinstance(config, dict):
        raise ValueError("protected buy config must be an object")
    if config.get("type") not in (None, executor_type):
        raise ValueError("protected buy executor type conflicts")
    if config.get("side") not in (1, "1", "BUY") or isinstance(
        config.get("side"), bool
    ):
        raise ValueError("protected normal execution requires BUY")
    if any(
        field in config for field in ("time_in_force", "timeinforce", "execution_mode")
    ):
        raise ValueError("protected buy config contains unsupported execution fields")
    if str(config.get("connector_name", "")).strip().lower() != "kraken":
        raise ValueError("protected normal execution requires Kraken spot")
    if _decimal(config.get("leverage", 1), "leverage", positive=True) != ONE:
        raise ValueError("protected normal execution requires leverage=1")
    pair = str(config.get("trading_pair", "")).strip().upper()
    if not pair or pair != trading_rule.trading_pair.strip().upper():
        raise ValueError("protected buy pair does not match trading rule")
    if not trading_rule.supports_limit_orders:
        raise ValueError("venue does not support LIMIT orders")
    tick = _decimal(
        trading_rule.min_price_increment, "min_price_increment", positive=True
    )
    step = _decimal(
        trading_rule.min_base_amount_increment,
        "min_base_amount_increment",
        positive=True,
    )
    minimum_base = _decimal(trading_rule.min_order_size, "min_order_size")
    minimum_quote = max(
        _decimal(trading_rule.min_notional_size, "min_notional_size"),
        _decimal(trading_rule.min_order_value, "min_order_value"),
    )
    observed_at = _timestamp(market_observed_at, "market_observed_at")
    current_time = _timestamp(now, "now")
    if (
        observed_at > current_time
        or current_time - observed_at > MAX_MARKET_AGE_SECONDS
    ):
        raise ValueError("protected buy market observation is stale or future")
    ask = _decimal(best_ask, "best_ask", positive=True)
    slippage = _decimal(max_price_slippage_bps, "max_price_slippage_bps")
    fees = _decimal(fee_allowance_bps, "fee_allowance_bps")
    if slippage >= BPS or fees >= BPS:
        raise ValueError(
            "protected buy slippage and fee allowances must be below 10000 bps"
        )
    budget, reference, original_cap = _parcel_geometry(executor_type, config)
    admitted_cap = reference * (ONE + slippage / BPS)
    if original_cap is not None:
        admitted_cap = min(admitted_cap, original_cap)
    price_cap = (admitted_cap / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    price = (ask / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if price <= ZERO or price > price_cap:
        raise ValueError(
            "fresh ask cannot be made marketable within the immutable buy price cap"
        )
    amount = (budget / (price * (ONE + fees / BPS)) / step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * step
    if executor_type == "order_executor":
        original_amount = _decimal(config["amount"], "amount", positive=True)
        original_step_amount = (original_amount / step).to_integral_value(
            rounding=ROUND_FLOOR
        ) * step
        amount = min(amount, original_step_amount)
    principal = amount * price
    fee_quote = principal * fees / BPS
    total = principal + fee_quote
    if amount <= ZERO or amount < minimum_base or principal < minimum_quote:
        raise ValueError("bounded protected buy cannot meet current venue minimums")
    if total > budget:
        raise RuntimeError("bounded protected buy exceeds its immutable quote budget")
    payload = {
        "type": "order_executor",
        "connector_name": "kraken",
        "trading_pair": pair,
        "side": 1,
        "amount": str(amount),
        "price": str(price),
        "execution_strategy": "LIMIT",
        "leverage": 1,
        "position_action": "OPEN",
    }
    for field in ("id", "controller_id", "level_id"):
        if config.get(field) is not None:
            if not isinstance(config[field], str) or not config[field].strip():
                raise ValueError(f"protected buy {field} must be a non-empty string")
            payload[field] = config[field]
    if config.get("timestamp") is not None:
        payload["timestamp"] = _timestamp(config["timestamp"], "timestamp")
    return BoundedBuyOrder(
        payload,
        budget,
        reference,
        price_cap,
        price,
        amount,
        principal,
        fee_quote,
        total,
    )


@_financial_precision
def build_protected_sell_limit_price(
    minimum_price: Decimal,
    *,
    best_bid: Decimal,
    trading_rule: KrakenTradingRule,
    market_observed_at: float,
    now: float,
) -> Decimal:
    """Select a legal marketable SELL price without lowering the profit floor.

    The caller supplies the exact immutable owner/basis/fee-derived minimum,
    and separately validates sell quantity and order minima. Bid evidence must
    represent a trusted venue observation; an API receipt timestamp alone is
    not proof of a live tracker. Partial fills, cancel races and uncertain
    submissions continue to require exact terminal reconciliation.
    """
    if not trading_rule.supports_limit_orders:
        raise ValueError("venue does not support LIMIT orders")
    tick = _decimal(
        trading_rule.min_price_increment, "min_price_increment", positive=True
    )
    floor = _decimal(minimum_price, "minimum_price", positive=True)
    bid = _decimal(best_bid, "best_bid", positive=True)
    observed_at = _timestamp(market_observed_at, "market_observed_at")
    current_time = _timestamp(now, "now")
    if (
        observed_at > current_time
        or current_time - observed_at > MAX_MARKET_AGE_SECONDS
    ):
        raise ValueError("protected sell market observation is stale or future")
    floor_tick = (floor / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    bid_tick = (bid / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if bid_tick <= ZERO or bid_tick < floor_tick:
        raise ValueError("fresh bid cannot meet the immutable protected SELL floor")
    return bid_tick
