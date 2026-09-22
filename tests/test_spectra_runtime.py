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
