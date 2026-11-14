#!/usr/bin/env python3
"""Initialize isolated Spectra paper state and a separately halted live state.

The paper defaults recover the historical 25% reserve / 5% per-asset envelope
as configurable starting policy, not immutable strategy constants. The live
state is created fail-closed and requires separate authorization and policy
configuration before it can allocate capital.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

PAPER_POLICY = {
    "reserve_floor_quote": "6250",
    "max_total_committed_quote": "18750",
    "max_pair_quote": "1250",
    "max_position_quote": "1250",
    "max_recovery_quote": "1250",
    "max_leverage": "1",
    "reservation_ttl_seconds": 120,
    "position_hold_snapshot_ttl_seconds": 30,
}
PAPER_BREAKER = {"mode": "NORMAL"}

# A live installation has a separate state directory, but no capital authority
# until the operator configures limits and deliberately releases the HALTED breaker.
LIVE_POLICY = {
    "reserve_floor_quote": "0",
    "max_total_committed_quote": "0",
    "max_pair_quote": "0",
    "max_position_quote": "0",
    "max_recovery_quote": "0",
    "max_leverage": "1",
    "reservation_ttl_seconds": 120,
    "position_hold_snapshot_ttl_seconds": 30,
}
LIVE_BREAKER = {"mode": "HALTED"}

LEGACY_PAPER_POLICY = {
    "reserve_floor_quote": "100",
    "max_total_committed_quote": "1000",
    "max_pair_quote": "1000",
    "max_position_quote": "1000",
    "max_recovery_quote": "0",
    "max_leverage": "1",
    "reservation_ttl_seconds": 120,
    "position_hold_snapshot_ttl_seconds": 30,
}


def has_paper_position_history(core_root: Path) -> bool:
    """Fail closed when a paper ledger contains any historical position."""
    database = core_root / "data" / "spectra-paper" / "economic_positions.sqlite3"
    if not database.exists():
        return False
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='economic_positions'"
            ).fetchone()
            if table is None:
                return True
            return conn.execute(
                "SELECT 1 FROM economic_positions LIMIT 1"
            ).fetchone() is not None
    except sqlite3.Error:
        # Unknown ledger state must never authorize a paper-account reset.
        return True


def _write_if_missing(path: Path, value: dict) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)


def configure(core_root: Path) -> Path:
    """Initialize missing mode-specific policy without overwriting operator state."""
    paper_dir = core_root / "data" / "spectra-paper"
    live_dir = core_root / "data" / "spectra"
    if paper_dir.resolve() == live_dir.resolve():
        raise ValueError("paper state must never share the live state directory")
    policy_path = paper_dir / "capital_policy.json"
    _write_if_missing(policy_path, PAPER_POLICY)
    _write_if_missing(paper_dir / "breaker_state.json", PAPER_BREAKER)
    _write_if_missing(live_dir / "capital_policy.json", LIVE_POLICY)
    _write_if_missing(live_dir / "breaker_state.json", LIVE_BREAKER)

    # Upgrade only the exact old demo template, and only before any position
    # history exists. Customized policy and every traded account are preserved.
    if policy_path.exists() and not has_paper_position_history(core_root):
        try:
            current_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_policy = None
        if current_policy == LEGACY_PAPER_POLICY:
            policy_path.write_text(
                json.dumps(PAPER_POLICY, indent=2) + "\n",
                encoding="utf-8",
            )
            policy_path.chmod(0o600)
    return paper_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, required=True)
    args = parser.parse_args()
    target = configure(args.core_root.expanduser().resolve())
    print(
        f"Initialized isolated Spectra paper state at {target}; "
        "live state remains separately isolated and HALTED."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
