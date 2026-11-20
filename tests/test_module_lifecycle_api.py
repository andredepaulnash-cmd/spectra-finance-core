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
async def test_module_trigger_endpoint_rejects_global_trigger_for_position_scoped_exits(monkeypatch, tmp_path):
    store = ModuleLifecycleStore(tmp_path / "module-controls.sqlite3")
    monkeypatch.setattr(
        servers,
        "get_config_manager",
        lambda: FakeConfigManager(servers.ServerPermission.OWNER),
    )
    monkeypatch.setattr(servers, "get_spectra_module_lifecycle_store", lambda: store)

    with pytest.raises(HTTPException) as exc:
        await servers.set_spectra_module_trigger(
            "local-paper",
            "dttp",
            servers.ModuleTriggerRequest(enabled=True),
            user=SimpleNamespace(id=7),
        )

    assert exc.value.status_code == 409
    control = next(row for row in store.snapshot() if row["module_id"] == "dttp")
    assert control["trigger_enabled"] is False
    assert control["state"] == "MONITORING"


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
    assert next(row for row in store.snapshot() if row["module_id"] == "dttp")["trigger_enabled"] is False
