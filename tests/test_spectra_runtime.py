import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from spectra_core.models import BreakerMode
from spectra_core.runtime import (
    get_breaker_mode,
    get_capital_governor,
    get_economic_position_ledger,
    get_spectra_accounting_health_snapshot,
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
        "SPECTRA_PAPER_STATE_DIR",
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
    # Older policy files omit max_entry_quote; paper mode allocates only the
    # amount left after the configured recovery ceiling.
    assert policy.max_entry_quote == Decimal("400")
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


def test_fetch_spectra_runtime_reports_local_paper_supervisor_status(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_PAPER_CONNECTORS", "kraken_paper_trade")
    monkeypatch.setattr("condor.fetchers.spectra_runtime.time.time", lambda: 1000.0)
    monkeypatch.setattr(
        "spectra_core.portfolio_supervisor.get_portfolio_supervisor_status",
        lambda: {"supervisor_state": "RUNNING", "updated_at": 1000.0},
    )

    result = asyncio.run(fetch_spectra_runtime(FakeClient(FakeResponse(404))))

    assert result["mode"] == "PAPER"
    assert result["paper_only"] is True
    assert result["live_dispatch_available"] is True
    assert result["live_dispatch_authorized"] is False
    assert result["paper_connectors"] == ["kraken_paper_trade"]
    assert result["healthy"] is True
    assert result["portfolio_supervisor"]["supervisor_state"] == "RUNNING"


def test_missing_supervisor_heartbeat_keeps_paper_mode_but_reports_unhealthy(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setattr(
        "spectra_core.portfolio_supervisor.get_portfolio_supervisor_status",
        lambda: {
            "supervisor_state": "NOT_STARTED",
            "updated_at": None,
            "error": "portfolio supervisor has not published a status yet",
        },
    )

    result = asyncio.run(fetch_spectra_runtime(FakeClient(FakeResponse(404))))

    assert result["mode"] == "PAPER"
    assert result["paper_only"] is True
    assert result["live_dispatch_authorized"] is False
    assert result["healthy"] is False
    assert result["portfolio_supervisor"]["supervisor_state"] == "NOT_STARTED"


def test_runtime_status_does_not_require_initialized_hummingbot_api_session(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setattr(
        "spectra_core.portfolio_supervisor.get_portfolio_supervisor_status",
        lambda: {"supervisor_state": "NOT_STARTED", "updated_at": None},
    )

    class Uninitialized:
        base_url = "http://127.0.0.1:8000"
        _session = None

    result = asyncio.run(fetch_spectra_runtime(Uninitialized()))

    assert result["mode"] == "PAPER"
    assert result["healthy"] is False
    assert result["live_dispatch_authorized"] is False



def test_spectra_agent_defaults_to_paper_only_unless_explicitly_overridden(
    monkeypatch,
):
    monkeypatch.delenv("SPECTRA_PAPER_ONLY", raising=False)
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    assert spectra_paper_only_enabled() is True

    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "true")
    assert spectra_paper_only_enabled() is False



def test_spectra_seat_defaults_to_paper_even_without_parent_agent_env(monkeypatch):
    monkeypatch.delenv("SPECTRA_PAPER_ONLY", raising=False)
    monkeypatch.delenv("CONDOR_AGENT_SLUG", raising=False)
    assert spectra_paper_only_enabled("spectra") is True
    assert spectra_paper_only_enabled("adaptive_grid_trader") is False
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    assert spectra_paper_only_enabled("") is True


def test_spectra_seat_cannot_override_paper_default_without_live_authorization(monkeypatch):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.delenv("SPECTRA_ALLOW_LIVE_EXECUTION", raising=False)
    with pytest.raises(RuntimeError, match="separate explicit authorization"):
        spectra_paper_only_enabled("spectra")


def test_condor_spectra_runtime_status_route_is_registered():
    from condor.fetchers import fetch_spectra_runtime
    from condor.server_data_service import ServerDataType
    from condor.web.routes.servers import router

    assert callable(fetch_spectra_runtime)
    assert ServerDataType.SPECTRA_RUNTIME.value == "spectra_runtime"
    assert any(
        route.path == "/servers/{name}/spectra/runtime"
        for route in router.routes
    )


def test_accounting_health_snapshot_reports_no_active_positions(
    monkeypatch,
    tmp_path,
):
    live_dir = tmp_path / "live-state"
    paper_dir = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(live_dir))
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(paper_dir))
    reset_runtime_cache()

    snapshot = get_spectra_accounting_health_snapshot(now=1_000)

    assert snapshot["state"] == "NO_ACTIVE_POSITIONS"
    assert snapshot["active_positions"] == 0
    assert snapshot["allows_new_exposure"] is True
    assert snapshot["positions"] == []

    reset_runtime_cache()


def test_accounting_health_snapshot_fails_closed_for_unassessed_position(
    monkeypatch,
    tmp_path,
):
    live_dir = tmp_path / "live-state"
    paper_dir = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(live_dir))
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(paper_dir))
    reset_runtime_cache()

    ledger = get_economic_position_ledger()
    ledger.open_position(
        position_id="unassessed-position",
        pair="BTC-USD",
        created_at=900,
    )

    snapshot = get_spectra_accounting_health_snapshot(now=1_000)

    assert snapshot["state"] == "HOLD"
    assert snapshot["active_positions"] == 1
    assert snapshot["allows_new_exposure"] is False
    assert snapshot["positions"][0]["state"] == "HOLD"

    reset_runtime_cache()
