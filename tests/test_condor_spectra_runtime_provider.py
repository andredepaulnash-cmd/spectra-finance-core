import pytest

from condor.agents.providers import list_core_providers
from condor.agents.providers.spectra_runtime import SpectraRuntimeProvider


@pytest.mark.asyncio
async def test_spectra_runtime_provider_injects_paper_mode_and_vcap_summary(monkeypatch):
    async def fake_fetch(_client):
        return {
            "product": "Spectra Finance",
            "mode": "PAPER",
            "paper_only": True,
            "live_dispatch_available": True,
            "paper_connector_count": 1,
            "healthy": True,
            "adaptive_vcap": {
                "active_fragments": 2,
                "waiting_fragments": 3,
                "reconciling_fragments": 1,
                "paused_fragments": 4,
            },
            "api_token": "must-not-appear",
        }

    monkeypatch.setattr(
        "condor.agents.providers.spectra_runtime.fetch_spectra_runtime",
        fake_fetch,
    )
    result = await SpectraRuntimeProvider().execute(None, {})
    assert result.data["mode"] == "PAPER"
    assert "api_token" not in result.data
    assert "mode=PAPER" in result.summary
    assert "active=2" in result.summary
    assert "reconciling=1" in result.summary


@pytest.mark.asyncio
async def test_spectra_runtime_provider_fails_closed_on_unavailable_runtime(monkeypatch):
    async def fake_fetch(_client):
        raise RuntimeError("runtime endpoint unavailable")

    monkeypatch.setattr(
        "condor.agents.providers.spectra_runtime.fetch_spectra_runtime",
        fake_fetch,
    )
    result = await SpectraRuntimeProvider().execute(None, {})
    assert result.data["mode"] == "UNAVAILABLE"
    assert result.data["paper_only"] is False
    assert "fail closed" in result.summary.lower()


def test_spectra_runtime_provider_is_registered_as_core():
    names = {provider.name for provider in list_core_providers()}
    assert "spectra_runtime" in names
