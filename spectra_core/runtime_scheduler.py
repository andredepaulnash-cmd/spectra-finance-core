from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path

from .economic_position import EconomicPositionLedger
from .vcap_runtime import VCAPRuntimePhase, VCAPRuntimeStore
from .vcap_state import VCAPStateStore


_RECONCILIATION_PHASES = {
    VCAPRuntimePhase.MUTATION_DISPATCHING,
    VCAPRuntimePhase.MUTATION_DISPATCHED,
    VCAPRuntimePhase.AWAITING_RECONCILIATION,
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


class FleetPositionState(StrEnum):
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    HOLD = "HOLD"
    CONFLICT = "CONFLICT"
    BLOCKED = "BLOCKED"
    UNBOUND = "UNBOUND"
    ERROR = "ERROR"


@dataclass(frozen=True)
class RuntimeSchedulerPolicy:
    max_positions_per_tick: int = 5
    active_interval_seconds: float = 15.0
    reconciliation_interval_seconds: float = 5.0
    hold_interval_seconds: float = 30.0
    conflict_interval_seconds: float = 120.0
    halted_interval_seconds: float = 15.0
    stale_heartbeat_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_positions_per_tick, bool)
            or not isinstance(self.max_positions_per_tick, int)
            or self.max_positions_per_tick <= 0
        ):
            raise ValueError("max_positions_per_tick must be a positive integer")
        for name in (
            "active_interval_seconds",
            "reconciliation_interval_seconds",
            "hold_interval_seconds",
            "conflict_interval_seconds",
            "halted_interval_seconds",
            "stale_heartbeat_seconds",
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class RuntimePositionBinding:
    position_id: str
    envelope_proposal_id: str
    pair: str
    bound_at: float
    updated_at: float


@dataclass(frozen=True)
class RuntimeServiceRecord:
    position_id: str
    state: FleetPositionState
    last_serviced_at: float | None
    next_due_at: float
    last_cycle_id: str | None
    last_runtime_phase: str | None
    last_error: str | None
    consecutive_failures: int
    service_count: int


@dataclass(frozen=True)
class RuntimeSchedulerTick:
    tick_id: str
    sequence: int
    breaker_mode: str
    active_position_ids: tuple[str, ...]
    selected_position_ids: tuple[str, ...]
    started_at: float
    completed_at: float | None


class RuntimeSchedulerStore:
    """Durable fleet scheduler state.

    Position-to-envelope bindings are explicit durable ownership links. Pair
    equality is never sufficient to infer a binding because multiple economic
    positions may legitimately trade the same pair.
    """

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_position_bindings (
                    position_id TEXT PRIMARY KEY,
                    envelope_proposal_id TEXT NOT NULL UNIQUE,
                    pair TEXT NOT NULL,
                    bound_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_scheduler_service (
                    position_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    last_serviced_at REAL,
                    next_due_at REAL NOT NULL,
                    last_cycle_id TEXT,
                    last_runtime_phase TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL,
                    service_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_scheduler_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    next_tick_sequence INTEGER NOT NULL,
                    cursor_position_id TEXT
                );

                INSERT OR IGNORE INTO runtime_scheduler_meta (
                    singleton, next_tick_sequence, cursor_position_id
                ) VALUES (1, 1, NULL);

                CREATE TABLE IF NOT EXISTS runtime_scheduler_ticks (
                    tick_id TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL UNIQUE,
                    breaker_mode TEXT NOT NULL,
                    active_position_ids_json TEXT NOT NULL,
                    selected_position_ids_json TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL
                );
                """
            )

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> RuntimePositionBinding:
        return RuntimePositionBinding(
            position_id=str(row["position_id"]),
            envelope_proposal_id=str(row["envelope_proposal_id"]),
            pair=str(row["pair"]),
            bound_at=float(row["bound_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _service_from_row(row: sqlite3.Row) -> RuntimeServiceRecord:
        return RuntimeServiceRecord(
            position_id=str(row["position_id"]),
            state=FleetPositionState(str(row["state"])),
            last_serviced_at=(
                None
                if row["last_serviced_at"] is None
                else float(row["last_serviced_at"])
            ),
            next_due_at=float(row["next_due_at"]),
            last_cycle_id=(
                None if row["last_cycle_id"] is None else str(row["last_cycle_id"])
            ),
            last_runtime_phase=(
                None
                if row["last_runtime_phase"] is None
                else str(row["last_runtime_phase"])
            ),
            last_error=(
                None if row["last_error"] is None else str(row["last_error"])
            ),
            consecutive_failures=int(row["consecutive_failures"]),
            service_count=int(row["service_count"]),
        )

    def binding_for_position(self, position_id: str) -> RuntimePositionBinding | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_position_bindings WHERE position_id = ?",
                (pid,),
            ).fetchone()
        return None if row is None else self._binding_from_row(row)

    def bind_position(
        self,
        *,
        position_id: str,
        envelope_proposal_id: str,
        ledger: EconomicPositionLedger,
        lifecycle_store: VCAPStateStore,
        now: float,
    ) -> RuntimePositionBinding:
        pid = _text(position_id, "position_id")
        envelope = _text(envelope_proposal_id, "envelope_proposal_id")
        timestamp = _time(now, "now")
        snapshot = ledger.snapshot(pid)
        state = lifecycle_store.load(envelope)
        if state is None:
            raise ValueError("cannot bind unknown VCAP lifecycle envelope")
        if snapshot.pair != state.pair:
            raise ValueError("position and VCAP envelope pair do not match")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                by_position = conn.execute(
                    "SELECT * FROM runtime_position_bindings WHERE position_id = ?",
                    (pid,),
                ).fetchone()
                by_envelope = conn.execute(
                    """
                    SELECT * FROM runtime_position_bindings
                    WHERE envelope_proposal_id = ?
                    """,
                    (envelope,),
                ).fetchone()

                if by_position is not None:
                    current = self._binding_from_row(by_position)
                    if (
                        current.envelope_proposal_id != envelope
                        or current.pair != snapshot.pair
                    ):
                        raise RuntimeError(
                            "scheduler position binding conflicts with durable ownership"
                        )
                    conn.execute(
                        """
                        UPDATE runtime_position_bindings
                        SET updated_at = ?
                        WHERE position_id = ?
                        """,
                        (timestamp, pid),
                    )
                else:
                    if by_envelope is not None:
                        raise RuntimeError(
                            "VCAP envelope is already bound to another position"
                        )
                    conn.execute(
                        """
                        INSERT INTO runtime_position_bindings (
                            position_id, envelope_proposal_id, pair,
                            bound_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (pid, envelope, snapshot.pair, timestamp, timestamp),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        binding = self.binding_for_position(pid)
        if binding is None:
            raise RuntimeError("scheduler binding disappeared")
        return binding

    def service_record(self, position_id: str) -> RuntimeServiceRecord | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_scheduler_service WHERE position_id = ?",
                (pid,),
            ).fetchone()
        return None if row is None else self._service_from_row(row)

    def ensure_positions(
        self,
        position_ids: tuple[str, ...],
        *,
        now: float,
    ) -> None:
        timestamp = _time(now, "now")
        with self._connect() as conn:
            for position_id in position_ids:
                pid = _text(position_id, "position_id")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runtime_scheduler_service (
                        position_id, state, last_serviced_at, next_due_at,
                        last_cycle_id, last_runtime_phase, last_error,
                        consecutive_failures, service_count
                    ) VALUES (?, ?, NULL, ?, NULL, NULL, NULL, 0, 0)
                    """,
                    (pid, FleetPositionState.ACTIVE.value, timestamp),
                )

    def _cursor(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cursor_position_id
                FROM runtime_scheduler_meta
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None or row["cursor_position_id"] is None:
            return None
        return str(row["cursor_position_id"])

    @staticmethod
    def _round_robin_order(
        position_ids: tuple[str, ...],
        cursor: str | None,
    ) -> tuple[str, ...]:
        if not position_ids or cursor not in position_ids:
            return position_ids
        index = position_ids.index(cursor)
        return position_ids[index + 1 :] + position_ids[: index + 1]

    def select_positions(
        self,
        *,
        active_position_ids: tuple[str, ...],
        runtime_store: VCAPRuntimeStore,
        policy: RuntimeSchedulerPolicy,
        now: float,
    ) -> tuple[str, ...]:
        timestamp = _time(now, "now")
        self.ensure_positions(active_position_ids, now=timestamp)
        ordered = self._round_robin_order(active_position_ids, self._cursor())

        due: list[str] = []
        for pid in ordered:
            record = self.service_record(pid)
            if record is None or record.next_due_at <= timestamp:
                due.append(pid)
        if not due:
            return ()

        # One fairness slot is always reserved for the round-robin head. This
        # prevents a permanently reconciling position from starving the fleet.
        selected = [due[0]]
        remaining_capacity = policy.max_positions_per_tick - 1
        if remaining_capacity <= 0:
            return tuple(selected)

        def priority(pid: str) -> tuple[int, float, int]:
            latest = runtime_store.latest_for_position(pid)
            record = self.service_record(pid)
            last = (
                0.0
                if record is None or record.last_serviced_at is None
                else record.last_serviced_at
            )
            if latest is not None and latest.phase in _RECONCILIATION_PHASES:
                rank = 0
            elif (
                latest is not None
                and timestamp - latest.heartbeat_at >= policy.stale_heartbeat_seconds
            ):
                rank = 1
            elif latest is not None and latest.phase == VCAPRuntimePhase.CONFLICT:
                rank = 3
            elif latest is not None and latest.phase == VCAPRuntimePhase.HOLD:
                rank = 2
            else:
                rank = 2
            return (rank, last, ordered.index(pid))

        rest = sorted((pid for pid in due if pid != due[0]), key=priority)
        selected.extend(rest[:remaining_capacity])
        return tuple(selected)

    def begin_tick(
        self,
        *,
        breaker_mode: str,
        active_position_ids: tuple[str, ...],
        selected_position_ids: tuple[str, ...],
        now: float,
    ) -> RuntimeSchedulerTick:
        mode = _text(breaker_mode, "breaker_mode").upper()
        timestamp = _time(now, "now")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT next_tick_sequence
                    FROM runtime_scheduler_meta
                    WHERE singleton = 1
                    """
                ).fetchone()
                if row is None:
                    raise RuntimeError("scheduler metadata missing")
                sequence = int(row["next_tick_sequence"])
                tick_id = f"fleet:tick:{sequence:08d}"
                conn.execute(
                    """
                    INSERT INTO runtime_scheduler_ticks (
                        tick_id, sequence, breaker_mode,
                        active_position_ids_json, selected_position_ids_json,
                        started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        tick_id,
                        sequence,
                        mode,
                        json.dumps(list(active_position_ids)),
                        json.dumps(list(selected_position_ids)),
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    UPDATE runtime_scheduler_meta
                    SET next_tick_sequence = ?
                    WHERE singleton = 1
                    """,
                    (sequence + 1,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return RuntimeSchedulerTick(
            tick_id=tick_id,
            sequence=sequence,
            breaker_mode=mode,
            active_position_ids=active_position_ids,
            selected_position_ids=selected_position_ids,
            started_at=timestamp,
            completed_at=None,
        )

    def record_outcome(
        self,
        *,
        position_id: str,
        state: FleetPositionState,
        now: float,
        policy: RuntimeSchedulerPolicy,
        cycle_id: str | None = None,
        runtime_phase: str | None = None,
        error: str | None = None,
    ) -> RuntimeServiceRecord:
        pid = _text(position_id, "position_id")
        timestamp = _time(now, "now")
        if not isinstance(state, FleetPositionState):
            raise TypeError("state must be FleetPositionState")

        if state == FleetPositionState.WAITING:
            delay = policy.reconciliation_interval_seconds
        elif state == FleetPositionState.HOLD:
            delay = policy.hold_interval_seconds
        elif state in {FleetPositionState.CONFLICT, FleetPositionState.ERROR}:
            delay = policy.conflict_interval_seconds
        elif state == FleetPositionState.BLOCKED:
            delay = policy.halted_interval_seconds
        else:
            delay = policy.active_interval_seconds

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT consecutive_failures, service_count
                FROM runtime_scheduler_service
                WHERE position_id = ?
                """,
                (pid,),
            ).fetchone()
            failures = 0 if row is None else int(row["consecutive_failures"])
            count = 0 if row is None else int(row["service_count"])
            failures = failures + 1 if state == FleetPositionState.ERROR else 0
            conn.execute(
                """
                INSERT INTO runtime_scheduler_service (
                    position_id, state, last_serviced_at, next_due_at,
                    last_cycle_id, last_runtime_phase, last_error,
                    consecutive_failures, service_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    state = excluded.state,
                    last_serviced_at = excluded.last_serviced_at,
                    next_due_at = excluded.next_due_at,
                    last_cycle_id = excluded.last_cycle_id,
                    last_runtime_phase = excluded.last_runtime_phase,
                    last_error = excluded.last_error,
                    consecutive_failures = excluded.consecutive_failures,
                    service_count = excluded.service_count
                """,
                (
                    pid,
                    state.value,
                    timestamp,
                    timestamp + delay,
                    cycle_id,
                    runtime_phase,
                    error,
                    failures,
                    count + 1,
                ),
            )
        record = self.service_record(pid)
        if record is None:
            raise RuntimeError("scheduler service outcome disappeared")
        return record

    def complete_tick(
        self,
        tick_id: str,
        *,
        cursor_position_id: str | None,
        now: float,
    ) -> None:
        tid = _text(tick_id, "tick_id")
        timestamp = _time(now, "now")
        cursor = (
            None
            if cursor_position_id is None
            else _text(cursor_position_id, "cursor_position_id")
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    """
                    UPDATE runtime_scheduler_ticks
                    SET completed_at = ?
                    WHERE tick_id = ? AND completed_at IS NULL
                    """,
                    (timestamp, tid),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("scheduler tick is missing or already completed")
                conn.execute(
                    """
                    UPDATE runtime_scheduler_meta
                    SET cursor_position_id = ?
                    WHERE singleton = 1
                    """,
                    (cursor,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def fleet_status(
        self,
        *,
        active_position_ids: tuple[str, ...],
        runtime_store: VCAPRuntimeStore,
        now: float,
        policy: RuntimeSchedulerPolicy,
    ) -> tuple[dict[str, object], ...]:
        timestamp = _time(now, "now")
        rows: list[dict[str, object]] = []
        for pid in active_position_ids:
            binding = self.binding_for_position(pid)
            service = self.service_record(pid)
            cycle = runtime_store.latest_for_position(pid)
            stale = (
                cycle is not None
                and not cycle.terminal
                and timestamp - cycle.heartbeat_at >= policy.stale_heartbeat_seconds
            )
            rows.append(
                {
                    "position_id": pid,
                    "envelope_proposal_id": (
                        None if binding is None else binding.envelope_proposal_id
                    ),
                    "pair": None if binding is None else binding.pair,
                    "fleet_state": (
                        FleetPositionState.UNBOUND.value
                        if binding is None
                        else (
                            FleetPositionState.ACTIVE.value
                            if service is None
                            else service.state.value
                        )
                    ),
                    "runtime_cycle_id": None if cycle is None else cycle.cycle_id,
                    "runtime_phase": None if cycle is None else cycle.phase.value,
                    "heartbeat_at": None if cycle is None else cycle.heartbeat_at,
                    "stale_heartbeat": stale,
                    "last_serviced_at": (
                        None if service is None else service.last_serviced_at
                    ),
                    "next_due_at": None if service is None else service.next_due_at,
                    "last_error": None if service is None else service.last_error,
                }
            )
        return tuple(rows)
