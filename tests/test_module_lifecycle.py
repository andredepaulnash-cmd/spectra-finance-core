import pytest

from spectra_core.module_lifecycle import ModuleControlError, ModuleLifecycleStore


def test_triggered_modules_start_safe_and_persist(tmp_path):
    path = tmp_path / "modules.sqlite"
    store = ModuleLifecycleStore(path)
    rows = {x["module_id"]: x for x in store.snapshot()}
    assert rows["vcap"]["state"] == "RUNNING"
    assert rows["dttp"]["state"] == "MONITORING"
    assert rows["dtsl"]["state"] == "BLOCKED"
    assert not rows["dttp"]["trigger_enabled"]
    store.set_trigger("dttp", True)
    reopened = ModuleLifecycleStore(path)
    dttp = {x["module_id"]: x for x in reopened.snapshot()}["dttp"]
    assert dttp["trigger_enabled"] is True
    assert dttp["state"] == "BLOCKED"  # implementation is not verified


def test_unverified_module_cannot_activate_even_if_trigger_requested(tmp_path):
    store = ModuleLifecycleStore(tmp_path / "modules.sqlite")
    store.set_trigger("dttp", True)
    with pytest.raises(ModuleControlError, match="implementation is unverified"):
        store.activate("dttp", reason="operator request")
    with pytest.raises(ModuleControlError, match="implementation is unverified"):
        store.activate("dtsl", reason="operator request")


def test_trigger_disable_prevents_activation_and_logs_transition(tmp_path):
    store = ModuleLifecycleStore(tmp_path / "modules.sqlite")
    store.set_trigger("dttp", True)
    store.set_trigger("dttp", False)
    with pytest.raises(ModuleControlError, match="trigger is disabled"):
        store.activate("dttp", reason="should not run")
    with store._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM module_control_events").fetchone()[0]
    assert count == 2


def test_unknown_and_non_trigger_modules_reject_toggle(tmp_path):
    store = ModuleLifecycleStore(tmp_path / "modules.sqlite")
    with pytest.raises(ModuleControlError):
        store.set_trigger("vcap", True)
    with pytest.raises(ModuleControlError):
        store.set_trigger("unknown", True)
