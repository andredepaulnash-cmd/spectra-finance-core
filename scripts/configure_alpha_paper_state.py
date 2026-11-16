#!/usr/bin/env python3
"""Create isolated, conservative Spectra paper-mode state for the alpha demo."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from spectra_core.models import (
    DEFAULT_PAPER_ENTRY_BUDGET_QUOTE,
    DEFAULT_PAPER_RECOVERY_BUDGET_QUOTE,
    DEFAULT_PAPER_TOTAL_CAPITAL_QUOTE,
)

PAPER_POLICY = {
    "reserve_floor_quote": "100",
    "max_total_committed_quote": str(DEFAULT_PAPER_TOTAL_CAPITAL_QUOTE),
    "max_pair_quote": str(DEFAULT_PAPER_TOTAL_CAPITAL_QUOTE),
    "max_position_quote": str(DEFAULT_PAPER_TOTAL_CAPITAL_QUOTE),
    "max_entry_quote": str(DEFAULT_PAPER_ENTRY_BUDGET_QUOTE),
    "max_recovery_quote": str(DEFAULT_PAPER_RECOVERY_BUDGET_QUOTE),
    "max_leverage": "1",
    "reservation_ttl_seconds": 120,
    "position_hold_snapshot_ttl_seconds": 30,
}
PAPER_BREAKER = {"mode": "NORMAL"}


def _write_if_missing(path: Path, value: dict) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)


def _add_missing_policy_defaults(path: Path) -> None:
    """Add newly introduced safeguards without overwriting operator settings."""
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError(f"paper capital policy must be a JSON object: {path}")
    changed = False
    for key, value in PAPER_POLICY.items():
        if key not in policy:
            policy[key] = value
            changed = True
    if changed:
        mode = path.stat().st_mode & 0o777
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(policy, indent=2) + "\\n",
            encoding="utf-8",
        )
        temporary.chmod(mode or 0o600)
        os.replace(temporary, path)


def configure(core_root: Path) -> Path:
    """Initialize isolated paper policy while preserving existing state."""
    paper_dir = core_root / "data" / "spectra-paper"
    live_dir = core_root / "data" / "spectra"
    if paper_dir.resolve() == live_dir.resolve():
        raise ValueError("paper state must never share the live state directory")
    policy_path = paper_dir / "capital_policy.json"
    _write_if_missing(policy_path, PAPER_POLICY)
    _add_missing_policy_defaults(policy_path)
    _write_if_missing(paper_dir / "breaker_state.json", PAPER_BREAKER)
    return paper_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, required=True)
    args = parser.parse_args()
    target = configure(args.core_root.expanduser().resolve())
    print(f"Initialized isolated Spectra paper state at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
