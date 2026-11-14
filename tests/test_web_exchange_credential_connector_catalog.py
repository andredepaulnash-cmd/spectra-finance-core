"""The credential picker must expose real exchange connectors, not paper adapters."""

from fastapi import FastAPI
from starlette.testclient import TestClient

import condor.web.routes.settings as settings_routes
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from config_manager import ServerPermission

SERVER = "alpha"
OWNER = WebUser(id=1, username="owner", first_name="O", role="user")


class FakeConfigManager:
    def get_server_permission(self, user_id, server_name):
        if user_id == OWNER.id and server_name == SERVER:
            return ServerPermission.OWNER
        return None

    def has_server_access(self, user_id, server_name, min_permission=None):
        return self.get_server_permission(user_id, server_name) is not None

    def is_admin(self, user_id):
        return False


class FakeServerDataService:
    async def get_or_fetch(self, server, data_type):
        assert server == SERVER
        return [
            "kraken",
            "kraken_paper_trade",
            "binance",
            "binance_testnet",
            "coinbase_sandbox",
            "hyperliquid_perpetual",
            "hummingbot/gateway",
        ]


def test_credential_picker_exposes_exchange_connectors_but_not_paper_adapters(monkeypatch):
    cm = FakeConfigManager()
    monkeypatch.setattr(settings_routes, "get_config_manager", lambda: cm)
    monkeypatch.setattr(settings_routes, "get_server_data_service", FakeServerDataService)
    monkeypatch.setattr("condor.web.auth.get_config_manager", lambda: cm)

    app = FastAPI()
    app.include_router(settings_routes.router)
    app.dependency_overrides[get_current_user] = lambda: OWNER

    response = TestClient(app).get(
        "/settings/connectors",
        params={"server": SERVER, "type": "spot"},
    )

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["connectors"]]
    assert "kraken" in names
    assert "binance" in names
    assert "kraken_paper_trade" not in names
    assert "binance_testnet" not in names
    assert "coinbase_sandbox" not in names
    assert "hyperliquid_perpetual" not in names
    assert "hummingbot/gateway" not in names
