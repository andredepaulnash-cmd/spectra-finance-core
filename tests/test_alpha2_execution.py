import json
import asyncio
from decimal import Decimal

import httpx
import pytest

from spectra_alpha2.execution import HummingbotPaperExecutionClient, ExecutionUncertain
from spectra_alpha2.governor import CapitalAuthorization, CapitalPool


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}


class FakeGovernor:
    def __init__(self):
        self.calls=[]
    def consume_authorization(self,*args,**kwargs):
        self.calls.append(("consume",args,kwargs))
    def issue_dispatch_receipt(self,*args,**kwargs):
        self.calls.append(("receipt",args,kwargs))
        return type("Receipt",(),{"token":"signed-paper-receipt"})()
    def bind_order_id(self,*args,**kwargs):
        self.calls.append(("bind",args,kwargs))
    def settle_fill(self,*args,**kwargs):
        self.calls.append(("settle",args,kwargs))
    def consume_exit_authorization(self,*args,**kwargs):
        self.calls.append(("consume_exit",args,kwargs))
    def issue_exit_dispatch_receipt(self,*args,**kwargs):
        self.calls.append(("exit_receipt",args,kwargs))
        return type("Receipt",(),{"token":"signed-paper-receipt"})()
    def bind_exit_order_id(self,*args,**kwargs):
        self.calls.append(("bind_exit",args,kwargs))
    def settle_exit_fill(self,*args,**kwargs):
        self.calls.append(("settle_exit",args,kwargs))
    def release_exit_authorization(self,*args,**kwargs):
        self.calls.append(("release_exit",args,kwargs))
    def activate_vcap_shard(self,*args,**kwargs):
        self.calls.append(("activate",args,kwargs))
    def release_unfilled_reservation(self,*args,**kwargs):
        self.calls.append(("release",args,kwargs))
    def list_pending_authorizations(self):
        return []


def authorization(action_id="INITIAL"):
    return CapitalAuthorization(
        authorization_id="auth-123",idempotency_key="idem-123",position_id="pos-1",
        asset_id="BTC-USD",action_id=action_id,pool=CapitalPool.INITIAL,
        gross_notional=Decimal("20"),fee_reserve=Decimal("0.08"),
    )


def order_body():
    return {
        "account_name":"master_account","connector_name":"kraken_paper_trade",
        "trading_pair":"BTC-USD","trade_type":"BUY","strategy_origin":"TOP_GAINER","amount":"0.2",
        "order_type":"MARKET","max_execution_price":"100","position_action":"OPEN",
    }


def handler_for(requests,*,status="FILLED",trade_price="100",trade_fee="0.08"):
    def handler(request):
        requests.append(request)
        if request.url.path=="/trading/orders":
            assert request.headers.get("X-Spectra-Dispatch-Receipt")=="signed-paper-receipt"
            return httpx.Response(201,json={"order_id":"paper-order-1","status":"submitted"})
        if request.url.path=="/trading/orders/search":
            filters=json.loads(request.content)
            assert isinstance(filters["start_time"],int)
            assert isinstance(filters["end_time"],int)
            return httpx.Response(200,json={"data":[{"order_id":"paper-order-1","status":status}]})
        if request.url.path=="/trading/trades":
            filters=json.loads(request.content)
            assert isinstance(filters["start_time"],int)
            assert isinstance(filters["end_time"],int)
            return httpx.Response(200,json={"data":[{"order_id":"paper-order-1","amount":"0.2","price":trade_price,"fee_paid":trade_fee,"fee_currency":"USD"}]})
        raise AssertionError(request.url.path)
    return handler


@pytest.mark.asyncio
async def test_executor_submits_only_governor_signed_order_and_settles_fill():
    requests=[]
    governor=FakeGovernor()
    client=HummingbotPaperExecutionClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        environment=SAFE_ENV,transport=httpx.MockTransport(handler_for(requests)),poll_interval_seconds=0,
    )
    result=await client.submit_authorized_buy(governor,authorization(),order_body(),timeout_seconds=1)
    assert result.order_id=="paper-order-1"
    assert result.status=="FILLED"
    assert result.gross_quote_notional==Decimal("20")
    assert result.fee_quote==Decimal("0.08")
    assert [x[0] for x in governor.calls]==["consume","receipt","bind","settle"]
    assert requests[0].url.path=="/trading/orders"
    await client.close()



