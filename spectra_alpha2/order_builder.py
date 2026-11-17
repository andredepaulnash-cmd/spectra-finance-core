"""Quote-budgeted, marketable LIMIT order construction."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from .market_data import MarketCandidate
from .policy import gross_sell_notional_for_net_target

ZERO=Decimal("0")


@dataclass(frozen=True, slots=True)
class PaperOrderDraft:
    request_body: dict[str,str]
    gross_quote_notional: Decimal
    limit_price: Decimal
    base_quantity: Decimal


def _round_to_increment(value: Decimal, increment: Decimal, rounding) -> Decimal:
    if value <= ZERO or increment <= ZERO:
        raise ValueError("value and exchange increment must be positive")
    units=(value/increment).to_integral_value(rounding=rounding)
    return units*increment


def build_buy_limit_order(
    candidate: MarketCandidate,
    *,
    gross_quote_budget: Decimal,
    max_slippage_fraction: Decimal=Decimal("0.001"),
    account_name: str="master_account",
) -> PaperOrderDraft:
    if not isinstance(gross_quote_budget,Decimal) or not gross_quote_budget.is_finite() or gross_quote_budget<=ZERO:
        raise ValueError("gross_quote_budget must be a positive Decimal")
    if not isinstance(max_slippage_fraction,Decimal) or not max_slippage_fraction.is_finite() or not ZERO<=max_slippage_fraction<=Decimal("0.02"):
        raise ValueError("max slippage must be between 0% and 2%")
    raw_limit=candidate.best_ask*(Decimal("1")+max_slippage_fraction)
    limit_price=_round_to_increment(raw_limit,candidate.price_increment,ROUND_UP)
    quantity=_round_to_increment(gross_quote_budget/limit_price,candidate.base_amount_increment,ROUND_DOWN)
    quote_notional=quantity*limit_price
    if quantity<candidate.min_order_size:
        raise ValueError("authorized budget is below exchange minimum base order size")
    if quote_notional<candidate.min_notional_size:
        raise ValueError("authorized budget is below exchange minimum quote notional")
    if quote_notional>gross_quote_budget:
        raise ValueError("rounded order would exceed the authorized quote budget")
    body={
        "account_name":account_name,
        "connector_name":"kraken_paper_trade",
        "trading_pair":candidate.trading_pair,
        "trade_type":"BUY",
        "amount":str(quantity),
        "order_type":"LIMIT",
        "price":str(limit_price),
        "position_action":"OPEN",
    }
    return PaperOrderDraft(body,quote_notional,limit_price,quantity)


def build_take_profit_limit_order(
    *,
    trading_pair: str,
    remaining_quantity: Decimal,
    fee_inclusive_cost_basis: Decimal,
    base_amount_increment: Decimal,
    price_increment: Decimal,
    account_name: str="master_account",
) -> PaperOrderDraft:
    if not all(isinstance(v,Decimal) and v.is_finite() and v>ZERO for v in (
        remaining_quantity,fee_inclusive_cost_basis,base_amount_increment,price_increment
    )):
        raise ValueError("take-profit order inputs must be positive finite Decimals")
    quantity=_round_to_increment(remaining_quantity,base_amount_increment,ROUND_DOWN)
    if quantity<=ZERO:
        raise ValueError("remaining quantity rounds below exchange increment")
    target_gross=gross_sell_notional_for_net_target(fee_inclusive_cost_basis)
    target_price=_round_to_increment(target_gross/quantity,price_increment,ROUND_UP)
    gross_proceeds=quantity*target_price
    body={
        "account_name":account_name,
        "connector_name":"kraken_paper_trade",
        "trading_pair":trading_pair,
        "trade_type":"SELL",
        "amount":str(quantity),
        "order_type":"LIMIT",
        "price":str(target_price),
        "position_action":"OPEN",
    }
    return PaperOrderDraft(body,gross_proceeds,target_price,quantity)
