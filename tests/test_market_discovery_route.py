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


def test_explicit_vcap_entry_route_reserves_candidate_but_never_dispatches_order(
    monkeypatch,
):
    calls = []
    client = object()

    class FakeConfigManager:
        async def get_client(self, name):
            calls.append(("client", name))
            return client

    async def fake_admission(
        selected_client, *, scan_result, trading_pair, account_name, now
    ):
        calls.append((
            "admission",
            selected_client,
            trading_pair,
            account_name,
            scan_result,
        ))
        return SimpleNamespace(
            pair="BTC-USD",
            proposal_id="spectra-entry-test",
            position_id="spectra-position-test",
            amount_quote=__import__("decimal").Decimal("20"),
            decision_status=__import__(
                "spectra_core.models", fromlist=["DecisionStatus"]
            ).DecisionStatus.RESERVED,
            decision_reason="reserved",
            selection_score=88.0,
            scanner_as_of=1_800_000_000.0,
            paper_only=True,
            order_dispatched=False,
        )

    monkeypatch.setattr(
        market_routes, "get_config_manager", lambda: FakeConfigManager()
    )
    monkeypatch.setattr(
        "spectra_core.vcap_entry.reserve_vcap_candidate_entry",
        fake_admission,
    )
    market_routes._discovery_cache.clear()
    key = "kraken:spectra-paper:80:25:10000000:90"
    scan = {
        "source": "Kraken spot market data via Hummingbot API",
        "paper_only": True,
        "orders_created": 0,
    }
    market_routes._discovery_cache[key] = (market_routes.time.monotonic(), scan)

    result = asyncio.run(
        market_routes.reserve_server_vcap_entry(
            name="spectra-paper",
            trading_pair="BTC-USD",
            account_name="master_account",
            user=SimpleNamespace(username="test-operator"),
        )
    )

    assert result["decision_status"] == "RESERVED"
    assert result["amount_quote"] == "20"
    assert result["proposal_id"] == "spectra-entry-test"
    assert result["position_id"] == "spectra-position-test"
    assert result["paper_only"] is True
    assert result["order_dispatched"] is False
    assert calls[0] == ("client", "spectra-paper")
    assert calls[1][0] == "admission"
    assert calls[1][1] is client
    assert calls[1][2] == "BTC-USD"


def test_vcap_entry_route_returns_conflict_for_refused_candidate(monkeypatch):
    from fastapi import HTTPException

    class FakeConfigManager:
        async def get_client(self, name):
            return object()

    async def refused(*args, **kwargs):
        raise ValueError("pair is not in the current mean-reversion shortlist")

    monkeypatch.setattr(
        market_routes, "get_config_manager", lambda: FakeConfigManager()
    )
    monkeypatch.setattr(
        "spectra_core.vcap_entry.reserve_vcap_candidate_entry",
        refused,
    )
    market_routes._discovery_cache.clear()
    key = "kraken:spectra-paper:80:25:10000000:90"
    market_routes._discovery_cache[key] = (
        market_routes.time.monotonic(),
        {
            "source": "Kraken spot market data via Hummingbot API",
            "paper_only": True,
            "orders_created": 0,
        },
    )

    try:
        asyncio.run(
            market_routes.reserve_server_vcap_entry(
                name="spectra-paper",
                trading_pair="ETH-USD",
                account_name="master_account",
                user=SimpleNamespace(username="test-operator"),
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "mean-reversion shortlist" in exc.detail
    else:
        raise AssertionError("refused candidate must return HTTP 409")
