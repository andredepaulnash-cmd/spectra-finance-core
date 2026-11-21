"""Quote-budgeted market buys and fee-aware LIMIT exit construction."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from .market_data import MarketCandidate

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class PaperOrderDraft:
    request_body: dict[str, str]
    gross_quote_notional: Decimal
    limit_price: Decimal
    base_quantity: Decimal


def _round_to_increment(value: Decimal, increment: Decimal, rounding) -> Decimal:
    if value <= ZERO or increment <= ZERO:
        raise ValueError("value and exchange increment must be positive")
    units = (value / increment).to_integral_value(rounding=rounding)
    return units * increment


def build_buy_market_order(
    candidate: MarketCandidate,
    *,
    gross_quote_budget: Decimal,
    max_slippage_fraction: Decimal = Decimal("0.001"),
    account_name: str = "master_account",
) -> PaperOrderDraft:
    """Build a MARKET BUY whose base quantity is conservatively budgeted.

    Hummingbot's spot MARKET BUY accepts base quantity. The maximum-price estimate
    is used only to size that quantity; it is deliberately not sent as a LIMIT
    price. The signed Governor reservation remains the authoritative quote cap,
    and fill settlement independently rejects actual gross/fees above that cap.
    """
    if not isinstance(gross_quote_budget, Decimal) or not gross_quote_budget.is_finite() or gross_quote_budget <= ZERO:
        raise ValueError("gross_quote_budget must be a positive Decimal")
    if not isinstance(max_slippage_fraction, Decimal) or not max_slippage_fraction.is_finite() or not ZERO <= max_slippage_fraction <= Decimal("0.02"):
        raise ValueError("max slippage must be between 0% and 2%")
    if candidate.best_ask <= ZERO or candidate.price_increment <= ZERO or candidate.base_amount_increment <= ZERO:
        raise ValueError("candidate ask and exchange increments must be positive")

    maximum_execution_price = _round_to_increment(
        candidate.best_ask * (ONE + max_slippage_fraction),
        candidate.price_increment,
        ROUND_UP,
    )
    quantity = _round_to_increment(
        gross_quote_budget / maximum_execution_price,
        candidate.base_amount_increment,
        ROUND_DOWN,
    )
    quote_notional = quantity * maximum_execution_price
    if quantity < candidate.min_order_size:
        raise ValueError("authorized budget is below exchange minimum base order size")
    if quote_notional < candidate.min_notional_size:
        raise ValueError("authorized budget is below exchange minimum quote notional")
    if quote_notional > gross_quote_budget:
        raise ValueError("market-buy quantity would exceed the authorized quote budget")

    body = {
        "account_name": account_name,
        "connector_name": "kraken_paper_trade",
        "trading_pair": candidate.trading_pair,
        "trade_type": "BUY",
        "amount": str(quantity),
        "order_type": "MARKET",
        "max_execution_price": str(maximum_execution_price),
        "position_action": "OPEN",
    }
    return PaperOrderDraft(body, quote_notional, maximum_execution_price, quantity)


