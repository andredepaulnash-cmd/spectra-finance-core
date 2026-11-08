#!/usr/bin/env python3
"""Create isolated, conservative Spectra paper-mode state for the alpha demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PAPER_POLICY = {
    "reserve_floor_quote": "100",
    "max_total_committed_quote": "1000",
    "max_pair_quote": "1000",
    "max_position_quote": "1000",
    "max_recovery_quote": "0",
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


def configure(core_root: Path) -> Path:
    """Initialize paper-only policy/breaker without touching live state."""
    paper_dir = core_root / "data" / "spectra-paper"
    live_dir = core_root / "data" / "spectra"
    if paper_dir.resolve() == live_dir.resolve():
        raise ValueError("paper state must never share the live state directory")
    _write_if_missing(paper_dir / "capital_policy.json", PAPER_POLICY)
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
