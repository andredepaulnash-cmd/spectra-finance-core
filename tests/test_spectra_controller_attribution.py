from mcp_servers.hummingbot_api.tools import executor_create


def test_explicit_controller_id_wins_for_spectra_agent(monkeypatch):
    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")

    assert executor_create._select_controller_tag("condor-portfolio-agent") == "condor-portfolio-agent"
    assert executor_create._select_controller_tag(None) == "spectra"


def test_non_spectra_agent_keeps_main_fallback(monkeypatch):
    monkeypatch.setattr(executor_create.settings, "agent_slug", "condor")

    assert executor_create._select_controller_tag(None) == "main"
    assert executor_create._select_controller_tag("condor-asset-agent") == "condor-asset-agent"
