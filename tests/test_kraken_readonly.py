import asyncio
import base64
import hashlib
import hmac
from urllib.parse import parse_qs

import pytest

from spectra_core.kraken_readonly import (
    KrakenReadOnlyClient,
    KrakenReadOnlyError,
)


class Response:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    async def json(self, content_type=None):
        return self.payload


class Session:
    def __init__(self, payload=None):
        self.payload = payload or {"error": [], "result": {"ZUSD": "0"}}
        self.calls = []

    def post(self, url, *, data, headers, timeout):
        self.calls.append((url, data, headers, timeout))
        return Response(self.payload)


def client(session=None):
    secret = base64.b64encode(b"test-secret-bytes").decode()
    return KrakenReadOnlyClient("test-api-key", secret, session=session)


def test_read_only_balance_signs_request_and_returns_result():
    session = Session({"error": [], "result": {"ZUSD": "0"}})
    api = client(session)

    result = asyncio.run(api.balance())

    assert result == {"ZUSD": "0"}
    url, body, headers, _ = session.calls[0]
    assert url.endswith("/0/private/Balance")
    parsed = parse_qs(body)
    nonce = parsed["nonce"][0]
    digest = hashlib.sha256((nonce + body).encode()).digest()
    message = b"/0/private/Balance" + digest
    expected = base64.b64encode(
        hmac.new(b"test-secret-bytes", message, hashlib.sha512).digest()
    ).decode()
    assert headers["API-Key"] == "test-api-key"
    assert headers["API-Sign"] == expected


def test_mutating_and_unknown_endpoints_are_rejected_before_network():
    session = Session()
    api = client(session)

    with pytest.raises(ValueError, match="read-only allowlist"):
        asyncio.run(api.query("AddOrder", {"pair": "XBTUSD"}))
    with pytest.raises(ValueError, match="read-only allowlist"):
        asyncio.run(api.query("../AddOrder"))

    assert session.calls == []


def test_environment_constructor_reads_only_named_environment_variables(monkeypatch):
    secret = base64.b64encode(b"env-secret").decode()
    monkeypatch.setenv("KRAKEN_API_KEY", "env-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", secret)

    api = KrakenReadOnlyClient.from_env()

    assert repr(api) == "KrakenReadOnlyClient(credentials=<redacted>)"


def test_missing_or_invalid_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    with pytest.raises(ValueError, match="credentials are required"):
        KrakenReadOnlyClient.from_env()

    with pytest.raises(ValueError, match="valid base64"):
        KrakenReadOnlyClient("key", "not-base64!")


def test_kraken_api_errors_are_reported_without_credentials():
    session = Session({"error": ["EAPI:Invalid key"], "result": {}})
    api = client(session)

    with pytest.raises(KrakenReadOnlyError, match="Invalid key"):
        asyncio.run(api.balance())
