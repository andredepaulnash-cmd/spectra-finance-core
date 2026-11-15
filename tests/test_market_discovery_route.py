import asyncio
from types import SimpleNamespace

from condor.web.routes import market as market_routes


def test_server_scoped_market_discovery_uses_selected_hummingbot_client(monkeypatch):
    calls = []

    class FakeConfigManager:
        async def get_client(self, name):
            calls.append(("client", name))
            return object()

    async def fake_scan(client, **kwargs):
        calls.append(("scan", kwargs))
        return {
            "source": "Kraken spot market data via Hummingbot API",
            "universe": "Kraken spot markets in configured quote assets",
            "paper_only": True,
            "orders_created": 0,
        }

    monkeypatch.setattr(market_routes, "get_config_manager", lambda: FakeConfigManager())
    monkeypatch.setattr("spectra_core.market_discovery.scan_kraken_universe", fake_scan)
    market_routes._discovery_cache.clear()

    result = asyncio.run(
        market_routes.get_server_market_discovery(
            name="spectra-paper",
            top_n=60,
            show_n=20,
            min_volume_usd=5_000_000,
            min_history_days=90,
            user=SimpleNamespace(username="test-operator"),
        )
    )

    assert result["source"] == "Kraken spot market data via Hummingbot API"
    assert result["paper_only"] is True
    assert result["orders_created"] == 0
    assert calls[0] == ("client", "spectra-paper")
    assert calls[1][0] == "scan"
    assert calls[1][1]["top_n"] == 60
    assert calls[1][1]["show_n"] == 20
    assert calls[1][1]["min_volume_usd"] == 5_000_000
    assert calls[1][1]["quote_assets"] == ("USD",)
