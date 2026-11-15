from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Iterable

from .executor_provenance_bridge import ExecutorLedgerSyncReport


class AccountingHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    HOLD = "HOLD"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class AccountingHealthPolicy:
    max_age_seconds: float = 30.0

    def __post_init__(self) -> None:
        age = float(self.max_age_seconds)
        if not isfinite(age) or age <= 0:
            raise ValueError("max_age_seconds must be finite and positive")
        object.__setattr__(self, "max_age_seconds", age)


@dataclass(frozen=True)
class AccountingHealthDecision:
    position_id: str
    state: AccountingHealthState
    assessed_at: float
    last_attempt_at: float | None
    last_success_at: float | None
    max_age_seconds: float
    proposal_count: int
    fully_reconciled_proposals: int
    known_orders: int
    bound_orders: int
    confirmed_fills: int
    ledgered_fills: int
    unresolved_order_ids: tuple[str, ...]
    unresolved_trade_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def age_seconds(self) -> float | None:
        if self.last_success_at is None:
            return None
        return max(0.0, self.assessed_at - self.last_success_at)

    @property
    def fully_reconciled(self) -> bool:
        return (
            self.proposal_count > 0
            and self.fully_reconciled_proposals == self.proposal_count
            and not self.unresolved_order_ids
            and not self.unresolved_trade_ids
        )

    def allows_live_mutation(self, *, now: float) -> bool:
        timestamp = _time(now, "now")
        if self.state != AccountingHealthState.HEALTHY:
            return False
        if self.last_success_at is None:
            return False
        if timestamp < self.last_success_at:
            return False
        return timestamp - self.last_success_at <= self.max_age_seconds


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


def _ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(value, "identifier") for value in values))


