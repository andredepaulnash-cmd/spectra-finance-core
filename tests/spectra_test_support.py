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
    # These tests use in-process fake clients, not the authenticated API
    # middleware. Receipt enforcement is exercised by dedicated transport tests.
    monkeypatch.setenv("SPECTRA_REQUIRE_CORE_RECEIPTS", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv(
        "SPECTRA_APPROVED_ASSETS_FILE",
        str(Path(__file__).parent / "fixtures" / "approved_assets.json"),
    )
    monkeypatch.setattr(settings, "paper_only", True)
    if agent_slug:
        monkeypatch.setattr(settings, "agent_slug", "spectra")



def configure_spectra_live_dispatch(monkeypatch, settings):
    """Opt a fake-client test into the explicitly authorized live code path."""
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "true")
    # Fake-client unit tests do not carry the real API dispatch middleware.
    monkeypatch.setenv("SPECTRA_REQUIRE_CORE_RECEIPTS", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv(
        "SPECTRA_DISPATCH_PRIVATE_KEY",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    monkeypatch.setattr(settings, "paper_only", False)
    monkeypatch.setattr(settings, "agent_slug", "spectra")