@pytest.mark.asyncio
async def test_executor_submits_governor_authorized_spot_sell_and_reconciles_net_fee():
    requests=[]
    governor=FakeGovernor()
    sell_body={
        "account_name":"master_account","connector_name":"kraken_paper_trade",
        "trading_pair":"BTC-USD","trade_type":"SELL","amount":"0.2",
        "order_type":"LIMIT","price":"105","position_action":"OPEN",
    }
    exit_auth=type("ExitAuth",(),{"authorization_id":"exit-123","idempotency_key":"exit-idem"})()
    client=HummingbotPaperExecutionClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        environment=SAFE_ENV,transport=httpx.MockTransport(handler_for(requests,trade_price="105",trade_fee="0.084")),poll_interval_seconds=0,
    )
    result=await client.submit_authorized_sell(governor,exit_auth,sell_body,timeout_seconds=1)
    assert result.order_id=="paper-order-1"
    assert result.gross_quote_notional==Decimal("21")
    assert result.fee_quote==Decimal("0.084")
    assert [x[0] for x in governor.calls]==["consume_exit","exit_receipt","bind_exit","settle_exit"]
    await client.close()


@pytest.mark.asyncio
async def test_executor_activates_vcap_shard_only_after_fill_settlement():
    requests=[]
    governor=FakeGovernor()
    client=HummingbotPaperExecutionClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        environment=SAFE_ENV,transport=httpx.MockTransport(handler_for(requests)),poll_interval_seconds=0,
    )
    await client.submit_authorized_buy(governor,authorization("VCAP_FAT1_1"),order_body(),shard_id="shard-1",timeout_seconds=1)
    assert [x[0] for x in governor.calls][-2:]==["settle","activate"]
    await client.close()


@pytest.mark.asyncio
async def test_executor_recovers_order_id_after_lost_submit_response():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path=="/trading/orders":
            raise httpx.ReadTimeout("response lost")
        if request.url.path=="/spectra/dispatch-receipts/auth-123":
            return httpx.Response(200,json={"order_id":"paper-order-1","response_status":201})
        if request.url.path=="/trading/orders/search":
            return httpx.Response(200,json={"data":[{"order_id":"paper-order-1","status":"FILLED"}]})
        if request.url.path=="/trading/trades":
            return httpx.Response(200,json={"data":[{"order_id":"paper-order-1","amount":"0.2","price":"100","fee_paid":"0.1","fee_currency":"USD"}]})
        raise AssertionError(request.url.path)
    governor=FakeGovernor()
    client=HummingbotPaperExecutionClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        environment=SAFE_ENV,transport=httpx.MockTransport(handler),poll_interval_seconds=0,
    )
    result=await client.submit_authorized_buy(governor,authorization(),order_body(),timeout_seconds=1)
    assert result.order_id=="paper-order-1"
    assert any(x[0]=="bind" for x in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_non_paper_connector_is_rejected_before_network_request():
    requests=[]
    governor=FakeGovernor()
    client=HummingbotPaperExecutionClient(
        "http://hummingbot",api_username="local-core",api_password="internal-only",
        environment=SAFE_ENV,transport=httpx.MockTransport(handler_for(requests)),poll_interval_seconds=0,
    )
    body={**order_body(),"connector_name":"kraken"}
    with pytest.raises(ExecutionUncertain,match="kraken_paper_trade"):
        await client.submit_authorized_buy(governor,authorization(),body,timeout_seconds=1)
    assert requests==[]
    assert governor.calls==[]
    await client.close()


def test_executor_requires_explicit_paper_environment():
    with pytest.raises(Exception):
        HummingbotPaperExecutionClient(
            "http://hummingbot",api_username="local-core",api_password="internal-only",
            environment={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"true"},
        )
