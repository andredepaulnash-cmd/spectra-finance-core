from __future__ import annotations

import os


_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


def paper_only_enabled() -> bool:
    """Resolve Spectra's execution mode, defaulting to fail-closed paper mode."""
    raw = os.getenv("SPECTRA_PAPER_ONLY", "true").strip().lower()
    if raw not in _TRUE | _FALSE:
        raise RuntimeError("SPECTRA_PAPER_ONLY must be an explicit boolean")

    enabled = raw in _TRUE
    if not enabled:
        allow_live = os.getenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false").strip().lower()
        if allow_live not in _TRUE:
            raise RuntimeError(
                "Spectra live execution requires separate explicit authorization "
                "through SPECTRA_ALLOW_LIVE_EXECUTION"
            )
    return enabled


def resolve_kraken_connector_name(requested: str | None = None) -> str:
    """Return the only Kraken connector allowed by the effective execution mode."""
    expected = "kraken_paper_trade" if paper_only_enabled() else "kraken"
    supplied = (requested or "").strip().lower()
    if supplied and supplied != expected:
        mode = "paper-only" if expected.endswith("_paper_trade") else "explicitly authorized live"
        raise ValueError(
            f"Spectra {mode} mode requires connector '{expected}', not {supplied!r}"
        )
    return expected
