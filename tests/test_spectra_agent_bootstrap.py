from types import SimpleNamespace

import pytest

from condor.runtime import spectra_agent_bootstrap as bootstrap


class FakeEngine:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.agent_id = "spectra_monitor.autonomous_paper_1"
        self._running = False
        self.started_with = None
        self.__class__.instances.append(self)

    async def start(self, bot=None):
        self.started_with = bot
        self._running = True


class FakeSupervisor:
    def __init__(self, engines=()):
        self.engines = list(engines)

    def for_strategy(self, agent_slug, strategy_slug):
        assert agent_slug == "spectra_monitor"
        assert strategy_slug == "autonomous_paper"
        return self.engines


def install_fakes(monkeypatch, *, tools=None, engines=()):
    agent = SimpleNamespace(
        slug="spectra_monitor",
        tools=list(tools or ["get_prices", "trading_agent_journal_write"]),
    )
    strategy = SimpleNamespace(
        slug="autonomous_paper",
        default_config={
            "frequency_sec": 300,
            "risk_limits": {"max_open_executors": 5},
        },
    )
    monkeypatch.setattr(
        bootstrap,
        "AgentStore",
        lambda: SimpleNamespace(get=lambda slug: agent if slug == "spectra_monitor" else None),
    )
    monkeypatch.setattr(
        bootstrap,
        "StrategyStore",
        lambda: SimpleNamespace(
            get=lambda agent_slug, slug: strategy
            if (agent_slug, slug) == ("spectra_monitor", "autonomous_paper")
            else None
        ),
    )
    supervisor = FakeSupervisor(engines)
    monkeypatch.setattr(bootstrap, "get_supervisor", lambda: supervisor)
    FakeEngine.instances = []
    monkeypatch.setattr(bootstrap, "TickEngine", FakeEngine)
    return supervisor


@pytest.mark.asyncio
async def test_bootstrap_is_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("SPECTRA_CONDOR_AUTO_START_AGENT", raising=False)
    assert await bootstrap.start_spectra_monitor_if_enabled() is False


@pytest.mark.asyncio
async def test_bootstrap_skips_when_openai_key_is_missing(monkeypatch):
    monkeypatch.setenv("SPECTRA_CONDOR_AUTO_START_AGENT", "true")
    monkeypatch.setenv("SPECTRA_CONDOR_AGENT_KEY", "openai:gpt-6-luna")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await bootstrap.start_spectra_monitor_if_enabled() is False


@pytest.mark.asyncio
async def test_bootstrap_refuses_acp_because_tool_allowlist_is_not_enforced(monkeypatch):
    monkeypatch.setenv("SPECTRA_CONDOR_AUTO_START_AGENT", "true")
    monkeypatch.setenv("SPECTRA_CONDOR_AGENT_KEY", "claude-code")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert await bootstrap.start_spectra_monitor_if_enabled() is False


@pytest.mark.asyncio
async def test_bootstrap_starts_one_read_only_pydantic_ai_loop(monkeypatch):
    monkeypatch.setenv("SPECTRA_CONDOR_AUTO_START_AGENT", "true")
    monkeypatch.setenv("SPECTRA_CONDOR_AGENT_KEY", "openai:gpt-6-luna")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SPECTRA_CONDOR_AGENT_INTERVAL_SECONDS", "180")
    install_fakes(monkeypatch)
    bot = object()

    assert await bootstrap.start_spectra_monitor_if_enabled(bot=bot) is True
    engine = FakeEngine.instances[0]
    assert engine.kwargs["config"]["agent_key"] == "openai:gpt-6-luna"
    assert engine.kwargs["config"]["execution_mode"] == "loop"
    assert engine.kwargs["config"]["frequency_sec"] == 180
    assert engine.kwargs["config"]["restart_on_boot"] is True
    assert engine.kwargs["config"]["server_name"] == "local"
    assert engine.started_with is bot


@pytest.mark.asyncio
async def test_bootstrap_refuses_agent_with_trade_tools(monkeypatch):
    monkeypatch.setenv("SPECTRA_CONDOR_AUTO_START_AGENT", "true")
    monkeypatch.setenv("SPECTRA_CONDOR_AGENT_KEY", "openai:gpt-6-luna")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    install_fakes(monkeypatch, tools=["get_prices", "create_grid_executor"])

    assert await bootstrap.start_spectra_monitor_if_enabled() is False
    assert FakeEngine.instances == []


@pytest.mark.asyncio
async def test_bootstrap_does_not_duplicate_running_session(monkeypatch):
    monkeypatch.setenv("SPECTRA_CONDOR_AUTO_START_AGENT", "true")
    monkeypatch.setenv("SPECTRA_CONDOR_AGENT_KEY", "openai:gpt-6-luna")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    running = SimpleNamespace(_running=True, agent_id="existing-monitor")
    install_fakes(monkeypatch, engines=[running])

    assert await bootstrap.start_spectra_monitor_if_enabled() is False
    assert FakeEngine.instances == []
