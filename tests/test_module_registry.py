from spectra_core.module_registry import get_spectra_module_registry


def test_registry_preserves_advanced_module_names_and_conservative_statuses():
    registry = get_spectra_module_registry()
    modules = {module["id"]: module for module in registry["modules"]}

    assert registry["product"] == "Spectra Finance"
    assert registry["live_trading_enabled"] is False
    assert {"dttp", "dtsl", "pssm", "vcap", "mir"} <= set(modules)
    assert modules["vcap"]["status"] == "partial"
    assert modules["dtsl"]["status"] == "unverified"
    assert modules["pssm"]["status"] == "not-implemented"
    assert modules["mir"]["status"] == "research-only"


def test_registry_returns_detached_snapshots():
    first = get_spectra_module_registry()
    first["modules"][0]["name"] = "changed"
    second = get_spectra_module_registry()
    assert second["modules"][0]["name"] == "Condor"


def test_condor_api_exposes_spectra_module_registry_route():
    from condor.web.routes.servers import router

    assert any(
        route.path == "/servers/{name}/spectra/modules" and "GET" in route.methods
        for route in router.routes
    )
