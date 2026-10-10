from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from .executor_accounting import (
    assess_recovery_exposure,
    reconcile_recovery_exposure_with_position_holds,
)
from .position_hold_accounting import (
    PositionHoldAccounting,
    position_hold_accounting_from_raw,
)
from .models import (
    CapitalDecision,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
    ExecutionProvenance,
    ExecutorAccountingObservation,
    ExecutorAccountingDelta,
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

    if (
        not isinstance(policy.position_hold_snapshot_ttl_seconds, int)
        or isinstance(policy.position_hold_snapshot_ttl_seconds, bool)
        or policy.position_hold_snapshot_ttl_seconds <= 0
    ):
        raise ValueError(
            "position_hold_snapshot_ttl_seconds must be a positive integer"
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

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entry_submission_claims (
                    proposal_id TEXT PRIMARY KEY,
                    base_asset TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    ledger_path TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    connector_name TEXT NOT NULL,
                    controller_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    executor_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS entry_submission_active_asset
                ON entry_submission_claims(base_asset)
                WHERE state IN ('COMMITTED', 'DISPATCHED');
                CREATE TABLE IF NOT EXISTS position_submission_bindings (
                    proposal_id TEXT PRIMARY KEY,
                    base_asset TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    ledger_path TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    connector_name TEXT NOT NULL,
                    controller_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    executor_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS executor_accounting_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    executor_type TEXT,
                    status TEXT NOT NULL,
                    close_type TEXT,
                    filled_amount_quote TEXT NOT NULL,
                    cum_fees_quote TEXT NOT NULL,
                    filled_delta_quote TEXT NOT NULL,
                    fee_delta_quote TEXT NOT NULL,
                    position_size_quote TEXT,
                    net_pnl_quote TEXT,
                    realized_pnl_quote TEXT,
                    observed_at REAL NOT NULL
                )
                """
            )

            accounting_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(executor_accounting_observations)"
                ).fetchall()
            }
            if "executor_type" not in accounting_columns:
                conn.execute(
                    "ALTER TABLE executor_accounting_observations "
                    "ADD COLUMN executor_type TEXT"
                )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_executor_accounting_executor
                ON executor_accounting_observations(
                    executor_id,
                    observation_id
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_hold_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    connector_name TEXT NOT NULL,
                    trading_pair TEXT NOT NULL,
                    controller_id TEXT NOT NULL,
                    observed_at REAL NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_position_hold_snapshots_scope
                ON position_hold_snapshots(
                    account_name,
                    connector_name,
                    trading_pair,
                    controller_id,
                    observed_at,
                    snapshot_id
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_hold_snapshot_rows (
                    snapshot_id INTEGER NOT NULL,
                    hold_index INTEGER NOT NULL,
                    buy_amount_base TEXT NOT NULL,
                    buy_amount_quote TEXT NOT NULL,
                    sell_amount_base TEXT NOT NULL,
                    sell_amount_quote TEXT NOT NULL,
                    realized_pnl_quote TEXT NOT NULL,
                    cum_fees_quote TEXT NOT NULL,
                    executor_ids_json TEXT NOT NULL,
                    last_updated TEXT,
                    PRIMARY KEY(snapshot_id, hold_index)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recovery_recycle_releases (
                    release_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    amount_quote TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_recovery_recycle_release_proposal
                ON recovery_recycle_releases(proposal_id)
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
                    account_name TEXT,
                    connector_name TEXT,
                    controller_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )

            provenance_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(execution_provenance)"
                ).fetchall()
            }

            for column_name in (
                "account_name",
                "connector_name",
                "controller_id",
            ):
                if column_name not in provenance_columns:
                    conn.execute(
                        "ALTER TABLE execution_provenance "
                        f"ADD COLUMN {column_name} TEXT"
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
            recovery_ledgers = self._attach_recovery_exposure_ledgers(conn)
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

                # Recovery attribution must be read under the same write
                # transaction as RESERVED/COMMITTED claims. Otherwise a
                # COMMITTED -> RECONCILED handoff could briefly disappear
                # from both buckets between two separate reads.
                reconciled_recovery = (
                    self._current_recovery_exposure_quote(conn, recovery_ledgers)
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
                    reconciled_recovery=reconciled_recovery,
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
        reconciled_recovery: Decimal,
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
            "reconciled_recovery": reconciled_recovery,
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
                reconciled_recovery
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
        *,
        recovery_scope: tuple[str, str, str] | None = None,
        position_id: str | None = None,
    ) -> bool:
        """Atomically verify and consume an exact RESERVED capital claim."""
        proposal_id = proposal.proposal_id.strip()
        pair = proposal.pair.strip().upper()

        if not proposal_id:
            raise ValueError("proposal_id is required")
        if not pair:
            raise ValueError("pair is required")

        with self._connect() as conn:
            recovery_ledger = None
            if recovery_scope is not None:
                from .economic_position import normalized_base_asset
                owner = conn.execute(
                    "SELECT ledger_path FROM entry_submission_claims WHERE base_asset = ? "
                    "AND state IN ('COMMITTED', 'DISPATCHED')",
                    (normalized_base_asset(pair),),
                ).fetchone()
                if owner is not None:
                    path = Path(owner["ledger_path"]).resolve()
                    if not path.is_file():
                        raise ValueError("protected RECOVERY economic ledger is unavailable")
                    schema = "main" if path == self.db_path.resolve() else "economic"
                    if schema != "main":
                        conn.execute("ATTACH DATABASE ? AS economic", (str(path),))
                    recovery_ledger = (schema, str(path))
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

                if recovery_scope is not None:
                    if proposal.intent != IntentKind.RECOVERY:
                        raise ValueError("recovery scope requires RECOVERY intent")
                    owner = self._validate_recovery_scope(
                        conn, proposal, recovery_scope, position_id, recovery_ledger
                    )
                    if owner is not None:
                        now = time.time()
                        conn.execute(
                            """INSERT INTO position_submission_bindings (
                                proposal_id,base_asset,position_id,ledger_path,pair,
                                account_name,connector_name,controller_id,state,created_at,updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,'COMMITTED',?,?)""",
                            (proposal_id,owner["base_asset"],owner["position_id"],
                             owner["ledger_path"],pair,*recovery_scope,now,now),
                        )

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

    @staticmethod
    def _submission_scope(
        account_name: str, connector_name: str, controller_id: str
    ) -> tuple[str, str, str]:
        values = (account_name, connector_name, controller_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("submission account, connector and controller are required")
        return (account_name.strip(), connector_name.strip().lower(), controller_id.strip())

    def get_entry_submission_claim(self, proposal_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM entry_submission_claims WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_entry_submissions_for_position(
        self, *, position_id: str, ledger_path: str | Path
    ) -> tuple[dict, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM entry_submission_claims
                WHERE position_id = ? AND ledger_path = ?
                ORDER BY created_at, proposal_id""",
                (position_id, str(Path(ledger_path).resolve())),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def get_asset_submission_proposals(self, base_asset: str) -> tuple[dict, ...]:
        """Expose all potentially live capital owners for terminal retirement.

        Canonical identity covers aliases and quote pairs, including legacy
        proposals that predate durable position binding. Unbound/uncertain
        claims must never disappear merely because they have no confirmed lot.
        """
        from .economic_position import normalized_base_asset
        base = normalized_base_asset(f"{base_asset}-USD")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT c.*, r.executor_id FROM capital_reservations c
                LEFT JOIN execution_receipts r ON r.proposal_id = c.proposal_id
                WHERE c.status IN ('RESERVED','COMMITTED','RECONCILED')
                ORDER BY c.created_at, c.proposal_id"""
            ).fetchall()
            bindings = {
                row["proposal_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM entry_submission_claims UNION ALL "
                    "SELECT * FROM position_submission_bindings"
                ).fetchall()
            }
        result = []
        for row in rows:
            if normalized_base_asset(row["pair"]) != base:
                continue
            item = dict(row)
            binding = bindings.get(row["proposal_id"])
            item.update(
                position_id=None if binding is None else binding["position_id"],
                ledger_path=None if binding is None else binding["ledger_path"],
                submission_state=None if binding is None else binding["state"],
            )
            result.append(item)
        return tuple(result)

    @staticmethod
    def _entry_retirement_proven(conn, schema: str, claim: sqlite3.Row) -> bool:
        # CLOSED inventory alone is insufficient: an old BUY may still fill.
        # The ledger writes this certificate only after complete terminal
        # executor/order/trade evidence has been reconciled.
        if claim["executor_id"] is None:
            return False
        exists = conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' "
            "AND name = 'economic_position_retirements'"
        ).fetchone()
        if exists is None:
            return False
        row = conn.execute(
            f"SELECT event_id, certificate_json FROM {schema}.economic_position_retirements "
            "WHERE position_id = ?", (claim["position_id"],)
        ).fetchone()
        position = conn.execute(
            f"SELECT status, pair, base_asset FROM {schema}.economic_positions WHERE position_id = ?",
            (claim["position_id"],),
        ).fetchone()
        if row is None or position is None or position["status"] not in {"CLOSED", "RESIDUAL"}:
            return False
        certificate = json.loads(row["certificate_json"])
        if not isinstance(certificate, dict):
            return False
        for field in ("proposal_ids", "executor_ids"):
            values = certificate.get(field)
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value for value in values
            ) or len(set(values)) != len(values):
                return False
        proof = certificate.get("proof_sha256")
        if not isinstance(proof, str) or len(proof) != 64 or any(
            char not in "0123456789abcdef" for char in proof
        ):
            return False
        payload = certificate.get("proof")
        if not isinstance(payload, dict) or hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest() != proof:
            return False
        owners = payload.get("owners")
        if not isinstance(owners, list) or any(not isinstance(owner, dict) for owner in owners):
            return False
        if {owner.get("proposal_id") for owner in owners} != set(certificate["proposal_ids"]):
            return False
        proven_executors = {
            owner.get("provenance", {}).get("executor_id") for owner in owners
        }
        if proven_executors != set(certificate["executor_ids"]):
            return False
        if (certificate.get("kind") == "RESIDUAL") != (position["status"] == "RESIDUAL"):
            return False
        lots = conn.execute(
            f"SELECT base_remaining, cost_basis_remaining_quote, entry_fees_remaining_quote "
            f"FROM {schema}.economic_lots WHERE position_id = ? AND ownership = 'ACTIVE_TRADE'",
            (claim["position_id"],),
        ).fetchall()
        for field, column in (
            ("remaining_base", "base_remaining"),
            ("cost_basis", "cost_basis_remaining_quote"),
            ("entry_fees", "entry_fees_remaining_quote"),
        ):
            expected = Decimal(certificate[field])
            actual = sum((Decimal(lot[column]) for lot in lots), ZERO)
            if not expected.is_finite() or expected < ZERO or actual != expected:
                return False
        return (
            certificate.get("event_id") == row["event_id"]
            and certificate.get("kind") in {"RESIDUAL", "EMPTY_TERMINAL", "FLAT_TERMINAL"}
            and position["pair"] == claim["pair"]
            and position["base_asset"] == claim["base_asset"]
            and certificate.get("position_id") == claim["position_id"]
            and certificate.get("ledger_path") == claim["ledger_path"]
            and certificate.get("pair") == claim["pair"]
            and certificate.get("base_asset") == claim["base_asset"]
            and claim["proposal_id"] in certificate.get("proposal_ids", [])
            and claim["executor_id"] in certificate.get("executor_ids", [])
        )

    def commit_entry_submission_if_matches(
        self, proposal: CapitalProposal, *, position_id: str,
        ledger_path: str | Path, account_name: str, connector_name: str,
        controller_id: str,
    ) -> bool:
        """Claim canonical ENTRY identity and capital before external submission.

        The caller must already own a zero-inventory ACTIVE prefill position.
        The attached economic ledger is read under the same SQLite write lock;
        only the capital database is mutated. Uncertain submissions keep their
        canonical identity through restart until terminal retirement is proven.
        """
        from .economic_position import normalized_base_asset

        if proposal.intent != IntentKind.ENTRY:
            raise ValueError("ENTRY identity claim requires ENTRY intent")
        if not isinstance(position_id, str) or not position_id.strip():
            raise ValueError("ENTRY submission requires an exact position_id")
        pid = position_id.strip()
        path = Path(ledger_path).resolve()
        if not path.is_file():
            raise ValueError("ENTRY economic ledger does not exist")
        scope = self._submission_scope(account_name, connector_name, controller_id)
        pair = proposal.pair.strip().upper()
        base = normalized_base_asset(pair)
        with self._connect() as conn:
            schema = "main" if path == self.db_path.resolve() else "economic"
            if schema == "economic":
                conn.execute("ATTACH DATABASE ? AS economic", (str(path),))
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_stale(conn)
                reservation = conn.execute(
                    "SELECT * FROM capital_reservations WHERE proposal_id = ?",
                    (proposal.proposal_id.strip(),),
                ).fetchone()
                if reservation is None or not (
                    reservation["status"] == DecisionStatus.RESERVED.value
                    and reservation["pair"] == pair
                    and reservation["intent"] == IntentKind.ENTRY.value
                    and Decimal(reservation["amount_quote"]) == proposal.amount_quote
                    and Decimal(reservation["leverage"]) == proposal.leverage
                ):
                    conn.rollback()
                    return False
                position = conn.execute(
                    f"SELECT * FROM {schema}.economic_positions WHERE position_id = ?",
                    (pid,),
                ).fetchone()
                if position is None or position["status"] != "ACTIVE" or position["pair"] != pair or position["base_asset"] != base:
                    raise ValueError("ENTRY requires its exact ACTIVE prefill position")
                active = conn.execute(
                    f"SELECT position_id FROM {schema}.economic_positions "
                    "WHERE base_asset = ? AND status = 'ACTIVE'", (base,),
                ).fetchall()
                if len(active) != 1 or active[0]["position_id"] != pid:
                    raise ValueError("canonical asset already has another ACTIVE position")
                lots = conn.execute(
                    f"SELECT base_remaining FROM {schema}.economic_lots "
                    "WHERE position_id = ? AND ownership = 'ACTIVE_TRADE'", (pid,),
                ).fetchall()
                if any(Decimal(row["base_remaining"]) != ZERO for row in lots):
                    raise ValueError("ENTRY cannot add a new submission to held active inventory")
                claims = conn.execute(
                    "SELECT * FROM entry_submission_claims WHERE base_asset = ? "
                    "AND state IN ('COMMITTED', 'DISPATCHED')", (base,),
                ).fetchall()
                for claim in claims:
                    if claim["ledger_path"] != str(path) or not self._entry_retirement_proven(conn, schema, claim):
                        raise ValueError("canonical asset already has a protected ENTRY submission")
                    conn.execute(
                        "UPDATE entry_submission_claims SET state = 'RETIRED', updated_at = ? "
                        "WHERE proposal_id = ?", (time.time(), claim["proposal_id"]),
                    )
                owners = conn.execute(
                    """SELECT c.*, r.executor_id FROM capital_reservations c
                    LEFT JOIN execution_receipts r ON r.proposal_id = c.proposal_id
                    WHERE c.status IN ('COMMITTED', 'RECONCILED')
                      AND c.proposal_id != ?""",
                    (proposal.proposal_id.strip(),),
                ).fetchall()
                for owner in owners:
                    if normalized_base_asset(owner["pair"]) != base:
                        continue
                    if not self._capital_submission_retirement_proven(
                        conn, schema, str(path), owner
                    ):
                        raise ValueError(
                            "canonical asset has an uncertified protected submission"
                        )
                now = time.time()
                conn.execute(
                    """INSERT INTO entry_submission_claims (
                        proposal_id,base_asset,position_id,ledger_path,pair,
                        account_name,connector_name,controller_id,state,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,'COMMITTED',?,?)""",
                    (proposal.proposal_id.strip(),base,pid,str(path),pair,*scope,now,now),
                )
                cursor = conn.execute(
                    "UPDATE capital_reservations SET status = ?, reason = ?, updated_at = ? "
                    "WHERE proposal_id = ? AND status = ?",
                    (DecisionStatus.COMMITTED.value,"committed with canonical ENTRY identity",now,
                     proposal.proposal_id.strip(),DecisionStatus.RESERVED.value),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("ENTRY capital claim changed during identity submission")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def _capital_submission_retirement_proven(
        self, conn: sqlite3.Connection, schema: str, ledger_path: str,
        owner: sqlite3.Row,
    ) -> bool:
        """Cover legacy/RECOVERY owners as well as canonical ENTRY claims."""
        from .economic_position import normalized_base_asset

        if owner["executor_id"] is None:
            return False
        binding = conn.execute(
            "SELECT * FROM entry_submission_claims WHERE proposal_id = ? "
            "UNION ALL SELECT * FROM position_submission_bindings WHERE proposal_id = ?",
            (owner["proposal_id"], owner["proposal_id"]),
        ).fetchone()
        if binding is not None:
            return (
                binding["ledger_path"] == ledger_path
                and binding["pair"] == owner["pair"]
                and binding["executor_id"] == owner["executor_id"]
                and self._entry_retirement_proven(conn, schema, binding)
            )
        exists = conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' "
            "AND name = 'economic_position_retirements'"
        ).fetchone()
        if exists is None:
            return False
        # Historical proposals did not persist a submission position. Only an
        # exact terminal certificate in the actual runtime ledger can prove
        # their ownership; a flat label or an empty lot query is insufficient.
        base = normalized_base_asset(owner["pair"])
        positions = conn.execute(
            f"SELECT r.position_id FROM {schema}.economic_position_retirements r "
            f"JOIN {schema}.economic_positions p ON p.position_id = r.position_id "
            "WHERE p.base_asset = ?", (base,),
        ).fetchall()
        for position in positions:
            claim = dict(owner)
            claim.update(position_id=position["position_id"], ledger_path=ledger_path,
                         base_asset=base)
            if self._entry_retirement_proven(conn, schema, claim):
                return True
        return False

    @staticmethod
    def _validate_recovery_scope(conn, proposal, scope, position_id, recovery_ledger):
        from .economic_position import normalized_base_asset
        claim = conn.execute(
            "SELECT * FROM entry_submission_claims WHERE base_asset = ? "
            "AND state IN ('COMMITTED', 'DISPATCHED')",
            (normalized_base_asset(proposal.pair),),
        ).fetchone()
        if claim is not None and (
            tuple(claim[name] for name in ("account_name","connector_name","controller_id")) != scope
            or claim["pair"] != proposal.pair.strip().upper()
            or claim["position_id"] != position_id
        ):
            raise ValueError("RECOVERY scope does not match the protected ENTRY position")
        if claim is not None:
            if recovery_ledger is None or recovery_ledger[1] != claim["ledger_path"]:
                raise ValueError("RECOVERY economic identity changed before commit")
            schema = recovery_ledger[0]
            position = conn.execute(
                f"SELECT status, pair, base_asset FROM {schema}.economic_positions "
                "WHERE position_id = ?", (claim["position_id"],),
            ).fetchone()
            if position is None or position["status"] != "ACTIVE" or position["pair"] != claim["pair"] or position["base_asset"] != claim["base_asset"]:
                raise ValueError("RECOVERY requires its exact ACTIVE economic position")
            certificate_table = conn.execute(
                f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' "
                "AND name = 'economic_position_retirements'"
            ).fetchone()
            if certificate_table is not None and conn.execute(
                f"SELECT 1 FROM {schema}.economic_position_retirements WHERE position_id = ?",
                (claim["position_id"],),
            ).fetchone() is not None:
                raise ValueError("RECOVERY position has already been terminally retired")
        return claim

    def commit_recovery_submission_if_matches(
        self, proposal: CapitalProposal, *, account_name: str,
        connector_name: str, controller_id: str, position_id: str | None = None,
    ) -> bool:
        """Preserve legacy recovery while preventing scoped ENTRY ownership drift."""
        scope = self._submission_scope(account_name, connector_name, controller_id)
        return self.commit_if_matches(
            proposal, recovery_scope=scope, position_id=position_id
        )

    def carve_reserved_recovery_tranche(
        self,
        *,
        envelope_proposal_id: str,
        tranche_proposal: CapitalProposal,
    ) -> bool:
        """Atomically carve one exact RECOVERY tranche from a RESERVED envelope.

        The protected total is unchanged: the parent envelope shrinks by the
        tranche amount while a child RESERVED proposal of exactly that amount is
        created. Exact replay of the same child proposal is idempotent. Any
        conflicting replay fails closed.
        """
        envelope_id = envelope_proposal_id.strip()
        child_id = tranche_proposal.proposal_id.strip()
        pair = tranche_proposal.pair.strip().upper()

        if not envelope_id:
            raise ValueError("envelope_proposal_id is required")
        if not child_id:
            raise ValueError("tranche proposal_id is required")
        if envelope_id == child_id:
            raise ValueError("tranche proposal_id must differ from envelope")
        if not pair:
            raise ValueError("tranche pair is required")
        if tranche_proposal.intent != IntentKind.RECOVERY:
            raise ValueError("VCAP tranche must use RECOVERY intent")
        if not isinstance(tranche_proposal.amount_quote, Decimal):
            raise TypeError("tranche amount_quote must be Decimal")
        if (
            not tranche_proposal.amount_quote.is_finite()
            or tranche_proposal.amount_quote <= ZERO
        ):
            raise ValueError("tranche amount_quote must be finite and positive")
        if not isinstance(tranche_proposal.leverage, Decimal):
            raise TypeError("tranche leverage must be Decimal")
        if (
            not tranche_proposal.leverage.is_finite()
            or tranche_proposal.leverage <= ZERO
        ):
            raise ValueError("tranche leverage must be finite and positive")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_stale(conn)

                child = conn.execute(
                    """
                    SELECT *
                    FROM capital_reservations
                    WHERE proposal_id = ?
                    """,
                    (child_id,),
                ).fetchone()

                if child is not None:
                    exact_replay = (
                        child["pair"] == pair
                        and child["intent"] == IntentKind.RECOVERY.value
                        and Decimal(child["amount_quote"])
                        == tranche_proposal.amount_quote
                        and Decimal(child["leverage"])
                        == tranche_proposal.leverage
                    )
                    conn.commit()
                    return exact_replay

                parent = conn.execute(
                    """
                    SELECT *
                    FROM capital_reservations
                    WHERE proposal_id = ?
                    """,
                    (envelope_id,),
                ).fetchone()

                if parent is None:
                    conn.commit()
                    return False

                parent_amount = Decimal(parent["amount_quote"])
                matches = (
                    parent["status"] == DecisionStatus.RESERVED.value
                    and parent["intent"] == IntentKind.RECOVERY.value
                    and parent["pair"] == pair
                    and Decimal(parent["leverage"])
                    == tranche_proposal.leverage
                    and parent_amount >= tranche_proposal.amount_quote
                )
                if not matches:
                    conn.commit()
                    return False

                remaining = parent_amount - tranche_proposal.amount_quote
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
                        child_id,
                        pair,
                        IntentKind.RECOVERY.value,
                        str(tranche_proposal.amount_quote),
                        str(tranche_proposal.leverage),
                        DecisionStatus.RESERVED.value,
                        "VCAP tranche carved from reserved recovery envelope",
                        parent["available_after_quote"],
                        now,
                        now,
                    ),
                )

                if remaining > ZERO:
                    conn.execute(
                        """
                        UPDATE capital_reservations
                        SET
                            amount_quote = ?,
                            reason = ?,
                            updated_at = ?
                        WHERE proposal_id = ?
                          AND status = ?
                        """,
                        (
                            str(remaining),
                            "VCAP recovery envelope partially carved",
                            now,
                            envelope_id,
                            DecisionStatus.RESERVED.value,
                        ),
                    )
                else:
                    conn.execute(
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
                            DecisionStatus.RELEASED.value,
                            "VCAP recovery envelope fully carved",
                            now,
                            envelope_id,
                            DecisionStatus.RESERVED.value,
                        ),
                    )

                conn.commit()
                return True

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
        account_name: str | None = None,
        connector_name: str | None = None,
        controller_id: str | None = None,
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

        scope_inputs = {
            "account_name": account_name,
            "connector_name": connector_name,
            "controller_id": controller_id,
        }
        supplied_scope = [
            value is not None
            for value in scope_inputs.values()
        ]

        if any(supplied_scope) and not all(supplied_scope):
            raise ValueError(
                "execution scope must include account_name, "
                "connector_name, and controller_id together"
            )

        normalized_scope: tuple[str, str, str] | None = None

        if all(supplied_scope):
            normalized: dict[str, str] = {}

            for field, value in scope_inputs.items():
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{field} must be a non-empty string"
                    )
                normalized[field] = value.strip()

            normalized["connector_name"] = (
                normalized["connector_name"].lower()
            )

            normalized_scope = (
                normalized["account_name"],
                normalized["connector_name"],
                normalized["controller_id"],
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                entry_claim = conn.execute(
                    "SELECT * FROM entry_submission_claims WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if entry_claim is not None and normalized_scope != tuple(
                    entry_claim[name] for name in ("account_name", "connector_name", "controller_id")
                ):
                    raise RuntimeError("execution receipt scope conflicts with ENTRY submission")
                position_claim = conn.execute(
                    "SELECT * FROM position_submission_bindings WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if position_claim is not None and normalized_scope != tuple(
                    position_claim[name] for name in ("account_name", "connector_name", "controller_id")
                ):
                    raise RuntimeError("execution receipt scope conflicts with position submission")
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
                            leverage,
                            account_name,
                            connector_name,
                            controller_id
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

                    if normalized_scope is not None:
                        persisted_scope = (
                            provenance["account_name"],
                            provenance["connector_name"],
                            provenance["controller_id"],
                        )

                        if persisted_scope != normalized_scope:
                            raise RuntimeError(
                                "execution provenance scope conflicts "
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
                        account_name,
                        connector_name,
                        controller_id,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        executor_id,
                        reservation["pair"],
                        reservation["intent"],
                        reservation["amount_quote"],
                        reservation["leverage"],
                        (
                            None
                            if normalized_scope is None
                            else normalized_scope[0]
                        ),
                        (
                            None
                            if normalized_scope is None
                            else normalized_scope[1]
                        ),
                        (
                            None
                            if normalized_scope is None
                            else normalized_scope[2]
                        ),
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

                if entry_claim is not None:
                    conn.execute(
                        "UPDATE entry_submission_claims SET state = 'DISPATCHED', "
                        "executor_id = ?, updated_at = ? WHERE proposal_id = ?",
                        (executor_id, now, proposal_id),
                    )
                if position_claim is not None:
                    conn.execute(
                        "UPDATE position_submission_bindings SET state = 'DISPATCHED', "
                        "executor_id = ?, updated_at = ? WHERE proposal_id = ?",
                        (executor_id, now, proposal_id),
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
                    leverage,
                    account_name,
                    connector_name,
                    controller_id
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
            account_name=(
                None
                if row["account_name"] is None
                else str(row["account_name"])
            ),
            connector_name=(
                None
                if row["connector_name"] is None
                else str(row["connector_name"])
            ),
            controller_id=(
                None
                if row["controller_id"] is None
                else str(row["controller_id"])
            ),
        )

    @staticmethod
    def _validate_accounting_decimal(
        value: Decimal,
        *,
        name: str,
        allow_negative: bool = False,
    ) -> None:
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        if not allow_negative and value < ZERO:
            raise ValueError(f"{name} cannot be negative")

    def reconcile_executor_accounting(
        self,
        observation: ExecutorAccountingObservation,
    ) -> ExecutorAccountingDelta:
        """
        Persist one cumulative Hummingbot executor observation.

        Filled amount and cumulative fees are monotonic counters. Only their
        deltas represent new accounting activity. Position/PnL fields are
        observations, not monotonic counters.
        """
        executor_id = observation.executor_id.strip()
        status = observation.status.strip()
        executor_type = (
            None
            if observation.executor_type is None
            else observation.executor_type.strip()
        )

        if not executor_id:
            raise ValueError("executor_id is required")
        if not status:
            raise ValueError("status is required")
        if observation.executor_type is not None and not executor_type:
            raise ValueError("executor_type cannot be empty")

        self._validate_accounting_decimal(
            observation.filled_amount_quote,
            name="filled_amount_quote",
        )
        self._validate_accounting_decimal(
            observation.cum_fees_quote,
            name="cum_fees_quote",
        )

        for name, value, allow_negative in (
            ("position_size_quote", observation.position_size_quote, False),
            ("net_pnl_quote", observation.net_pnl_quote, True),
            ("realized_pnl_quote", observation.realized_pnl_quote, True),
        ):
            if value is not None:
                self._validate_accounting_decimal(
                    value,
                    name=name,
                    allow_negative=allow_negative,
                )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                provenance = conn.execute(
                    """
                    SELECT
                        proposal_id,
                        executor_id,
                        pair,
                        intent
                    FROM execution_provenance
                    WHERE executor_id = ?
                    """,
                    (executor_id,),
                ).fetchone()

                if provenance is None:
                    raise RuntimeError(
                        f"executor has no Spectra provenance: {executor_id}"
                    )

                previous = conn.execute(
                    """
                    SELECT *
                    FROM executor_accounting_observations
                    WHERE executor_id = ?
                    ORDER BY observation_id DESC
                    LIMIT 1
                    """,
                    (executor_id,),
                ).fetchone()

                previous_filled = (
                    ZERO
                    if previous is None
                    else Decimal(previous["filled_amount_quote"])
                )
                previous_fees = (
                    ZERO
                    if previous is None
                    else Decimal(previous["cum_fees_quote"])
                )

                # GridExecutor changes the meaning of filled_amount_quote when
                # it terminates with POSITION_HOLD: while live it includes the
                # current open position plus matched volume, but POSITION_HOLD
                # reports matched volume only. That legal terminal handoff can
                # therefore make the raw Hummingbot value move backwards.
                #
                # Preserve our monotonic accounting high-water mark for that
                # one proven transition. We cannot reconstruct any additional
                # terminal trading volume from the rebased field safely, so its
                # filled delta is zero. All other regressions remain fatal.
                grid_position_hold_rebase = (
                    previous is not None
                    and executor_type == "grid_executor"
                    and status == "TERMINATED"
                    and observation.close_type == "POSITION_HOLD"
                    and observation.filled_amount_quote < previous_filled
                )

                if (
                    observation.filled_amount_quote < previous_filled
                    and not grid_position_hold_rebase
                ):
                    raise RuntimeError(
                        "filled_amount_quote moved backwards for "
                        f"{executor_id}"
                    )

                if observation.cum_fees_quote < previous_fees:
                    raise RuntimeError(
                        f"cum_fees_quote moved backwards for {executor_id}"
                    )

                accounting_filled = (
                    previous_filled
                    if grid_position_hold_rebase
                    else observation.filled_amount_quote
                )

                filled_delta = accounting_filled - previous_filled
                fee_delta = observation.cum_fees_quote - previous_fees

                comparable = (
                    executor_type,
                    status,
                    observation.close_type,
                    accounting_filled,
                    observation.cum_fees_quote,
                    observation.position_size_quote,
                    observation.net_pnl_quote,
                    observation.realized_pnl_quote,
                )

                if previous is not None:
                    previous_comparable = (
                        previous["executor_type"],
                        previous["status"],
                        previous["close_type"],
                        Decimal(previous["filled_amount_quote"]),
                        Decimal(previous["cum_fees_quote"]),
                        (
                            None
                            if previous["position_size_quote"] is None
                            else Decimal(previous["position_size_quote"])
                        ),
                        (
                            None
                            if previous["net_pnl_quote"] is None
                            else Decimal(previous["net_pnl_quote"])
                        ),
                        (
                            None
                            if previous["realized_pnl_quote"] is None
                            else Decimal(previous["realized_pnl_quote"])
                        ),
                    )

                    if comparable == previous_comparable:
                        # A successful exact-scope refresh is new freshness
                        # evidence, even when counters and terminal state are
                        # unchanged. Keep one observation and zero deltas.
                        conn.execute(
                            "UPDATE executor_accounting_observations SET observed_at = ? "
                            "WHERE observation_id = ?",
                            (time.time(), previous["observation_id"]),
                        )
                        conn.commit()
                        return ExecutorAccountingDelta(
                            proposal_id=str(provenance["proposal_id"]),
                            executor_id=executor_id,
                            pair=str(provenance["pair"]),
                            intent=IntentKind(str(provenance["intent"])),
                            filled_delta_quote=ZERO,
                            fee_delta_quote=ZERO,
                            cumulative_filled_quote=accounting_filled,
                            cumulative_fees_quote=observation.cum_fees_quote,
                            position_size_quote=observation.position_size_quote,
                            status=status,
                            close_type=observation.close_type,
                            executor_type=executor_type,
                        )

                conn.execute(
                    """
                    INSERT INTO executor_accounting_observations (
                        proposal_id,
                        executor_id,
                        pair,
                        intent,
                        executor_type,
                        status,
                        close_type,
                        filled_amount_quote,
                        cum_fees_quote,
                        filled_delta_quote,
                        fee_delta_quote,
                        position_size_quote,
                        net_pnl_quote,
                        realized_pnl_quote,
                        observed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provenance["proposal_id"],
                        executor_id,
                        provenance["pair"],
                        provenance["intent"],
                        executor_type,
                        status,
                        observation.close_type,
                        str(accounting_filled),
                        str(observation.cum_fees_quote),
                        str(filled_delta),
                        str(fee_delta),
                        (
                            None
                            if observation.position_size_quote is None
                            else str(observation.position_size_quote)
                        ),
                        (
                            None
                            if observation.net_pnl_quote is None
                            else str(observation.net_pnl_quote)
                        ),
                        (
                            None
                            if observation.realized_pnl_quote is None
                            else str(observation.realized_pnl_quote)
                        ),
                        time.time(),
                    ),
                )

                conn.commit()

                return ExecutorAccountingDelta(
                    proposal_id=str(provenance["proposal_id"]),
                    executor_id=executor_id,
                    pair=str(provenance["pair"]),
                    intent=IntentKind(str(provenance["intent"])),
                    filled_delta_quote=filled_delta,
                    fee_delta_quote=fee_delta,
                    cumulative_filled_quote=accounting_filled,
                    cumulative_fees_quote=observation.cum_fees_quote,
                    position_size_quote=observation.position_size_quote,
                    status=status,
                    close_type=observation.close_type,
                    executor_type=executor_type,
                )

            except Exception:
                conn.rollback()
                raise


    def get_latest_executor_accounting(
        self, executor_id: str
    ) -> tuple[ExecutorAccountingObservation, float] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM executor_accounting_observations WHERE executor_id = ? "
                "ORDER BY observation_id DESC LIMIT 1", (executor_id,),
            ).fetchone()
        if row is None:
            return None
        optional = lambda name: None if row[name] is None else Decimal(row[name])
        observation = ExecutorAccountingObservation(
            executor_id=row["executor_id"], executor_type=row["executor_type"],
            status=row["status"], close_type=row["close_type"],
            filled_amount_quote=Decimal(row["filled_amount_quote"]),
            cum_fees_quote=Decimal(row["cum_fees_quote"]),
            position_size_quote=optional("position_size_quote"),
            net_pnl_quote=optional("net_pnl_quote"),
            realized_pnl_quote=optional("realized_pnl_quote"),
        )
        return observation, float(row["observed_at"])

    @staticmethod
    def _position_hold_scope(
        *,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str,
    ) -> tuple[str, str, str, str]:
        values = {
            "account_name": account_name,
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "controller_id": controller_id,
        }

        normalized: dict[str, str] = {}

        for field, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
            normalized[field] = value.strip()

        normalized["trading_pair"] = normalized["trading_pair"].upper()

        return (
            normalized["account_name"],
            normalized["connector_name"],
            normalized["trading_pair"],
            normalized["controller_id"],
        )

    def record_position_hold_snapshot(
        self,
        *,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str,
        holds: tuple[PositionHoldAccounting, ...],
        observed_at: float | None = None,
    ) -> int:
        """Persist one successful exact-scope ACTIVE PositionHold fetch."""

        scope = self._position_hold_scope(
            account_name=account_name,
            connector_name=connector_name,
            trading_pair=trading_pair,
            controller_id=controller_id,
        )

        if not isinstance(holds, tuple):
            raise ValueError("holds must be a tuple")

        if len(holds) > 1:
            raise ValueError(
                "exact PositionHold scope cannot contain multiple ACTIVE rows"
            )

        timestamp = time.time() if observed_at is None else observed_at

        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0
        ):
            raise ValueError(
                "observed_at must be a finite non-negative timestamp"
            )

        timestamp = float(timestamp)

        validated: list[PositionHoldAccounting] = []

        for hold in holds:
            if not isinstance(hold, PositionHoldAccounting):
                raise ValueError(
                    "holds must contain PositionHoldAccounting values"
                )

            raw = {
                "account_name": hold.account_name,
                "connector_name": hold.connector_name,
                "trading_pair": hold.trading_pair,
                "controller_id": hold.controller_id,
                "status": hold.status,
                "buy_amount_base": str(hold.buy_amount_base),
                "buy_amount_quote": str(hold.buy_amount_quote),
                "sell_amount_base": str(hold.sell_amount_base),
                "sell_amount_quote": str(hold.sell_amount_quote),
                "realized_pnl_quote": str(hold.realized_pnl_quote),
                "cum_fees_quote": str(hold.cum_fees_quote),
                "executor_ids": list(hold.executor_ids),
                "last_updated": hold.last_updated,
            }

            validated.append(
                position_hold_accounting_from_raw(
                    raw,
                    expected_account_name=scope[0],
                    expected_connector_name=scope[1],
                    expected_trading_pair=scope[2],
                    expected_controller_id=scope[3],
                )
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO position_hold_snapshots (
                        account_name,
                        connector_name,
                        trading_pair,
                        controller_id,
                        observed_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (*scope, timestamp),
                )

                snapshot_id = int(cursor.lastrowid)

                for hold_index, hold in enumerate(validated):
                    conn.execute(
                        """
                        INSERT INTO position_hold_snapshot_rows (
                            snapshot_id,
                            hold_index,
                            buy_amount_base,
                            buy_amount_quote,
                            sell_amount_base,
                            sell_amount_quote,
                            realized_pnl_quote,
                            cum_fees_quote,
                            executor_ids_json,
                            last_updated
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            hold_index,
                            str(hold.buy_amount_base),
                            str(hold.buy_amount_quote),
                            str(hold.sell_amount_base),
                            str(hold.sell_amount_quote),
                            str(hold.realized_pnl_quote),
                            str(hold.cum_fees_quote),
                            json.dumps(
                                list(hold.executor_ids),
                                separators=(",", ":"),
                            ),
                            hold.last_updated,
                        ),
                    )

                conn.commit()
                return snapshot_id

            except Exception:
                conn.rollback()
                raise

    def _fresh_position_hold_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str,
        now: float | None = None,
    ) -> tuple[PositionHoldAccounting, ...] | None:
        """Return fresh validated evidence, preserving empty vs missing."""

        scope = self._position_hold_scope(
            account_name=account_name,
            connector_name=connector_name,
            trading_pair=trading_pair,
            controller_id=controller_id,
        )

        current_time = time.time() if now is None else now

        if (
            isinstance(current_time, bool)
            or not isinstance(current_time, (int, float))
            or not math.isfinite(float(current_time))
        ):
            raise ValueError("now must be a finite timestamp")

        current_time = float(current_time)

        snapshot = conn.execute(
            """
            SELECT snapshot_id, observed_at
            FROM position_hold_snapshots
            WHERE account_name = ?
              AND connector_name = ?
              AND trading_pair = ?
              AND controller_id = ?
            ORDER BY observed_at DESC, snapshot_id DESC
            LIMIT 1
            """,
            scope,
        ).fetchone()

        if snapshot is None:
            return None

        observed_at = float(snapshot["observed_at"])

        if not math.isfinite(observed_at):
            raise RuntimeError(
                "persisted PositionHold snapshot timestamp is invalid"
            )

        age = current_time - observed_at

        # Future-dated or stale evidence is never allowed to reduce exposure.
        if (
            age < 0
            or age > self.policy.position_hold_snapshot_ttl_seconds
        ):
            return None

        rows = conn.execute(
            """
            SELECT *
            FROM position_hold_snapshot_rows
            WHERE snapshot_id = ?
            ORDER BY hold_index
            """,
            (snapshot["snapshot_id"],),
        ).fetchall()

        if len(rows) > 1:
            raise RuntimeError(
                "persisted exact PositionHold snapshot contains "
                "multiple ACTIVE rows"
            )

        holds: list[PositionHoldAccounting] = []

        for row in rows:
            try:
                executor_ids = json.loads(row["executor_ids_json"])
            except Exception as exc:
                raise RuntimeError(
                    "persisted PositionHold executor_ids are invalid"
                ) from exc

            raw = {
                "account_name": scope[0],
                "connector_name": scope[1],
                "trading_pair": scope[2],
                "controller_id": scope[3],
                "status": "ACTIVE",
                "buy_amount_base": row["buy_amount_base"],
                "buy_amount_quote": row["buy_amount_quote"],
                "sell_amount_base": row["sell_amount_base"],
                "sell_amount_quote": row["sell_amount_quote"],
                "realized_pnl_quote": row["realized_pnl_quote"],
                "cum_fees_quote": row["cum_fees_quote"],
                "executor_ids": executor_ids,
                "last_updated": row["last_updated"],
            }

            holds.append(
                position_hold_accounting_from_raw(
                    raw,
                    expected_account_name=scope[0],
                    expected_connector_name=scope[1],
                    expected_trading_pair=scope[2],
                    expected_controller_id=scope[3],
                )
            )

        return tuple(holds)

    def get_fresh_position_hold_snapshot(
        self,
        *,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str,
        now: float | None = None,
    ) -> tuple[PositionHoldAccounting, ...] | None:
        """Read the latest fresh exact-scope PositionHold snapshot."""

        with self._connect() as conn:
            return self._fresh_position_hold_snapshot(
                conn,
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair,
                controller_id=controller_id,
                now=now,
            )

    def _recovery_recycle_release_quote(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
    ) -> Decimal:
        rows = conn.execute(
            """
            SELECT amount_quote
            FROM recovery_recycle_releases
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchall()
        return sum(
            (Decimal(str(row["amount_quote"])) for row in rows),
            ZERO,
        )

    def record_recovery_recycle_release(
        self,
        *,
        release_id: str,
        proposal_id: str,
        amount_quote: Decimal,
    ) -> Decimal:
        """Release only proven RECOVERY basis monetized by partial recycling.

        This does not mark an executor flat. It reduces the Governor's recovery
        exposure by exact cost basis that the economic ledger has already
        consumed through confirmed SELL fills.
        """

        rid = str(release_id).strip()
        pid = str(proposal_id).strip()
        if not rid or not pid:
            raise ValueError("release_id and proposal_id are required")
        self._validate_accounting_decimal(
            amount_quote,
            name="amount_quote",
        )
        if amount_quote <= ZERO:
            raise ValueError("amount_quote must be positive")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                provenance = conn.execute(
                    """
                    SELECT intent, authorized_quote
                    FROM execution_provenance
                    WHERE proposal_id = ?
                    """,
                    (pid,),
                ).fetchone()
                if provenance is None:
                    raise ValueError(
                        "recycle release requires exact execution provenance"
                    )
                if str(provenance["intent"]) != IntentKind.RECOVERY.value:
                    raise ValueError(
                        "only RECOVERY capital may receive recycle release"
                    )

                existing = conn.execute(
                    """
                    SELECT proposal_id, amount_quote
                    FROM recovery_recycle_releases
                    WHERE release_id = ?
                    """,
                    (rid,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["proposal_id"]) != pid
                        or Decimal(str(existing["amount_quote"])) != amount_quote
                    ):
                        raise RuntimeError(
                            "recovery recycle release replay conflicts"
                        )
                    conn.commit()
                    return self._recovery_recycle_release_quote(conn, pid)

                released = self._recovery_recycle_release_quote(conn, pid)
                authorized = Decimal(str(provenance["authorized_quote"]))
                if released + amount_quote > authorized:
                    raise ValueError(
                        "recycle release exceeds authorized recovery capital"
                    )

                conn.execute(
                    """
                    INSERT INTO recovery_recycle_releases (
                        release_id, proposal_id, amount_quote, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (rid, pid, str(amount_quote), time.time()),
                )
                conn.commit()
                return released + amount_quote
            except Exception:
                conn.rollback()
                raise

    def _attach_recovery_exposure_ledgers(
        self, conn: sqlite3.Connection
    ) -> dict[str, str]:
        """Attach scoped ledgers before the governor/ledger write-lock boundary.

        An unavailable or unattachable ledger is omitted deliberately: its
        scoped owner retains full authorization in the exposure calculation.
        """
        schemas = {
            str(Path(row["file"]).resolve()): str(row["name"])
            for row in conn.execute("PRAGMA database_list").fetchall()
            if row["file"]
        }
        rows = conn.execute(
            "SELECT DISTINCT ledger_path FROM position_submission_bindings"
        ).fetchall()
        for index, row in enumerate(rows):
            path = Path(row["ledger_path"]).resolve()
            if str(path) in schemas or not path.is_file():
                continue
            schema = f"recovery_ledger_{index}"
            try:
                conn.execute(f"ATTACH DATABASE ? AS {schema}", (str(path),))
            except sqlite3.Error:
                continue
            schemas[str(path)] = schema
        return schemas

    def _scoped_recovery_basis_floor(
        self,
        conn: sqlite3.Connection,
        provenance: ExecutionProvenance,
        recovery_ledgers: dict[str, str],
    ) -> Decimal:
        """Preserve exact remaining lot basis for guarded RECOVERY ownership."""
        binding = conn.execute(
            "SELECT * FROM position_submission_bindings WHERE proposal_id = ?",
            (provenance.proposal_id,),
        ).fetchone()
        if binding is None:
            # Historical unbound executions keep their existing semantics.
            return ZERO
        fallback = provenance.authorized_quote
        exact_scope = (
            binding["executor_id"] == provenance.executor_id
            and binding["pair"] == provenance.pair
            and binding["account_name"] == provenance.account_name
            and binding["connector_name"] == provenance.connector_name
            and binding["controller_id"] == provenance.controller_id
            and binding["state"] == "DISPATCHED"
        )
        schema = recovery_ledgers.get(str(Path(binding["ledger_path"]).resolve()))
        if not exact_scope or schema is None:
            return fallback
        try:
            position = conn.execute(
                f"SELECT pair, base_asset, status FROM {schema}.economic_positions "
                "WHERE position_id = ?",
                (binding["position_id"],),
            ).fetchone()
            if (
                position is None
                or position["pair"] != binding["pair"]
                or position["base_asset"] != binding["base_asset"]
                or position["status"] not in {"ACTIVE", "RESIDUAL", "CLOSED"}
            ):
                return fallback
            # Inspect every lot for this owner so a conflicting position or
            # executor can never be silently excluded by a narrow SQL filter.
            lots = conn.execute(
                f"SELECT * FROM {schema}.economic_lots WHERE proposal_id = ?",
                (provenance.proposal_id,),
            ).fetchall()
            basis = ZERO
            for lot in lots:
                if (
                    lot["position_id"] != binding["position_id"]
                    or lot["pair"] != binding["pair"]
                    or lot["executor_id"] != provenance.executor_id
                    or lot["source"] != "VCAP_RECOVERY"
                    or lot["ownership"] != "ACTIVE_TRADE"
                ):
                    return fallback
                remaining = Decimal(lot["cost_basis_remaining_quote"])
                if not remaining.is_finite() or remaining < ZERO:
                    return fallback
                basis += remaining
            return basis
        except (sqlite3.Error, ValueError, TypeError, ArithmeticError):
            return fallback

    def _current_recovery_exposure_quote(
        self,
        conn: sqlite3.Connection,
        recovery_ledgers: dict[str, str],
    ) -> Decimal:
        """Compute reconciled RECOVERY exposure using one DB transaction."""
        rows = conn.execute(
            """
            SELECT
                p.proposal_id,
                p.executor_id,
                p.pair,
                p.intent,
                p.authorized_quote,
                p.leverage,
                p.account_name,
                p.connector_name,
                p.controller_id,

                o.observation_id,
                o.executor_type AS obs_executor_type,
                o.status AS obs_status,
                o.close_type AS obs_close_type,
                o.filled_amount_quote AS obs_filled_amount_quote,
                o.cum_fees_quote AS obs_cum_fees_quote,
                o.position_size_quote AS obs_position_size_quote,
                o.net_pnl_quote AS obs_net_pnl_quote,
                o.realized_pnl_quote AS obs_realized_pnl_quote
            FROM execution_provenance AS p
            LEFT JOIN executor_accounting_observations AS o
              ON o.observation_id = (
                  SELECT MAX(latest.observation_id)
                  FROM executor_accounting_observations AS latest
                  WHERE latest.executor_id = p.executor_id
              )
            WHERE p.intent = ?
            ORDER BY p.proposal_id
            """,
            (IntentKind.RECOVERY.value,),
        ).fetchall()

        total = ZERO

        for row in rows:
            provenance = ExecutionProvenance(
                proposal_id=str(row["proposal_id"]),
                executor_id=str(row["executor_id"]),
                pair=str(row["pair"]),
                intent=IntentKind(str(row["intent"])),
                authorized_quote=Decimal(str(row["authorized_quote"])),
                leverage=Decimal(str(row["leverage"])),
                account_name=(
                    None
                    if row["account_name"] is None
                    else str(row["account_name"])
                ),
                connector_name=(
                    None
                    if row["connector_name"] is None
                    else str(row["connector_name"])
                ),
                controller_id=(
                    None
                    if row["controller_id"] is None
                    else str(row["controller_id"])
                ),
            )
            basis_floor = self._scoped_recovery_basis_floor(
                conn, provenance, recovery_ledgers
            )

            if row["observation_id"] is None:
                released = self._recovery_recycle_release_quote(
                    conn,
                    provenance.proposal_id,
                )
                total += max(
                    provenance.authorized_quote - released,
                    basis_floor,
                    ZERO,
                )
                continue

            observation = ExecutorAccountingObservation(
                executor_id=str(row["executor_id"]),
                executor_type=(
                    None
                    if row["obs_executor_type"] is None
                    else str(row["obs_executor_type"])
                ),
                status=str(row["obs_status"]),
                close_type=(
                    None
                    if row["obs_close_type"] is None
                    else str(row["obs_close_type"])
                ),
                filled_amount_quote=Decimal(
                    str(row["obs_filled_amount_quote"])
                ),
                cum_fees_quote=Decimal(
                    str(row["obs_cum_fees_quote"])
                ),
                position_size_quote=(
                    None
                    if row["obs_position_size_quote"] is None
                    else Decimal(str(row["obs_position_size_quote"]))
                ),
                net_pnl_quote=(
                    None
                    if row["obs_net_pnl_quote"] is None
                    else Decimal(str(row["obs_net_pnl_quote"]))
                ),
                realized_pnl_quote=(
                    None
                    if row["obs_realized_pnl_quote"] is None
                    else Decimal(str(row["obs_realized_pnl_quote"]))
                ),
            )

            assessment = assess_recovery_exposure(
                observation,
                provenance,
            )

            if assessment.requires_position_hold_reconciliation:
                scope = (
                    provenance.account_name,
                    provenance.connector_name,
                    provenance.controller_id,
                )

                # Historical provenance predates exact execution scope. It may
                # never use a PositionHold snapshot to reduce exposure.
                if any(value is None for value in scope):
                    released = self._recovery_recycle_release_quote(
                        conn,
                        provenance.proposal_id,
                    )
                    total += max(
                        assessment.exposure_quote - released,
                        basis_floor,
                        ZERO,
                    )
                    continue

                holds = self._fresh_position_hold_snapshot(
                    conn,
                    account_name=provenance.account_name,
                    connector_name=provenance.connector_name,
                    trading_pair=provenance.pair,
                    controller_id=provenance.controller_id,
                )

                # Missing/stale evidence and even a fresh empty exact scope
                # remain fail-closed through the reconciliation helper.
                if holds is not None:
                    assessment = (
                        reconcile_recovery_exposure_with_position_holds(
                            observation,
                            provenance,
                            holds,
                        )
                    )

            released = self._recovery_recycle_release_quote(
                conn,
                provenance.proposal_id,
            )
            total += max(assessment.exposure_quote - released, basis_floor, ZERO)

        return total

    def current_recovery_exposure_quote(self) -> Decimal:
        """Return current quote capital attributable to RECOVERY executors."""
        with self._connect() as conn:
            recovery_ledgers = self._attach_recovery_exposure_ledgers(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                exposure = self._current_recovery_exposure_quote(conn, recovery_ledgers)
                conn.commit()
                return exposure
            except Exception:
                conn.rollback()
                raise

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
