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


def test_legacy_hummingbot_runtime_endpoint_is_not_required_for_paper_status(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setattr("condor.fetchers.spectra_runtime.time.time", lambda: 1000.0)
    monkeypatch.setattr(
        "spectra_core.portfolio_supervisor.get_portfolio_supervisor_status",
        lambda: {"supervisor_state": "RUNNING", "updated_at": 1000.0},
    )
    result = asyncio.run(
        fetch_spectra_runtime(FakeClient(FakeResponse(status=404)))
    )
    assert result["mode"] == "PAPER"
    assert result["paper_only"] is True
    assert result["healthy"] is True
    assert result["live_dispatch_authorized"] is False


def test_missing_paper_connector_fails_health_check_closed(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_PAPER_CONNECTORS", "")
    monkeypatch.setattr(
        "spectra_core.portfolio_supervisor.get_portfolio_supervisor_status",
        lambda: {"supervisor_state": "RUNNING", "updated_at": 1000.0},
    )
    monkeypatch.setattr("condor.fetchers.spectra_runtime.time.time", lambda: 1000.0)
    result = asyncio.run(
        fetch_spectra_runtime(FakeClient(FakeResponse(payload=["legacy", "payload"])))
    )
    assert result["mode"] == "PAPER"
    assert result["paper_only"] is True
    assert result["paper_connector_count"] == 0
    assert result["paper_connector_status"] == "MISSING"
    assert result["healthy"] is False
    assert result["live_dispatch_authorized"] is False
