import pytest

from spectra_core.execution_mode import paper_only_enabled, resolve_kraken_connector_name


def test_execution_defaults_to_paper_connector(monkeypatch):
    monkeypatch.delenv("SPECTRA_PAPER_ONLY", raising=False)
    monkeypatch.delenv("SPECTRA_ALLOW_LIVE_EXECUTION", raising=False)
    assert paper_only_enabled() is True
    assert resolve_kraken_connector_name() == "kraken_paper_trade"


def test_paper_mode_rejects_live_connector(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    with pytest.raises(ValueError, match="requires connector 'kraken_paper_trade'"):
        resolve_kraken_connector_name("kraken")


def test_live_mode_requires_separate_explicit_authorization(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.delenv("SPECTRA_ALLOW_LIVE_EXECUTION", raising=False)
    with pytest.raises(RuntimeError, match="separate explicit authorization"):
        resolve_kraken_connector_name()


def test_explicitly_authorized_live_mode_uses_live_connector(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "true")
    assert resolve_kraken_connector_name() == "kraken"


def test_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "sometimes")
    with pytest.raises(RuntimeError, match="explicit boolean"):
        resolve_kraken_connector_name()
