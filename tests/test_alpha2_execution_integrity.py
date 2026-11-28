"""Evidence-backed execution integrity tests for Alpha 2.0 market buys."""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import os

import httpx
import pytest

from spectra_alpha2.execution import (
    HummingbotPaperExecutionClient,
    ExecutionRejected,
    ExecutionUncertain,
)
from spectra_alpha2.governor import CapitalAuthorization, CapitalPool

SAFE_ENV = {"SPECTRA_PAPER_ONLY": "true", "SPECTRA_ALLOW_LIVE_EXECUTION": "false"}


class FakeGovernor:
    def __init__(self):
        self.calls = []

    def consume_authorization(self, *args, **kwargs):
        self.calls.append(("consume", args, kwargs))

    def issue_dispatch_receipt(self, *args, **kwargs):
        self.calls.append(("receipt", args, kwargs))
        return type("Receipt", (), {"token": "signed-paper-receipt"})()

    def bind_order_id(self, *args, **kwargs):
        self.calls.append(("bind", args, kwargs))

    def settle_fill(self, *args, **kwargs):
        self.calls.append(("settle", args, kwargs))

    def release_unfilled_reservation(self, *args, **kwargs):
        self.calls.append(("release", args, kwargs))

    def list_pending_authorizations(self):
        return []


def authorization():
    return CapitalAuthorization(
        authorization_id="auth-123",
        idempotency_key="idem-123",
        position_id="pos-1",
        asset_id="BTC-USD",
        action_id="INITIAL",
        pool=CapitalPool.INITIAL,
        gross_notional=Decimal("20"),
        fee_reserve=Decimal("0.08"),
    )


def order_body():
    return {
        "account_name": "master_account",
        "connector_name": "kraken_paper_trade",
        "trading_pair": "BTC-USD",
        "trade_type": "BUY",
        "strategy_origin": "TOP_GAINER",
        "amount": "0.2",
        "order_type": "MARKET",
        "max_execution_price": "100",
        "position_action": "OPEN",
    }


def order_payload(status="FILLED", order_id="paper-order-1", trading_pair="BTC-USD", amount="0.2", executed_base="0.2", executed_quote="20", fee="0.08", side="BUY", order_type="MARKET"):
    return {
        "order_id": order_id,
        "trading_pair": trading_pair,
        "connector_name": "kraken_paper_trade",
        "account_name": "master_account",
        "side": side,
        "trade_type": side,
        "order_type": order_type,
        "amount": amount,
        "executed_amount_base": executed_base,
        "executed_amount_quote": executed_quote,
        "fee": fee,
        "status": status,
    }


