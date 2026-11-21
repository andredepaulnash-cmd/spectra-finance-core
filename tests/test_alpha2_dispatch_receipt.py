import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from spectra_alpha2 import dispatch_receipt as receipts


def setup_signing(monkeypatch):
    private=Ed25519PrivateKey.generate()
    raw=private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv("SPECTRA_PAPER_ONLY","true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION","false")
    monkeypatch.setenv("SPECTRA_DISPATCH_POLICY_VERSION","spectra-alpha2-v1")
    monkeypatch.setenv("SPECTRA_DISPATCH_PRIVATE_KEY",base64.urlsafe_b64encode(raw).decode().rstrip("="))
    return private


def decode(token):
    payload,signature=token.split(".")
    pad=lambda x: x+"="*(-len(x)%4)
    return json.loads(base64.urlsafe_b64decode(pad(payload))),base64.urlsafe_b64decode(pad(signature))


def trade_body():
    return {
        "account_name":"master_account",
        "connector_name":"kraken_paper_trade",
        "trading_pair":"BTC-USDT",
        "trade_type":"BUY",
        "amount":"0.0003",
        "order_type":"MARKET",
        "max_execution_price":"65000",
        "position_action":"OPEN",
    }


def test_signed_trade_receipt_binds_exact_paper_order_and_governor_id(monkeypatch):
    private=setup_signing(monkeypatch)
    body=trade_body()
    receipt=receipts._issue_executor_dispatch_receipt(
        request_body=body,
        proposal_id="capital-reservation-123",
        intent="ENTRY",
        amount_quote="20",
        now=time.time(),
        operation="place_trade",
        method="POST",
        path="/trading/orders",
    )
    payload,signature=decode(receipt.token)
    private.public_key().verify(signature,receipts.canonical_json(payload))
    assert payload["proposal_id"]=="capital-reservation-123"
    assert payload["connector_name"]=="kraken_paper_trade"
    assert payload["trading_pair"]=="BTC-USDT"
    assert payload["amount"]=="20"
    assert payload["body_sha256"]==receipts.canonical_sha256(body)
    assert payload["policy_version"]=="spectra-alpha2-v1"


def test_dispatch_signing_fails_closed_without_paper_flags(monkeypatch):
    setup_signing(monkeypatch)
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION","true")
    with pytest.raises(RuntimeError,match="SPECTRA_ALLOW_LIVE_EXECUTION must be exactly"):
        receipts._issue_executor_dispatch_receipt(
            request_body=trade_body(),proposal_id="reservation-1",intent="ENTRY",
            amount_quote="20",now=time.time(),operation="place_trade",
        )


def test_live_connector_and_non_usdt_market_receipts_are_rejected(monkeypatch):
    setup_signing(monkeypatch)
    body=trade_body()
    body["connector_name"]="kraken"
    with pytest.raises(ValueError,match="restricted to kraken_paper_trade"):
        receipts._issue_executor_dispatch_receipt(
            request_body=body,proposal_id="reservation-1",intent="ENTRY",
            amount_quote="20",now=time.time(),operation="place_trade",
        )
    body=trade_body()
    body["trading_pair"]="BTC-USD"
    with pytest.raises(ValueError,match="USDT-quoted"):
        receipts._issue_executor_dispatch_receipt(
            request_body=body,proposal_id="reservation-1",intent="ENTRY",
            amount_quote="20",now=time.time(),operation="place_trade",
        )


def test_cancel_receipt_binds_account_connector_order_and_intent(monkeypatch):
    private=setup_signing(monkeypatch)
    receipt=receipts._issue_executor_dispatch_receipt(
        request_body={},proposal_id="reservation-cancel-1",intent="CANCEL",
        amount_quote="20",now=time.time(),operation="cancel_order",
        method="POST",
        path="/trading/master_account/kraken_paper_trade/orders/client-123/cancel",
        account_name="master_account",connector_name="kraken_paper_trade",
        trading_pair="BTC-USDT",
    )
    payload,signature=decode(receipt.token)
    private.public_key().verify(signature,receipts.canonical_json(payload))
    assert payload["operation"]=="cancel_order"
    assert payload["intent"]=="CANCEL"
    assert payload["path"].endswith("/client-123/cancel")


def test_cancel_receipt_rejects_wrong_connector(monkeypatch):
    setup_signing(monkeypatch)
    with pytest.raises(ValueError,match="only Kraken paper orders"):
        receipts._issue_executor_dispatch_receipt(
            request_body={},proposal_id="reservation-cancel-1",intent="CANCEL",
            amount_quote="20",now=time.time(),operation="cancel_order",
            method="POST",
            path="/trading/master_account/binance_paper_trade/orders/client-123/cancel",
            account_name="master_account",connector_name="binance_paper_trade",
            trading_pair="BTC-USDT",
        )
