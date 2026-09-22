from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .capital_governor import CapitalGovernor
from .models import CapitalPolicy


def spectra_state_dir() -> Path:
    override = (os.getenv("SPECTRA_STATE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()

    return Path(__file__).resolve().parent.parent / "data" / "spectra"


@lru_cache(maxsize=1)
def get_capital_governor() -> CapitalGovernor:
    state_dir = spectra_state_dir()
    policy_path = state_dir / "capital_policy.json"
    db_path = state_dir / "capital.sqlite3"

    if not policy_path.is_file():
        raise RuntimeError(
            f"Spectra capital policy is not configured: {policy_path}"
        )

    raw = json.loads(policy_path.read_text())

    required = (
        "reserve_floor_quote",
        "max_total_committed_quote",
        "max_pair_quote",
        "max_position_quote",
        "max_recovery_quote",
        "max_leverage",
        "reservation_ttl_seconds",
    )

    missing = [name for name in required if name not in raw]
    if missing:
        raise RuntimeError(
            "Spectra capital policy is missing: " + ", ".join(missing)
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
        reservation_ttl_seconds=int(raw["reservation_ttl_seconds"]),
    )

    return CapitalGovernor(db_path, policy)


def reset_runtime_cache() -> None:
    get_capital_governor.cache_clear()
