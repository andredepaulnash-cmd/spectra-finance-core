"""Explicit local PAPER entrypoint. Not registered with the live scheduler/MCP.

Default-off, no network, no exchange adapter and no account credentials. Inputs
must be synthetic JSONL or the exact frozen Jan-June research file. Actual venue
contracts and any other historical/current market source are unsupported.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

from .crash_short_model import (
    ASSETS,
    FROZEN_START,
    INPUT_SHA256,
    STEP,
    PaperSource,
    canonical,
)
from .crash_short_paper import PaperShortStore
from .crash_short_signal import Bar


def run_crash_short_paper_cycle(
    *,
    enabled: bool = False,
    store: PaperShortStore | None = None,
    timestamp: int | None = None,
    bars: Mapping[str, Bar] | None = None,
    control: str = "RUN",
    execution_mode: str = "PAPER",
) -> dict[str, Any]:
    """One opt-in, retrospective five-minute cycle, with no live capability.

    Disabled calls do not create a database or mutate an existing book. An open
    position still belongs to that book. To mark/close it, explicitly enable the
    entrypoint with PAUSE/CLOSE_ONLY and supply further observed intervals.
    """
    if execution_mode != "PAPER":
        raise ValueError("Only PAPER mode exists; live dispatch is unavailable")
    if type(enabled) is not bool:
        raise ValueError("enabled must be an explicit boolean")
    if not enabled:
        return {
            "status": "DISABLED",
            "execution_mode": "PAPER",
            "existing_book": store.status() if store is not None else None,
        }
    if not isinstance(store, PaperShortStore) or timestamp is None or bars is None:
        raise ValueError(
            "Enabled paper cycles require store, timestamp and BTC/ETH observations"
        )
    return store.process_cycle(timestamp, bars, control=control)


def read_paper_short_status(path: str | Path) -> dict[str, Any]:
    """Inspect an existing database in SQLite read-only mode; never initialize."""
    path = Path(path)
    if not path.is_file():
        return {"status": "DISABLED", "reason": "NO_PAPER_BOOK"}
    return PaperShortStore.inspect(path)


def _synthetic_rows(data: bytes):
    """Explicit synthetic user fixture, not a historical-data ingestion service."""
    with io.StringIO(data.decode("utf-8")) as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {"timestamp", "bars"}:
                raise ValueError(
                    f"Line {line_number}: expected timestamp and bars only"
                )
            timestamp = row["timestamp"]
            if not isinstance(row["bars"], dict) or set(row["bars"]) != set(ASSETS):
                raise ValueError(f"Line {line_number}: BTC and ETH bars required")
            bars = {}
            for asset in ASSETS:
                value = row["bars"][asset]
                if not isinstance(value, dict) or set(value) != {
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "trades",
                }:
                    raise ValueError(
                        f"Line {line_number}: six OHLCVT fields required (use null for missing)"
                    )
                bars[asset] = Bar(timestamp=timestamp, **value)
            yield timestamp, bars


def _frozen_rows(data: bytes):
    # Imported only for the approved offline reproduction; no new runtime
    # dependency is needed for synthetic beta use. Hash before loading bytes.
    import numpy as np

    data = np.load(io.BytesIO(data), allow_pickle=False)
    if data.shape != (52128, 15, 6):
        raise ValueError("Unexpected frozen Jan-June array shape")
    for index, values in enumerate(data):
        timestamp = FROZEN_START + index * STEP
        yield timestamp, {
            asset: Bar(timestamp, *values[j]) for j, asset in enumerate(ASSETS)
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-paper",
        action="store_true",
        help="Explicitly opt in; absent means no input/state is opened",
    )
    parser.add_argument("--source", choices=("synthetic", "frozen-jan-june"))
    parser.add_argument("--input", type=Path)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("data/crash_short_paper/crash_short_paper.sqlite3"),
    )
    parser.add_argument("--run-id", default="birthday-paper-beta")
    parser.add_argument(
        "--control", choices=("run", "pause", "close-only"), default="run"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Read existing paper status without mutation",
    )
    parser.add_argument(
        "--until",
        type=int,
        help="Process only interval openings below this Unix timestamp; same immutable input can resume later",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Verify committed prefix with original controls; apply requested control only to new intervals",
    )
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = read_paper_short_status(args.state)
        elif not args.enable_paper:
            result = run_crash_short_paper_cycle()
        else:
            if args.source is None or args.input is None:
                parser.error("--enable-paper requires --source and --input")
            input_bytes = args.input.read_bytes()
            digest = hashlib.sha256(input_bytes).hexdigest()
            if args.source == "frozen-jan-june" and digest != INPUT_SHA256:
                raise ValueError("Input does not match the frozen Jan-June SHA256")
            source = PaperSource(
                "SYNTHETIC" if args.source == "synthetic" else "FROZEN_JAN_JUNE_2026",
                digest,
            )
            rows = (
                _synthetic_rows(input_bytes)
                if args.source == "synthetic"
                else _frozen_rows(input_bytes)
            )
            with PaperShortStore(args.state, source, run_id=args.run_id) as store:
                count = 0
                for timestamp, bars in rows:
                    if args.until is not None and timestamp >= args.until:
                        break
                    control = args.control.upper().replace("-", "_")
                    if args.resume:
                        prior = store.connection.execute(
                            "SELECT receipt FROM paper_cycles WHERE timestamp=?",
                            (timestamp,),
                        ).fetchone()
                        if prior is not None:
                            control = json.loads(prior[0])["control"]
                    result = run_crash_short_paper_cycle(
                        enabled=True,
                        store=store,
                        timestamp=timestamp,
                        bars=bars,
                        control=control,
                    )
                    count += 1
                if not count:
                    raise ValueError("Input contains no intervals")
                result = {**result, "processed_or_replayed_intervals": count}
        print(canonical(result))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(
            canonical({"status": "RECONCILIATION_REQUIRED", "error": str(exc)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
