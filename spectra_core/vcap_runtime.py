from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path

from .asset_fingerprint import AssetFingerprint
from .decision_trace import append_decision_trace, initialize_decision_trace


class VCAPRuntimePhase(StrEnum):
    STARTED = "STARTED"
    ACCOUNTING_PRECHECKED = "ACCOUNTING_PRECHECKED"
    MARKET_REFRESHED = "MARKET_REFRESHED"
    PLANNED = "PLANNED"
    MUTATION_DISPATCHING = "MUTATION_DISPATCHING"
    MUTATION_DISPATCHED = "MUTATION_DISPATCHED"
    AWAITING_RECONCILIATION = "AWAITING_RECONCILIATION"
    COMPLETED = "COMPLETED"
    HOLD = "HOLD"
    CONFLICT = "CONFLICT"


TERMINAL_RUNTIME_PHASES = {
    VCAPRuntimePhase.COMPLETED,
    VCAPRuntimePhase.HOLD,
    VCAPRuntimePhase.CONFLICT,
}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _time(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


@dataclass(frozen=True)
class VCAPRuntimePolicy:
    market_max_age_seconds: float = 30.0
    order_book_depth: int = 10
    partial_attempt_deferral_enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.partial_attempt_deferral_enabled) is not bool:
            raise TypeError("partial_attempt_deferral_enabled must be bool")
        max_age = float(self.market_max_age_seconds)
        if not isfinite(max_age) or max_age <= 0:
            raise ValueError("market_max_age_seconds must be finite and positive")
        if (
            isinstance(self.order_book_depth, bool)
            or not isinstance(self.order_book_depth, int)
            or self.order_book_depth <= 0
        ):
            raise ValueError("order_book_depth must be a positive integer")
        object.__setattr__(self, "market_max_age_seconds", max_age)


@dataclass(frozen=True)
class VCAPRuntimeCycle:
    cycle_id: str
    position_id: str
    envelope_proposal_id: str
    sequence: int
    phase: VCAPRuntimePhase
    planned_action: str | None
    mutation_key: str | None
    mutation_proposal_id: str | None
    decision_reasons: tuple[str, ...]
    execution_result: dict[str, object] | None
    created_at: float
    heartbeat_at: float
    completed_at: float | None
    last_error: str | None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_RUNTIME_PHASES


