import time
from decimal import Decimal

import httpx
import pytest

from spectra_alpha2.condor import CondorAnalyzer
from spectra_alpha2.market_data import HummingbotPaperDataClient, MarketCandidate, ResearchScanner


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}


class FakeClient:
    async def get_tickers(self):
        now=time.time()
        return {
            "BTC-USDT":{"price":65000,"quote_volume":5000000,"base_volume":80,"timestamp":now},
            "ETH-USDT":{"price":3500,"quote_volume":1500000,"base_volume":400,"timestamp":now},
            "USDC-USDT":{"price":1,"quote_volume":900000,"base_volume":900000,"timestamp":now},
            "DOGE-USDT":{"price":0.1,"quote_volume":20000,"base_volume":200000,"timestamp":now},
            "OLD-USDT":{"price":1,"quote_volume":1000000,"base_volume":1000000,"timestamp":now-1000},
            "BTC-USD":{"price":65000,"quote_volume":5000000,"base_volume":80,"timestamp":now},
        }
    async def get_trading_rules(self):
        return {
            pair:{"supports_limit_orders":True,"min_order_size":"0.00001","min_notional_size":"5"}
            for pair in ("BTC-USDT","ETH-USDT","USDC-USDT","DOGE-USDT","OLD-USDT")
        }
    async def get_order_book(self,pair,depth=5):
        if pair=="ETH-USDT":
            return {"bids":[{"price":"3499","amount":"100"}],"asks":[{"price":"3501","amount":"100"}]}
        return {"bids":[{"price":"64990","amount":"10"}],"asks":[{"price":"65010","amount":"10"}]}


@pytest.mark.asyncio
async def test_research_scanner_filters_quote_volume_spread_staleness_and_quote_asset():
    scanner=ResearchScanner(FakeClient(),clock=time.time)
    candidates=await scanner.scan()
    assert [x.trading_pair for x in candidates]==["BTC-USDT","ETH-USDT"]
    assert all(x.quote_asset=="USDT" for x in candidates)
    assert all(x.spread_fraction<=Decimal("0.003") for x in candidates)


def test_condor_ranks_liquid_tight_spread_candidates_and_explains_score():
    def candidate(pair,volume,spread):
        return MarketCandidate(
            trading_pair=pair,base_asset=pair.split("-")[0],quote_asset="USDT",
            last_price=Decimal("1"),best_bid=Decimal("0.999"),best_ask=Decimal("1.001"),
            quote_volume_24h=Decimal(str(volume)),base_volume_24h=Decimal("100"),
            spread_fraction=Decimal(str(spread)),ticker_timestamp=time.time(),
            min_order_size=Decimal("0.001"),min_notional_size=Decimal("5"),
        )
    ranked=CondorAnalyzer().recommend([
        candidate("LOW-USDT",100000,0.0025),
        candidate("HIGH-USDT",10000000,0.0005),
    ],limit=2)
    assert ranked[0].trading_pair=="HIGH-USDT"
    assert ranked[0].rationale
    assert ranked[0].score>ranked[1].score


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
        return httpx.Response(200,json={"trading_pair":"BTC-USDT","bids":[],"asks":[]})
    client=HummingbotPaperDataClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        transport=httpx.MockTransport(handler),environment=SAFE_ENV,
    )
    await client.get_tickers()
    await client.get_trading_rules()
    await client.get_order_book("BTC-USDT")
    assert all("kraken_paper_trade" in request.url.path or request.url.path=="/market-data/order-book" for request in requests)
    assert requests[0].url.params["connectors"]=="kraken_paper_trade"
    await client.close()
