"""Bootstraps the read-only Spectra Condor monitor when explicitly enabled."""

from __future__ import annotations

import logging
import os

from condor.agents.agent import AgentStore
from condor.agents.engine import TickEngine
from condor.agents.strategy import StrategyStore
from condor.runtime.loops import get_supervisor

log = logging.getLogger(__name__)

_FORBIDDEN_TOOLS = {
    "create_grid_executor",
    "create_dca_executor",
    "create_order",
    "cancel_order",
    "stop_executor",
    "withdraw",
    "transfer",
    "delegate",
}


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _frequency_seconds() -> int:
    raw = os.getenv("SPECTRA_CONDOR_AGENT_INTERVAL_SECONDS", "300")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 300
    return min(max(value, 60), 3600)


async def start_spectra_monitor_if_enabled(bot=None) -> bool:
    """Start one PydanticAI-backed read-only monitor loop.

    The tool allowlist is enforced by the PydanticAI client. ACP clients do not
    enforce the same allowlist, so this unattended monitor deliberately refuses
    to auto-start under an ACP model key.
    """
    if not _enabled("SPECTRA_CONDOR_AUTO_START_AGENT", default=False):
        return False

    agent_key = os.getenv(
        "SPECTRA_CONDOR_AGENT_KEY",
        "openai:gpt-6-luna",
    ).strip()
    if not agent_key.startswith("openai:"):
        log.error(
            "Spectra monitor not started: unattended tool allowlist requires "
            "a PydanticAI OpenAI model key (openai:...)."
        )
        return False
    if not os.getenv("OPENAI_API_KEY", "").strip():
        log.info(
            "Spectra monitor not started: OPENAI_API_KEY is absent. "
            "The deterministic paper supervisor can still operate."
        )
        return False

    try:
        agent = AgentStore().get("spectra_monitor")
        strategy = StrategyStore().get("spectra_monitor", "autonomous_paper")
    except Exception:
        log.exception("Spectra monitor agent or strategy could not be loaded")
        return False
    if agent is None or strategy is None:
        log.error("Spectra monitor agent/strategy files are missing")
        return False

    tools = {str(tool) for tool in (agent.tools or [])}
    forbidden = tools & _FORBIDDEN_TOOLS
    if forbidden:
        log.error(
            "Spectra monitor refused to start; unsafe tools are allowlisted: %s",
            ", ".join(sorted(forbidden)),
        )
        return False

    supervisor = get_supervisor()
    for running in supervisor.for_strategy("spectra_monitor", "autonomous_paper"):
        if getattr(running, "_running", False):
            log.info("Spectra monitor already running as %s", running.agent_id)
            return False

    config = dict(strategy.default_config or {})
    config.update(
        {
            "execution_mode": "loop",
            "frequency_sec": _frequency_seconds(),
            "restart_on_boot": True,
            "server_name": "local",
            "agent_key": agent_key,
        }
    )
    engine = TickEngine(
        agent=agent,
        strategy=strategy,
        config=config,
        chat_id=0,
        user_id=0,
    )
    try:
        await engine.start(bot=bot)
    except Exception:
        log.exception("Spectra monitor loop failed to start")
        return False
    log.info(
        "Spectra Condor monitor started: %s (frequency=%ss, restart_on_boot=true)",
        engine.agent_id,
        config["frequency_sec"],
    )
    return True
