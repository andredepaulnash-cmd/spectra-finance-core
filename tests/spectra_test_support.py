"""Shared fixtures for offline Spectra paper-dispatch tests."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class FakeSession:
    def __init__(self):
        self.headers = {}


def configure_spectra_paper_dispatch(monkeypatch, settings, *, agent_slug=True):
    """Provide a deterministic local signing key and enforce paper-only settings."""
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    encoded = base64.urlsafe_b64encode(private_raw).decode("ascii").rstrip("=")
    monkeypatch.setenv("SPECTRA_DISPATCH_PRIVATE_KEY", encoded)
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv(
        "SPECTRA_APPROVED_ASSETS_FILE",
        str(Path(__file__).parent / "fixtures" / "approved_assets.json"),
    )
    monkeypatch.setattr(settings, "paper_only", True)
    if agent_slug:
        monkeypatch.setattr(settings, "agent_slug", "spectra")
