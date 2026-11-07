import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from spectra_core.models import BreakerMode
from spectra_core.runtime import (
    get_breaker_mode,
    get_capital_governor,
    load_capital_policy,
    reset_runtime_cache,
    spectra_live_state_dir,
    spectra_paper_only_enabled,
    spectra_state_dir,
)


def _valid_policy():
    return {
        "reserve_floor_quote": "100",
        "max_total_committed_quote": "500",
        "max_pair_quote": "150",
        "max_position_quote": "150",
        "max_recovery_quote": "100",
        "max_leverage": "1",
        "reservation_ttl_seconds": 120,
        "position_hold_snapshot_ttl_seconds": 30,
    }


def test_missing_capital_policy_fails_closed(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "SPECTRA_STATE_DIR",
        str(tmp_path),
    )
    reset_runtime_cache()

    with pytest.raises(
        RuntimeError,
        match="not configured",
    ):
        get_capital_governor()

    reset_runtime_cache()


def test_malformed_capital_policy_fails_closed(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "capital_policy.json").write_text(
        "{not-json"
    )

    monkeypatch.setenv(
        "SPECTRA_STATE_DIR",
        str(tmp_path),
    )
    reset_runtime_cache()

    with pytest.raises(
        RuntimeError,
        match="capital policy is invalid",
    ):
        get_capital_governor()

    reset_runtime_cache()


def test_state_dir_override_controls_governor_storage(
    monkeypatch,
    tmp_path,
):
    state_dir = tmp_path / "spectra-state"
    state_dir.mkdir()

    (state_dir / "capital_policy.json").write_text(
        json.dumps(_valid_policy())
    )

    monkeypatch.setenv(
        "SPECTRA_STATE_DIR",
        str(state_dir),
    )
    reset_runtime_cache()

    governor = get_capital_governor()

    assert governor.db_path == state_dir / "capital.sqlite3"
    assert governor.policy.reserve_floor_quote == Decimal("100")
    assert governor.policy.max_leverage == Decimal("1")

    reset_runtime_cache()


def test_shipped_breaker_template_is_halted():
    root = Path(__file__).resolve().parents[1]

    raw = json.loads(
        (
            root
            / "examples"
            / "spectra"
            / "breaker_state.json"
        ).read_text()
    )

    assert BreakerMode(raw["mode"]) == BreakerMode.HALTED


def test_shipped_capital_policy_template_is_fail_closed():
    root = Path(__file__).resolve().parents[1]

    raw = json.loads(
        (
            root
            / "examples"
            / "spectra"
            / "capital_policy.json"
        ).read_text()
    )

    assert Decimal(raw["max_total_committed_quote"]) == 0
    assert Decimal(raw["max_pair_quote"]) == 0
    assert Decimal(raw["max_position_quote"]) == 0
    assert Decimal(raw["max_recovery_quote"]) == 0


def test_missing_breaker_state_is_halted(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "SPECTRA_STATE_DIR",
        str(tmp_path),
    )

    assert get_breaker_mode() == BreakerMode.HALTED



def test_load_capital_policy_is_read_only(monkeypatch, tmp_path):
    state_dir = tmp_path / "spectra-state"
    state_dir.mkdir()

    (state_dir / "capital_policy.json").write_text(
        json.dumps(_valid_policy())
    )

    monkeypatch.setenv("SPECTRA_STATE_DIR", str(state_dir))
    reset_runtime_cache()

    db_path = state_dir / "capital.sqlite3"
    assert not db_path.exists()

    policy = load_capital_policy()

    assert policy.reserve_floor_quote == Decimal("100")
    assert policy.max_leverage == Decimal("1")
    assert not db_path.exists()

    reset_runtime_cache()


def test_load_capital_policy_validates_without_creating_db(
    monkeypatch,
    tmp_path,
):
    state_dir = tmp_path / "spectra-state"
    state_dir.mkdir()

    raw = _valid_policy()
    raw["max_leverage"] = "0"

    (state_dir / "capital_policy.json").write_text(
        json.dumps(raw)
    )

    monkeypatch.setenv("SPECTRA_STATE_DIR", str(state_dir))
    reset_runtime_cache()

    db_path = state_dir / "capital.sqlite3"

    with pytest.raises(
        RuntimeError,
        match="max_leverage must be positive",
    ):
        load_capital_policy()

    assert not db_path.exists()

    reset_runtime_cache()


def test_capital_policy_requires_position_hold_snapshot_ttl(
    monkeypatch,
    tmp_path,
):
    policy = _valid_policy()
    del policy["position_hold_snapshot_ttl_seconds"]

    (tmp_path / "capital_policy.json").write_text(
        json.dumps(policy)
    )

    monkeypatch.setenv("SPECTRA_STATE_DIR", str(tmp_path))
    reset_runtime_cache()

    with pytest.raises(
        RuntimeError,
        match="position_hold_snapshot_ttl_seconds",
    ):
        load_capital_policy()

    reset_runtime_cache()


