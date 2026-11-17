"""Read the local Spectra mode and supervisor heartbeat for Condor agents."""

from __future__ import annotations

import os
import time


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _paper_connectors() -> list[str]:
    raw = os.getenv("SPECTRA_PAPER_CONNECTORS", "kraken_paper_trade")
    return sorted({item.strip().lower() for item in raw.split(",") if item.strip()})


async def fetch_spectra_runtime(_client=None, **_kw) -> dict:
    """Return fail-closed local runtime status without exposing credentials.

    Condor and the Spectra portfolio supervisor run in the same Core process.
    Reading the supervisor's atomically persisted status is more authoritative
    than inventing a Hummingbot API extension route or relying on stale chat
    context. The Hummingbot executors/positions providers independently report
    exchange-side state.
    """
    from spectra_core.portfolio_supervisor import get_portfolio_supervisor_status
    from spectra_core.runtime import spectra_paper_only_enabled

    now = time.time()
    paper_only = bool(spectra_paper_only_enabled())
    live_authorized = _env_bool("SPECTRA_ALLOW_LIVE_EXECUTION", False)
    connectors = _paper_connectors()
    tick_interval = max(
        1.0,
        float(os.getenv("SPECTRA_PORTFOLIO_TICK_SECONDS", "10") or "10"),
    )

    portfolio = get_portfolio_supervisor_status()
    updated_at = portfolio.get("updated_at")
    try:
        status_age = max(0.0, now - float(updated_at)) if updated_at is not None else None
    except (TypeError, ValueError):
        status_age = None

    supervisor_state = str(portfolio.get("supervisor_state", "UNAVAILABLE")).upper()
    if status_age is not None and status_age > max(30.0, tick_interval * 3):
        supervisor_state = "STALE"

    mode = "PAPER" if paper_only else ("LIVE_AUTHORIZED" if live_authorized else "LIVE_LOCKED")
    healthy = bool(
        paper_only
        and connectors
        and supervisor_state == "RUNNING"
        and status_age is not None
        and status_age <= max(30.0, tick_interval * 3)
    )

    return {
        "product": "Spectra Finance",
        "mode": mode,
        "paper_only": paper_only,
        # Capability is distinct from authorization. Live code remains present
        # for a future user-authorized mode switch, but is not authorized here.
        "live_dispatch_available": True,
        "live_dispatch_authorized": live_authorized,
        "paper_connector_count": len(connectors),
        "paper_connectors": connectors,
        "paper_connector_status": "CONFIGURED" if connectors else "MISSING",
        "healthy": healthy,
        "portfolio_supervisor": {
            **portfolio,
            "supervisor_state": supervisor_state,
            "status_age_seconds": status_age,
        },
    }
