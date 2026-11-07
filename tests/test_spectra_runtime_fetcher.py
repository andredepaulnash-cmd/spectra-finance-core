import asyncio

from condor.fetchers.spectra_runtime import fetch_spectra_runtime


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        if self.status >= 400 and self.status != 404:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.payload


class FakeSession:
    closed = False

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        return self.response


class FakeClient:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, response):
        self._session = FakeSession(response)


def test_fetch_spectra_runtime_returns_paper_only_status():
    payload = {
        "mode": "PAPER_ONLY",
        "paper_only": True,
        "live_dispatch_available": False,
        "paper_connector_count": 1,
        "paper_connectors": ["paper-account/kraken_paper_trade"],
    }
    client = FakeClient(FakeResponse(200, payload))

    result = asyncio.run(fetch_spectra_runtime(client))

    assert result == payload
    assert client._session.calls == [
        ("http://127.0.0.1:8000/spectra/runtime", 5)
    ]


def test_missing_runtime_endpoint_is_explicitly_unsupported():
    client = FakeClient(FakeResponse(404))

    result = asyncio.run(fetch_spectra_runtime(client))

    assert result["mode"] == "UNSUPPORTED"
    assert result["paper_only"] is False
    assert result["live_dispatch_available"] is True


def test_uninitialized_client_fails_closed():
    class Uninitialized:
        base_url = "http://127.0.0.1:8000"
        _session = None

    try:
        asyncio.run(fetch_spectra_runtime(Uninitialized()))
    except RuntimeError as exc:
        assert "not initialized" in str(exc)
    else:
        raise AssertionError("uninitialized Hummingbot client was accepted")