def test_position_hold_snapshot_ttl_must_be_positive_integer(
    monkeypatch,
    tmp_path,
):
    for bad_value in (0, -1, True):
        policy = _valid_policy()
        policy["position_hold_snapshot_ttl_seconds"] = bad_value

        (tmp_path / "capital_policy.json").write_text(
            json.dumps(policy)
        )

        monkeypatch.setenv("SPECTRA_STATE_DIR", str(tmp_path))
        reset_runtime_cache()

        with pytest.raises(
            RuntimeError,
            match="position_hold_snapshot_ttl_seconds",
        ):
            load_capital_policy()

        reset_runtime_cache()



def test_paper_state_dir_is_separate_from_live_state(
    monkeypatch,
    tmp_path,
):
    live_dir = tmp_path / "live-state"
    paper_dir = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(live_dir))
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(paper_dir))

    assert spectra_live_state_dir() == live_dir
    assert spectra_state_dir() == paper_dir
    assert spectra_state_dir() != spectra_live_state_dir()


def test_paper_state_dir_cannot_equal_live_state(
    monkeypatch,
    tmp_path,
):
    same_dir = tmp_path / "same-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(same_dir))
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(same_dir))

    with pytest.raises(RuntimeError, match="must be separate"):
        spectra_state_dir()


def test_paper_mode_reads_live_policy_as_read_only_fallback(
    monkeypatch,
    tmp_path,
):
    live_dir = tmp_path / "live-state"
    paper_dir = tmp_path / "paper-state"
    live_dir.mkdir()
    paper_dir.mkdir()
    (live_dir / "capital_policy.json").write_text(
        json.dumps(_valid_policy())
    )

    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(live_dir))
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(paper_dir))
    reset_runtime_cache()

    policy = load_capital_policy()

    assert policy.reserve_floor_quote == Decimal("100")
    assert policy.max_leverage == Decimal("1")
    assert not (paper_dir / "capital.sqlite3").exists()
    assert not (live_dir / "capital.sqlite3").exists()

    reset_runtime_cache()


def test_paper_breaker_prefers_paper_file_then_falls_back_live(
    monkeypatch,
    tmp_path,
):
    live_dir = tmp_path / "live-state"
    paper_dir = tmp_path / "paper-state"
    live_dir.mkdir()
    paper_dir.mkdir()
    (live_dir / "breaker_state.json").write_text(
        json.dumps({"mode": "NORMAL"})
    )

    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(live_dir))
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(paper_dir))

    assert get_breaker_mode() == BreakerMode.NORMAL

    (paper_dir / "breaker_state.json").write_text(
        json.dumps({"mode": "HALTED"})
    )
    assert get_breaker_mode() == BreakerMode.HALTED


def test_invalid_paper_mode_value_fails_closed(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "sometimes")
    with pytest.raises(RuntimeError, match="explicit boolean"):
        spectra_state_dir()


# Spectra runtime status fetcher tests
import asyncio

from condor.fetchers.spectra_runtime import fetch_spectra_runtime


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        if self.status >= 400 and self.status != 404:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.payload


class FakeSession:
    closed = False

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        return self.response


class FakeClient:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, response):
        self._session = FakeSession(response)


def test_fetch_spectra_runtime_returns_paper_only_status():
    payload = {
        "mode": "PAPER_ONLY",
        "paper_only": True,
        "live_dispatch_available": False,
        "paper_connector_count": 1,
        "paper_connectors": ["paper-account/kraken_paper_trade"],
    }
    client = FakeClient(FakeResponse(200, payload))

    result = asyncio.run(fetch_spectra_runtime(client))

    assert result == payload
    assert client._session.calls == [
        ("http://127.0.0.1:8000/spectra/runtime", 5)
    ]


def test_missing_runtime_endpoint_is_explicitly_unsupported():
    client = FakeClient(FakeResponse(404))

    result = asyncio.run(fetch_spectra_runtime(client))

    assert result["mode"] == "UNSUPPORTED"
    assert result["paper_only"] is False
    assert result["live_dispatch_available"] is True


def test_uninitialized_client_fails_closed():
    class Uninitialized:
        base_url = "http://127.0.0.1:8000"
        _session = None

    try:
        asyncio.run(fetch_spectra_runtime(Uninitialized()))
    except RuntimeError as exc:
        assert "not initialized" in str(exc)
    else:
        raise AssertionError("uninitialized Hummingbot client was accepted")



def test_spectra_agent_defaults_to_paper_only_unless_explicitly_overridden(
    monkeypatch,
):
    monkeypatch.delenv("SPECTRA_PAPER_ONLY", raising=False)
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    assert spectra_paper_only_enabled() is True

    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    assert spectra_paper_only_enabled() is False
