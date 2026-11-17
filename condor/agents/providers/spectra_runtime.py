"""Condor core provider for the authenticated Spectra runtime boundary."""

from __future__ import annotations

import logging
from typing import Any

from condor.fetchers.spectra_runtime import fetch_spectra_runtime

from . import register_provider
from .base import BaseProvider, ProviderResult

log = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "password", "secret", "token", "credential")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class SpectraRuntimeProvider(BaseProvider):
    """Expose paper/live mode and Spectra supervisor health to every agent tick."""

    name = "spectra_runtime"
    is_core = True

    async def execute(
        self,
        client: Any,
        config: dict,
        agent_id: str = "",
        bot_names: list[str] | None = None,
        since: float = 0.0,
    ) -> ProviderResult:
        try:
            payload = await fetch_spectra_runtime(client)
            if not isinstance(payload, dict):
                raise TypeError("Spectra runtime response must be an object")
            data = _redact(payload)
        except Exception as exc:
            log.warning("Spectra runtime provider unavailable: %s", str(exc)[:240])
            data = {
                "product": "Spectra Finance",
                "mode": "UNAVAILABLE",
                "paper_only": False,
                "live_dispatch_available": False,
                "paper_connector_count": 0,
                "healthy": False,
                "error": str(exc)[:240],
            }
            return ProviderResult(
                name=self.name,
                data=data,
                summary=(
                    "Spectra Runtime: UNAVAILABLE — fail closed. Do not infer that "
                    "paper trading is ready or that live dispatch is disabled safely. "
                    "The deterministic supervisor and runtime boundary need diagnosis."
                ),
            )

        mode = str(data.get("mode", "UNAVAILABLE")).upper()
        paper_only = bool(data.get("paper_only", False))
        live_available = bool(data.get("live_dispatch_available", False))
        connectors = data.get("paper_connector_count", 0)
        healthy = bool(data.get("healthy", mode not in {"UNAVAILABLE", "UNSUPPORTED"}))
        lines = [
            f"Spectra Runtime: mode={mode} | healthy={healthy}",
            f"  Paper-only={paper_only} | live dispatch available={live_available} "
            f"| live authorized={bool(data.get('live_dispatch_authorized', False))}",
            f"  Paper connectors={connectors} "
            f"| connector status={data.get('paper_connector_status', 'unknown')}",
        ]
        supervisor = data.get("portfolio_supervisor")
        if isinstance(supervisor, dict):
            lines.append(
                "  Portfolio supervisor: "
                f"state={supervisor.get('supervisor_state', 'unknown')} "
                f"status_age_seconds={supervisor.get('status_age_seconds', 'unknown')}"
            )
        vcap = data.get("adaptive_vcap")
        if isinstance(vcap, dict):
            lines.append(
                "  Adaptive VCAP: "
                f"active={vcap.get('active_fragments', 0)} "
                f"waiting={vcap.get('waiting_fragments', 0)} "
                f"reconciling={vcap.get('reconciling_fragments', 0)} "
                f"paused={vcap.get('paused_fragments', 0)}"
            )
        if not healthy or mode in {"UNAVAILABLE", "UNSUPPORTED"}:
            lines.append("  SAFETY: runtime status is not verified; make no execution assumptions.")
        return ProviderResult(
            name=self.name,
            data=data,
            summary="\n".join(lines),
        )


register_provider(SpectraRuntimeProvider())
