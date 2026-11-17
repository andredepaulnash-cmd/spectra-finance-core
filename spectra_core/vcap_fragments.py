"""Persistent per-asset Adaptive VCAP fragment management.

Each fragment gets its own lifecycle and runtime SQLite stores. The underlying
EconomicPositionLedger remains the single source of truth for the asset's
aggregate inventory; fragment stores own independent recovery budgets, ladders
and execution-cycle memory.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Iterable

from .adaptive_vcap import (
    AdaptiveVCAPPhase,
    AdaptiveVCAPPolicy,
    VCAPFragmentCandidate,
    VCAPFragmentStatus,
    next_adaptive_vcap_step,
    select_active_vcap_fragments,
)
from .vcap_runtime import VCAPRuntimeCycle, VCAPRuntimePhase, VCAPRuntimeStore
from .vcap_state import VCAPLifecycleState, VCAPLifecycleStatus, VCAPStateStore


ZERO = Decimal("0")
CENT = Decimal("0.01")
_PENDING_PHASES = {
    VCAPRuntimePhase.MUTATION_DISPATCHING,
    VCAPRuntimePhase.MUTATION_DISPATCHED,
    VCAPRuntimePhase.AWAITING_RECONCILIATION,
}


def _decimal(value: Decimal, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if positive and value <= ZERO:
        raise ValueError(f"{field} must be positive")
    return value


def _time(value: float, field: str) -> float:
    result = float(value)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


@dataclass(frozen=True)
class VCAPFragmentRecord:
    fragment_id: str
    position_id: str
    envelope_proposal_id: str
    pair: str
    ordinal: int
    pre_vcap_step: int
    status: VCAPFragmentStatus
    phase: AdaptiveVCAPPhase
    fat1_step: int
    skinny_step: int
    fat2_step: int
    budget_quote: Decimal
    remaining_budget_quote: Decimal
    priority_score: Decimal
    active_mutation_proposal_id: str | None
    pending_phase: AdaptiveVCAPPhase | None
    last_cycle_id: str | None
    last_cycle_phase: str | None
    last_reason: str | None
    last_completed_at: float
    created_at: float
    updated_at: float

    def step_for(self, phase: AdaptiveVCAPPhase) -> int:
        if phase is AdaptiveVCAPPhase.PRE_VCAP:
            return self.pre_vcap_step
        if phase is AdaptiveVCAPPhase.FAT_1:
            return self.fat1_step
        if phase is AdaptiveVCAPPhase.SKINNY:
            return self.skinny_step
        if phase is AdaptiveVCAPPhase.FAT_2:
            return self.fat2_step
        return 0


class VCAPFragmentManager:
    """Durable fragment queue and isolated per-fragment runtime stores."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: AdaptiveVCAPPolicy | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or AdaptiveVCAPPolicy()
        self.path = self.root / "fragments.sqlite3"
        self._stores: dict[str, tuple[VCAPStateStore, VCAPRuntimeStore]] = {}
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vcap_fragments (
                    fragment_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    envelope_proposal_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    pre_vcap_step INTEGER NOT NULL DEFAULT 0,
                    fat1_step INTEGER NOT NULL DEFAULT 0,
                    skinny_step INTEGER NOT NULL DEFAULT 0,
                    fat2_step INTEGER NOT NULL DEFAULT 0,
                    budget_quote TEXT NOT NULL,
                    remaining_budget_quote TEXT NOT NULL,
                    priority_score TEXT NOT NULL DEFAULT '0',
                    active_mutation_proposal_id TEXT,
                    pending_phase TEXT,
                    last_cycle_id TEXT,
                    last_cycle_phase TEXT,
                    last_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(position_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS vcap_fragments_position_status_idx
                ON vcap_fragments(position_id, status, priority_score);
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(vcap_fragments)")
            }
            if "last_completed_at" not in columns:
                conn.execute(
                    "ALTER TABLE vcap_fragments ADD COLUMN last_completed_at REAL NOT NULL DEFAULT 0"
                )
            if "pre_vcap_step" not in columns:
                conn.execute(
                    "ALTER TABLE vcap_fragments ADD COLUMN pre_vcap_step INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _fragment_id(position_id: str, ordinal: int) -> str:
        return f"{position_id}:vcap-fragment:{ordinal + 1}"

    def _fragment_dir(self, fragment_id: str) -> Path:
        digest = hashlib.sha256(fragment_id.encode("utf-8")).hexdigest()
        path = self.root / "runtime" / digest[:2] / digest
        path.mkdir(parents=True, exist_ok=True)
        return path

    def stores(
        self,
        fragment_id: str,
    ) -> tuple[VCAPStateStore, VCAPRuntimeStore]:
        cached = self._stores.get(fragment_id)
        if cached is not None:
            return cached
        directory = self._fragment_dir(fragment_id)
        stores = (
            VCAPStateStore(directory / "lifecycle.sqlite3"),
            VCAPRuntimeStore(directory / "runtime.sqlite3"),
        )
        self._stores[fragment_id] = stores
        return stores

    def ensure_fragments(
        self,
        *,
        position_id: str,
        envelope_proposal_id: str,
        pair: str,
        base_state: VCAPLifecycleState,
        max_position_quote: Decimal,
        now: float,
    ) -> tuple[VCAPFragmentRecord, ...]:
        if not position_id.strip() or not envelope_proposal_id.strip():
            raise ValueError("position and envelope IDs are required")
        _decimal(max_position_quote, "max_position_quote", positive=True)
        now = _time(now, "now")
        normalized_pair = pair.strip().upper()
        if normalized_pair != base_state.pair:
            raise ValueError("fragment pair does not match lifecycle envelope")
        if base_state.envelope_proposal_id != envelope_proposal_id:
            raise ValueError("base lifecycle state belongs to another envelope")

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM vcap_fragments WHERE position_id = ? ORDER BY ordinal",
                (position_id,),
            ).fetchall()
            if existing:
                records = tuple(self._record(row) for row in existing)
                if (
                    len(records) != self.policy.fragment_count
                    or any(
                        record.envelope_proposal_id != envelope_proposal_id
                        or record.pair != normalized_pair
                        for record in records
                    )
                ):
                    raise RuntimeError("persisted VCAP fragment ownership does not match position")
                return records

            position_headroom = max_position_quote - base_state.cost_basis_quote
            available = min(base_state.remaining_authorized_quote, position_headroom)
            if available <= ZERO:
                raise ValueError("position has no remaining governed recovery capacity")
            budget = (available / Decimal(self.policy.fragment_count)).quantize(
                CENT,
                rounding=ROUND_DOWN,
            )
            if budget <= ZERO:
                raise ValueError("per-fragment recovery capacity is below quote precision")

            fragment_ids: list[str] = []
            for ordinal in range(self.policy.fragment_count):
                fragment_id = self._fragment_id(position_id, ordinal)
                fragment_ids.append(fragment_id)
                conn.execute(
                    """INSERT INTO vcap_fragments (
                        fragment_id, position_id, envelope_proposal_id, pair, ordinal,
                        status, phase, fat1_step, skinny_step, fat2_step,
                        budget_quote, remaining_budget_quote, priority_score,
                        active_mutation_proposal_id, pending_phase, last_cycle_id,
                        last_cycle_phase, last_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, '0',
                              NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                    (
                        fragment_id,
                        position_id,
                        envelope_proposal_id,
                        normalized_pair,
                        ordinal,
                        VCAPFragmentStatus.WAITING.value,
                        AdaptiveVCAPPhase.PRE_VCAP.value,
                        str(budget),
                        str(budget),
                        now,
                        now,
                    ),
                )

        for fragment_id in fragment_ids:
            lifecycle_store, _ = self.stores(fragment_id)
            if lifecycle_store.load(envelope_proposal_id) is None:
                lifecycle_store.save(
                    replace(
                        base_state,
                        next_sequence=1,
                        remaining_authorized_quote=budget,
                        recovery_proposal_ids=(),
                        status=base_state.status,
                        updated_at=now,
                    )
                )
        return self.list_fragments(position_id)

    def _record(self, row: sqlite3.Row) -> VCAPFragmentRecord:
        pending_phase = row["pending_phase"]
        return VCAPFragmentRecord(
            fragment_id=str(row["fragment_id"]),
            position_id=str(row["position_id"]),
            envelope_proposal_id=str(row["envelope_proposal_id"]),
            pair=str(row["pair"]),
            ordinal=int(row["ordinal"]),
            pre_vcap_step=int(row["pre_vcap_step"]),
            status=VCAPFragmentStatus(str(row["status"])),
            phase=AdaptiveVCAPPhase(str(row["phase"])),
            fat1_step=int(row["fat1_step"]),
            skinny_step=int(row["skinny_step"]),
            fat2_step=int(row["fat2_step"]),
            budget_quote=Decimal(str(row["budget_quote"])),
            remaining_budget_quote=Decimal(str(row["remaining_budget_quote"])),
            priority_score=Decimal(str(row["priority_score"])),
            active_mutation_proposal_id=row["active_mutation_proposal_id"],
            pending_phase=(
                None if pending_phase is None else AdaptiveVCAPPhase(str(pending_phase))
            ),
            last_cycle_id=row["last_cycle_id"],
            last_cycle_phase=row["last_cycle_phase"],
            last_reason=row["last_reason"],
            last_completed_at=float(row["last_completed_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def list_fragments(self, position_id: str) -> tuple[VCAPFragmentRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vcap_fragments WHERE position_id = ? ORDER BY ordinal",
                (position_id,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def get_fragment(self, fragment_id: str) -> VCAPFragmentRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vcap_fragments WHERE fragment_id = ?",
                (fragment_id,),
            ).fetchone()
        if row is None:
            raise KeyError(fragment_id)
        return self._record(row)

    def _update(self, fragment_id: str, *, now: float, **fields: object) -> None:
        allowed = {
            "status",
            "phase",
            "pre_vcap_step",
            "fat1_step",
            "skinny_step",
            "fat2_step",
            "remaining_budget_quote",
            "priority_score",
            "active_mutation_proposal_id",
            "pending_phase",
            "last_cycle_id",
            "last_cycle_phase",
            "last_reason",
            "last_completed_at",
        }
        if not fields or set(fields) - allowed:
            raise ValueError("unsupported or empty fragment update")
        assignments: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if isinstance(value, (Decimal, AdaptiveVCAPPhase, VCAPFragmentStatus)):
                value = value.value if isinstance(value, (AdaptiveVCAPPhase, VCAPFragmentStatus)) else str(value)
            values.append(value)
        assignments.append("updated_at = ?")
        values.extend((_time(now, "now"), fragment_id))
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE vcap_fragments SET {', '.join(assignments)} WHERE fragment_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(fragment_id)

    def set_priority_scores(
        self,
        position_id: str,
        score: Decimal,
        *,
        now: float,
    ) -> None:
        if (
            not isinstance(score, Decimal)
            or not score.is_finite()
            or score < ZERO
            or score > Decimal("1")
        ):
            raise ValueError("priority score must be a Decimal between zero and one")
        with self._connect() as conn:
            conn.execute(
                """UPDATE vcap_fragments
                   SET priority_score = ?, updated_at = ?
                   WHERE position_id = ?""",
                (str(score), _time(now, "now"), position_id),
            )

    def _all_steps_exhausted(self, record: VCAPFragmentRecord) -> bool:
        pre_vcap_complete = (
            record.ordinal != 0
            or record.pre_vcap_step >= len(self.policy.pre_vcap_dca_triggers_bps)
        )
        return (
            pre_vcap_complete
            and record.fat1_step >= len(self.policy.fat1_multipliers)
            and record.skinny_step >= len(self.policy.skinny_multipliers)
            and record.fat2_step >= len(self.policy.fat2_multipliers)
        )

    def _pending_cycle(self, fragment_id: str) -> VCAPRuntimeCycle | None:
        record = self.get_fragment(fragment_id)
        _, runtime_store = self.stores(fragment_id)
        cycle = runtime_store.latest_for_position(record.position_id)
        if cycle is not None and cycle.phase in _PENDING_PHASES:
            return cycle
        return None

    def select_active(
        self,
        position_id: str,
        phase: AdaptiveVCAPPhase,
        *,
        now: float,
        drawdown_bps: int | None = None,
    ) -> tuple[VCAPFragmentRecord, ...]:
        records = self.list_fragments(position_id)
        if not records:
            return ()
        if (
            phase in {
                AdaptiveVCAPPhase.FAT_1,
                AdaptiveVCAPPhase.SKINNY,
                AdaptiveVCAPPhase.FAT_2,
            }
            and records[0].pre_vcap_step < len(self.policy.pre_vcap_dca_triggers_bps)
        ):
            # Complete the conventional DCA ladder before activating VCAP
            # fragments, including when price gaps through earlier triggers.
            phase = AdaptiveVCAPPhase.PRE_VCAP
        if drawdown_bps is not None and (
            isinstance(drawdown_bps, bool)
            or not isinstance(drawdown_bps, int)
            or drawdown_bps < 0
        ):
            raise ValueError("drawdown_bps must be a non-negative integer")
        candidates: list[VCAPFragmentCandidate] = []
        pending_by_id: dict[str, VCAPRuntimeCycle] = {}
        missing_state_ids: set[str] = set()
        held_state_ids: set[str] = set()
        for record in records:
            pending = self._pending_cycle(record.fragment_id)
            if pending is not None:
                pending_by_id[record.fragment_id] = pending
            lifecycle_store, _ = self.stores(record.fragment_id)
            fragment_state = lifecycle_store.load(record.envelope_proposal_id)
            if fragment_state is None:
                missing_state_ids.add(record.fragment_id)
            elif fragment_state.status is not VCAPLifecycleStatus.ACTIVE:
                held_state_ids.add(record.fragment_id)
            if phase is AdaptiveVCAPPhase.PRE_VCAP:
                step = (
                    self.policy.pre_vcap_dca_quote
                    if (
                        record.ordinal == 0
                        and drawdown_bps is not None
                        and record.pre_vcap_step < len(self.policy.pre_vcap_dca_triggers_bps)
                        and drawdown_bps >= self.policy.pre_vcap_dca_triggers_bps[record.pre_vcap_step]
                    )
                    else None
                )
            else:
                step = next_adaptive_vcap_step(
                    phase,
                    record.step_for(phase),
                    self.policy,
                )
            has_steps = (
                fragment_state is not None
                and fragment_state.status is VCAPLifecycleStatus.ACTIVE
                and step is not None
                and record.remaining_budget_quote > ZERO
                and step <= record.remaining_budget_quote
            )
            candidates.append(
                VCAPFragmentCandidate(
                    fragment_id=record.fragment_id,
                    ordinal=record.ordinal,
                    status=(
                        VCAPFragmentStatus.RECONCILING
                        if pending is not None
                        else (
                            VCAPFragmentStatus.CONFLICT
                            if fragment_state is None
                            else record.status
                        )
                    ),
                    priority_score=record.priority_score,
                    pending_executor=pending is not None,
                    has_phase_steps=has_steps,
                    last_completed_at=record.last_completed_at,
                )
            )

        selected = select_active_vcap_fragments(
            tuple(candidates),
            phase=phase,
            policy=self.policy,
        )
        selected_ids = {candidate.fragment_id for candidate in selected}
        pending_ids = set(pending_by_id)
        for record in records:
            if (
                record.remaining_budget_quote <= ZERO
                or self._all_steps_exhausted(record)
            ):
                status = VCAPFragmentStatus.EXHAUSTED
            elif record.fragment_id in pending_ids:
                status = VCAPFragmentStatus.RECONCILING
            elif record.fragment_id in missing_state_ids:
                status = VCAPFragmentStatus.CONFLICT
            elif record.status is VCAPFragmentStatus.CONFLICT:
                status = VCAPFragmentStatus.CONFLICT
            elif record.status is VCAPFragmentStatus.EXHAUSTED:
                status = VCAPFragmentStatus.EXHAUSTED
            elif phase is AdaptiveVCAPPhase.RECOVERED:
                status = VCAPFragmentStatus.RECOVERED
            elif record.fragment_id in held_state_ids:
                status = VCAPFragmentStatus.PAUSED
            elif phase is AdaptiveVCAPPhase.PRE_VCAP:
                status = (
                    VCAPFragmentStatus.ACTIVE
                    if record.fragment_id in selected_ids
                    else VCAPFragmentStatus.PAUSED
                )
            elif phase is AdaptiveVCAPPhase.DEEP_GOVERNOR:
                status = VCAPFragmentStatus.PAUSED
            elif record.fragment_id in selected_ids:
                status = VCAPFragmentStatus.ACTIVE
            elif record.status in {
                VCAPFragmentStatus.ACTIVE,
                VCAPFragmentStatus.RECONCILING,
            }:
                status = VCAPFragmentStatus.WAITING
            else:
                status = record.status
            self._update(
                record.fragment_id,
                now=now,
                status=status,
                phase=phase,
            )
        return tuple(self.get_fragment(candidate.fragment_id) for candidate in selected)

    def next_step_quote(
        self,
        fragment_id: str,
        phase: AdaptiveVCAPPhase,
    ) -> Decimal | None:
        record = self.get_fragment(fragment_id)
        if phase is AdaptiveVCAPPhase.PRE_VCAP:
            if (
                record.ordinal != 0
                or record.pre_vcap_step >= len(self.policy.pre_vcap_dca_triggers_bps)
            ):
                return None
            amount = self.policy.pre_vcap_dca_quote
        else:
            amount = next_adaptive_vcap_step(
                phase,
                record.step_for(phase),
                self.policy,
            )
        if amount is None or amount > record.remaining_budget_quote:
            return None
        return amount

    def refresh_position_state(
        self,
        fragment_id: str,
        *,
        snapshot_base: Decimal,
        snapshot_cost_basis_quote: Decimal,
        now: float,
    ) -> VCAPLifecycleState | None:
        record = self.get_fragment(fragment_id)
        lifecycle_store, runtime_store = self.stores(fragment_id)
        state = lifecycle_store.load(record.envelope_proposal_id)
        if state is None:
            return None
        latest = runtime_store.latest_for_position(record.position_id)
        if latest is not None and (
            latest.phase in _PENDING_PHASES
            or latest.phase is VCAPRuntimePhase.CONFLICT
        ):
            return state
        if snapshot_base <= ZERO or snapshot_cost_basis_quote <= ZERO:
            return state
        refreshed = replace(
            state,
            position_base=snapshot_base,
            cost_basis_quote=snapshot_cost_basis_quote,
            updated_at=_time(now, "now"),
        )
        lifecycle_store.save(refreshed)
        return refreshed

    def record_cycle_result(
        self,
        fragment_id: str,
        result: dict[str, object],
        *,
        now: float,
    ) -> VCAPFragmentRecord:
        record = self.get_fragment(fragment_id)
        lifecycle_store, runtime_store = self.stores(fragment_id)
        state = lifecycle_store.load(record.envelope_proposal_id)
        cycle = runtime_store.latest_for_position(record.position_id)
        if state is None:
            self._update(
                fragment_id,
                now=now,
                status=VCAPFragmentStatus.CONFLICT,
                active_mutation_proposal_id=None,
                pending_phase=None,
                last_reason="fragment_lifecycle_state_missing",
            )
            return self.get_fragment(fragment_id)
        if cycle is None:
            self._update(
                fragment_id,
                now=now,
                last_reason="fragment_cycle_missing_after_run",
            )
            return self.get_fragment(fragment_id)

        remaining = (
            record.remaining_budget_quote
            if state is None
            else state.remaining_authorized_quote
        )
        reasons = result.get("reasons", [])
        reason_text = (
            " | ".join(str(value) for value in reasons)
            if isinstance(reasons, (list, tuple))
            else str(reasons)
        )
        fields: dict[str, object] = {
            "remaining_budget_quote": remaining,
            "last_cycle_id": cycle.cycle_id,
            "last_cycle_phase": cycle.phase.value,
            "last_reason": reason_text[:1000],
        }
        if cycle.phase in _PENDING_PHASES:
            fields.update(
                status=VCAPFragmentStatus.RECONCILING,
                active_mutation_proposal_id=cycle.mutation_proposal_id,
                pending_phase=record.pending_phase or record.phase,
            )
        elif cycle.phase is VCAPRuntimePhase.CONFLICT:
            fields.update(
                status=VCAPFragmentStatus.CONFLICT,
                active_mutation_proposal_id=None,
                pending_phase=None,
            )
        elif (
            cycle.phase is VCAPRuntimePhase.COMPLETED
            and cycle.planned_action == "EXECUTE_TRANCHE"
            and cycle.mutation_proposal_id is not None
            and not (
                record.last_cycle_id == cycle.cycle_id
                and record.last_cycle_phase == VCAPRuntimePhase.COMPLETED.value
            )
        ):
            completed_phase = record.pending_phase or record.phase
            step_field = {
                AdaptiveVCAPPhase.PRE_VCAP: "pre_vcap_step",
                AdaptiveVCAPPhase.FAT_1: "fat1_step",
                AdaptiveVCAPPhase.SKINNY: "skinny_step",
                AdaptiveVCAPPhase.FAT_2: "fat2_step",
            }.get(completed_phase)
            if step_field is not None:
                fields[step_field] = getattr(record, step_field) + 1
            fields.update(
                status=VCAPFragmentStatus.WAITING,
                active_mutation_proposal_id=None,
                pending_phase=None,
                last_completed_at=now,
            )
        elif cycle.phase is VCAPRuntimePhase.HOLD:
            fields.update(
                status=VCAPFragmentStatus.WAITING,
                active_mutation_proposal_id=None,
                pending_phase=None,
            )
        else:
            fields.update(
                status=VCAPFragmentStatus.WAITING,
                active_mutation_proposal_id=None,
                pending_phase=None,
            )

        self._update(fragment_id, now=now, **fields)
        updated = self.get_fragment(fragment_id)
        if updated.remaining_budget_quote <= ZERO or self._all_steps_exhausted(updated):
            self._update(
                fragment_id,
                now=now,
                status=VCAPFragmentStatus.EXHAUSTED,
            )
            return self.get_fragment(fragment_id)
        return updated

    def latest_cycle_for_position(self, position_id: str) -> VCAPRuntimeCycle | None:
        cycles: list[VCAPRuntimeCycle] = []
        for record in self.list_fragments(position_id):
            _, runtime_store = self.stores(record.fragment_id)
            cycle = runtime_store.latest_for_position(position_id)
            if cycle is not None:
                cycles.append(cycle)
        if not cycles:
            return None
        return max(cycles, key=lambda cycle: cycle.heartbeat_at)

    def list_pending_reconciliation_position_ids(self, *, limit: int = 1000) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT position_id FROM vcap_fragments
                   WHERE status IN (?, ?) ORDER BY position_id LIMIT ?""",
                (
                    VCAPFragmentStatus.ACTIVE.value,
                    VCAPFragmentStatus.RECONCILING.value,
                    limit,
                ),
            ).fetchall()
        pending: list[str] = []
        for row in rows:
            position_id = str(row["position_id"])
            if any(
                self._pending_cycle(record.fragment_id) is not None
                for record in self.list_fragments(position_id)
            ):
                pending.append(position_id)
        return tuple(pending[:limit])

    def summary(self) -> dict[str, object]:
        """Return aggregate fragment counts without exposing account-level secrets."""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS count FROM vcap_fragments"
            ).fetchone()
            positions = conn.execute(
                "SELECT COUNT(DISTINCT position_id) AS count FROM vcap_fragments"
            ).fetchone()
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM vcap_fragments GROUP BY status"
            ).fetchall()
            phase_rows = conn.execute(
                "SELECT phase, COUNT(*) AS count FROM vcap_fragments GROUP BY phase"
            ).fetchall()
            pre_vcap_rows = conn.execute(
                "SELECT pre_vcap_step FROM vcap_fragments WHERE ordinal = 0"
            ).fetchall()
        statuses = {str(row["status"]): int(row["count"]) for row in status_rows}
        phases = {str(row["phase"]): int(row["count"]) for row in phase_rows}
        return {
            "status": "READY" if int(total["count"]) else "EMPTY",
            "position_count": int(positions["count"]),
            "fragment_count": int(total["count"]),
            "pre_vcap_dca_steps_completed": sum(
                int(row["pre_vcap_step"]) for row in pre_vcap_rows
            ),
            "active_fragments": statuses.get(VCAPFragmentStatus.ACTIVE.value, 0),
            "waiting_fragments": statuses.get(VCAPFragmentStatus.WAITING.value, 0),
            "reconciling_fragments": statuses.get(VCAPFragmentStatus.RECONCILING.value, 0),
            "paused_fragments": statuses.get(VCAPFragmentStatus.PAUSED.value, 0),
            "recovered_fragments": statuses.get(VCAPFragmentStatus.RECOVERED.value, 0),
            "exhausted_fragments": statuses.get(VCAPFragmentStatus.EXHAUSTED.value, 0),
            "conflict_fragments": statuses.get(VCAPFragmentStatus.CONFLICT.value, 0),
            "by_status": statuses,
            "by_phase": phases,
            "fragments_per_position": self.policy.fragment_count,
            "max_active_per_position": self.policy.max_active_fragments_per_position,
        }

    def all_active_position_ids(self, *, limit: int = 1000) -> tuple[str, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT position_id FROM vcap_fragments
                   WHERE status IN (?, ?) ORDER BY position_id LIMIT ?""",
                (
                    VCAPFragmentStatus.ACTIVE.value,
                    VCAPFragmentStatus.RECONCILING.value,
                    limit,
                ),
            ).fetchall()
        return tuple(str(row["position_id"]) for row in rows)
