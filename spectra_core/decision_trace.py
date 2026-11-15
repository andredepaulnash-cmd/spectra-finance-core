"""Append-only decision evidence. Reading a trace never authorizes a mutation.

Writers use the caller's SQLite transaction so a decision and its evidence
commit together. Only deterministic, allowlisted decision facts should be
recorded here; raw connector payloads and credentials do not belong in traces.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Mapping


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _fact(value: object) -> object:
    if isinstance(value, Decimal):
        # Invalid numbers themselves can be the reason a governor rejects a
        # proposal. Preserve those as text, never as non-standard JSON NaN.
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else str(value)
    # The governor deliberately rejects unsupported numeric inputs. Observing
    # that rejection must not throw and roll it back, nor serialize arbitrary
    # object contents (which could contain credentials or mutable state).
    kind = type(value)
    return f"unsupported:{kind.__module__}.{kind.__qualname__}"


@dataclass(frozen=True)
class DecisionTrace:
    trace_id: str
    subject_id: str
    stage: str
    disposition: str
    reasons: tuple[str, ...]
    observed_at: float
    facts_json: str

    @property
    def facts(self) -> dict[str, object]:
        return json.loads(self.facts_json)


def initialize_decision_trace(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS decision_traces (
            trace_id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            observed_at REAL NOT NULL,
            facts_json TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS decision_trace_subject_idx
        ON decision_traces(subject_id, observed_at, trace_id)""")


def append_decision_trace(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    stage: str,
    disposition: str,
    reasons: tuple[str, ...],
    observed_at: float,
    facts: Mapping[str, object],
    trace_id: str | None = None,
) -> DecisionTrace:
    """Append exact immutable evidence without committing the transaction.

    The content-derived default identity makes exact retries idempotent. An
    explicit identity with different evidence is a conflict, never overwrite.
    This function does not read or write reservations, lots or execution state.
    """
    if isinstance(observed_at, bool) or not isfinite(float(observed_at)):
        raise ValueError("observed_at must be finite")
    timestamp = float(observed_at)
    if timestamp <= 0:
        raise ValueError("observed_at must be positive")
    sid = _text(subject_id, "subject_id")
    step = _text(stage, "stage")
    outcome = _text(disposition, "disposition")
    reason_values = tuple(str(value) for value in reasons)
    if not reason_values:
        raise ValueError("at least one decision reason is required")
    payload = json.dumps(
        {_text(key, "fact key"): _fact(value) for key, value in facts.items()},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    reason_json = json.dumps(reason_values, separators=(",", ":"))
    values = (sid, step, outcome, reason_json, timestamp, payload)
    identity = (
        trace_id
        or "decision:"
        + hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
    )
    identity = _text(identity, "trace_id")
    existing = conn.execute(
        "SELECT subject_id, stage, disposition, reasons_json, observed_at, facts_json "
        "FROM decision_traces WHERE trace_id = ?",
        (identity,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise RuntimeError("decision trace identity conflicts with prior evidence")
    else:
        conn.execute(
            "INSERT INTO decision_traces VALUES (?, ?, ?, ?, ?, ?, ?)",
            (identity, *values),
        )
    return DecisionTrace(
        identity, sid, step, outcome, reason_values, timestamp, payload
    )


def read_decision_traces(
    path: str | Path, *, subject_id: str
) -> tuple[DecisionTrace, ...]:
    """Read a consistent snapshot without creating a file or changing schema."""
    sid = _text(subject_id, "subject_id")
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_traces'"
        ).fetchone():
            return ()
        rows = conn.execute(
            "SELECT trace_id, subject_id, stage, disposition, reasons_json, "
            "observed_at, facts_json FROM decision_traces "
            "WHERE subject_id = ? ORDER BY observed_at, rowid",
            (sid,),
        ).fetchall()
    return tuple(
        DecisionTrace(
            row[0], row[1], row[2], row[3], tuple(json.loads(row[4])), row[5], row[6]
        )
        for row in rows
    )
