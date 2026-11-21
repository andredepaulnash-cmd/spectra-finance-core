from decimal import Decimal
import unittest

from spectra_alpha2.market_data import MarketCandidate
from spectra_alpha2.order_builder import build_buy_market_order, build_take_profit_limit_order


def candidate():
    return MarketCandidate(
        trading_pair="BTC-USDT",base_asset="BTC",quote_asset="USDT",
        last_price=Decimal("10"),best_bid=Decimal("9.99"),best_ask=Decimal("10"),
        quote_volume_24h=Decimal("1000000"),base_volume_24h=Decimal("100000"),
        spread_fraction=Decimal("0.001"),ticker_timestamp=1,
        min_order_size=Decimal("0.001"),min_notional_size=Decimal("5"),
        base_amount_increment=Decimal("0.001"),price_increment=Decimal("0.01"),
    )


class PaperOrderBuilderTests(unittest.TestCase):
    def test_market_buy_is_quote_budgeted_and_carries_no_limit_price(self):
        order=build_buy_market_order(candidate(),gross_quote_budget=Decimal("20"))
        self.assertEqual(order.request_body["connector_name"],"kraken_paper_trade")
        self.assertEqual(order.request_body["order_type"],"MARKET")
        self.assertEqual(order.request_body["trade_type"],"BUY")
        self.assertNotIn("price",order.request_body)
        self.assertEqual(order.request_body["max_execution_price"],str(order.limit_price))
        self.assertGreaterEqual(order.limit_price,candidate().best_ask)
        self.assertLessEqual(order.gross_quote_notional,Decimal("20"))
        self.assertEqual(order.gross_quote_notional,order.base_quantity*order.limit_price)

    def test_market_buy_rejects_budget_below_exchange_minimum(self):
        with self.assertRaises(ValueError):
            build_buy_market_order(candidate(),gross_quote_budget=Decimal("4"))

    def test_take_profit_limit_nets_at_least_174_percent_after_sell_fee(self):
        cost=Decimal("100.40")
        order=build_take_profit_limit_order(
            trading_pair="BTC-USDT",
            remaining_quantity=Decimal("1"),
            fee_inclusive_cost_basis=cost,
            base_amount_increment=Decimal("0.001"),
            price_increment=Decimal("0.01"),
        )
        net=order.gross_quote_notional*Decimal("0.996")
        self.assertGreaterEqual(net,cost*Decimal("1.0174"))
        self.assertEqual(order.request_body["trade_type"],"SELL")
        self.assertEqual(order.request_body["order_type"],"LIMIT")
        self.assertEqual(order.request_body["position_action"],"OPEN")


if __name__=="__main__":
    unittest.main()
