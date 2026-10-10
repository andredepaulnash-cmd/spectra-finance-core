from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, localcontext
from enum import StrEnum
from math import isfinite
from pathlib import Path
from time import time as wall_time

from .capital_governor import CapitalGovernor
from .decimal_math import exact_decimal_difference, exact_decimal_sum
from .economic_position import (
    EconomicLot,
    EconomicLotSource,
    EconomicPositionLedger,
    EconomicPositionStatus,
    OwnershipBucket,
)
from .kraken_market_fabric import KrakenTradingRule
from .models import IntentKind

ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and result <= ZERO:
        raise ValueError(f"{field} must be positive")
    if non_negative and result < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return result


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


class PartialRecycleAction(StrEnum):
    NONE = "NONE"
    REPAIR_PENDING = "REPAIR_PENDING"
    RECYCLE = "RECYCLE"


class PartialRecycleManifestStatus(StrEnum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class PartialRecyclePolicy:
    target_net_profit_bps: Decimal = Decimal("174")

    def __post_init__(self) -> None:
        target = _decimal(
            self.target_net_profit_bps,
            "target_net_profit_bps",
            non_negative=True,
        )
        object.__setattr__(self, "target_net_profit_bps", target)


@dataclass(frozen=True)
class PartialRecycleOwner:
    proposal_id: str
    executor_id: str
    lot_ids: tuple[str, ...]
    target_base: Decimal
    target_cost_basis_quote: Decimal
    target_entry_fees_quote: Decimal


@dataclass(frozen=True)
class PartialRecycleManifest:
    manifest_id: str
    position_id: str
    pair: str
    status: PartialRecycleManifestStatus
    owners: tuple[PartialRecycleOwner, ...]
    target_base: Decimal
    target_cost_basis_quote: Decimal
    target_entry_fees_quote: Decimal
    reference_price: Decimal
    min_order_size: Decimal
    min_notional_quote: Decimal
    expected_exit_cost_bps: Decimal
    estimated_net_pnl_quote: Decimal
    estimated_net_return_bps: Decimal
    sell_executor_id: str
    created_at: float
    completed_at: float | None
    # Legacy rows have no recorded protection and cannot dispatch a new sale.
    target_net_profit_bps: Decimal | None = None
    protected_limit_price: Decimal | None = None
    cash_proceeds_route: str | None = None
    predecessor_manifest_id: str | None = None

    @property
    def lot_ids(self) -> tuple[str, ...]:
        return tuple(lot_id for owner in self.owners for lot_id in owner.lot_ids)

    @property
    def proposal_ids(self) -> tuple[str, ...]:
        return tuple(owner.proposal_id for owner in self.owners)


@dataclass(frozen=True)
class PartialRecycleDecision:
    action: PartialRecycleAction
    manifest: PartialRecycleManifest | None
    repair_lot_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PartialRecycleProgress:
    manifest_id: str
    remaining_base: Decimal
    realized_pnl_quote: Decimal
    completed: bool
    terminal: bool = False


@dataclass(frozen=True)
class PartialRecycleDispatchGuard:
    allowed: bool
    sell_base: Decimal
    cost_basis_quote: Decimal
    entry_fees_quote: Decimal
    expected_exit_cost_bps: Decimal
    target_net_profit_bps: Decimal
    estimated_net_pnl_quote: Decimal
    estimated_net_return_bps: Decimal
    reasons: tuple[str, ...]


class PartialRecycleManifestConflict(ValueError):
    """A concurrent or uncertified lifecycle prevents a new partial scope."""


def _whole_exit_blocks_partial(connection, position_id):
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "economic_exit_manifests" not in tables:
        return False
    claims_exist = "economic_exit_dispatch_claims" in tables
    from .economic_exit import economic_exit_terminal_certificate

    for manifest in connection.execute(
        "SELECT manifest_id, status FROM economic_exit_manifests WHERE position_id = ?",
        (position_id,),
    ).fetchall():
        claimed = (
            claims_exist
            and connection.execute(
                "SELECT 1 FROM economic_exit_dispatch_claims WHERE manifest_id = ?",
                (manifest["manifest_id"],),
            ).fetchone()
            is not None
        )
        if claimed:
            if (
                economic_exit_terminal_certificate(
                    connection, str(manifest["manifest_id"]), schema="main"
                )
                is None
            ):
                return True
        elif str(manifest["status"]) == "OPEN":
            return True
    return False


class PartialRecycleStore:
    """Durable partial-recycle manifests and exact realized fill journal."""

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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS partial_recycle_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owners_json TEXT NOT NULL,
                    target_base TEXT NOT NULL,
                    target_cost_basis_quote TEXT NOT NULL,
                    target_entry_fees_quote TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    min_order_size TEXT NOT NULL,
                    min_notional_quote TEXT NOT NULL,
                    expected_exit_cost_bps TEXT NOT NULL,
                    estimated_net_pnl_quote TEXT NOT NULL,
                    estimated_net_return_bps TEXT NOT NULL,
                    sell_executor_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    target_net_profit_bps TEXT,
                    protected_limit_price TEXT,
                    cash_proceeds_route TEXT,
                    predecessor_manifest_id TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    partial_recycle_open_position_unique
                ON partial_recycle_manifests(position_id)
                WHERE status = 'OPEN';

                CREATE TABLE IF NOT EXISTS partial_recycle_fills (
                    trade_id TEXT PRIMARY KEY,
                    manifest_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    base_sold TEXT NOT NULL,
                    gross_proceeds_quote TEXT NOT NULL,
                    exit_fee_quote TEXT NOT NULL,
                    realized_pnl_quote TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    FOREIGN KEY(manifest_id)
                        REFERENCES partial_recycle_manifests(manifest_id)
                );

                CREATE TABLE IF NOT EXISTS partial_recycle_dispatch_claims (
                    manifest_id TEXT PRIMARY KEY,
                    claimed_at REAL NOT NULL,
                    terminal_reconciled_at REAL,
                    terminal_proof_json TEXT,
                    FOREIGN KEY(manifest_id)
                        REFERENCES partial_recycle_manifests(manifest_id)
                );
                """)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(partial_recycle_manifests)")
            }
            for name in (
                "target_net_profit_bps",
                "protected_limit_price",
                "cash_proceeds_route",
                "predecessor_manifest_id",
            ):
                if name not in columns:
                    # No guessed profit policy or cash purpose for legacy rows.
                    conn.execute(
                        f"ALTER TABLE partial_recycle_manifests ADD COLUMN {name} TEXT"
                    )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS partial_recycle_protection_conflicts (
                    manifest_id TEXT NOT NULL,
                    conflict TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    PRIMARY KEY (manifest_id, conflict)
                )
                """)
            claim_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(partial_recycle_dispatch_claims)"
                )
            }
            for name, sql_type in (
                ("terminal_reconciled_at", "REAL"),
                ("terminal_proof_json", "TEXT"),
            ):
                if name not in claim_columns:
                    conn.execute(
                        f"ALTER TABLE partial_recycle_dispatch_claims ADD COLUMN {name} {sql_type}"
                    )

    @staticmethod
    def _owners_json(owners: tuple[PartialRecycleOwner, ...]) -> str:
        return json.dumps(
            [
                {
                    "proposal_id": owner.proposal_id,
                    "executor_id": owner.executor_id,
                    "lot_ids": list(owner.lot_ids),
                    "target_base": str(owner.target_base),
                    "target_cost_basis_quote": str(owner.target_cost_basis_quote),
                    "target_entry_fees_quote": str(owner.target_entry_fees_quote),
                }
                for owner in owners
            ],
            sort_keys=True,
        )

    @staticmethod
    def _owners_from_json(raw: str) -> tuple[PartialRecycleOwner, ...]:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise RuntimeError("corrupt partial recycle owner manifest")
        owners: list[PartialRecycleOwner] = []
        for row in parsed:
            if not isinstance(row, dict):
                raise RuntimeError("corrupt partial recycle owner row")
            lot_ids = row.get("lot_ids")
            if not isinstance(lot_ids, list) or not lot_ids:
                raise RuntimeError("partial recycle owner has no lots")
            owners.append(
                PartialRecycleOwner(
                    proposal_id=_text(row.get("proposal_id"), "proposal_id"),
                    executor_id=_text(row.get("executor_id"), "executor_id"),
                    lot_ids=tuple(_text(value, "lot_id") for value in lot_ids),
                    target_base=_decimal(
                        row.get("target_base"),
                        "target_base",
                        positive=True,
                    ),
                    target_cost_basis_quote=_decimal(
                        row.get("target_cost_basis_quote"),
                        "target_cost_basis_quote",
                        positive=True,
                    ),
                    target_entry_fees_quote=_decimal(
                        row.get("target_entry_fees_quote"),
                        "target_entry_fees_quote",
                        non_negative=True,
                    ),
                )
            )
        return tuple(owners)

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> PartialRecycleManifest:
        return PartialRecycleManifest(
            manifest_id=str(row["manifest_id"]),
            position_id=str(row["position_id"]),
            pair=str(row["pair"]),
            status=PartialRecycleManifestStatus(str(row["status"])),
            owners=cls._owners_from_json(str(row["owners_json"])),
            target_base=Decimal(str(row["target_base"])),
            target_cost_basis_quote=Decimal(str(row["target_cost_basis_quote"])),
            target_entry_fees_quote=Decimal(str(row["target_entry_fees_quote"])),
            reference_price=Decimal(str(row["reference_price"])),
            min_order_size=Decimal(str(row["min_order_size"])),
            min_notional_quote=Decimal(str(row["min_notional_quote"])),
            expected_exit_cost_bps=Decimal(str(row["expected_exit_cost_bps"])),
            estimated_net_pnl_quote=Decimal(str(row["estimated_net_pnl_quote"])),
            estimated_net_return_bps=Decimal(str(row["estimated_net_return_bps"])),
            sell_executor_id=str(row["sell_executor_id"]),
            created_at=float(row["created_at"]),
            completed_at=(
                None if row["completed_at"] is None else float(row["completed_at"])
            ),
            target_net_profit_bps=(
                None
                if row["target_net_profit_bps"] is None
                else Decimal(str(row["target_net_profit_bps"]))
            ),
            protected_limit_price=(
                None
                if row["protected_limit_price"] is None
                else Decimal(str(row["protected_limit_price"]))
            ),
            predecessor_manifest_id=(
                None
                if row["predecessor_manifest_id"] is None
                else str(row["predecessor_manifest_id"])
            ),
            cash_proceeds_route=row["cash_proceeds_route"],
        )

    def load(self, manifest_id: str) -> PartialRecycleManifest | None:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM partial_recycle_manifests WHERE manifest_id = ?",
                (mid,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def open_for_position(
        self,
        position_id: str,
    ) -> PartialRecycleManifest | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM partial_recycle_manifests
                WHERE position_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pid, PartialRecycleManifestStatus.OPEN.value),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def latest_for_position(self, position_id: str) -> PartialRecycleManifest | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM partial_recycle_manifests WHERE position_id = ? ORDER BY rowid DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def create(
        self,
        manifest: PartialRecycleManifest,
    ) -> PartialRecycleManifest:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT * FROM partial_recycle_manifests
                    WHERE manifest_id = ?
                    """,
                    (manifest.manifest_id,),
                ).fetchone()
                if existing is not None:
                    current = self._from_row(existing)
                    if current != manifest:
                        raise RuntimeError("partial recycle manifest replay conflicts")
                    conn.rollback()
                    return current
                if _whole_exit_blocks_partial(conn, manifest.position_id):
                    raise PartialRecycleManifestConflict(
                        "open_whole_exit_blocks_partial_manifest_creation"
                    )
                previous = conn.execute(
                    "SELECT * FROM partial_recycle_manifests "
                    "WHERE position_id = ? ORDER BY rowid DESC LIMIT 1",
                    (manifest.position_id,),
                ).fetchone()
                predecessor = manifest.predecessor_manifest_id
                if previous is None:
                    if predecessor is not None:
                        raise PartialRecycleManifestConflict(
                            "partial_generation_predecessor_changed"
                        )
                elif (
                    predecessor != str(previous["manifest_id"])
                    or str(previous["status"]) not in {"TERMINAL", "COMPLETED"}
                    or partial_dispatch_terminal_certificate(
                        conn, str(previous["manifest_id"]), schema="main"
                    )
                    is None
                ):
                    raise PartialRecycleManifestConflict(
                        "terminal_proof_required_for_new_partial_generation"
                    )
                # Historical uncertain creates cannot be bypassed by installing
                # a different owner scope or terminal generation.
                for claim in conn.execute(
                    "SELECT c.manifest_id FROM partial_recycle_dispatch_claims c "
                    "JOIN partial_recycle_manifests m USING (manifest_id) "
                    "WHERE m.position_id = ?",
                    (manifest.position_id,),
                ).fetchall():
                    if (
                        partial_dispatch_terminal_certificate(
                            conn, str(claim["manifest_id"]), schema="main"
                        )
                        is None
                    ):
                        raise PartialRecycleManifestConflict(
                            "uncertain_partial_dispatch_blocks_new_generation"
                        )
                conn.execute(
                    """
                    INSERT INTO partial_recycle_manifests (
                        manifest_id, position_id, pair, status, owners_json,
                        target_base, target_cost_basis_quote,
                        target_entry_fees_quote, reference_price,
                        min_order_size, min_notional_quote,
                        expected_exit_cost_bps, estimated_net_pnl_quote,
                        estimated_net_return_bps, sell_executor_id,
                        created_at, completed_at, target_net_profit_bps,
                        protected_limit_price, cash_proceeds_route, predecessor_manifest_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        manifest.manifest_id,
                        manifest.position_id,
                        manifest.pair,
                        manifest.status.value,
                        self._owners_json(manifest.owners),
                        str(manifest.target_base),
                        str(manifest.target_cost_basis_quote),
                        str(manifest.target_entry_fees_quote),
                        str(manifest.reference_price),
                        str(manifest.min_order_size),
                        str(manifest.min_notional_quote),
                        str(manifest.expected_exit_cost_bps),
                        str(manifest.estimated_net_pnl_quote),
                        str(manifest.estimated_net_return_bps),
                        manifest.sell_executor_id,
                        manifest.created_at,
                        (
                            None
                            if manifest.target_net_profit_bps is None
                            else str(manifest.target_net_profit_bps)
                        ),
                        (
                            None
                            if manifest.protected_limit_price is None
                            else str(manifest.protected_limit_price)
                        ),
                        manifest.cash_proceeds_route,
                        manifest.predecessor_manifest_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        created = self.load(manifest.manifest_id)
        if created is None:
            raise RuntimeError("partial recycle manifest creation failed")
        return created

    def dispatch_claimed(self, manifest_id: str) -> bool:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM partial_recycle_dispatch_claims WHERE manifest_id = ?",
                (mid,),
            ).fetchone()
        return row is not None

    def claim_dispatch(
        self,
        manifest: PartialRecycleManifest,
        *,
        now: float,
        ledger: EconomicPositionLedger,
        governor: CapitalGovernor,
        trading_rule: KrakenTradingRule,
        current_price: Decimal,
        expected_exit_cost_bps: Decimal | None = None,
        market_observed_at: float | None = None,
    ) -> PartialRecycleDispatchGuard | None:
        """Atomically recheck exact ownership and claim one external create.

        Return a denied guard without claiming, or None for a previous claim.
        The caller must create immediately, without another external await.
        """
        if self.path.resolve() != ledger.path.resolve():
            raise ValueError("partial dispatch claim must share the economic ledger")
        mid = _text(manifest.manifest_id, "manifest_id")
        timestamp = _time(now, "now")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM partial_recycle_manifests WHERE manifest_id = ?",
                (mid,),
            ).fetchone()
            if row is None or str(row["status"]) != "OPEN":
                raise RuntimeError("dispatch requires an open durable manifest")
            if self._from_row(row) != manifest:
                raise RuntimeError("partial recycle immutable durable manifest changed")
            if (
                conn.execute(
                    "SELECT 1 FROM partial_recycle_dispatch_claims WHERE manifest_id = ?",
                    (mid,),
                ).fetchone()
                is not None
            ):
                return None
            guard = assess_partial_recycle_dispatch(
                manifest,
                ledger=ledger,
                governor=governor,
                trading_rule=trading_rule,
                current_price=current_price,
                expected_exit_cost_bps=expected_exit_cost_bps,
                _connection=conn,
            )
            market_now = wall_time()
            if market_observed_at is None:
                guard = replace(
                    guard,
                    allowed=False,
                    reasons=(*guard.reasons, "market_observation_missing"),
                )
            else:
                observed_at = _time(market_observed_at, "market_observed_at")
                if observed_at > market_now or market_now - observed_at > 30.0:
                    guard = replace(
                        guard,
                        allowed=False,
                        reasons=(*guard.reasons, "market_observation_stale_or_future"),
                    )
            if _whole_exit_blocks_partial(conn, manifest.position_id):
                guard = replace(
                    guard,
                    allowed=False,
                    reasons=(*guard.reasons, "open_whole_exit_blocks_partial_dispatch"),
                )
            if not guard.allowed:
                return guard
            conn.execute(
                "INSERT INTO partial_recycle_dispatch_claims "
                "(manifest_id, claimed_at) VALUES (?, ?)",
                (mid, timestamp),
            )
            conn.commit()
        return guard

    def record_terminal_dispatch(
        self, manifest_id: str, *, proof: dict, now: float
    ) -> None:
        """Certify terminal order/fill evidence against the durable fill journal.

        Caller has fetched the exact sell executor and cumulative evidence.
        Certification never expires the one-shot create claim.
        """
        mid = _text(manifest_id, "manifest_id")
        timestamp = _time(now, "now")
        payload = json.dumps(proof, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM partial_recycle_manifests WHERE manifest_id = ?", (mid,)
            ).fetchone()
            if row is None or proof.get("executor_id") != str(row["sell_executor_id"]):
                raise RuntimeError("partial terminal proof executor mismatch")
            manifest = self._from_row(row)
            if (
                conn.execute(
                    "SELECT 1 FROM partial_recycle_protection_conflicts WHERE manifest_id = ?",
                    (mid,),
                ).fetchone()
                is not None
            ):
                raise RuntimeError(
                    "partial recycle profit protection conflict requires resolution"
                )
            journal = conn.execute(
                "SELECT * FROM partial_recycle_fills WHERE manifest_id = ?", (mid,)
            ).fetchall()
            _validate_terminal_proof(
                proof,
                mid,
                manifest.sell_executor_id,
                journal,
                protected_limit_price=manifest.protected_limit_price,
                cash_proceeds_route=manifest.cash_proceeds_route,
            )
            claim = conn.execute(
                "SELECT * FROM partial_recycle_dispatch_claims WHERE manifest_id = ?",
                (mid,),
            ).fetchone()
            if (
                claim is not None
                and claim["terminal_proof_json"] is not None
                and str(claim["terminal_proof_json"]) != payload
            ):
                raise RuntimeError("partial terminal certificate replay conflicts")
            conn.execute(
                "INSERT OR IGNORE INTO partial_recycle_dispatch_claims (manifest_id, claimed_at) VALUES (?, ?)",
                (mid, timestamp),
            )
            conn.execute(
                "UPDATE partial_recycle_dispatch_claims SET terminal_reconciled_at = COALESCE(terminal_reconciled_at, ?), terminal_proof_json = ? WHERE manifest_id = ?",
                (timestamp, payload, mid),
            )
            remaining = exact_decimal_sum(
                (
                    lot.base_remaining
                    for lot in _manifest_active_lots_from_connection(conn, manifest)
                    if lot.lot_id in manifest.lot_ids
                ),
            )
            status = (
                PartialRecycleManifestStatus.COMPLETED
                if remaining == ZERO
                else PartialRecycleManifestStatus.TERMINAL
            )
            conn.execute(
                "UPDATE partial_recycle_manifests SET status = ?, completed_at = COALESCE(completed_at, ?) WHERE manifest_id = ?",
                (status.value, timestamp, mid),
            )
            conn.commit()

    def terminal_proof(self, manifest_id: str) -> dict | None:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            return partial_dispatch_terminal_certificate(conn, mid, schema="main")

    def protection_conflicts(self, manifest_id: str) -> tuple[str, ...]:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conflict FROM partial_recycle_protection_conflicts "
                "WHERE manifest_id = ? ORDER BY conflict",
                (mid,),
            ).fetchall()
        return tuple(str(row["conflict"]) for row in rows)

    def record_protection_conflict(
        self, *, manifest_id: str, conflict: str, now: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO partial_recycle_protection_conflicts VALUES (?, ?, ?)",
                (
                    _text(manifest_id, "manifest_id"),
                    _text(conflict, "conflict"),
                    _time(now, "now"),
                ),
            )

    def fill_recorded(self, trade_id: str) -> bool:
        tid = _text(trade_id, "trade_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM partial_recycle_fills WHERE trade_id = ?",
                (tid,),
            ).fetchone()
        return row is not None

    def record_fill(
        self,
        *,
        trade_id: str,
        manifest_id: str,
        order_id: str,
        base_sold: Decimal,
        gross_proceeds_quote: Decimal,
        exit_fee_quote: Decimal,
        realized_pnl_quote: Decimal,
        occurred_at: float,
    ) -> None:
        tid = _text(trade_id, "trade_id")
        mid = _text(manifest_id, "manifest_id")
        oid = _text(order_id, "order_id")
        timestamp = _time(occurred_at, "occurred_at")
        values = (
            mid,
            oid,
            str(_decimal(base_sold, "base_sold", positive=True)),
            str(
                _decimal(
                    gross_proceeds_quote,
                    "gross_proceeds_quote",
                    non_negative=True,
                )
            ),
            str(
                _decimal(
                    exit_fee_quote,
                    "exit_fee_quote",
                    non_negative=True,
                )
            ),
            str(_decimal(realized_pnl_quote, "realized_pnl_quote")),
            timestamp,
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM partial_recycle_fills WHERE trade_id = ?",
                (tid,),
            ).fetchone()
            if existing is not None:
                actual = (
                    str(existing["manifest_id"]),
                    str(existing["order_id"]),
                    str(existing["base_sold"]),
                    str(existing["gross_proceeds_quote"]),
                    str(existing["exit_fee_quote"]),
                    str(existing["realized_pnl_quote"]),
                    float(existing["occurred_at"]),
                )
                if actual != values:
                    raise RuntimeError("partial recycle fill replay conflicts")
                return
            conn.execute(
                """
                INSERT INTO partial_recycle_fills (
                    trade_id, manifest_id, order_id, base_sold,
                    gross_proceeds_quote, exit_fee_quote,
                    realized_pnl_quote, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, *values),
            )

    def durable_fill_geometry(
        self, manifest_id: str
    ) -> tuple[tuple[str, Decimal, Decimal], ...]:
        """Exact disposed quantity and realized P&L for completion audits."""
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_id, base_sold, realized_pnl_quote "
                "FROM partial_recycle_fills WHERE manifest_id = ? ORDER BY trade_id",
                (mid,),
            ).fetchall()
        return tuple(
            (
                str(row["trade_id"]),
                _decimal(row["base_sold"], "base_sold", positive=True),
                _decimal(row["realized_pnl_quote"], "realized_pnl_quote"),
            )
            for row in rows
        )

    def realized_pnl(self, manifest_id: str) -> Decimal:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT realized_pnl_quote
                FROM partial_recycle_fills
                WHERE manifest_id = ?
                """,
                (mid,),
            ).fetchall()
        return exact_decimal_sum(
            (Decimal(str(row["realized_pnl_quote"])) for row in rows)
        )

    def mark_completed(self, manifest_id: str, *, now: float) -> None:
        mid = _text(manifest_id, "manifest_id")
        timestamp = _time(now, "now")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE partial_recycle_manifests
                SET status = ?, completed_at = ?
                WHERE manifest_id = ? AND status = ?
                """,
                (
                    PartialRecycleManifestStatus.COMPLETED.value,
                    timestamp,
                    mid,
                    PartialRecycleManifestStatus.OPEN.value,
                ),
            )


def _validate_terminal_proof(
    proof,
    manifest_id,
    sell_executor_id,
    journal,
    *,
    protected_limit_price=None,
    cash_proceeds_route=None,
):
    if not isinstance(proof, dict):
        raise RuntimeError("partial terminal proof is not an object")
    expected_hash = proof.get("sha256")
    body = {key: value for key, value in proof.items() if key != "sha256"}
    actual_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        expected_hash != actual_hash
        or proof.get("schema") != "spectra-partial-terminal-v1"
        or proof.get("manifest_id") != manifest_id
        or proof.get("sell_executor_id") != sell_executor_id
        or proof.get("executor_id") != sell_executor_id
        or proof.get("executor_status") != "TERMINATED"
        or proof.get("all_orders_terminal") is not True
        or proof.get("exact_order_fill_match") is not True
    ):
        raise RuntimeError("partial terminal proof is incomplete or conflicts")
    orders, trades, expected = (
        proof.get("orders"),
        proof.get("trades"),
        proof.get("durable_fills"),
    )
    if not all(isinstance(rows, list) for rows in (orders, trades, expected)):
        raise RuntimeError("partial terminal proof lacks cumulative evidence")
    by_order = {row["order_id"]: row for row in orders}
    by_trade = {row["trade_id"]: row for row in trades}
    by_fill = {row["trade_id"]: row for row in expected}
    if (
        len(by_order) != len(orders)
        or len(by_trade) != len(trades)
        or len(by_fill) != len(expected)
        or set(by_trade) != set(by_fill)
    ):
        raise RuntimeError("partial terminal proof repeats or omits evidence")
    if protected_limit_price is not None:
        if (
            proof.get("protected_limit_price") is None
            or _decimal(
                proof["protected_limit_price"],
                "terminal_protected_price",
                positive=True,
            )
            != protected_limit_price
            or proof.get("cash_proceeds_route") != cash_proceeds_route
            or cash_proceeds_route != "PARK"
        ):
            raise RuntimeError(
                "partial terminal proof lost immutable profit protection or cash route"
            )
        for order in orders:
            if (
                order.get("side") != "SELL"
                or order.get("order_type") != "LIMIT"
                or order.get("limit_price") is None
                or _decimal(order["limit_price"], "terminal_order_price", positive=True)
                < protected_limit_price
            ):
                raise RuntimeError(
                    "partial terminal proof contains an unprotected order"
                )
        if any(
            _decimal(trade.get("price"), "terminal_trade_price", positive=True)
            < protected_limit_price
            for trade in trades
        ):
            raise RuntimeError("partial terminal proof includes a below-floor fill")
    terminal_statuses = {
        "FILLED",
        "CANCELED",
        "CANCELLED",
        "FAILED",
        "REJECTED",
        "EXPIRED",
    }
    for order_id, order in by_order.items():
        if order.get("status") not in terminal_statuses:
            raise RuntimeError("partial terminal proof includes a live order")
        filled = _decimal(
            order["filled_base"], "terminal_filled_base", non_negative=True
        )
        amount = _decimal(
            order["amount_base"], "terminal_amount_base", non_negative=True
        )
        if filled > amount or filled != exact_decimal_sum(
            (
                _decimal(trade["amount_base"], "terminal_trade_base", non_negative=True)
                for trade in trades
                if trade["order_id"] == order_id
            ),
        ):
            raise RuntimeError("partial terminal proof order fills do not match")
        if order["status"] == "FILLED" and filled != amount:
            raise RuntimeError("partial terminal proof FILLED order is incomplete")
    if any(trade["order_id"] not in by_order for trade in trades):
        raise RuntimeError("partial terminal proof trade references unknown order")
    if _decimal(
        proof["executor_filled_quote"],
        "terminal_executor_filled_quote",
        non_negative=True,
    ) != exact_decimal_sum(
        (
            _decimal(trade["quote_amount"], "terminal_trade_quote", non_negative=True)
            for trade in trades
        ),
    ):
        raise RuntimeError("partial terminal proof executor volume does not match")
    actual = {str(fill["trade_id"]): fill for fill in journal}
    if set(actual) != set(by_fill):
        raise RuntimeError("partial terminal proof fill journal incomplete")
    for fill in expected:
        stored = actual[fill["trade_id"]]
        trade = by_trade[fill["trade_id"]]
        if (
            fill["order_id"] != trade["order_id"]
            or _decimal(fill["base_sold"], "terminal_base_sold", non_negative=True)
            != exact_decimal_sum(
                (
                    _decimal(
                        trade["amount_base"], "terminal_trade_base", non_negative=True
                    ),
                    _decimal(trade["base_fee"], "terminal_base_fee", non_negative=True),
                )
            )
            or _decimal(fill["exit_fee_quote"], "terminal_exit_fee", non_negative=True)
            != _decimal(
                trade["exit_fee_quote"], "terminal_trade_exit_fee", non_negative=True
            )
            or _decimal(
                fill["gross_proceeds_quote"],
                "terminal_gross_proceeds",
                non_negative=True,
            )
            != _decimal(
                trade["quote_amount"], "terminal_trade_quote", non_negative=True
            )
            or str(stored["order_id"]) != fill["order_id"]
        ) or any(
            Decimal(str(stored[field]))
            != _decimal(fill[field], field, non_negative=True)
            for field in ("base_sold", "gross_proceeds_quote", "exit_fee_quote")
        ):
            raise RuntimeError("partial terminal proof conflicts with durable fills")


def partial_dispatch_terminal_certificate(connection, manifest_id, *, schema=None):
    """Validate a persisted certificate while the caller holds its ledger lock."""
    databases = connection.execute("PRAGMA database_list").fetchall()
    if schema is not None and (
        not isinstance(schema, str)
        or not schema.isidentifier()
        or schema not in {str(database[1]) for database in databases}
    ):
        raise ValueError("invalid partial certificate database schema")
    requested_schema = schema
    schema = None
    for database in databases:
        name = str(database[1])
        if not name.isidentifier() or (
            requested_schema is not None and name != requested_schema
        ):
            continue
        if (
            connection.execute(
                f"SELECT 1 FROM {name}.sqlite_master WHERE type='table' AND name='partial_recycle_dispatch_claims'"
            ).fetchone()
            is not None
        ):
            schema = name
            break
    if schema is None:
        return None
    columns = {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA {schema}.table_info(partial_recycle_dispatch_claims)"
        )
    }
    if not {"terminal_reconciled_at", "terminal_proof_json"}.issubset(columns):
        return None
    row = connection.execute(
        f"SELECT terminal_reconciled_at, terminal_proof_json FROM {schema}.partial_recycle_dispatch_claims WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    proof = json.loads(str(row[1]))
    manifest = connection.execute(
        f"SELECT sell_executor_id, status, protected_limit_price, cash_proceeds_route FROM {schema}.partial_recycle_manifests WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    if manifest is None or str(manifest[1]) not in {"COMPLETED", "TERMINAL"}:
        raise RuntimeError("partial terminal certificate has nonterminal manifest")
    _time(row[0], "terminal_reconciled_at")
    journal = connection.execute(
        f"SELECT * FROM {schema}.partial_recycle_fills WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchall()
    conflicts_table = connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='partial_recycle_protection_conflicts'"
    ).fetchone()
    if (
        conflicts_table is not None
        and connection.execute(
            f"SELECT 1 FROM {schema}.partial_recycle_protection_conflicts WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        is not None
    ):
        raise RuntimeError(
            "partial terminal certificate has profit protection conflicts"
        )
    _validate_terminal_proof(
        proof,
        manifest_id,
        str(manifest[0]),
        journal,
        protected_limit_price=(
            None if manifest[2] is None else Decimal(str(manifest[2]))
        ),
        cash_proceeds_route=manifest[3],
    )
    return proof


def protected_terminal_buy_source_is_passive(
    *,
    proposal_id,
    executor_id,
    position_id,
    pair,
    governor,
    ledger,
    observation,
    order_snapshot,
    trade_snapshot,
    executor_config=None,
    _connection=None,
    _schema="main",
) -> bool:
    """Prove a bounded protected BUY has no order or unjournaled-fill work.

    Generic TERMINATED order executors do not establish source passivity. The
    exact durable funded parcel and receipt, cumulative venue evidence and
    economic journal must all agree. Caller may reuse its ledger transaction.
    """
    submission = governor.get_protected_buy_submission(proposal_id)
    provenance = governor.get_execution_provenance(proposal_id)
    if (
        submission is None
        or provenance is None
        or governor.get_protected_buy_conflicts(proposal_id)
    ):
        return False
    config = submission.get("config", {})
    if (
        submission.get("submission_state") != "DISPATCHED"
        or submission.get("executor_id") != executor_id
        or submission.get("position_id") != position_id
        or Path(submission.get("ledger_path", "")).resolve() != ledger.path.resolve()
        or submission.get("pair") != pair
        or submission.get("account_name") != provenance.account_name
        or submission.get("connector_name") != provenance.connector_name
        or governor.get_execution_receipt(proposal_id) != executor_id
        or provenance.executor_id != executor_id
        or provenance.pair != pair
        or provenance.intent not in {IntentKind.ENTRY, IntentKind.RECOVERY}
        or config.get("type") != "order_executor"
        or config.get("side") != 1
        or isinstance(config.get("side"), bool)
        or config.get("execution_strategy") != "LIMIT"
        or config.get("connector_name") != provenance.connector_name
        or config.get("trading_pair") != pair
        or observation.executor_id != executor_id
        or observation.executor_type != "order_executor"
        or observation.status.strip().upper() != "TERMINATED"
        or (observation.close_type or "").strip().upper()
        not in {"COMPLETED", "FAILED", "EARLY_STOP", "POSITION_HOLD"}
        or order_snapshot.executor_id != executor_id
        or trade_snapshot.executor_id != executor_id
    ):
        return False
    amount = _decimal(config.get("amount"), "protected_buy_amount", positive=True)
    ceiling = _decimal(config.get("price"), "protected_buy_price", positive=True)
    if not isinstance(executor_config, dict):
        return False
    for key in (
        "id",
        "type",
        "connector_name",
        "trading_pair",
        "side",
        "execution_strategy",
        "leverage",
        "position_action",
        "controller_id",
    ):
        if key in config and executor_config.get(key) != config[key]:
            return False
    if (
        _decimal(executor_config.get("amount"), "actual_buy_amount", positive=True)
        != amount
        or _decimal(executor_config.get("price"), "actual_buy_price", positive=True)
        != ceiling
    ):
        return False
    orders = {order.order_id: order for order in order_snapshot.orders}
    trades = {trade.trade_id: trade for trade in trade_snapshot.trades}
    if (
        not orders
        or not trades
        or len(orders) != len(order_snapshot.orders)
        or len(trades) != len(trade_snapshot.trades)
    ):
        return False
    terminal = {"FILLED", "CANCELED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}
    for order in orders.values():
        if (
            order.executor_id != executor_id
            or order.side != "BUY"
            or order.order_type.strip().upper() != "LIMIT"
            or order.price is None
            or order.price <= ZERO
            or order.price > ceiling
            or order.amount <= ZERO
            or order.amount > amount
            or order.filled_amount < ZERO
            or order.filled_amount > order.amount
            or order.status.strip().upper() not in terminal
            or (
                order.status.strip().upper() == "FILLED"
                and order.filled_amount != order.amount
            )
            or order.filled_amount
            != exact_decimal_sum(
                trade.amount_base
                for trade in trades.values()
                if trade.order_id == order.order_id
            )
        ):
            return False
    if exact_decimal_sum(
        trade.amount_base for trade in trades.values()
    ) > amount or observation.filled_amount_quote != exact_decimal_sum(
        trade.quote_amount for trade in trades.values()
    ):
        return False
    own_connection = _connection is None
    connection = ledger._connect() if own_connection else _connection
    try:
        if (
            not isinstance(_schema, str)
            or not _schema.isidentifier()
            or _schema
            not in {str(row[1]) for row in connection.execute("PRAGMA database_list")}
        ):
            raise ValueError("invalid protected BUY ledger schema")
        tables = {
            str(row[0])
            for row in connection.execute(
                f"SELECT name FROM {_schema}.sqlite_master WHERE type='table'"
            )
        }
        if not {
            "economic_fill_bindings",
            "economic_trade_fill_reconciliation",
        }.issubset(tables):
            return False
        bindings = connection.execute(
            f"SELECT * FROM {_schema}.economic_fill_bindings WHERE position_id = ? AND proposal_id = ?",
            (position_id, proposal_id),
        ).fetchall()
        if set(orders) != {str(row["order_id"]) for row in bindings}:
            return False
        for row in bindings:
            if (
                str(row["executor_id"]) != executor_id
                or str(row["pair"]) != pair
                or str(row["account_name"]) != provenance.account_name
                or str(row["connector_name"]) != provenance.connector_name
                or str(row["role"])
                != (
                    "BASE_ENTRY"
                    if provenance.intent == IntentKind.ENTRY
                    else "VCAP_RECOVERY"
                )
            ):
                return False
        journal = connection.execute(
            f"SELECT * FROM {_schema}.economic_trade_fill_reconciliation WHERE position_id = ? "
            + "AND order_id IN ("
            + ",".join("?" for _ in orders)
            + ")",
            (position_id, *orders),
        ).fetchall()
        recorded = {str(row["trade_id"]): row for row in journal}
        if set(recorded) != set(trades):
            return False
        for trade_id, trade in trades.items():
            row = recorded[trade_id]
            signature = (
                trade.order_id,
                trade.account_name,
                trade.connector_name,
                trade.pair,
                trade.side,
                trade.amount_base,
                trade.price,
                trade.fee_paid,
                trade.fee_currency,
                trade.occurred_at,
            )
            actual = (
                str(row["order_id"]),
                str(row["account_name"]),
                str(row["connector_name"]),
                str(row["pair"]),
                str(row["side"]),
                Decimal(str(row["amount_base"])),
                Decimal(str(row["price"])),
                Decimal(str(row["fee_paid"])),
                row["fee_currency"],
                float(row["occurred_at"]),
            )
            if (
                signature != actual
                or trade.order_id not in orders
                or trade.side != "BUY"
                or trade.account_name != provenance.account_name
                or trade.connector_name != provenance.connector_name
                or trade.pair != pair
                or trade.price > ceiling
            ):
                return False
        return True
    finally:
        if own_connection:
            connection.close()


def _owner_from_lots(
    lots: list[EconomicLot],
    *,
    governor: CapitalGovernor,
    pair: str,
) -> PartialRecycleOwner:
    first = lots[0]
    if first.proposal_id is None or first.executor_id is None:
        raise ValueError("recovery lot lacks exact execution ownership")
    proposal_id = first.proposal_id
    executor_id = first.executor_id
    provenance = governor.get_execution_provenance(proposal_id)
    receipt = governor.get_execution_receipt(proposal_id)
    if provenance is None or receipt != executor_id:
        raise ValueError("recovery lot lost exact execution provenance")
    if provenance.executor_id != executor_id:
        raise ValueError("recovery executor ownership changed")
    if provenance.intent != IntentKind.RECOVERY:
        raise ValueError("partial recycle may only harvest RECOVERY inventory")
    if provenance.pair.strip().upper() != pair:
        raise ValueError("partial recycle owner crosses trading pair")
    if provenance.account_name is None or provenance.connector_name is None:
        raise ValueError("partial recycle owner has unscoped provenance")
    for lot in lots:
        if (
            lot.proposal_id != proposal_id
            or lot.executor_id != executor_id
            or lot.source != EconomicLotSource.VCAP_RECOVERY
            or lot.ownership != OwnershipBucket.ACTIVE_TRADE
        ):
            raise ValueError("partial recycle owner contains mixed ownership")
    return PartialRecycleOwner(
        proposal_id=proposal_id,
        executor_id=executor_id,
        lot_ids=tuple(lot.lot_id for lot in lots),
        target_base=exact_decimal_sum((lot.base_remaining for lot in lots)),
        target_cost_basis_quote=exact_decimal_sum(
            (lot.cost_basis_remaining_quote for lot in lots)
        ),
        target_entry_fees_quote=exact_decimal_sum(
            (lot.entry_fees_remaining_quote for lot in lots)
        ),
    )


def _owner_geometry(
    owner: PartialRecycleOwner,
    *,
    price: Decimal,
    exit_cost_bps: Decimal,
) -> tuple[Decimal, Decimal]:
    with localcontext() as context:
        context.prec = 80
        gross = owner.target_base * price
        exit_cost = gross * exit_cost_bps / TEN_THOUSAND
        pnl = exact_decimal_sum(
            (
                gross,
                -exit_cost,
                -owner.target_cost_basis_quote,
                -owner.target_entry_fees_quote,
            )
        )
        invested = exact_decimal_sum(
            (owner.target_cost_basis_quote, owner.target_entry_fees_quote)
        )
        return_bps = ZERO if invested <= ZERO else pnl * TEN_THOUSAND / invested
    return pnl, return_bps


def _aggregate_geometry(
    owners: tuple[PartialRecycleOwner, ...],
    *,
    price: Decimal,
    exit_cost_bps: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    base = exact_decimal_sum((owner.target_base for owner in owners))
    basis = exact_decimal_sum((owner.target_cost_basis_quote for owner in owners))
    fees = exact_decimal_sum((owner.target_entry_fees_quote for owner in owners))
    with localcontext() as context:
        context.prec = 80
        gross = base * price
        exit_cost = gross * exit_cost_bps / TEN_THOUSAND
        pnl = exact_decimal_sum((gross, -exit_cost, -basis, -fees))
        invested = exact_decimal_sum((basis, fees))
        return_bps = ZERO if invested <= ZERO else pnl * TEN_THOUSAND / invested
    return base, basis, fees, pnl, return_bps


def _manifest_id(
    *,
    position_id: str,
    owners: tuple[PartialRecycleOwner, ...],
    predecessor_manifest_id: str | None = None,
) -> str:
    payload = "|".join(
        [
            position_id,
            *(
                f"{owner.proposal_id}:{owner.executor_id}:" + ",".join(owner.lot_ids)
                for owner in owners
            ),
        ]
    )
    if predecessor_manifest_id is not None:
        payload += (
            "|generation-v2|"
            + predecessor_manifest_id
            + "|"
            + "|".join(
                f"{owner.proposal_id}:{owner.target_base}:{owner.target_cost_basis_quote}:{owner.target_entry_fees_quote}"
                for owner in owners
            )
        )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"partial-recycle:{digest}"


def protected_partial_recycle_price(
    *,
    target_base: Decimal,
    target_cost_basis_quote: Decimal,
    target_entry_fees_quote: Decimal,
    target_net_profit_bps: Decimal,
    expected_exit_cost_bps: Decimal,
    trading_rule: KrakenTradingRule,
) -> Decimal:
    """Minimum tick-valid price for the exact proportionally disposed parcel.

    Entry fees are distinct from acquisition basis. Exit costs include the
    already configured fee/slippage allowance; they are not counted twice.
    No quantity rounding may expand or change the immutable lot selection.
    """
    base = _decimal(target_base, "target_base", positive=True)
    basis = _decimal(target_cost_basis_quote, "target_cost_basis_quote", positive=True)
    fees = _decimal(
        target_entry_fees_quote, "target_entry_fees_quote", non_negative=True
    )
    target = _decimal(target_net_profit_bps, "target_net_profit_bps", non_negative=True)
    costs = _decimal(
        expected_exit_cost_bps, "expected_exit_cost_bps", non_negative=True
    )
    if costs >= TEN_THOUSAND:
        raise ValueError("expected_exit_cost_bps must be below 10000")
    if trading_rule.supports_limit_orders is not True:
        raise ValueError("partial recycle requires supported LIMIT orders")
    tick = _decimal(
        trading_rule.min_price_increment, "min_price_increment", positive=True
    )
    step = _decimal(
        trading_rule.min_base_amount_increment,
        "min_base_amount_increment",
        positive=True,
    )
    minimum_base = _decimal(
        trading_rule.min_order_size, "min_order_size", non_negative=True
    )
    minimum_quote = _decimal(
        trading_rule.effective_min_quote, "effective_min_quote", non_negative=True
    )
    with localcontext() as context:
        context.prec = 80
        if base < minimum_base or base % step != ZERO:
            raise ValueError(
                "partial recycle base is below minimum or not exactly quantized"
            )
        required = exact_decimal_sum((basis, fees)) * (1 + target / TEN_THOUSAND)
        price = max(
            required / (base * (1 - costs / TEN_THOUSAND)), minimum_quote / base
        )
        return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def validate_partial_recycle_manifest(
    manifest: PartialRecycleManifest,
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    _connection: sqlite3.Connection | None = None,
) -> None:
    if _connection is None:
        durable = PartialRecycleStore(ledger.path).load(manifest.manifest_id)
    else:
        row = _connection.execute(
            "SELECT * FROM partial_recycle_manifests WHERE manifest_id = ?",
            (manifest.manifest_id,),
        ).fetchone()
        durable = None if row is None else PartialRecycleStore._from_row(row)
    if durable is None or durable != replace(
        manifest, status=durable.status, completed_at=durable.completed_at
    ):
        raise RuntimeError(
            "partial recycle immutable manifest differs from durable state"
        )
    protected = manifest.protected_limit_price is not None
    if manifest.cash_proceeds_route not in {None, "PARK"}:
        raise RuntimeError("partial recycle cash route must preserve PARK purpose")
    if protected != (manifest.target_net_profit_bps is not None):
        raise RuntimeError("partial recycle profit protection is incomplete")
    if protected:
        price = _decimal(
            manifest.protected_limit_price, "protected_limit_price", positive=True
        )
        target = _decimal(
            manifest.target_net_profit_bps, "target_net_profit_bps", non_negative=True
        )
        costs = _decimal(
            manifest.expected_exit_cost_bps, "expected_exit_cost_bps", non_negative=True
        )
        if costs >= TEN_THOUSAND:
            raise RuntimeError("partial recycle exit costs invalidate protection")
        with localcontext() as context:
            context.prec = 80
            invested = (
                manifest.target_cost_basis_quote + manifest.target_entry_fees_quote
            )
            if price * manifest.target_base * (1 - costs / TEN_THOUSAND) < invested * (
                1 + target / TEN_THOUSAND
            ):
                raise RuntimeError(
                    "partial recycle limit violates the configured profit floor"
                )
    snapshot = (
        ledger.snapshot(manifest.position_id)
        if _connection is None
        else ledger._snapshot_with_conn(_connection, manifest.position_id)
    )
    active = {
        lot.lot_id: lot for lot in _manifest_active_lots(ledger, manifest, _connection)
    }
    if snapshot.status != EconomicPositionStatus.ACTIVE and any(
        active[lot_id].base_remaining > ZERO
        for lot_id in manifest.lot_ids
        if lot_id in active
    ):
        # A crash may occur after terminal dust ownership is certified but
        # before its runtime cycle completes. Only that exact proven old sale
        # can replay against RESIDUAL ownership; this grants no new dispatch.
        if _connection is None:
            certificate = ledger.get_retirement_certificate(manifest.position_id)
            terminal = PartialRecycleStore(ledger.path).terminal_proof(
                manifest.manifest_id
            )
        else:
            row = _connection.execute(
                "SELECT certificate_json FROM economic_position_retirements WHERE position_id = ?",
                (manifest.position_id,),
            ).fetchone()
            certificate = None if row is None else json.loads(str(row[0]))
            terminal = partial_dispatch_terminal_certificate(
                _connection, manifest.manifest_id, schema="main"
            )
        proof = None if certificate is None else certificate.get("proof")
        valid_hash = (
            isinstance(proof, dict)
            and certificate.get("proof_sha256")
            == hashlib.sha256(
                json.dumps(proof, sort_keys=True, default=str).encode()
            ).hexdigest()
        )
        if (
            snapshot.status != EconomicPositionStatus.RESIDUAL
            or not valid_hash
            or terminal is None
            or certificate.get("position_id") != manifest.position_id
            or certificate.get("ledger_path") != str(ledger.path.resolve())
            or certificate.get("pair") != manifest.pair
            or Decimal(str(certificate.get("remaining_base"))) != snapshot.active_base
            or Decimal(str(certificate.get("cost_basis")))
            != snapshot.active_cost_basis_quote
            or Decimal(str(certificate.get("entry_fees")))
            != snapshot.active_entry_fees_quote
            or terminal not in proof.get("partial_terminal_certificates", [])
            or not set(manifest.proposal_ids).issubset(
                set(certificate.get("proposal_ids", []))
            )
        ):
            raise RuntimeError("partial recycle position is no longer active")
    if snapshot.pair != manifest.pair:
        raise RuntimeError("partial recycle manifest pair changed")

    for owner in manifest.owners:
        provenance = governor.get_execution_provenance(owner.proposal_id)
        receipt = governor.get_execution_receipt(owner.proposal_id)
        if (
            provenance is None
            or receipt != owner.executor_id
            or provenance.executor_id != owner.executor_id
            or provenance.intent != IntentKind.RECOVERY
        ):
            raise RuntimeError("partial recycle source ownership is no longer exact")
        current_base = ZERO
        for lot_id in owner.lot_ids:
            lot = active.get(lot_id)
            if lot is None:
                continue
            if (
                lot.proposal_id != owner.proposal_id
                or lot.executor_id != owner.executor_id
                or lot.source != EconomicLotSource.VCAP_RECOVERY
                or lot.ownership != OwnershipBucket.ACTIVE_TRADE
            ):
                raise RuntimeError("partial recycle lot ownership changed")
            current_base = exact_decimal_sum((current_base, lot.base_remaining))
        if current_base > owner.target_base:
            raise RuntimeError(
                "partial recycle inventory increased after manifest creation"
            )


def _manifest_active_lots(ledger, manifest, connection=None):
    if connection is None:
        return ledger.list_active_lots(manifest.position_id)
    return _manifest_active_lots_from_connection(connection, manifest)


def _manifest_active_lots_from_connection(connection, manifest):
    rows = connection.execute(
        "SELECT * FROM economic_lots WHERE position_id = ? AND ownership = ? "
        "ORDER BY created_at, lot_id",
        (manifest.position_id, OwnershipBucket.ACTIVE_TRADE.value),
    ).fetchall()
    return tuple(EconomicPositionLedger._lot_from_row(row) for row in rows)


def remaining_partial_recycle_base(
    manifest: PartialRecycleManifest,
    *,
    ledger: EconomicPositionLedger,
) -> Decimal:
    active = {lot.lot_id: lot for lot in ledger.list_active_lots(manifest.position_id)}
    return exact_decimal_sum(
        (
            active[lot_id].base_remaining
            for lot_id in manifest.lot_ids
            if lot_id in active
        )
    )


def assess_partial_recycle_dispatch(
    manifest: PartialRecycleManifest,
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    expected_exit_cost_bps: Decimal | None = None,
    _connection: sqlite3.Connection | None = None,
) -> PartialRecycleDispatchGuard:
    """Reprice only exact remaining ownership before a NEW sell dispatch.

    Costs may become more conservative, but a current caller cannot lower the
    immutable manifest cost allowance or profit target. This guard authorizes
    modeled submission economics, not guaranteed future market fills.
    """
    validate_partial_recycle_manifest(
        manifest, ledger=ledger, governor=governor, _connection=_connection
    )
    price = _decimal(current_price, "current_price", positive=True)
    target = _decimal(
        (
            ZERO
            if manifest.target_net_profit_bps is None
            else manifest.target_net_profit_bps
        ),
        "target_net_profit_bps",
        non_negative=True,
    )
    cost = _decimal(
        manifest.expected_exit_cost_bps, "manifest_exit_cost_bps", non_negative=True
    )
    if expected_exit_cost_bps is not None:
        cost = max(
            cost,
            _decimal(
                expected_exit_cost_bps, "expected_exit_cost_bps", non_negative=True
            ),
        )
    if cost >= TEN_THOUSAND:
        raise ValueError("exit cost bps must be < 10000")
    if trading_rule.trading_pair.strip().upper() != manifest.pair:
        raise ValueError("partial recycle trading rule pair changed")
    step = _decimal(
        trading_rule.min_base_amount_increment,
        "min_base_amount_increment",
        positive=True,
    )
    min_base = _decimal(
        trading_rule.min_order_size, "min_order_size", non_negative=True
    )
    min_quote = _decimal(
        trading_rule.effective_min_quote, "effective_min_quote", non_negative=True
    )
    active = {
        lot.lot_id: lot for lot in _manifest_active_lots(ledger, manifest, _connection)
    }
    owned = [active[lot_id] for lot_id in manifest.lot_ids if lot_id in active]
    remaining = exact_decimal_sum(lot.base_remaining for lot in owned)
    if _connection is None:
        recorded = PartialRecycleStore(ledger.path).durable_fill_geometry(
            manifest.manifest_id
        )
        disposed = exact_decimal_sum(base for _, base, _ in recorded)
    else:
        disposed = exact_decimal_sum(
            Decimal(str(row[0]))
            for row in _connection.execute(
                "SELECT base_sold FROM partial_recycle_fills WHERE manifest_id = ?",
                (manifest.manifest_id,),
            )
        )
    coverage_exact = disposed == exact_decimal_difference(
        manifest.target_base, remaining
    )
    basis = exact_decimal_sum(lot.cost_basis_remaining_quote for lot in owned)
    fees = exact_decimal_sum(lot.entry_fees_remaining_quote for lot in owned)
    with localcontext() as context:
        context.prec = 80
        sell_base = (remaining / step).to_integral_value(rounding=ROUND_DOWN) * step
        # Quantization leaves the exact unsold lot basis and fees owned.
        ratio = ZERO if remaining == ZERO else sell_base / remaining
        sell_basis, sell_fees = basis * ratio, fees * ratio
        invested = exact_decimal_sum((sell_basis, sell_fees))
        proceeds = sell_base * price * (Decimal("1") - cost / TEN_THOUSAND)
        pnl = exact_decimal_difference(proceeds, invested)
        return_bps = ZERO if invested <= ZERO else pnl / invested * TEN_THOUSAND
    reasons: list[str] = []
    current_snapshot = (
        ledger.snapshot(manifest.position_id)
        if _connection is None
        else ledger._snapshot_with_conn(_connection, manifest.position_id)
    )
    if current_snapshot.status != EconomicPositionStatus.ACTIVE:
        reasons.append("retired_position_blocks_new_partial_dispatch")
    if any(
        governor.get_protected_buy_conflicts(owner.proposal_id)
        for owner in manifest.owners
    ):
        reasons.append("protected_buy_source_conflict_requires_reconciliation")
    if not coverage_exact:
        reasons.append("remaining_inventory_lacks_durable_partial_fill_coverage")
    if manifest.protected_limit_price is None or manifest.target_net_profit_bps is None:
        reasons.append("legacy_partial_recycle_requires_protected_replan")
    if not trading_rule.supports_limit_orders:
        reasons.append("current_limit_order_not_supported")
    if sell_base <= ZERO or sell_base < min_base or sell_base * price < min_quote:
        reasons.append("current_exchange_minimum_blocks_recycle")
    if invested <= ZERO or pnl <= ZERO or return_bps < target:
        reasons.append("current_partial_recycle_profit_floor_not_met")
    if manifest.protected_limit_price is not None:
        if price < manifest.protected_limit_price:
            reasons.append("current_bid_below_saved_protected_limit")
        if sell_base > ZERO and sell_base >= min_base:
            try:
                required_price = protected_partial_recycle_price(
                    target_base=sell_base,
                    target_cost_basis_quote=sell_basis,
                    target_entry_fees_quote=sell_fees,
                    target_net_profit_bps=target,
                    expected_exit_cost_bps=cost,
                    trading_rule=trading_rule,
                )
                if (
                    required_price > manifest.protected_limit_price
                    or manifest.protected_limit_price % trading_rule.min_price_increment
                    != ZERO
                ):
                    reasons.append("saved_partial_limit_requires_protected_replan")
            except ValueError:
                reasons.append("current_limit_rule_blocks_recycle")
    return PartialRecycleDispatchGuard(
        allowed=not reasons,
        sell_base=sell_base,
        cost_basis_quote=sell_basis,
        entry_fees_quote=sell_fees,
        expected_exit_cost_bps=cost,
        target_net_profit_bps=target,
        estimated_net_pnl_quote=pnl,
        estimated_net_return_bps=return_bps,
        reasons=tuple(reasons),
    )


def assess_partial_recycle_manifest(
    manifest: PartialRecycleManifest,
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    store: PartialRecycleStore,
    now: float,
) -> PartialRecycleProgress:
    if store.protection_conflicts(manifest.manifest_id):
        raise RuntimeError(
            "partial recycle profit protection conflict requires resolution"
        )
    validate_partial_recycle_manifest(
        manifest,
        ledger=ledger,
        governor=governor,
    )
    remaining = remaining_partial_recycle_base(
        manifest,
        ledger=ledger,
    )
    recorded = store.durable_fill_geometry(manifest.manifest_id)
    # Ledger, Governor and journal updates are replayable, but not one atomic
    # transaction. A crash must not turn an unjournaled disposal into success.
    if exact_decimal_sum((base for _, base, _ in recorded)) != exact_decimal_difference(
        manifest.target_base, remaining
    ):
        raise RuntimeError(
            "partial recycle disposed inventory lacks exact durable fill coverage"
        )
    if manifest.target_net_profit_bps is not None:
        with localcontext() as context:
            context.prec = 80
            invested = (
                manifest.target_cost_basis_quote + manifest.target_entry_fees_quote
            )
            for trade_id, base, pnl in recorded:
                required_pnl = (
                    invested
                    * base
                    / manifest.target_base
                    * manifest.target_net_profit_bps
                    / TEN_THOUSAND
                )
                if pnl < required_pnl:
                    store.record_protection_conflict(
                        manifest_id=manifest.manifest_id,
                        conflict=f"profit_floor_breached:{trade_id}",
                        now=now,
                    )
        if store.protection_conflicts(manifest.manifest_id):
            raise RuntimeError(
                "partial recycle profit protection conflict requires resolution"
            )
    completed = remaining == ZERO
    if completed and manifest.status == PartialRecycleManifestStatus.OPEN:
        store.mark_completed(manifest.manifest_id, now=now)
    return PartialRecycleProgress(
        manifest_id=manifest.manifest_id,
        remaining_base=remaining,
        realized_pnl_quote=store.realized_pnl(manifest.manifest_id),
        completed=completed,
        terminal=store.terminal_proof(manifest.manifest_id) is not None,
    )


def plan_partial_recycle(
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    position_id: str,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    expected_exit_fee_bps: Decimal,
    slippage_allowance_bps: Decimal,
    now: float,
    policy: PartialRecyclePolicy | None = None,
    store: PartialRecycleStore | None = None,
) -> PartialRecycleDecision:
    """Select exact profitable RECOVERY owners for one partial recycle mutation.

    The planner never sells wallet balance. It may consolidate several exact
    RECOVERY owners into one sell manifest when that is necessary to repair a
    sub-minimum chunk, but the aggregate must still satisfy the configured net
    profit floor after expected exit costs.
    """

    pid = _text(position_id, "position_id")
    price = _decimal(current_price, "current_price", positive=True)
    fee_bps = _decimal(
        expected_exit_fee_bps,
        "expected_exit_fee_bps",
        non_negative=True,
    )
    slippage_bps = _decimal(
        slippage_allowance_bps,
        "slippage_allowance_bps",
        non_negative=True,
    )
    if fee_bps + slippage_bps >= TEN_THOUSAND:
        raise ValueError("expected exit costs must be below 10000 bps")
    timestamp = _time(now, "now")
    recycle_policy = policy or PartialRecyclePolicy()
    manifest_store = store or PartialRecycleStore(ledger.path)

    existing = manifest_store.open_for_position(pid)
    if existing is not None:
        validate_partial_recycle_manifest(
            existing,
            ledger=ledger,
            governor=governor,
        )
        return PartialRecycleDecision(
            action=PartialRecycleAction.RECYCLE,
            manifest=existing,
            repair_lot_ids=(),
            reasons=("resume_open_partial_recycle_manifest",),
        )

    snapshot = ledger.snapshot(pid)
    if snapshot.status != EconomicPositionStatus.ACTIVE:
        return PartialRecycleDecision(
            action=PartialRecycleAction.NONE,
            manifest=None,
            repair_lot_ids=(),
            reasons=("economic_position_closed",),
        )
    if snapshot.pair != trading_rule.trading_pair.strip().upper():
        raise ValueError("partial recycle trading rule pair mismatch")

    grouped: dict[tuple[str, str], list[EconomicLot]] = {}
    repair_lot_ids: list[str] = []
    for lot in ledger.list_active_lots(pid):
        if lot.source != EconomicLotSource.VCAP_RECOVERY:
            continue
        if lot.proposal_id is None or lot.executor_id is None:
            raise ValueError(f"recovery lot {lot.lot_id} lacks exact ownership")
        grouped.setdefault(
            (lot.proposal_id, lot.executor_id),
            [],
        ).append(lot)

    if not grouped:
        return PartialRecycleDecision(
            action=PartialRecycleAction.NONE,
            manifest=None,
            repair_lot_ids=(),
            reasons=("no_active_recovery_lots",),
        )

    if any(
        governor.get_protected_buy_conflicts(proposal_id) for proposal_id, _ in grouped
    ):
        return PartialRecycleDecision(
            action=PartialRecycleAction.NONE,
            manifest=None,
            repair_lot_ids=tuple(repair_lot_ids),
            reasons=("protected_buy_source_conflict_requires_reconciliation",),
        )
    exit_cost_bps = fee_bps + slippage_bps
    owners = tuple(
        _owner_from_lots(
            grouped[key],
            governor=governor,
            pair=snapshot.pair,
        )
        for key in sorted(grouped)
    )

    profitable: list[PartialRecycleOwner] = []
    repair: list[PartialRecycleOwner] = []
    for owner in owners:
        pnl, return_bps = _owner_geometry(
            owner,
            price=price,
            exit_cost_bps=exit_cost_bps,
        )
        sellable = (
            owner.target_base >= trading_rule.min_order_size
            and owner.target_base * price >= trading_rule.effective_min_quote
        )
        if not sellable:
            repair.append(owner)
            repair_lot_ids.extend(owner.lot_ids)
        if pnl > ZERO and return_bps >= recycle_policy.target_net_profit_bps:
            profitable.append(owner)

    if not profitable:
        action = (
            PartialRecycleAction.REPAIR_PENDING if repair else PartialRecycleAction.NONE
        )
        return PartialRecycleDecision(
            action=action,
            manifest=None,
            repair_lot_ids=tuple(repair_lot_ids),
            reasons=(
                (
                    "subminimum_recovery_inventory_requires_consolidation"
                    if repair
                    else "no_recovery_owner_meets_partial_profit_floor"
                ),
            ),
        )

    selected = list(profitable)

    # Absorb sub-minimum recovery owners when the combined recycle remains
    # profitable. This is the deterministic dust-repair path: exact owned
    # recovery inventory is consolidated; retained/unattributed inventory is
    # never eligible.
    for owner in repair:
        if owner in selected:
            continue
        candidate = tuple((*selected, owner))
        base, _, _, pnl, return_bps = _aggregate_geometry(
            candidate,
            price=price,
            exit_cost_bps=exit_cost_bps,
        )
        if (
            base >= trading_rule.min_order_size
            and base * price >= trading_rule.effective_min_quote
            and pnl > ZERO
            and return_bps >= recycle_policy.target_net_profit_bps
        ):
            selected.append(owner)

    selected_tuple = tuple(sorted(selected, key=lambda owner: owner.proposal_id))
    base, basis, fees, pnl, return_bps = _aggregate_geometry(
        selected_tuple,
        price=price,
        exit_cost_bps=exit_cost_bps,
    )
    if (
        base < trading_rule.min_order_size
        or base * price < trading_rule.effective_min_quote
    ):
        return PartialRecycleDecision(
            action=PartialRecycleAction.REPAIR_PENDING,
            manifest=None,
            repair_lot_ids=tuple(repair_lot_ids),
            reasons=("profitable_recovery_inventory_is_below_exchange_minimum",),
        )
    if pnl <= ZERO or return_bps < recycle_policy.target_net_profit_bps:
        return PartialRecycleDecision(
            action=PartialRecycleAction.NONE,
            manifest=None,
            repair_lot_ids=tuple(repair_lot_ids),
            reasons=("aggregate_partial_recycle_profit_floor_not_met",),
        )

    predecessor = manifest_store.latest_for_position(pid)
    if (
        predecessor is not None
        and manifest_store.terminal_proof(predecessor.manifest_id) is None
    ):
        return PartialRecycleDecision(
            action=PartialRecycleAction.NONE,
            manifest=None,
            repair_lot_ids=tuple(repair_lot_ids),
            reasons=("terminal_proof_required_for_new_partial_generation",),
        )
    predecessor_id = None if predecessor is None else predecessor.manifest_id
    mid = _manifest_id(
        position_id=pid, owners=selected_tuple, predecessor_manifest_id=predecessor_id
    )
    step = _decimal(
        trading_rule.min_base_amount_increment,
        "min_base_amount_increment",
        positive=True,
    )
    with localcontext() as context:
        context.prec = 80
        sell_base = (base / step).to_integral_value(rounding=ROUND_DOWN) * step
        if sell_base <= ZERO or sell_base < trading_rule.min_order_size:
            return PartialRecycleDecision(
                action=PartialRecycleAction.REPAIR_PENDING,
                manifest=None,
                repair_lot_ids=tuple(repair_lot_ids),
                reasons=("quantized_recovery_inventory_is_below_exchange_minimum",),
            )
        ratio = sell_base / base
        protected_price = protected_partial_recycle_price(
            target_base=sell_base,
            target_cost_basis_quote=basis * ratio,
            target_entry_fees_quote=fees * ratio,
            target_net_profit_bps=recycle_policy.target_net_profit_bps,
            expected_exit_cost_bps=exit_cost_bps,
            trading_rule=trading_rule,
        )
    manifest = PartialRecycleManifest(
        manifest_id=mid,
        position_id=pid,
        pair=snapshot.pair,
        status=PartialRecycleManifestStatus.OPEN,
        owners=selected_tuple,
        target_base=base,
        target_cost_basis_quote=basis,
        target_entry_fees_quote=fees,
        reference_price=price,
        min_order_size=trading_rule.min_order_size,
        min_notional_quote=trading_rule.effective_min_quote,
        expected_exit_cost_bps=exit_cost_bps,
        estimated_net_pnl_quote=pnl,
        estimated_net_return_bps=return_bps,
        sell_executor_id=f"{mid}:sell",
        created_at=timestamp,
        completed_at=None,
        target_net_profit_bps=recycle_policy.target_net_profit_bps,
        predecessor_manifest_id=predecessor_id,
        protected_limit_price=protected_price,
        cash_proceeds_route="PARK",
    )
    try:
        created = manifest_store.create(manifest)
    except PartialRecycleManifestConflict as conflict:
        return PartialRecycleDecision(
            action=PartialRecycleAction.NONE,
            manifest=None,
            repair_lot_ids=tuple(repair_lot_ids),
            reasons=(str(conflict),),
        )
    validate_partial_recycle_manifest(
        created,
        ledger=ledger,
        governor=governor,
    )
    return PartialRecycleDecision(
        action=PartialRecycleAction.RECYCLE,
        manifest=created,
        repair_lot_ids=tuple(repair_lot_ids),
        reasons=(
            "profitable_recovery_inventory_selected",
            "partial_recycle_manifest_checkpointed",
        ),
    )
