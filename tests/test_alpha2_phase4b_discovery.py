import time
from decimal import Decimal

import httpx
import pytest

from spectra_alpha2.market_data import ResearchScanner


def candidate_client(**overrides):
    now=time.time()
    ticker={"price":"10","quote_volume":"1000000","base_volume":"100000","timestamp":now}
    rule={"supports_limit_orders":False,"supports_market_orders":True,"min_order_size":"0.001","min_notional_size":"0","min_base_amount_increment":"0.001","min_price_increment":"0.01"}
    ticker.update(overrides.get("ticker",{}))
    rule.update(overrides.get("rule",{}))
    class C:
        async def get_hourly_candles(self,pair,max_records=28):
            return [{"timestamp":now-25*3600,"close":"10"},{"timestamp":now-24*3600,"close":"10"}]
        async def get_order_book(self,pair,depth=5):
            return {"bids":[{"price":"9.99"}],"asks":[{"price":"10.01"}]}
    return C(), ticker, rule, now


@pytest.mark.asyncio
async def test_market_buy_capable_asset_not_rejected_for_missing_limit_support():
    client,ticker,rule,now=candidate_client()
    scanner=ResearchScanner(client,entry_quote_assets={"USD"},min_quote_volume_24h=Decimal("100000"))
    rejections=[]
    c=await scanner._candidate_from_snapshot("XYZ-USD",ticker,rule,now=now,require_gainer=True,rejection_sink=rejections)
    assert c is not None
    assert rejections==[]
    # missing supports_market_orders key does not reject; explicit false does
    rule2=dict(rule); rule2.pop("supports_market_orders"); rule2["supports_limit_orders"]=False
    c2=await scanner._candidate_from_snapshot("XYZ-USD",ticker,rule2,now=now,require_gainer=True)
    assert c2 is not None
    rejections2=[]
    rule3=dict(rule); rule3["supports_market_orders"]=False
    c3=await scanner._candidate_from_snapshot("XYZ-USD",ticker,rule3,now=now,require_gainer=True,rejection_sink=rejections2)
    assert c3 is None and rejections2==["unsupported_market_orders"]


@pytest.mark.asyncio
async def test_no_positive_24h_change_required_for_mean_reversion():
    client,ticker,rule,now=candidate_client()
    class Neg(client.__class__):
        async def get_hourly_candles(self,pair,max_records=28):
            return [{"timestamp":now-25*3600,"close":"20"},{"timestamp":now-24*3600,"close":"20"}]
    scanner=ResearchScanner(Neg(),entry_quote_assets={"USD"},min_quote_volume_24h=Decimal("100000"))
    c=await scanner._candidate_from_snapshot("XYZ-USD",ticker,rule,now=now,require_gainer=True)
    assert c is not None and c.change_24h_fraction < 0


@pytest.mark.asyncio
async def test_missing_or_stale_candle_data_controlled_and_isolated():
    class StaleClient:
        def __init__(self): self.now=time.time()
        async def get_tickers(self):
            return {
                "AAA-USD":{"price":"10","quote_volume":"1000000","base_volume":"100000","timestamp":self.now},
                "BBB-USD":{"price":"10","quote_volume":"1000000","base_volume":"100000","timestamp":self.now},
            }
        async def get_trading_rules(self):
            rule={"supports_limit_orders":False,"min_order_size":"0.001","min_notional_size":"0","min_base_amount_increment":"0.001","min_price_increment":"0.01"}
            return {"AAA-USD":dict(rule),"BBB-USD":dict(rule)}
        async def get_hourly_candles(self,pair,max_records=28):
            if pair=="AAA-USD":
                raise httpx.HTTPError("candle source unavailable")
            return [{"timestamp":self.now-25*3600,"close":"10"},{"timestamp":self.now-24*3600,"close":"10"}]
        async def get_order_book(self,pair,depth=5):
            return {"bids":[{"price":"9.99"}],"asks":[{"price":"10.01"}]}
    scanner=ResearchScanner(StaleClient(),entry_quote_assets={"USD"},min_quote_volume_24h=Decimal("100000"))
    candidates=await scanner.scan()
    pairs={c.trading_pair for c in candidates}
    assert "BBB-USD" in pairs  # one unavailable historical source does not eliminate entire pool
    assert "AAA-USD" not in pairs
    assert scanner.last_scan_diagnostics["rejection_counts"].get("missing_or_stale_candles",0) >= 1


@pytest.mark.asyncio
async def test_rejection_reasons_attributable_and_liquidity_preserved():
    client,ticker,rule,now=candidate_client(ticker={"quote_volume":"1"})
    scanner=ResearchScanner(client,entry_quote_assets={"USD"},min_quote_volume_24h=Decimal("100000"))
    rejections=[]
    c=await scanner._candidate_from_snapshot("XYZ-USD",ticker,rule,now=now,require_gainer=True,rejection_sink=rejections)
    assert c is None and rejections==["min_quote_volume"]
    # spread still enforced
    class Wide(client.__class__):
        async def get_order_book(self,pair,depth=5):
            return {"bids":[{"price":"1"}],"asks":[{"price":"100"}]}
    client2,ticker2,rule2,now2=candidate_client()
    scanner2=ResearchScanner(Wide(),entry_quote_assets={"USD"},min_quote_volume_24h=Decimal("100000"))
    rejections2=[]
    c2=await scanner2._candidate_from_snapshot("XYZ-USD",ticker2,rule2,now=now2,require_gainer=True,rejection_sink=rejections2)
    assert c2 is None and rejections2==["spread_above_max"]