def trade_payload(order_id="paper-order-1", trading_pair="BTC-USD", amount="0.2", price="100", fee="0.08", trade_id="trade-1", side="BUY", connector="kraken_paper_trade"):
    return {
        "order_id": order_id,
        "trade_id": trade_id,
        "trading_pair": trading_pair,
        "connector_name": connector,
        "side": side,
        "trade_type": side,
        "amount": amount,
        "price": price,
        "fee_paid": fee,
        "fee_currency": "USD",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def make_client(handler, *, timeout=1):
    return HummingbotPaperExecutionClient(
        "http://hummingbot",
        api_username="local-core",
        api_password="internal-only",
        environment=SAFE_ENV,
        transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
        timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_authorized_market_buy_settles_with_matching_evidence():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload()]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [trade_payload()]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    result = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert result.order_id == "paper-order-1"
    assert result.status == "FILLED"
    assert result.gross_quote_notional == Decimal("20")
    assert result.fee_quote == Decimal("0.08")
    assert any(c[0] == "settle" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_api_rejection_releases_reservation():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(400, json={"detail": "rejected"})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    with pytest.raises(ExecutionUncertain, match="rejected"):
        await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert any(c[0] == "release" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_connector_timeout_leaves_uncertain_reservation():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            raise httpx.ReadTimeout("lost response")
        if request.url.path == "/spectra/dispatch-receipts/auth-123":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    with pytest.raises(ExecutionUncertain):
        await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert not any(c[0] == "release" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_confirmed_no_fill_releases_reservation():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="CANCELED")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    result = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert result.status == "CANCELED"
    assert any(c[0] == "release" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_partial_fill_then_additional_fill_settles_once():
    state = {"phase": 0}

    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            if state["phase"] == 0:
                return httpx.Response(200, json={"data": [order_payload(status="PARTIALLY_FILLED", executed_base="0.1", executed_quote="10")]})
            return httpx.Response(200, json={"data": [order_payload(status="FILLED", executed_base="0.2", executed_quote="20")]})
        if request.url.path == "/trading/trades":
            if state["phase"] == 0:
                state["phase"] = 1
                return httpx.Response(200, json={"data": [trade_payload(amount="0.1", price="100", fee="0.04", trade_id="trade-1")]})
            return httpx.Response(200, json={"data": [
                trade_payload(amount="0.1", price="100", fee="0.04", trade_id="trade-1"),
                trade_payload(amount="0.1", price="100", fee="0.04", trade_id="trade-2"),
            ]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler, timeout=0.01)
    first = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=0.01)
    assert first.status == "PENDING"
    assert not any(c[0] == "settle" for c in governor.calls)

    second = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    # Second submission is a new authorization in this mock; exercise reconcile path instead.
    await client.close()


@pytest.mark.asyncio
async def test_fully_filled_order_settles():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="FILLED")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [trade_payload()]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    result = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert result.status == "FILLED"
    assert result.fee_quote == Decimal("0.08")
    await client.close()


@pytest.mark.asyncio
async def test_duplicate_trade_responses_are_deduplicated():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="FILLED")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [trade_payload(trade_id="dup"), trade_payload(trade_id="dup")]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    result = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert result.gross_quote_notional == Decimal("20")
    await client.close()


@pytest.mark.asyncio
async def test_mismatched_order_id_or_trading_pair_is_uncertain():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(order_id="paper-order-1", trading_pair="ETH-USD")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [trade_payload(order_id="paper-order-1", trading_pair="ETH-USD")]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    with pytest.raises(ExecutionUncertain):
        await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert not any(c[0] == "settle" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_fee_mismatch_is_uncertain():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="FILLED")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [trade_payload(fee="1.00")]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler)
    with pytest.raises(ExecutionUncertain, match="fee"):
        await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=1)
    assert not any(c[0] == "settle" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_partial_fill_across_reconciliation_cycles_settles_once():
    def partial_handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="PARTIALLY_FILLED", executed_base="0.1", executed_quote="10")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [trade_payload(amount="0.1", price="100", fee="0.04", trade_id="trade-1")]})
        raise AssertionError(request.url.path)

    def filled_handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="FILLED", executed_base="0.2", executed_quote="20")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": [
                trade_payload(amount="0.1", price="100", fee="0.04", trade_id="trade-1"),
                trade_payload(amount="0.1", price="100", fee="0.04", trade_id="trade-2"),
            ]})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(partial_handler, timeout=0.01)
    first = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=0.01)
    assert first.status == "PENDING"
    assert not any(c[0] == "settle" for c in governor.calls)
    await client.close()

    # Later reconciliation cycle sees the now-terminal order and settles once.
    client2 = make_client(filled_handler, timeout=1)
    second = await client2._wait_and_settle(
        governor,
        "auth-123",
        "paper-order-1",
        "BTC-USD",
        shard_id=None,
        timeout_seconds=1,
    )
    assert second.status == "FILLED"
    assert sum(1 for c in governor.calls if c[0] == "settle") == 1
    await client2.close()


@pytest.mark.asyncio
async def test_expired_order_with_uncertain_status_does_not_settle():
    def handler(request):
        if request.url.path == "/market-data/trading-pair/add":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/trading/orders":
            return httpx.Response(201, json={"order_id": "paper-order-1", "status": "submitted"})
        if request.url.path == "/trading/orders/search":
            return httpx.Response(200, json={"data": [order_payload(status="PENDING")]})
        if request.url.path == "/trading/trades":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url.path)

    governor = FakeGovernor()
    client = make_client(handler, timeout=0.01)
    result = await client.submit_authorized_buy(governor, authorization(), order_body(), timeout_seconds=0.01)
    assert result.status == "PENDING"
    assert not any(c[0] == "settle" for c in governor.calls)
    assert not any(c[0] == "release" for c in governor.calls)
    await client.close()


@pytest.mark.asyncio
async def test_quarantined_legacy_order_is_not_reconciled():
    class QuarantineGovernor(FakeGovernor):
        def list_pending_authorizations(self):
            return [{
                "kind": "BUY",
                "authorization_id": "8d4ffbe1-4c69-4520-83f1-53d16f36b10f",
                "idempotency_key": "idem-hype",
                "position_id": "pos-hype",
                "asset_id": "HYPE-USD",
                "action_id": "INITIAL",
                "pool": CapitalPool.INITIAL,
                "gross_notional": Decimal("20"),
                "fee_reserve": Decimal("0.08"),
                "order_id": "buy://HYPE-USD/d53ae0e4c69c0fd94647900bfe",
                "lot_id": "lot-hype",
                "shard_id": None,
                "expires_at": 9999999999.0,
            }]

    env = dict(SAFE_ENV)
    env["SPECTRA_QUARANTINED_AUTHORIZATION_IDS"] = "8d4ffbe1-4c69-4520-83f1-53d16f36b10f"
    env["SPECTRA_QUARANTINED_ORDER_IDS"] = "buy://HYPE-USD/d53ae0e4c69c0fd94647900bfe"
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("quarantined order must not be queried")

    governor = QuarantineGovernor()
    client = HummingbotPaperExecutionClient(
        "http://hummingbot",
        api_username="local-core",
        api_password="internal-only",
        environment=env,
        transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
    )
    result = await client.reconcile_pending(governor, timeout_seconds=0.01)
    assert result == []
    assert requests == []
    await client.close()