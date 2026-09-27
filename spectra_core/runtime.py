from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .accounting_health import AccountingHealthSupervisor
from .capital_governor import CapitalGovernor, validate_capital_policy
from .economic_position import EconomicPositionLedger
from .models import BreakerMode, CapitalPolicy
from .vcap_runtime import VCAPRuntimeStore
from .vcap_state import VCAPStateStore


def spectra_state_dir() -> Path:
    override = (os.getenv("SPECTRA_STATE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()

    return Path(__file__).resolve().parent.parent / "data" / "spectra"


def get_breaker_mode() -> BreakerMode:
    """
    Read Spectra's persisted execution breaker.

    This is intentionally not cached: changing breaker_state.json must affect
    the next authorization attempt.

    Missing, malformed, or unknown state fails closed as HALTED.
    """
    path = spectra_state_dir() / "breaker_state.json"

    if not path.is_file():
        return BreakerMode.HALTED

    try:
        raw = json.loads(path.read_text())
    except Exception:
        return BreakerMode.HALTED

    if not isinstance(raw, dict):
        return BreakerMode.HALTED

    try:
        return BreakerMode(
            str(raw.get("mode") or "").strip().upper()
        )
    except ValueError:
        return BreakerMode.HALTED


def load_capital_policy() -> CapitalPolicy:
    """Load and validate Spectra's capital policy without mutating state."""
    state_dir = spectra_state_dir()
    policy_path = state_dir / "capital_policy.json"

    if not policy_path.is_file():
        raise RuntimeError(
            f"Spectra capital policy is not configured: {policy_path}"
        )

    try:
        raw = json.loads(policy_path.read_text())
    except Exception as exc:
        raise RuntimeError(
            f"Spectra capital policy is invalid: {policy_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"Spectra capital policy must be a JSON object: {policy_path}"
        )

    required = (
        "reserve_floor_quote",
        "max_total_committed_quote",
        "max_pair_quote",
        "max_position_quote",
        "max_recovery_quote",
        "max_leverage",
        "reservation_ttl_seconds",
        "position_hold_snapshot_ttl_seconds",
    )

    missing = [name for name in required if name not in raw]
    if missing:
        raise RuntimeError(
            "Spectra capital policy is missing: " + ", ".join(missing)
        )

    try:
        ttl = raw["reservation_ttl_seconds"]
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            raise ValueError(
                "reservation_ttl_seconds must be an integer"
            )

        hold_ttl = raw["position_hold_snapshot_ttl_seconds"]
        if not isinstance(hold_ttl, int) or isinstance(hold_ttl, bool):
            raise ValueError(
                "position_hold_snapshot_ttl_seconds must be an integer"
            )

        policy = CapitalPolicy(
            reserve_floor_quote=Decimal(str(raw["reserve_floor_quote"])),
            max_total_committed_quote=Decimal(
                str(raw["max_total_committed_quote"])
            ),
            max_pair_quote=Decimal(str(raw["max_pair_quote"])),
            max_position_quote=Decimal(str(raw["max_position_quote"])),
            max_recovery_quote=Decimal(str(raw["max_recovery_quote"])),
            max_leverage=Decimal(str(raw["max_leverage"])),
            reservation_ttl_seconds=ttl,
            position_hold_snapshot_ttl_seconds=hold_ttl,
        )

        validate_capital_policy(policy)
        return policy

    except Exception as exc:
        raise RuntimeError(
            f"Spectra capital policy is invalid: {policy_path}: {exc}"
        ) from exc


@lru_cache(maxsize=1)
def get_economic_position_ledger() -> EconomicPositionLedger:
    return EconomicPositionLedger(
        spectra_state_dir() / "economic_positions.sqlite3"
    )


@lru_cache(maxsize=1)
def get_accounting_health_supervisor() -> AccountingHealthSupervisor:
    # Health watermarks intentionally live beside the economic ledger tables.
    return AccountingHealthSupervisor(
        spectra_state_dir() / "economic_positions.sqlite3"
    )


@lru_cache(maxsize=1)
def get_vcap_state_store() -> VCAPStateStore:
    return VCAPStateStore(spectra_state_dir() / "vcap_state.sqlite3")


@lru_cache(maxsize=1)
def get_vcap_runtime_store() -> VCAPRuntimeStore:
    return VCAPRuntimeStore(spectra_state_dir() / "vcap_runtime.sqlite3")


@lru_cache(maxsize=1)
def get_capital_governor() -> CapitalGovernor:
    state_dir = spectra_state_dir()
    db_path = state_dir / "capital.sqlite3"
    policy = load_capital_policy()

    return CapitalGovernor(db_path, policy)


def reset_runtime_cache() -> None:
    get_capital_governor.cache_clear()
    get_economic_position_ledger.cache_clear()
    get_accounting_health_supervisor.cache_clear()
    get_vcap_state_store.cache_clear()
    get_vcap_runtime_store.cache_clear()
