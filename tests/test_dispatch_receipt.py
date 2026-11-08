import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_servers.hummingbot_api.tools.dispatch_receipts import dispatch_with_receipt
from spectra_core.runtime import spectra_paper_only_enabled
from spectra_core.dispatch_receipt import (
    canonical_sha256,
    issue_executor_dispatch_receipt,
    validate_dispatch_signing_key,
)


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _body():
    return {
        "account_name": "paper-account",
        "controller_id": "spectra",
        "executor_config": {
            "type": "grid_executor",
            "connector_name": "kraken_paper_trade",
            "trading_pair": "XRP-USD",
            "total_amount_quote": "20",
        },
    }


def test_executor_receipt_signs_exact_request_and_core_scope(monkeypatch):
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv("SPECTRA_DISPATCH_PRIVATE_KEY", _encoded(private_raw))
    body = _body()

    receipt = issue_executor_dispatch_receipt(
        request_body=body,
        proposal_id="proposal-123",
        intent="ENTRY",
        amount_quote="20",
        now=1000,
    )

    payload_part, signature_part = receipt.token.split(".")
    payload_bytes = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
    signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
    private.public_key().verify(signature, payload_bytes)
    payload = json.loads(payload_bytes)

    assert payload["proposal_id"] == "proposal-123"
    assert payload["intent"] == "ENTRY"
    assert payload["account_name"] == "paper-account"
    assert payload["connector_name"] == "kraken_paper_trade"
    assert payload["trading_pair"] == "XRP-USD"
    assert payload["operation"] == "create_executor"
    assert payload["amount"] == "20"
    assert payload["method"] == "POST"
    assert payload["path"] == "/executors"
    assert payload["body_sha256"] == canonical_sha256(body)
    assert payload["config_sha256"] == canonical_sha256(body["executor_config"])
    assert payload["expires_at"] == 1030
    assert receipt.receipt_id == payload["receipt_id"]


def test_signing_key_must_be_configured_before_dispatch(monkeypatch):
    monkeypatch.delenv("SPECTRA_DISPATCH_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="is required"):
        validate_dispatch_signing_key()


def test_invalid_ttl_and_unscoped_payloads_are_rejected(monkeypatch):
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    monkeypatch.setenv("SPECTRA_DISPATCH_PRIVATE_KEY", _encoded(private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )))
    with pytest.raises(ValueError, match="TTL"):
        issue_executor_dispatch_receipt(
            request_body=_body(), proposal_id="p1", intent="ENTRY",
            amount_quote="20", now=1000, ttl_seconds=120,
        )
    with pytest.raises(ValueError, match="scope"):
        issue_executor_dispatch_receipt(
            request_body={"executor_config": _body()["executor_config"]},
            proposal_id="p1", intent="ENTRY", amount_quote="20", now=1000,
        )


def test_spectra_agent_cannot_disable_paper_mode_without_separate_gate(monkeypatch):
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    with pytest.raises(RuntimeError, match="live execution is disabled"):
        spectra_paper_only_enabled()

    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    assert spectra_paper_only_enabled() is True


def test_executor_stop_receipt_signs_exact_id_and_shared_transport(monkeypatch):
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv("SPECTRA_DISPATCH_PRIVATE_KEY", _encoded(private_raw))

    class Session:
        def __init__(self):
            self.headers = {}

    class Executors:
        def __init__(self, headers):
            self.headers = headers
            self.receipt_token = None

        async def stop_executor(self, *, executor_id, keep_position):
            self.receipt_token = self.headers.get("X-Spectra-Dispatch-Receipt")
            assert self.receipt_token
            assert executor_id == "executor-123"
            assert keep_position is True
            return {"executor_id": executor_id}

    class Client:
        def __init__(self):
            self._session = Session()
            self.executors = Executors(self._session.headers)

    client = Client()
    result, receipt = asyncio.run(
        dispatch_with_receipt(
            client,
            api_method="stop_executor",
            request_body={"keep_position": True},
            request_kwargs={"executor_id": "executor-123", "keep_position": True},
            proposal_id="exit-manifest-123",
            intent="EXIT",
            amount_quote="15",
            operation="stop_executor",
            method="POST",
            path="/executors/executor-123/stop",
            executor_id="executor-123",
            account_name="paper-account",
            connector_name="kraken_paper_trade",
            trading_pair="XRP-USD",
        )
    )

    payload_part, signature_part = client.executors.receipt_token.split(".")
    payload_bytes = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
    signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
    private.public_key().verify(signature, payload_bytes)
    payload = json.loads(payload_bytes)

    assert result["executor_id"] == "executor-123"
    assert receipt.receipt_id == payload["receipt_id"]
    assert payload["operation"] == "stop_executor"
    assert payload["executor_id"] == "executor-123"
    assert payload["path"] == "/executors/executor-123/stop"
    assert payload["intent"] == "EXIT"
    assert client._session.headers == {}
