import time
from decimal import Decimal

import httpx
import pytest

from spectra_alpha2.condor import CondorAnalyzer
from spectra_alpha2.market_data import HummingbotPaperDataClient, MarketCandidate, ResearchScanner
from spectra_alpha2.order_builder import build_buy_market_order


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}


class FakeClient:
    async def get_tickers(self):
        now=time.time()
        return {
            "BTC-USDT":{"price":65000,"quote_volume":15000000,"base_volume":80,"timestamp":now},
            "ETH-USDT":{"price":3500,"quote_volume":12000000,"base_volume":400,"timestamp":now},
            "LOSER-USDT":{"price":5,"quote_volume":11000000,"base_volume":240000,"timestamp":now},
            "USDC-USDT":{"price":1,"quote_volume":900000,"base_volume":900000,"timestamp":now},
            "DOGE-USDT":{"price":0.1,"quote_volume":20000,"base_volume":200000,"timestamp":now},
            "OLD-USDT":{"price":1,"quote_volume":1000000,"base_volume":1000000,"timestamp":now-1000},
            "BTC-USD":{"price":65000,"quote_volume":5000000,"base_volume":80,"timestamp":now},
        }
    async def get_trading_rules(self):
        return {
            pair:{"supports_limit_orders":True,"min_order_size":"0.00001","min_notional_size":"5",
                  "min_base_amount_increment":"0.00001","min_price_increment":"0.1"}
            for pair in ("BTC-USDT","ETH-USDT","LOSER-USDT","USDC-USDT","DOGE-USDT","OLD-USDT")
        }
    async def get_hourly_candles(self,pair,max_records=28):
        now=time.time()
        price={"BTC-USDT":65000,"ETH-USDT":3500,"LOSER-USDT":5}.get(pair,1)
        prior=price/1.02 if pair!="LOSER-USDT" else price/0.90
        return [{"timestamp":now-25*3600,"close":prior},{"timestamp":now-24*3600,"close":prior}]
    async def get_order_book(self,pair,depth=5):
        if pair=="ETH-USDT":
            return {"bids":[{"price":"3499","amount":"100"}],"asks":[{"price":"3501","amount":"100"}]}
        return {"bids":[{"price":"64990","amount":"10"}],"asks":[{"price":"65010","amount":"10"}]}


@pytest.mark.asyncio
async def test_research_scanner_filters_quote_volume_spread_staleness_quote_asset_and_mean_reversion():
    scanner=ResearchScanner(FakeClient(),entry_quote_assets={"USDT"},min_quote_volume_24h=Decimal("10000000"),clock=time.time)
    candidates=await scanner.scan()
    assert [x.trading_pair for x in candidates]==["BTC-USDT","ETH-USDT","LOSER-USDT"]
    loser=next(x for x in candidates if x.trading_pair=="LOSER-USDT")
    assert loser.change_24h_fraction < 0  # no positive 24h change required for mean-reversion evaluation
    assert all(x.quote_asset=="USDT" for x in candidates)
    assert all(x.spread_fraction<=Decimal("0.003") for x in candidates)



class KrakenUsdZeroNotionalClient(FakeClient):
    async def get_tickers(self):
        now=time.time()
        return {
            "BTC-USD":{"price":65000,"quote_volume":15000000,"base_volume":230,"timestamp":now},
        }

    async def get_trading_rules(self):
        # Kraken publishes ordermin but no separate min-notional constraint.
        return {
            "BTC-USD":{
                "supports_limit_orders":True,
                "min_order_size":"0.00001",
                "min_notional_size":"0",
                "min_base_amount_increment":"0.00001",
                "min_price_increment":"0.1",
            }
        }

    async def get_hourly_candles(self,pair,max_records=28):
        now=time.time()
        return [
            {"timestamp":now-25*3600,"close":"63725"},
            {"timestamp":now-24*3600,"close":"63725"},
        ]

    async def get_order_book(self,pair,depth=5):
        return {
            "bids":[{"price":"64990","amount":"10"}],
            "asks":[{"price":"65010","amount":"10"}],
        }


@pytest.mark.asyncio
async def test_kraken_usd_market_with_zero_min_notional_remains_buy_eligible():
    scanner=ResearchScanner(
        KrakenUsdZeroNotionalClient(),
        entry_quote_assets={"USD"},
        min_quote_volume_24h=Decimal("10000000"),
        clock=time.time,
    )
    candidates=await scanner.scan()
    assert [candidate.trading_pair for candidate in candidates]==["BTC-USD"]
    candidate=candidates[0]
    assert candidate.min_notional_size==Decimal("0")
    draft=build_buy_market_order(candidate,gross_quote_budget=Decimal("20"))
    assert draft.request_body["trading_pair"]=="BTC-USD"
    assert draft.request_body["order_type"]=="MARKET"
    assert draft.base_quantity>=candidate.min_order_size


