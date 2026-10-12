"""Compare a completed paper CLI database against immutable frozen reference.

Read-only verification, not a strategy replay. No data downloads. The database
must have been produced via the documented entrypoint from the hash-pinned input.
All 52,128 account rows, 181 daily signals, fills and entry opportunities are
compared, including deliberately skipped episodes. Run from repository root.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = 1e-8


def verify(state: Path, reference: Path) -> dict:
    hashes = json.loads(
        (
            ROOT / "docs/research-results/crash-short-paper-beta/reference-hashes.json"
        ).read_text()
    )
    for name, digest in hashes.items():
        if hashlib.sha256((reference / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Frozen reference hash mismatch: {name}")
    connection = sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True)
    errors = {}

    def equal(actual, expected, label):
        error = abs(float(actual) - float(expected))
        if not math.isfinite(error) or error > TOLERANCE:
            raise AssertionError((label, actual, expected, error))
        errors[label] = max(errors.get(label, 0), error)

    def rows(name):
        return list(csv.DictReader((reference / name).open(newline="")))

    try:
        identity = json.loads(
            connection.execute(
                "SELECT identity FROM paper_state WHERE id=1"
            ).fetchone()[0]
        )
        assert identity["execution_mode"] == "PAPER"
        assert identity["source"] == {
            "kind": "FROZEN_JAN_JUNE_2026",
            "dataset_sha256": "5f4b40da6cd468c0d66bce148e1648c71d29f41942d16b137ca55aa32c12eefd",
        }
        assert (
            identity["protocol_sha256"]
            == hashes["FROZEN_BTC_ETH_MATCHED_PROTOCOL.json"]
        )
        count = 0
        cursor = connection.execute(
            "SELECT receipt FROM paper_cycles ORDER BY timestamp"
        )
        with gzip.open(reference / "account_5m.csv.gz", "rt", newline="") as handle:
            for expected in csv.DictReader(handle):
                row = cursor.fetchone()
                assert row is not None
                actual = json.loads(row[0])
                account = actual["account"]
                assert actual["control"] == "RUN"
                assert actual["timestamp"] == int(expected["timestamp"])
                assert actual["signal"]["label"] == expected["signal"]
                assert actual["episode"] == int(expected["episode"])
                for key, value in expected.items():
                    if key in {"timestamp", "utc", "signal", "episode"}:
                        continue
                    equal(account[key], value, "account:" + key)
                count += 1
        assert cursor.fetchone() is None and count == 52128
        signals = [
            json.loads(r[0])
            for r in connection.execute(
                "SELECT payload FROM paper_events WHERE kind='SIGNAL' ORDER BY timestamp"
            )
        ]
        expected_signals = rows("verified_daily_labels.csv")
        assert len(signals) == len(expected_signals) == 181
        for actual, expected in zip(signals, expected_signals):
            assert actual["decision_at"] == int(expected["timestamp"])
            assert actual["label"] == expected["label"]
            assert actual["last_source_bar_open"] == (
                int(expected["last_source_bar_open"])
                if expected["last_source_bar_open"]
                else None
            )
            for field in ("return_1d", "return_7d", "drawdown_7d"):
                if expected[field]:
                    equal(actual[field], expected[field], "signal:" + field)
                else:
                    assert actual[field] is None
        fills = [
            json.loads(r[0])
            for r in connection.execute(
                "SELECT payload FROM paper_events WHERE kind='FILL' ORDER BY timestamp,event_id"
            )
        ]
        expected_fills = rows("fills.csv")
        assert len(fills) == len(expected_fills) == 12
        keyed = {(f["asset"], f["action"], f["fill_timestamp"]): f for f in fills}
        assert len(keyed) == len(fills)
        for expected in expected_fills:
            actual = keyed[
                (expected["asset"], expected["action"], int(expected["fill_timestamp"]))
            ]
            for field in ("episode", "decision_timestamp", "fill_timestamp"):
                assert actual[field] == int(expected[field])
            for field in (
                "observed_open",
                "quantity",
                "reference_notional",
                "cost_overlay",
            ):
                equal(actual[field], expected[field], "fill:" + field)
            assert (
                actual["previous_observed_bar_valid"]
                and actual["current_open_observed"]
            )
        opportunities = [
            json.loads(r[0])
            for r in connection.execute(
                "SELECT payload FROM paper_events WHERE kind='ENTRY_OPPORTUNITY' ORDER BY timestamp,event_id"
            )
        ]
        expected_opportunities = rows("entry_opportunities.csv")
        assert len(opportunities) == len(expected_opportunities)
        keyed = {(r["asset"], r["timestamp"]): r for r in opportunities}
        assert len(keyed) == len(opportunities)
        for expected in expected_opportunities:
            actual = keyed[(expected["asset"], int(expected["timestamp"]))]
            assert actual["episode"] == int(expected["episode"])
            assert actual["daily_decision_at"] == int(
                expected["daily_decision_timestamp"]
            )
            assert (actual["reason"] == "FILLED") == (
                expected["entry_filled"] == "True"
            )
            assert actual["reason"] == expected["reason"]
            equal(
                actual["gross_gate_equity"],
                expected["gross_gate_equity"],
                "entry:gross_gate_equity",
            )
            equal(
                actual["return_1d"], expected["trailing24h_return"], "entry:return_1d"
            )
        assert not connection.execute(
            "SELECT 1 FROM paper_events WHERE kind='EXIT_DELAY'"
        ).fetchone()
        last = json.loads(
            connection.execute(
                "SELECT receipt FROM paper_cycles ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()[0]
        )
        return {
            "status": "PASS",
            "protocol_sha256": identity["protocol_sha256"],
            "input_sha256": identity["source"]["dataset_sha256"],
            "account_samples": count,
            "daily_signals": len(signals),
            "fills": len(fills),
            "entry_opportunities": len(opportunities),
            "maximum_absolute_differences": errors,
            "absolute_tolerance": TOLERANCE,
            "ending_equities": {
                mode: last["account"][mode + "_equity"]
                for mode in ("gross", "cost", "funding")
            },
            "current_or_holdout_data_used": False,
        }
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    result = verify(args.state, args.reference)
    text = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.receipt:
        args.receipt.write_text(text)
    print(text, end="")
