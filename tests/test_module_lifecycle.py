import pytest

from spectra_core.module_lifecycle import ModuleControlError, ModuleLifecycleStore


def test_source_gated_adaptive_exit_modules_start_in_monitoring_without_global_trigger(tmp_path):
    path = tmp_path / "modules.sqlite"
    store = ModuleLifecycleStore(path)
    rows = {x["module_id"]: x for x in store.snapshot()}
    assert rows["vcap"]["state"] == "MONITORING"
    assert rows["dttp"]["state"] == "MONITORING"
    assert rows["dtsl"]["state"] == "MONITORING"
    assert not rows["dttp"]["trigger_enabled"]
    assert not rows["dtsl"]["trigger_enabled"]
    with pytest.raises(ModuleControlError, match="no independent activation trigger"):
        store.set_trigger("dttp", True)
    reopened = ModuleLifecycleStore(path)
    dttp = {x["module_id"]: x for x in reopened.snapshot()}["dttp"]
    assert dttp["trigger_enabled"] is False
    assert dttp["state"] == "MONITORING"


def test_integrated_adaptive_exit_modules_cannot_be_activated_globally(tmp_path):
    store = ModuleLifecycleStore(tmp_path / "modules.sqlite")
    with pytest.raises(ModuleControlError, match="trigger is disabled"):
        store.activate("dttp", reason="global activation is not supported")
    with pytest.raises(ModuleControlError, match="trigger is disabled"):
        store.activate("dtsl", reason="global activation is not supported")


def test_default_unverified_rows_migrate_to_monitoring_without_overwriting_operator_state(tmp_path):
    path = tmp_path / "modules.sqlite"
    with ModuleLifecycleStore(path)._connect() as db:
        db.execute(
            "UPDATE module_controls SET state='BLOCKED',reason=? WHERE module_id='dtsl'",
            ("runtime initialized; implementation not yet verified",),
        )
        db.execute(
            "UPDATE module_controls SET state='BLOCKED',reason=? WHERE module_id='dttp'",
            ("operator requested a hold",),
        )
    rows = {x["module_id"]: x for x in ModuleLifecycleStore(path).snapshot()}
    assert rows["dtsl"]["state"] == "MONITORING"
    assert rows["dttp"]["state"] == "BLOCKED"


def test_unknown_and_non_trigger_modules_reject_toggle(tmp_path):
    store = ModuleLifecycleStore(tmp_path / "modules.sqlite")
    with pytest.raises(ModuleControlError):
        store.set_trigger("vcap", True)
    with pytest.raises(ModuleControlError):
        store.set_trigger("dttp", True)
    with pytest.raises(ModuleControlError):
        store.set_trigger("dtsl", True)
    with pytest.raises(ModuleControlError):
        store.set_trigger("unknown", True)