class VCAPRuntimeStore:
    """Durable runtime-cycle and fingerprint memory for closed-loop VCAP."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            initialize_decision_trace(conn)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS vcap_runtime_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    envelope_proposal_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    planned_action TEXT,
                    mutation_key TEXT,
                    mutation_proposal_id TEXT,
                    decision_reasons_json TEXT NOT NULL,
                    execution_result_json TEXT,
                    created_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    completed_at REAL,
                    last_error TEXT,
                    UNIQUE(position_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS
                    vcap_runtime_cycles_position_idx
                ON vcap_runtime_cycles(position_id, sequence);

                CREATE TABLE IF NOT EXISTS vcap_runtime_fingerprints (
                    trading_pair TEXT PRIMARY KEY,
                    fingerprint_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """)

    @staticmethod
    def _cycle_from_row(row: sqlite3.Row) -> VCAPRuntimeCycle:
        raw_reasons = json.loads(str(row["decision_reasons_json"]))
        if not isinstance(raw_reasons, list):
            raise RuntimeError("corrupt VCAP runtime decision reasons")
        raw_result = row["execution_result_json"]
        execution_result = None
        if raw_result is not None:
            parsed = json.loads(str(raw_result))
            if not isinstance(parsed, dict):
                raise RuntimeError("corrupt VCAP runtime execution result")
            execution_result = parsed

        return VCAPRuntimeCycle(
            cycle_id=str(row["cycle_id"]),
            position_id=str(row["position_id"]),
            envelope_proposal_id=str(row["envelope_proposal_id"]),
            sequence=int(row["sequence"]),
            phase=VCAPRuntimePhase(str(row["phase"])),
            planned_action=(
                None if row["planned_action"] is None else str(row["planned_action"])
            ),
            mutation_key=(
                None if row["mutation_key"] is None else str(row["mutation_key"])
            ),
            mutation_proposal_id=(
                None
                if row["mutation_proposal_id"] is None
                else str(row["mutation_proposal_id"])
            ),
            decision_reasons=tuple(str(value) for value in raw_reasons),
            execution_result=execution_result,
            created_at=float(row["created_at"]),
            heartbeat_at=float(row["heartbeat_at"]),
            completed_at=(
                None if row["completed_at"] is None else float(row["completed_at"])
            ),
            last_error=(None if row["last_error"] is None else str(row["last_error"])),
        )

    def load(self, cycle_id: str) -> VCAPRuntimeCycle | None:
        cid = _text(cycle_id, "cycle_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vcap_runtime_cycles WHERE cycle_id = ?",
                (cid,),
            ).fetchone()
        return None if row is None else self._cycle_from_row(row)

    def latest_for_position(self, position_id: str) -> VCAPRuntimeCycle | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM vcap_runtime_cycles
                WHERE position_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (pid,),
            ).fetchone()
        return None if row is None else self._cycle_from_row(row)

    def begin_or_resume(
        self,
        *,
        position_id: str,
        envelope_proposal_id: str,
        now: float,
    ) -> VCAPRuntimeCycle:
        pid = _text(position_id, "position_id")
        envelope = _text(envelope_proposal_id, "envelope_proposal_id")
        timestamp = _time(now, "now")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT *
                    FROM vcap_runtime_cycles
                    WHERE position_id = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (pid,),
                ).fetchone()

                if row is not None:
                    current = self._cycle_from_row(row)
                    if current.envelope_proposal_id != envelope:
                        raise RuntimeError(
                            "runtime position changed VCAP envelope ownership"
                        )
                    if not current.terminal:
                        conn.execute(
                            """
                            UPDATE vcap_runtime_cycles
                            SET heartbeat_at = ?
                            WHERE cycle_id = ?
                            """,
                            (timestamp, current.cycle_id),
                        )
                        conn.commit()
                        refreshed = self.load(current.cycle_id)
                        if refreshed is None:
                            raise RuntimeError("runtime cycle disappeared")
                        return refreshed
                    next_sequence = current.sequence + 1
                else:
                    next_sequence = 1

                cycle_id = f"{pid}:cycle:{next_sequence:08d}"
                conn.execute(
                    """
                    INSERT INTO vcap_runtime_cycles (
                        cycle_id,
                        position_id,
                        envelope_proposal_id,
                        sequence,
                        phase,
                        planned_action,
                        mutation_key,
                        mutation_proposal_id,
                        decision_reasons_json,
                        execution_result_json,
                        created_at,
                        heartbeat_at,
                        completed_at,
                        last_error
                    )
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, '[]', NULL, ?, ?, NULL, NULL)
                    """,
                    (
                        cycle_id,
                        pid,
                        envelope,
                        next_sequence,
                        VCAPRuntimePhase.STARTED.value,
                        timestamp,
                        timestamp,
                    ),
                )
                append_decision_trace(
                    conn,
                    subject_id=cycle_id,
                    stage="VCAP_RUNTIME",
                    disposition=VCAPRuntimePhase.STARTED.value,
                    reasons=("runtime_cycle_started",),
                    observed_at=timestamp,
                    facts={"position_id": pid, "envelope_proposal_id": envelope},
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        created = self.load(cycle_id)
        if created is None:
            raise RuntimeError("runtime cycle creation failed")
        return created

    def transition(
        self,
        cycle_id: str,
        *,
        phase: VCAPRuntimePhase,
        now: float,
        expected_phases: tuple[VCAPRuntimePhase, ...] | None = None,
        planned_action: str | None = None,
        mutation_key: str | None = None,
        mutation_proposal_id: str | None = None,
        decision_reasons: tuple[str, ...] | None = None,
        execution_result: dict[str, object] | None = None,
        last_error: str | None = None,
    ) -> VCAPRuntimeCycle:
        cid = _text(cycle_id, "cycle_id")
        if not isinstance(phase, VCAPRuntimePhase):
            raise TypeError("phase must be VCAPRuntimePhase")
        timestamp = _time(now, "now")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM vcap_runtime_cycles WHERE cycle_id = ?",
                    (cid,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown runtime cycle: {cid}")
                current = self._cycle_from_row(row)
                if expected_phases is not None and current.phase not in set(
                    expected_phases
                ):
                    raise RuntimeError(
                        f"runtime cycle phase {current.phase.value} cannot "
                        f"transition to {phase.value}"
                    )

                action_value = (
                    current.planned_action
                    if planned_action is None
                    else _text(planned_action, "planned_action")
                )
                key_value = (
                    current.mutation_key
                    if mutation_key is None
                    else _text(mutation_key, "mutation_key")
                )
                proposal_value = (
                    current.mutation_proposal_id
                    if mutation_proposal_id is None
                    else _text(
                        mutation_proposal_id,
                        "mutation_proposal_id",
                    )
                )
                reasons_value = (
                    current.decision_reasons
                    if decision_reasons is None
                    else tuple(str(value) for value in decision_reasons)
                )
                result_value = (
                    current.execution_result
                    if execution_result is None
                    else execution_result
                )
                completed_at = timestamp if phase in TERMINAL_RUNTIME_PHASES else None

                conn.execute(
                    """
                    UPDATE vcap_runtime_cycles
                    SET
                        phase = ?,
                        planned_action = ?,
                        mutation_key = ?,
                        mutation_proposal_id = ?,
                        decision_reasons_json = ?,
                        execution_result_json = ?,
                        heartbeat_at = ?,
                        completed_at = ?,
                        last_error = ?
                    WHERE cycle_id = ?
                    """,
                    (
                        phase.value,
                        action_value,
                        key_value,
                        proposal_value,
                        json.dumps(list(reasons_value)),
                        (
                            None
                            if result_value is None
                            else json.dumps(result_value, sort_keys=True, default=str)
                        ),
                        timestamp,
                        completed_at,
                        last_error,
                        cid,
                    ),
                )
                append_decision_trace(
                    conn,
                    subject_id=cid,
                    stage="VCAP_RUNTIME",
                    disposition=phase.value,
                    reasons=reasons_value or ("runtime_phase_transition",),
                    observed_at=timestamp,
                    facts={
                        "position_id": current.position_id,
                        "envelope_proposal_id": current.envelope_proposal_id,
                        "previous_phase": current.phase.value,
                        "planned_action": action_value,
                        "mutation_key": key_value,
                        "mutation_proposal_id": proposal_value,
                        "terminal": phase in TERMINAL_RUNTIME_PHASES,
                        "has_error": last_error is not None,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        updated = self.load(cid)
        if updated is None:
            raise RuntimeError("runtime cycle disappeared after transition")
        return updated

    def save_fingerprint(
        self,
        fingerprint: AssetFingerprint,
        *,
        now: float,
    ) -> None:
        timestamp = _time(now, "now")
        payload = json.dumps(
            fingerprint.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vcap_runtime_fingerprints (
                    trading_pair,
                    fingerprint_json,
                    updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(trading_pair) DO UPDATE SET
                    fingerprint_json = excluded.fingerprint_json,
                    updated_at = excluded.updated_at
                """,
                (fingerprint.trading_pair, payload, timestamp),
            )

    def load_fingerprint(self, trading_pair: str) -> AssetFingerprint | None:
        pair = _text(trading_pair, "trading_pair").upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT fingerprint_json
                FROM vcap_runtime_fingerprints
                WHERE trading_pair = ?
                """,
                (pair,),
            ).fetchone()
        if row is None:
            return None
        raw = json.loads(str(row["fingerprint_json"]))
        if not isinstance(raw, dict):
            raise RuntimeError("corrupt runtime fingerprint")
        fingerprint = AssetFingerprint.from_dict(raw)
        if fingerprint.trading_pair != pair:
            raise RuntimeError("runtime fingerprint pair mismatch")
        return fingerprint
