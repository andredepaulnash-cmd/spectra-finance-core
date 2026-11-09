from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from condor.web.routes import servers
from spectra_core.module_lifecycle import ModuleLifecycleStore


class FakeConfigManager:
    def __init__(self, permission):
        self.permission = permission

    def has_server_access(self, user_id, server_name):
        return True

    def get_server_permission(self, user_id, server_name):
        return self.permission

    def is_admin(self, user_id):
        return False


@pytest.mark.asyncio
async def test_module_trigger_endpoint_is_owner_only_and_never_activates(monkeypatch, tmp_path):
    store = ModuleLifecycleStore(tmp_path / "module-controls.sqlite3")
    monkeypatch.setattr(
        servers,
        "get_config_manager",
        lambda: FakeConfigManager(servers.ServerPermission.OWNER),
    )
    monkeypatch.setattr(servers, "get_spectra_module_lifecycle_store", lambda: store)

    result = await servers.set_spectra_module_trigger(
        "local-paper",
        "dttp",
        servers.ModuleTriggerRequest(enabled=True),
        user=SimpleNamespace(id=7),
    )

    assert result["control"]["trigger_enabled"] is True
    assert result["control"]["state"] == "BLOCKED"
    assert result["activation_performed"] is False
    assert result["live_trading_enabled"] is False
    assert result["orders_created"] == 0


@pytest.mark.asyncio
async def test_module_trigger_endpoint_rejects_shared_trader(monkeypatch, tmp_path):
    store = ModuleLifecycleStore(tmp_path / "module-controls.sqlite3")
    monkeypatch.setattr(
        servers,
        "get_config_manager",
        lambda: FakeConfigManager(servers.ServerPermission.TRADER),
    )
    monkeypatch.setattr(servers, "get_spectra_module_lifecycle_store", lambda: store)

    with pytest.raises(HTTPException) as exc:
        await servers.set_spectra_module_trigger(
            "local-paper",
            "dttp",
            servers.ModuleTriggerRequest(enabled=True),
            user=SimpleNamespace(id=7),
        )
    assert exc.value.status_code == 403
    assert store.snapshot()[1]["trigger_enabled"] is False
