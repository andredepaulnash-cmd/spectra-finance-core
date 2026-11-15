from spectra_core.module_registry import get_spectra_module_registry


def test_registry_preserves_dual_mode_architecture_and_conservative_readiness():
    registry = get_spectra_module_registry()
    modules = {module["id"]: module for module in registry["modules"]}

    assert registry["product"] == "Spectra Finance"
    assert registry["live_trading_available"] is True
    assert registry["live_trading_enabled"] is False
    assert registry["live_execution_requires_explicit_authorization"] is True
    assert registry["paper_only_default"] is True
    assert registry["paper_only_target"] is False
    assert {"asset-intelligence", "portfolio-scheduler", "dttp", "dtsl", "pssm", "vcap", "mir", "live-execution"} <= set(modules)
    assert modules["vcap"]["status"] == "partial"
    assert modules["portfolio-scheduler"]["status"] == "integration-in-progress"
    assert modules["dttp"]["readiness"] == "source-recovered"
    assert modules["dtsl"]["status"] == "unverified"
    assert modules["pssm"]["status"] == "not-implemented"
    assert modules["mir"]["status"] == "research-only"
    assert modules["live-execution"]["status"] == "available-behind-authorization"
    assert any("Condor" in item["name"] for item in registry["system_flow"])
    assert any("Hummingbot" in item["name"] for item in registry["system_flow"])


def test_registry_returns_detached_snapshots():
    first = get_spectra_module_registry()
    first["modules"][0]["name"] = "changed"
    second = get_spectra_module_registry()
    assert second["modules"][0]["name"] == "Spectra Intelligence"
