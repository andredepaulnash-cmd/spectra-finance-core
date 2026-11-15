import asyncio

from condor.fetchers.spectra_runtime import fetch_spectra_runtime


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.payload


class FakeSession:
    closed = False

    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


class FakeClient:
    def __init__(self, response):
        self._session = FakeSession(response)
        self.base_url = "http://127.0.0.1:8000"


def test_legacy_api_is_not_reported_as_live_dispatch_available():
    result = asyncio.run(
        fetch_spectra_runtime(FakeClient(FakeResponse(status=404)))
    )
    assert result["mode"] == "UNSUPPORTED"
    assert result["paper_only"] is False
    assert result["live_dispatch_available"] is False


def test_invalid_runtime_payload_fails_closed():
    result = asyncio.run(
        fetch_spectra_runtime(FakeClient(FakeResponse(payload=["not", "an", "object"])))
    )
    assert result["mode"] == "UNAVAILABLE"
    assert result["paper_only"] is False
    assert result["live_dispatch_available"] is False