class AccountingHealthSupervisor:
    """Durable accounting freshness and reconciliation circuit breaker.

    The watermark is stored beside the economic ledger so a process restart
    cannot silently forget whether a position was last proven reconciled.
    Only a complete exact-provenance sync advances last_success_at.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        policy: AccountingHealthPolicy | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or AccountingHealthPolicy()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS economic_accounting_health (
                    position_id TEXT PRIMARY KEY,
                    recorded_state TEXT NOT NULL,
                    last_attempt_at REAL NOT NULL,
                    last_success_at REAL,
                    proposal_count INTEGER NOT NULL,
                    fully_reconciled_proposals INTEGER NOT NULL,
                    known_orders INTEGER NOT NULL,
                    bound_orders INTEGER NOT NULL,
                    confirmed_fills INTEGER NOT NULL,
                    ledgered_fills INTEGER NOT NULL,
                    unresolved_order_ids_json TEXT NOT NULL,
                    unresolved_trade_ids_json TEXT NOT NULL,
                    reason TEXT NOT NULL
                )
                """)

    def record_reports(
        self,
        *,
        position_id: str,
        reports: Iterable[ExecutorLedgerSyncReport],
        now: float,
    ) -> AccountingHealthDecision:
        pid = _text(position_id, "position_id")
        timestamp = _time(now, "now")
        rows = tuple(reports)
        if not rows:
            return self.record_hold(
                position_id=pid,
                now=timestamp,
                reason="accounting_sync_has_no_proposals",
            )

        for report in rows:
            if report.position_id != pid:
                raise ValueError("accounting report belongs to another position")

        unresolved_orders = _ids(
            order_id for report in rows for order_id in report.unresolved_order_ids
        )
        unresolved_trades = _ids(
            trade_id for report in rows for trade_id in report.unresolved_trade_ids
        )
        fully = sum(1 for report in rows if report.fully_reconciled)
        complete = (
            fully == len(rows) and not unresolved_orders and not unresolved_trades
        )
        state = (
            AccountingHealthState.HEALTHY if complete else AccountingHealthState.HOLD
        )
        reason = (
            "all_executor_accounting_fully_reconciled"
            if complete
            else "executor_accounting_requires_reconciliation"
        )
        # Preserve the narrow policy-only HOLD needed to continue recording an
        # already dispatched sale. Missing orders/trades or lagging cumulative
        # fill evidence retain the generic HOLD and cannot grant that path.
        if (
            not complete
            and not unresolved_orders
            and not unresolved_trades
            and all(
                report.fully_reconciled
                or (
                    "protected_buy_requires_reconciliation" in report.reasons
                    and "protected_buy_fill_coverage_incomplete" not in report.reasons
                )
                for report in rows
            )
        ):
            reason = "protected_buy_requires_reconciliation"

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT last_success_at
                FROM economic_accounting_health
                WHERE position_id = ?
                """,
                (pid,),
            ).fetchone()
            previous_success = (
                None
                if existing is None or existing["last_success_at"] is None
                else float(existing["last_success_at"])
            )
            last_success = timestamp if complete else previous_success
            conn.execute(
                """
                INSERT INTO economic_accounting_health (
                    position_id,
                    recorded_state,
                    last_attempt_at,
                    last_success_at,
                    proposal_count,
                    fully_reconciled_proposals,
                    known_orders,
                    bound_orders,
                    confirmed_fills,
                    ledgered_fills,
                    unresolved_order_ids_json,
                    unresolved_trade_ids_json,
                    reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    recorded_state = excluded.recorded_state,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    proposal_count = excluded.proposal_count,
                    fully_reconciled_proposals = excluded.fully_reconciled_proposals,
                    known_orders = excluded.known_orders,
                    bound_orders = excluded.bound_orders,
                    confirmed_fills = excluded.confirmed_fills,
                    ledgered_fills = excluded.ledgered_fills,
                    unresolved_order_ids_json = excluded.unresolved_order_ids_json,
                    unresolved_trade_ids_json = excluded.unresolved_trade_ids_json,
                    reason = excluded.reason
                """,
                (
                    pid,
                    state.value,
                    timestamp,
                    last_success,
                    len(rows),
                    fully,
                    sum(report.known_orders for report in rows),
                    sum(report.bound_orders for report in rows),
                    sum(report.confirmed_fills for report in rows),
                    sum(report.ledgered_fills for report in rows),
                    json.dumps(list(unresolved_orders)),
                    json.dumps(list(unresolved_trades)),
                    reason,
                ),
            )

        return self.assess(position_id=pid, now=timestamp)

    def _record_failure(
        self,
        *,
        position_id: str,
        now: float,
        state: AccountingHealthState,
        reason: str,
    ) -> AccountingHealthDecision:
        pid = _text(position_id, "position_id")
        timestamp = _time(now, "now")
        message = _text(reason, "reason")
        if state not in {
            AccountingHealthState.HOLD,
            AccountingHealthState.CONFLICT,
        }:
            raise ValueError("failure state must be HOLD or CONFLICT")

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT last_success_at
                FROM economic_accounting_health
                WHERE position_id = ?
                """,
                (pid,),
            ).fetchone()
            last_success = (
                None
                if existing is None or existing["last_success_at"] is None
                else float(existing["last_success_at"])
            )
            conn.execute(
                """
                INSERT INTO economic_accounting_health (
                    position_id,
                    recorded_state,
                    last_attempt_at,
                    last_success_at,
                    proposal_count,
                    fully_reconciled_proposals,
                    known_orders,
                    bound_orders,
                    confirmed_fills,
                    ledgered_fills,
                    unresolved_order_ids_json,
                    unresolved_trade_ids_json,
                    reason
                )
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, '[]', '[]', ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    recorded_state = excluded.recorded_state,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    proposal_count = 0,
                    fully_reconciled_proposals = 0,
                    known_orders = 0,
                    bound_orders = 0,
                    confirmed_fills = 0,
                    ledgered_fills = 0,
                    unresolved_order_ids_json = '[]',
                    unresolved_trade_ids_json = '[]',
                    reason = excluded.reason
                """,
                (
                    pid,
                    state.value,
                    timestamp,
                    last_success,
                    message,
                ),
            )

        return self.assess(position_id=pid, now=timestamp)

    def record_hold(
        self,
        *,
        position_id: str,
        now: float,
        reason: str,
    ) -> AccountingHealthDecision:
        return self._record_failure(
            position_id=position_id,
            now=now,
            state=AccountingHealthState.HOLD,
            reason=reason,
        )

    def record_conflict(
        self,
        *,
        position_id: str,
        now: float,
        reason: str,
    ) -> AccountingHealthDecision:
        return self._record_failure(
            position_id=position_id,
            now=now,
            state=AccountingHealthState.CONFLICT,
            reason=reason,
        )

    def assess(
        self,
        *,
        position_id: str,
        now: float,
    ) -> AccountingHealthDecision:
        pid = _text(position_id, "position_id")
        timestamp = _time(now, "now")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_accounting_health
                WHERE position_id = ?
                """,
                (pid,),
            ).fetchone()

        if row is None:
            return AccountingHealthDecision(
                position_id=pid,
                state=AccountingHealthState.HOLD,
                assessed_at=timestamp,
                last_attempt_at=None,
                last_success_at=None,
                max_age_seconds=self.policy.max_age_seconds,
                proposal_count=0,
                fully_reconciled_proposals=0,
                known_orders=0,
                bound_orders=0,
                confirmed_fills=0,
                ledgered_fills=0,
                unresolved_order_ids=(),
                unresolved_trade_ids=(),
                reasons=("accounting_health_missing",),
            )

        recorded = AccountingHealthState(str(row["recorded_state"]))
        last_attempt = float(row["last_attempt_at"])
        last_success = (
            None if row["last_success_at"] is None else float(row["last_success_at"])
        )
        reason = str(row["reason"])

        state = recorded
        reasons = [reason]
        if recorded == AccountingHealthState.HEALTHY:
            if last_success is None:
                state = AccountingHealthState.HOLD
                reasons.append("accounting_success_watermark_missing")
            elif timestamp < last_success:
                state = AccountingHealthState.CONFLICT
                reasons.append("accounting_clock_regression")
            elif timestamp - last_success > self.policy.max_age_seconds:
                state = AccountingHealthState.STALE
                reasons.append("accounting_reconciliation_stale")

        try:
            unresolved_orders = tuple(json.loads(str(row["unresolved_order_ids_json"])))
            unresolved_trades = tuple(json.loads(str(row["unresolved_trade_ids_json"])))
        except json.JSONDecodeError as exc:
            raise RuntimeError("corrupt accounting health watermark") from exc

        return AccountingHealthDecision(
            position_id=pid,
            state=state,
            assessed_at=timestamp,
            last_attempt_at=last_attempt,
            last_success_at=last_success,
            max_age_seconds=self.policy.max_age_seconds,
            proposal_count=int(row["proposal_count"]),
            fully_reconciled_proposals=int(row["fully_reconciled_proposals"]),
            known_orders=int(row["known_orders"]),
            bound_orders=int(row["bound_orders"]),
            confirmed_fills=int(row["confirmed_fills"]),
            ledgered_fills=int(row["ledgered_fills"]),
            unresolved_order_ids=unresolved_orders,
            unresolved_trade_ids=unresolved_trades,
            reasons=tuple(reasons),
        )
