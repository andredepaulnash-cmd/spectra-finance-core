from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from .models import (
    CapitalDecision,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
    ExecutionProvenance,
)


ZERO = Decimal("0")


def validate_capital_policy(policy: CapitalPolicy) -> None:
    """Validate capital limits without touching persistent state."""
    values = (
        policy.reserve_floor_quote,
        policy.max_total_committed_quote,
        policy.max_pair_quote,
        policy.max_position_quote,
        policy.max_recovery_quote,
    )

    if any(value < ZERO for value in values):
        raise ValueError("capital policy limits cannot be negative")

    if policy.max_leverage <= ZERO:
        raise ValueError("max_leverage must be positive")

    if (
        not isinstance(policy.reservation_ttl_seconds, int)
        or isinstance(policy.reservation_ttl_seconds, bool)
        or policy.reservation_ttl_seconds <= 0
    ):
        raise ValueError(
            "reservation_ttl_seconds must be a positive integer"
        )


class CapitalGovernor:
    """
    Deterministic capital authorization boundary.

    This component does not place orders. It decides whether capital
    may be reserved for a proposal and records that decision atomically.
    """

    def __init__(self, db_path: str | Path, policy: CapitalPolicy):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy
        self._validate_policy()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS capital_reservations (
                    proposal_id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    amount_quote TEXT NOT NULL,
                    leverage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    available_after_quote TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_capital_reservations_status_pair
                ON capital_reservations(status, pair)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_receipts (
                    proposal_id TEXT PRIMARY KEY,
                    executor_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_provenance (
                    proposal_id TEXT PRIMARY KEY,
                    executor_id TEXT NOT NULL UNIQUE,
                    pair TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    authorized_quote TEXT NOT NULL,
                    leverage TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

            # Upgrade existing reconciled receipts deterministically. This is
            # provenance of authorization/executor ownership, not fill data.
            conn.execute(
                """
                INSERT OR IGNORE INTO execution_provenance (
                    proposal_id,
                    executor_id,
                    pair,
                    intent,
                    authorized_quote,
                    leverage,
                    created_at
                )
                SELECT
                    r.proposal_id,
                    e.executor_id,
                    r.pair,
                    r.intent,
                    r.amount_quote,
                    r.leverage,
                    e.created_at
                FROM execution_receipts AS e
                JOIN capital_reservations AS r
                  ON r.proposal_id = e.proposal_id
                WHERE r.status = ?
                """,
                (DecisionStatus.RECONCILED.value,),
            )

            conflict = conn.execute(
                """
                SELECT
                    r.proposal_id
                FROM execution_receipts AS e
                JOIN capital_reservations AS r
                  ON r.proposal_id = e.proposal_id
                LEFT JOIN execution_provenance AS p
                  ON p.proposal_id = r.proposal_id
                WHERE r.status = ?
                  AND (
                      p.proposal_id IS NULL
                      OR p.executor_id != e.executor_id
                      OR p.pair != r.pair
                      OR p.intent != r.intent
                      OR p.authorized_quote != r.amount_quote
                      OR p.leverage != r.leverage
                  )
                LIMIT 1
                """,
                (DecisionStatus.RECONCILED.value,),
            ).fetchone()

            if conflict is not None:
                raise RuntimeError(
                    "execution provenance conflicts with reconciled "
                    f"reservation: {conflict['proposal_id']}"
                )

    def _validate_policy(self) -> None:
        validate_capital_policy(self.policy)

    @staticmethod
    def _sum_amounts(rows) -> Decimal:
        return sum(
            (Decimal(row["amount_quote"]) for row in rows),
            ZERO,
        )

    def _reserved(
        self,
        conn: sqlite3.Connection,
        *,
        pair: str | None = None,
        intent: IntentKind | None = None,
    ) -> Decimal:
        sql = """
            SELECT amount_quote
            FROM capital_reservations
            WHERE status = ?
        """

        params: list[str] = [DecisionStatus.RESERVED.value]

        if pair is not None:
            sql += " AND pair = ?"
            params.append(pair)

        if intent is not None:
            sql += " AND intent = ?"
            params.append(intent.value)

        rows = conn.execute(sql, params).fetchall()
        return self._sum_amounts(rows)

    def _expire_stale(
        self,
        conn: sqlite3.Connection,
        *,
        now: float | None = None,
    ) -> int:
        current_time = time.time() if now is None else now
        cutoff = (
            current_time
            - self.policy.reservation_ttl_seconds
        )

        cursor = conn.execute(
            """
            UPDATE capital_reservations
            SET
                status = ?,
                reason = ?,
                updated_at = ?
            WHERE status = ?
              AND created_at <= ?
            """,
            (
                DecisionStatus.EXPIRED.value,
                "reservation lease expired",
                current_time,
                DecisionStatus.RESERVED.value,
                cutoff,
            ),
        )

        return cursor.rowcount

    def _protected(
        self,
        conn: sqlite3.Connection,
        *,
        pair: str | None = None,
        intent: IntentKind | None = None,
    ) -> Decimal:
        sql = """
            SELECT amount_quote
            FROM capital_reservations
            WHERE status IN (?, ?)
        """

        params: list[str] = [
            DecisionStatus.RESERVED.value,
            DecisionStatus.COMMITTED.value,
        ]

        if pair is not None:
            sql += " AND pair = ?"
            params.append(pair)

        if intent is not None:
            sql += " AND intent = ?"
            params.append(intent.value)

        rows = conn.execute(sql, params).fetchall()
        return self._sum_amounts(rows)

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> CapitalDecision:
        active = row["status"] in {
            DecisionStatus.RESERVED.value,
            DecisionStatus.COMMITTED.value,
        }

        return CapitalDecision(
            proposal_id=row["proposal_id"],
            status=DecisionStatus(row["status"]),
            reason=row["reason"],
            reserved_quote=(
                Decimal(row["amount_quote"])
                if active
                else ZERO
            ),
            available_after_quote=Decimal(
                row["available_after_quote"]
            ),
        )

    def reserve(
        self,
        proposal: CapitalProposal,
        snapshot: CapitalSnapshot,
    ) -> CapitalDecision:
        pair = proposal.pair.strip().upper()

        if not proposal.proposal_id.strip():
            raise ValueError("proposal_id is required")

        if not pair:
            raise ValueError("pair is required")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                self._expire_stale(conn)

                existing = conn.execute(
                    """
                    SELECT *
                    FROM capital_reservations
                    WHERE proposal_id = ?
                    """,
                    (proposal.proposal_id,),
                ).fetchone()

                # Idempotency:
                # the same proposal_id can never reserve twice.
                if existing is not None:
                    conn.commit()
                    return self._decision_from_row(existing)

                active_total = self._protected(conn)

                active_pair = self._protected(
                    conn,
                    pair=pair,
                )

                active_recovery = self._protected(
                    conn,
                    intent=IntentKind.RECOVERY,
                )

                amount = proposal.amount_quote

                available_after = (
                    snapshot.free_quote
                    - active_total
                    - amount
                )

                reason = self._rejection_reason(
                    proposal=proposal,
                    snapshot=snapshot,
                    active_total=active_total,
                    active_pair=active_pair,
                    active_recovery=active_recovery,
                    available_after=available_after,
                )

                if reason:
                    status = DecisionStatus.REJECTED
                else:
                    status = DecisionStatus.RESERVED
                    reason = (
                        "authorized within deterministic "
                        "capital envelope"
                    )

                now = time.time()

                conn.execute(
                    """
                    INSERT INTO capital_reservations (
                        proposal_id,
                        pair,
                        intent,
                        amount_quote,
                        leverage,
                        status,
                        reason,
                        available_after_quote,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposal_id,
                        pair,
                        proposal.intent.value,
                        str(amount),
                        str(proposal.leverage),
                        status.value,
                        reason,
                        str(available_after),
                        now,
                        now,
                    ),
                )

                conn.commit()

                return CapitalDecision(
                    proposal_id=proposal.proposal_id,
                    status=status,
                    reason=reason,
                    reserved_quote=(
                        amount
                        if status == DecisionStatus.RESERVED
                        else ZERO
                    ),
                    available_after_quote=available_after,
                )

            except Exception:
                conn.rollback()
                raise

    def _rejection_reason(
        self,
        *,
        proposal: CapitalProposal,
        snapshot: CapitalSnapshot,
        active_total: Decimal,
        active_pair: Decimal,
        active_recovery: Decimal,
        available_after: Decimal,
    ) -> str:
        p = self.policy
        amount = proposal.amount_quote

        # Fail closed on invalid numeric state.
        numeric_values = {
            "amount_quote": amount,
            "leverage": proposal.leverage,
            "free_quote": snapshot.free_quote,
            "committed_quote": snapshot.committed_quote,
            "pair_exposure_quote": snapshot.pair_exposure_quote,
            "position_exposure_quote": snapshot.position_exposure_quote,
            "recovery_exposure_quote": snapshot.recovery_exposure_quote,
            "active_total": active_total,
            "active_pair": active_pair,
            "active_recovery": active_recovery,
            "available_after": available_after,
        }

        for name, value in numeric_values.items():
            if not isinstance(value, Decimal):
                return f"{name} must be Decimal"
            if not value.is_finite():
                return f"{name} must be finite"

        if amount <= ZERO:
            return "amount_quote must be positive"

        if proposal.leverage <= ZERO:
            return "leverage must be positive"

        if proposal.leverage > p.max_leverage:
            return (
                f"leverage {proposal.leverage} exceeds "
                f"max_leverage {p.max_leverage}"
            )

        if any(
            value < ZERO
            for value in (
                snapshot.free_quote,
                snapshot.committed_quote,
                snapshot.pair_exposure_quote,
                snapshot.position_exposure_quote,
                snapshot.recovery_exposure_quote,
            )
        ):
            return "capital snapshot contains negative exposure/balance"

        # Absolute liquidity floor.
        if available_after < p.reserve_floor_quote:
            return (
                f"reserve floor breach: "
                f"available_after={available_after}, "
                f"floor={p.reserve_floor_quote}"
            )

        # Global committed-capital ceiling.
        total_after = (
            snapshot.committed_quote
            + active_total
            + amount
        )

        if total_after > p.max_total_committed_quote:
            return (
                f"total capital cap exceeded: "
                f"{total_after} > "
                f"{p.max_total_committed_quote}"
            )

        # Per-pair concentration ceiling.
        pair_after = (
            snapshot.pair_exposure_quote
            + active_pair
            + amount
        )

        if pair_after > p.max_pair_quote:
            return (
                f"pair exposure cap exceeded: "
                f"{pair_after} > "
                f"{p.max_pair_quote}"
            )

        # Per-position ceiling.
        position_after = (
            snapshot.position_exposure_quote
            + active_pair
            + amount
        )

        if position_after > p.max_position_quote:
            return (
                f"position exposure cap exceeded: "
                f"{position_after} > "
                f"{p.max_position_quote}"
            )

        # Recovery capital is separately bounded.
        if proposal.intent == IntentKind.RECOVERY:
            recovery_after = (
                snapshot.recovery_exposure_quote
                + active_recovery
                + amount
            )

            if recovery_after > p.max_recovery_quote:
                return (
                    f"recovery budget exceeded: "
                    f"{recovery_after} > "
                    f"{p.max_recovery_quote}"
                )

        return ""

    def commit_if_matches(
        self,
        proposal: CapitalProposal,
        reason: str = "committed for matched execution",
    ) -> bool:
        """Atomically verify and consume an exact RESERVED capital claim."""
        proposal_id = proposal.proposal_id.strip()
        pair = proposal.pair.strip().upper()

        if not proposal_id:
            raise ValueError("proposal_id is required")
        if not pair:
            raise ValueError("pair is required")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_stale(conn)

                row = conn.execute(
                    """
                    SELECT *
                    FROM capital_reservations
                    WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()

                if row is None:
                    conn.commit()
                    return False

                matches = (
                    row["status"] == DecisionStatus.RESERVED.value
                    and row["pair"] == pair
                    and row["intent"] == proposal.intent.value
                    and Decimal(row["amount_quote"]) == proposal.amount_quote
                    and Decimal(row["leverage"]) == proposal.leverage
                )

                if not matches:
                    conn.commit()
                    return False

                cursor = conn.execute(
                    """
                    UPDATE capital_reservations
                    SET status = ?, reason = ?, updated_at = ?
                    WHERE proposal_id = ?
                      AND status = ?
                    """,
                    (
                        DecisionStatus.COMMITTED.value,
                        reason,
                        time.time(),
                        proposal_id,
                        DecisionStatus.RESERVED.value,
                    ),
                )

                conn.commit()
                return cursor.rowcount == 1

            except Exception:
                conn.rollback()
                raise

    def release(
        self,
        proposal_id: str,
        reason: str = "released",
    ) -> bool:
        """
        Release an existing RESERVED proposal.

        Returns True only when an active reservation was actually released.
        """
        return self._transition(
            proposal_id=proposal_id,
            target=DecisionStatus.RELEASED,
            reason=reason,
        )

    def commit(
        self,
        proposal_id: str,
        reason: str = "committed",
    ) -> bool:
        """
        Durably claim a RESERVED proposal for external execution.

        COMMITTED must be recorded BEFORE any external execution call.
        It remains protected until explicit reconciliation proves that
        Hummingbot/exchange state has absorbed the capital.
        """
        return self._transition(
            proposal_id=proposal_id,
            target=DecisionStatus.COMMITTED,
            reason=reason,
        )

    def _transition(
        self,
        *,
        proposal_id: str,
        target: DecisionStatus,
        reason: str,
    ) -> bool:
        proposal_id = proposal_id.strip()

        if not proposal_id:
            raise ValueError("proposal_id is required")

        if target not in {
            DecisionStatus.COMMITTED,
            DecisionStatus.RELEASED,
        }:
            raise ValueError(
                f"unsupported reservation transition: {target}"
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                cursor = conn.execute(
                    """
                    UPDATE capital_reservations
                    SET
                        status = ?,
                        reason = ?,
                        updated_at = ?
                    WHERE proposal_id = ?
                      AND status = ?
                    """,
                    (
                        target.value,
                        reason,
                        time.time(),
                        proposal_id,
                        DecisionStatus.RESERVED.value,
                    ),
                )

                conn.commit()
                return cursor.rowcount == 1

            except Exception:
                conn.rollback()
                raise

    def reconcile(
        self,
        proposal_id: str,
        reason: str = "reconciled with execution state",
    ) -> bool:
        """
        Mark COMMITTED capital as RECONCILED.

        Call this only after deterministic evidence confirms that the
        corresponding Hummingbot/exchange state now accounts for it.
        """
        proposal_id = proposal_id.strip()

        if not proposal_id:
            raise ValueError("proposal_id is required")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                cursor = conn.execute(
                    """
                    UPDATE capital_reservations
                    SET
                        status = ?,
                        reason = ?,
                        updated_at = ?
                    WHERE proposal_id = ?
                      AND status = ?
                    """,
                    (
                        DecisionStatus.RECONCILED.value,
                        reason,
                        time.time(),
                        proposal_id,
                        DecisionStatus.COMMITTED.value,
                    ),
                )

                conn.commit()
                return cursor.rowcount == 1

            except Exception:
                conn.rollback()
                raise

    def reconcile_execution(
        self,
        *,
        proposal_id: str,
        executor_id: str,
        reason: str = "reconciled with Hummingbot executor receipt",
    ) -> bool:
        """
        Atomically bind one Spectra proposal to one Hummingbot executor and
        transition COMMITTED -> RECONCILED.

        Exact replay of the same proposal/executor mapping is idempotent.
        Conflicting mappings fail closed.
        """
        proposal_id = proposal_id.strip()
        executor_id = executor_id.strip()

        if not proposal_id:
            raise ValueError("proposal_id is required")
        if not executor_id:
            raise ValueError("executor_id is required")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                reservation = conn.execute(
                    """
                    SELECT
                        status,
                        pair,
                        intent,
                        amount_quote,
                        leverage
                    FROM capital_reservations
                    WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()

                if reservation is None:
                    conn.rollback()
                    return False

                receipt = conn.execute(
                    """
                    SELECT executor_id
                    FROM execution_receipts
                    WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()

                if receipt is not None:
                    existing_executor = receipt["executor_id"]

                    if existing_executor != executor_id:
                        raise RuntimeError(
                            "conflicting execution receipt for "
                            f"{proposal_id}: existing={existing_executor!r}, "
                            f"new={executor_id!r}"
                        )

                    if reservation["status"] != DecisionStatus.RECONCILED.value:
                        raise RuntimeError(
                            "execution receipt exists but proposal is not "
                            f"RECONCILED: {proposal_id}"
                        )

                    provenance = conn.execute(
                        """
                        SELECT
                            executor_id,
                            pair,
                            intent,
                            authorized_quote,
                            leverage
                        FROM execution_provenance
                        WHERE proposal_id = ?
                        """,
                        (proposal_id,),
                    ).fetchone()

                    if provenance is None:
                        raise RuntimeError(
                            "execution receipt exists but provenance is "
                            f"missing: {proposal_id}"
                        )

                    expected = (
                        executor_id,
                        reservation["pair"],
                        reservation["intent"],
                        reservation["amount_quote"],
                        reservation["leverage"],
                    )
                    actual = (
                        provenance["executor_id"],
                        provenance["pair"],
                        provenance["intent"],
                        provenance["authorized_quote"],
                        provenance["leverage"],
                    )

                    if actual != expected:
                        raise RuntimeError(
                            "execution provenance conflicts with reservation "
                            f"for {proposal_id}"
                        )

                    conn.commit()
                    return True

                other = conn.execute(
                    """
                    SELECT proposal_id
                    FROM execution_receipts
                    WHERE executor_id = ?
                    """,
                    (executor_id,),
                ).fetchone()

                if other is not None:
                    raise RuntimeError(
                        f"executor_id {executor_id!r} is already bound to "
                        f"proposal {other['proposal_id']!r}"
                    )

                if reservation["status"] != DecisionStatus.COMMITTED.value:
                    conn.rollback()
                    return False

                now = time.time()

                conn.execute(
                    """
                    INSERT INTO execution_receipts (
                        proposal_id,
                        executor_id,
                        created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (proposal_id, executor_id, now),
                )

                conn.execute(
                    """
                    INSERT INTO execution_provenance (
                        proposal_id,
                        executor_id,
                        pair,
                        intent,
                        authorized_quote,
                        leverage,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        executor_id,
                        reservation["pair"],
                        reservation["intent"],
                        reservation["amount_quote"],
                        reservation["leverage"],
                        now,
                    ),
                )

                cursor = conn.execute(
                    """
                    UPDATE capital_reservations
                    SET
                        status = ?,
                        reason = ?,
                        updated_at = ?
                    WHERE proposal_id = ?
                      AND status = ?
                    """,
                    (
                        DecisionStatus.RECONCILED.value,
                        reason,
                        now,
                        proposal_id,
                        DecisionStatus.COMMITTED.value,
                    ),
                )

                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "proposal changed state during execution reconciliation"
                    )

                conn.commit()
                return True

            except Exception:
                conn.rollback()
                raise

    def get_execution_receipt(
        self,
        proposal_id: str,
    ) -> str | None:
        """Return the executor bound to a proposal, if one exists."""
        proposal_id = proposal_id.strip()

        if not proposal_id:
            raise ValueError("proposal_id is required")

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT executor_id
                FROM execution_receipts
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()

        return None if row is None else str(row["executor_id"])

    def get_execution_provenance(
        self,
        proposal_id: str,
    ) -> ExecutionProvenance | None:
        """Return immutable authorization provenance for one executor."""
        proposal_id = proposal_id.strip()

        if not proposal_id:
            raise ValueError("proposal_id is required")

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    proposal_id,
                    executor_id,
                    pair,
                    intent,
                    authorized_quote,
                    leverage
                FROM execution_provenance
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()

        if row is None:
            return None

        return ExecutionProvenance(
            proposal_id=str(row["proposal_id"]),
            executor_id=str(row["executor_id"]),
            pair=str(row["pair"]),
            intent=IntentKind(str(row["intent"])),
            authorized_quote=Decimal(str(row["authorized_quote"])),
            leverage=Decimal(str(row["leverage"])),
        )

    def protected_quote(self) -> Decimal:
        """
        Capital still protected by local Spectra accounting.

        Includes RESERVED and COMMITTED rows.
        """
        with self._connect() as conn:
            self._expire_stale(conn)
            return self._protected(conn)

    def active_reserved_quote(self) -> Decimal:
        """Total capital presently held by RESERVED proposals."""
        with self._connect() as conn:
            return self._reserved(conn)

    def get_decision(
        self,
        proposal_id: str,
    ) -> CapitalDecision | None:
        """Read the current recorded state of one proposal."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM capital_reservations
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()

        if row is None:
            return None

        return self._decision_from_row(row)