def test_condor_ranks_liquid_tight_spread_candidates_and_explains_score():
    def candidate(pair,volume,spread):
        return MarketCandidate(
            trading_pair=pair,base_asset=pair.split("-")[0],quote_asset="USDT",
            last_price=Decimal("1"),best_bid=Decimal("0.999"),best_ask=Decimal("1.001"),
            quote_volume_24h=Decimal(str(volume)),base_volume_24h=Decimal("100"),
            spread_fraction=Decimal(str(spread)),ticker_timestamp=time.time(),
            min_order_size=Decimal("0.001"),min_notional_size=Decimal("5"),
            base_amount_increment=Decimal("0.001"),price_increment=Decimal("0.01"),
            change_24h_fraction=Decimal("0.01") if pair=="LOW-USDT" else Decimal("0.04"),
        )
    ranked=CondorAnalyzer().recommend([
        candidate("LOW-USDT",100000,0.0025),
        candidate("HIGH-USDT",10000000,0.0005),
    ],limit=2)
    assert ranked[0].trading_pair=="HIGH-USDT"
    assert ranked[0].rationale
    assert ranked[0].score>ranked[1].score


def test_condor_prioritizes_largest_positive_24h_gainer_before_liquidity_score():
    def item(pair,volume,change):
        return MarketCandidate(
            trading_pair=pair,base_asset=pair.split("-")[0],quote_asset="USD",
            last_price=Decimal("1"),best_bid=Decimal("0.999"),best_ask=Decimal("1.001"),
            quote_volume_24h=Decimal(str(volume)),base_volume_24h=Decimal("100"),
            spread_fraction=Decimal("0.001"),ticker_timestamp=time.time(),
            min_order_size=Decimal("0.001"),min_notional_size=Decimal("0"),
            base_amount_increment=Decimal("0.001"),price_increment=Decimal("0.01"),
            change_24h_fraction=Decimal(str(change)),
        )
    ranked=CondorAnalyzer().recommend([
        item("LIQUID-USD",100000000,0.03),
        item("GAINER-USD",1000000,0.25),
    ],limit=2)
    assert ranked[0].trading_pair=="GAINER-USD"


def test_condor_is_only_a_recommendation_layer():
    analyzer=CondorAnalyzer()
    assert not hasattr(analyzer,"place_order")
    assert not hasattr(analyzer,"submit_order")


def test_paper_data_client_rejects_missing_or_live_flags():
    with pytest.raises(Exception):
        HummingbotPaperDataClient("http://localhost:8000",api_username="u",api_password="p",environment={})
    with pytest.raises(Exception):
        HummingbotPaperDataClient(
            "http://localhost:8000",api_username="u",api_password="p",
            environment={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"true"},
        )


@pytest.mark.asyncio
async def test_paper_data_client_uses_only_paper_connector_and_public_data():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path=="/market-data/tickers":
            return httpx.Response(200,json={"tickers":{"kraken_paper_trade":{"BTC-USDT":{"price":1}}}})
        if request.url.path=="/connectors/kraken_paper_trade/trading-rules":
            return httpx.Response(200,json={"BTC-USDT":{"supports_limit_orders":True}})
        if request.url.path=="/market-data/candles":
            return httpx.Response(200,json=[{"timestamp":time.time()-86400,"close":"1"}])
        return httpx.Response(200,json={"trading_pair":"BTC-USDT","bids":[],"asks":[]})
    client=HummingbotPaperDataClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        transport=httpx.MockTransport(handler),environment=SAFE_ENV,
    )
    await client.get_tickers()
    await client.get_trading_rules()
    await client.get_order_book("BTC-USDT")
    await client.get_hourly_candles("BTC-USDT")
    assert requests[0].url.path=="/market-data/tickers"
    assert requests[0].url.params["connectors"]=="kraken_paper_trade"
    assert requests[1].url.path=="/connectors/kraken_paper_trade/trading-rules"
    assert requests[2].url.path=="/market-data/order-book"
    assert requests[3].url.path=="/market-data/candles"
    assert requests[3].content.find(b'"connector_name":"kraken"') >= 0
    await client.close()
