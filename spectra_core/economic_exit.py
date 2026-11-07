from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from math import isfinite
from pathlib import Path
from typing import Iterable

from .capital_governor import CapitalGovernor
from .decimal_math import exact_decimal_difference, exact_decimal_sum
from .economic_position import (
    EconomicLot,
    EconomicPositionLedger,
    EconomicPositionStatus,
)
from .kraken_market_fabric import KrakenTradingRule
from .models import IntentKind
from .partial_recycle import protected_partial_recycle_price

ZERO = Decimal("0")
BPS = Decimal("10000")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(
    value: Decimal,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and value <= ZERO:
        raise ValueError(f"{field} must be positive")
    if non_negative and value < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return value


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


class EconomicExitManifestStatus(StrEnum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"


class EconomicExitProgressState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    DUST_BLOCKED = "DUST_BLOCKED"
    RESIDUAL_RETAINED = "RESIDUAL_RETAINED"


@dataclass(frozen=True)
class EconomicExitProgress:
    manifest_id: str
    state: EconomicExitProgressState
    remaining_active_base: Decimal
    realized_pnl_quote: Decimal
    strategic_retained_base: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EconomicExitOwner:
    proposal_id: str
    executor_id: str
    intent: IntentKind
    account_name: str
    connector_name: str
    lot_ids: tuple[str, ...]
    target_base: Decimal
    target_cost_basis_quote: Decimal
    target_entry_fees_quote: Decimal


@dataclass(frozen=True)
class EconomicPositionExitManifest:
    manifest_id: str
    position_id: str
    pair: str
    status: EconomicExitManifestStatus
    target_active_base: Decimal
    target_cost_basis_quote: Decimal
    target_entry_fees_quote: Decimal
    reference_price: Decimal
    min_order_size: Decimal
    min_notional_quote: Decimal
    realized_pnl_before_quote: Decimal
    strategic_retained_base_at_creation: Decimal
    unattributed_base_at_creation: Decimal
    owners: tuple[EconomicExitOwner, ...]
    created_at: float
    completed_at: float | None
    target_net_profit_bps: Decimal | None = None
    expected_exit_cost_bps: Decimal | None = None
    protected_limit_price: Decimal | None = None
    sell_executor_id: str | None = None
    cash_proceeds_route: str | None = None

    @property
    def proposal_ids(self) -> tuple[str, ...]:
        return tuple(owner.proposal_id for owner in self.owners)

    @property
    def executor_ids(self) -> tuple[str, ...]:
        return tuple(owner.executor_id for owner in self.owners)

    @property
    def lot_ids(self) -> tuple[str, ...]:
        return tuple(lot_id for owner in self.owners for lot_id in owner.lot_ids)


class EconomicExitManifestStore:
    """Persist immutable full-position exit scope beside the economic ledger."""

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
                CREATE TABLE IF NOT EXISTS economic_exit_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_active_base TEXT NOT NULL,
                    target_cost_basis_quote TEXT NOT NULL,
                    target_entry_fees_quote TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    min_order_size TEXT NOT NULL,
                    min_notional_quote TEXT NOT NULL,
                    realized_pnl_before_quote TEXT NOT NULL,
                    strategic_retained_base_at_creation TEXT NOT NULL,
                    unattributed_base_at_creation TEXT NOT NULL,
                    owners_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    economic_exit_manifest_open_position_idx
                ON economic_exit_manifests(position_id)
                WHERE status = 'OPEN';
                """)
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(economic_exit_manifests)")
            }
            for column in (
                "target_net_profit_bps",
                "expected_exit_cost_bps",
                "protected_limit_price",
                "sell_executor_id",
                "cash_proceeds_route",
            ):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE economic_exit_manifests ADD COLUMN {column} TEXT"
                    )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS economic_exit_dispatch_claims (
                    manifest_id TEXT PRIMARY KEY, claimed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS economic_exit_protection_conflicts (
                    manifest_id TEXT NOT NULL, conflict TEXT NOT NULL,
                    observed_at REAL NOT NULL, PRIMARY KEY (manifest_id, conflict)
                );
                CREATE TABLE IF NOT EXISTS economic_exit_finality (
                    manifest_id TEXT PRIMARY KEY, executor_id TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS economic_exit_fills (
                    trade_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL,
                    order_id TEXT NOT NULL, base_sold TEXT NOT NULL,
                    gross_proceeds_quote TEXT NOT NULL, exit_fee_quote TEXT NOT NULL,
                    allocated_basis_quote TEXT NOT NULL, allocated_entry_fees_quote TEXT NOT NULL,
                    realized_pnl_quote TEXT NOT NULL, price TEXT NOT NULL, occurred_at REAL NOT NULL
                );
            """)
            finality_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(economic_exit_finality)")
            }
            if "proof_json" not in finality_columns:
                conn.execute(
                    "ALTER TABLE economic_exit_finality ADD COLUMN proof_json TEXT"
                )

    @staticmethod
    def _owners_json(owners: Iterable[EconomicExitOwner]) -> str:
        return json.dumps(
            [
                {
                    "proposal_id": owner.proposal_id,
                    "executor_id": owner.executor_id,
                    "intent": owner.intent.value,
                    "account_name": owner.account_name,
                    "connector_name": owner.connector_name,
                    "lot_ids": list(owner.lot_ids),
                    "target_base": str(owner.target_base),
                    "target_cost_basis_quote": str(owner.target_cost_basis_quote),
                    "target_entry_fees_quote": str(owner.target_entry_fees_quote),
                }
                for owner in owners
            ],
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _owners_from_json(raw: str) -> tuple[EconomicExitOwner, ...]:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise RuntimeError("corrupt economic exit owner manifest")

        result: list[EconomicExitOwner] = []
        for row in parsed:
            if not isinstance(row, dict):
                raise RuntimeError("corrupt economic exit owner manifest")
            lots = row.get("lot_ids")
            if not isinstance(lots, list) or not lots:
                raise RuntimeError("corrupt economic exit owner lot scope")
            result.append(
                EconomicExitOwner(
                    proposal_id=_text(row.get("proposal_id"), "proposal_id"),
                    executor_id=_text(row.get("executor_id"), "executor_id"),
                    intent=IntentKind(str(row.get("intent"))),
                    account_name=_text(
                        row.get("account_name"),
                        "account_name",
                    ),
                    connector_name=_text(
                        row.get("connector_name"),
                        "connector_name",
                    ).lower(),
                    lot_ids=tuple(_text(value, "lot_id") for value in lots),
                    target_base=Decimal(str(row.get("target_base"))),
                    target_cost_basis_quote=Decimal(
                        str(row.get("target_cost_basis_quote"))
                    ),
                    target_entry_fees_quote=Decimal(
                        str(row.get("target_entry_fees_quote"))
                    ),
                )
            )
        return tuple(result)

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> EconomicPositionExitManifest:
        return EconomicPositionExitManifest(
            manifest_id=str(row["manifest_id"]),
            position_id=str(row["position_id"]),
            pair=str(row["pair"]),
            status=EconomicExitManifestStatus(str(row["status"])),
            target_active_base=Decimal(str(row["target_active_base"])),
            target_cost_basis_quote=Decimal(str(row["target_cost_basis_quote"])),
            target_entry_fees_quote=Decimal(str(row["target_entry_fees_quote"])),
            reference_price=Decimal(str(row["reference_price"])),
            min_order_size=Decimal(str(row["min_order_size"])),
            min_notional_quote=Decimal(str(row["min_notional_quote"])),
            realized_pnl_before_quote=Decimal(str(row["realized_pnl_before_quote"])),
            strategic_retained_base_at_creation=Decimal(
                str(row["strategic_retained_base_at_creation"])
            ),
            unattributed_base_at_creation=Decimal(
                str(row["unattributed_base_at_creation"])
            ),
            owners=cls._owners_from_json(str(row["owners_json"])),
            created_at=float(row["created_at"]),
            completed_at=(
                None if row["completed_at"] is None else float(row["completed_at"])
            ),
            target_net_profit_bps=(
                None
                if row["target_net_profit_bps"] is None
                else Decimal(row["target_net_profit_bps"])
            ),
            expected_exit_cost_bps=(
                None
                if row["expected_exit_cost_bps"] is None
                else Decimal(row["expected_exit_cost_bps"])
            ),
            protected_limit_price=(
                None
                if row["protected_limit_price"] is None
                else Decimal(row["protected_limit_price"])
            ),
            sell_executor_id=row["sell_executor_id"],
            cash_proceeds_route=row["cash_proceeds_route"],
        )

    def load(self, manifest_id: str) -> EconomicPositionExitManifest | None:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_exit_manifests
                WHERE manifest_id = ?
                """,
                (mid,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def open_for_position(
        self,
        position_id: str,
    ) -> EconomicPositionExitManifest | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_exit_manifests
                WHERE position_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pid, EconomicExitManifestStatus.OPEN.value),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def latest_for_position(
        self,
        position_id: str,
    ) -> EconomicPositionExitManifest | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_exit_manifests
                WHERE position_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pid,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def create(
        self,
        manifest: EconomicPositionExitManifest,
    ) -> EconomicPositionExitManifest:
        if manifest.status != EconomicExitManifestStatus.OPEN:
            raise ValueError("new economic exit manifest must be OPEN")
        owners_json = self._owners_json(manifest.owners)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM economic_exit_manifests
                    WHERE manifest_id = ?
                    """,
                    (manifest.manifest_id,),
                ).fetchone()
                if existing is not None:
                    current = self._from_row(existing)
                    if current != manifest:
                        raise RuntimeError("economic exit manifest replay conflicts")
                    conn.rollback()
                    return current

                open_row = conn.execute(
                    """
                    SELECT *
                    FROM economic_exit_manifests
                    WHERE position_id = ? AND status = ?
                    """,
                    (
                        manifest.position_id,
                        EconomicExitManifestStatus.OPEN.value,
                    ),
                ).fetchone()
                if open_row is not None:
                    conn.rollback()
                    return self._from_row(open_row)

                # Whole-position and partial sells share one ownership ledger.
                # Recheck both scope and dispatch exclusions inside the same
                # write transaction used to install the immutable manifest.
                tables = {
                    str(row["name"])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "partial_recycle_manifests" in tables:
                    if conn.execute(
                        "SELECT 1 FROM partial_recycle_manifests WHERE position_id = ? AND status = 'OPEN' LIMIT 1",
                        (manifest.position_id,),
                    ).fetchone():
                        raise ValueError(
                            "open partial manifest blocks whole-position exit"
                        )
                    if "partial_recycle_dispatch_claims" in tables:
                        from .partial_recycle import (
                            partial_dispatch_terminal_certificate,
                        )

                        claims = conn.execute(
                            "SELECT d.manifest_id FROM partial_recycle_dispatch_claims d JOIN partial_recycle_manifests m "
                            "ON m.manifest_id = d.manifest_id WHERE m.position_id = ?",
                            (manifest.position_id,),
                        ).fetchall()
                        for claim in claims:
                            if (
                                partial_dispatch_terminal_certificate(
                                    conn, str(claim["manifest_id"]), schema="main"
                                )
                                is None
                            ):
                                raise ValueError(
                                    "unproven partial dispatch blocks whole-position exit"
                                )
                position = conn.execute(
                    "SELECT pair,status FROM economic_positions WHERE position_id = ?",
                    (manifest.position_id,),
                ).fetchone()
                if (
                    position is None
                    or str(position["status"]) != EconomicPositionStatus.ACTIVE.value
                    or str(position["pair"]) != manifest.pair
                ):
                    raise ValueError(
                        "whole-position exit requires unchanged ACTIVE ownership"
                    )
                lots = conn.execute(
                    "SELECT * FROM economic_lots WHERE position_id = ? AND ownership = 'ACTIVE_TRADE'",
                    (manifest.position_id,),
                ).fetchall()
                remaining = {
                    str(row["lot_id"]): row
                    for row in lots
                    if Decimal(str(row["base_remaining"])) > ZERO
                }
                if set(remaining) != set(manifest.lot_ids):
                    raise ValueError(
                        "whole-position exit lot scope changed before manifest claim"
                    )
                for owner in manifest.owners:
                    scoped = [remaining[lot_id] for lot_id in owner.lot_ids]
                    if any(
                        str(row["proposal_id"]) != owner.proposal_id
                        or str(row["executor_id"]) != owner.executor_id
                        for row in scoped
                    ):
                        raise ValueError(
                            "whole-position exit owner changed before manifest claim"
                        )
                    if (
                        exact_decimal_sum(
                            Decimal(str(row["base_remaining"])) for row in scoped
                        )
                        != owner.target_base
                        or exact_decimal_sum(
                            Decimal(str(row["cost_basis_remaining_quote"]))
                            for row in scoped
                        )
                        != owner.target_cost_basis_quote
                        or exact_decimal_sum(
                            Decimal(str(row["entry_fees_remaining_quote"]))
                            for row in scoped
                        )
                        != owner.target_entry_fees_quote
                    ):
                        raise ValueError(
                            "whole-position exit geometry changed before manifest claim"
                        )

                conn.execute(
                    """
                    INSERT INTO economic_exit_manifests (
                        manifest_id,
                        position_id,
                        pair,
                        status,
                        target_active_base,
                        target_cost_basis_quote,
                        target_entry_fees_quote,
                        reference_price,
                        min_order_size,
                        min_notional_quote,
                        realized_pnl_before_quote,
                        strategic_retained_base_at_creation,
                        unattributed_base_at_creation,
                        owners_json,
                        created_at,
                        completed_at,
                        target_net_profit_bps, expected_exit_cost_bps,
                        protected_limit_price, sell_executor_id, cash_proceeds_route
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.manifest_id,
                        manifest.position_id,
                        manifest.pair,
                        manifest.status.value,
                        str(manifest.target_active_base),
                        str(manifest.target_cost_basis_quote),
                        str(manifest.target_entry_fees_quote),
                        str(manifest.reference_price),
                        str(manifest.min_order_size),
                        str(manifest.min_notional_quote),
                        str(manifest.realized_pnl_before_quote),
                        str(manifest.strategic_retained_base_at_creation),
                        str(manifest.unattributed_base_at_creation),
                        owners_json,
                        manifest.created_at,
                        (
                            None
                            if manifest.target_net_profit_bps is None
                            else str(manifest.target_net_profit_bps)
                        ),
                        (
                            None
                            if manifest.expected_exit_cost_bps is None
                            else str(manifest.expected_exit_cost_bps)
                        ),
                        (
                            None
                            if manifest.protected_limit_price is None
                            else str(manifest.protected_limit_price)
                        ),
                        manifest.sell_executor_id,
                        manifest.cash_proceeds_route,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        created = self.load(manifest.manifest_id)
        if created is None:
            raise RuntimeError("economic exit manifest creation failed")
        return created

    def dispatch_claimed(self, manifest_id: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM economic_exit_dispatch_claims WHERE manifest_id = ?",
                    (_text(manifest_id, "manifest_id"),),
                ).fetchone()
                is not None
            )

    def claim_dispatch(self, *, manifest_id: str, now: float) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM economic_exit_manifests WHERE manifest_id = ?",
                (_text(manifest_id, "manifest_id"),),
            ).fetchone()
            if row is None or row["status"] != EconomicExitManifestStatus.OPEN.value:
                raise RuntimeError("economic exit dispatch requires an OPEN manifest")
            if conn.execute(
                "SELECT 1 FROM economic_exit_dispatch_claims WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone():
                return False
            manifest = self._from_row(
                conn.execute(
                    "SELECT * FROM economic_exit_manifests WHERE manifest_id = ?",
                    (manifest_id,),
                ).fetchone()
            )
            position = conn.execute(
                "SELECT status FROM economic_positions WHERE position_id = ?",
                (manifest.position_id,),
            ).fetchone()
            if (
                position is None
                or position["status"] != EconomicPositionStatus.ACTIVE.value
            ):
                raise RuntimeError(
                    "economic exit dispatch requires unchanged ACTIVE ownership"
                )
            lots = conn.execute(
                "SELECT * FROM economic_lots WHERE position_id = ? AND ownership = 'ACTIVE_TRADE'",
                (manifest.position_id,),
            ).fetchall()
            remaining = {
                str(lot["lot_id"]): lot
                for lot in lots
                if Decimal(lot["base_remaining"]) > ZERO
            }
            if set(remaining) != set(manifest.lot_ids) or any(
                exact_decimal_sum(
                    Decimal(remaining[lid][field]) for lid in manifest.lot_ids
                )
                != target
                for field, target in (
                    ("base_remaining", manifest.target_active_base),
                    ("cost_basis_remaining_quote", manifest.target_cost_basis_quote),
                    ("entry_fees_remaining_quote", manifest.target_entry_fees_quote),
                )
            ):
                raise RuntimeError(
                    "economic exit ownership changed before dispatch claim"
                )
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_recycle_manifests'"
            ).fetchone():
                if conn.execute(
                    "SELECT 1 FROM partial_recycle_manifests WHERE position_id = ? AND status='OPEN'",
                    (manifest.position_id,),
                ).fetchone():
                    raise RuntimeError(
                        "open partial manifest blocks economic exit dispatch"
                    )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO economic_exit_dispatch_claims VALUES (?, ?)",
                (manifest_id, _time(now, "now")),
            )
            return cursor.rowcount == 1

    def protection_conflicts(self, manifest_id: str) -> tuple[str, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conflict FROM economic_exit_protection_conflicts "
                "WHERE manifest_id = ? ORDER BY conflict",
                (manifest_id,),
            ).fetchall()
        return tuple(str(row["conflict"]) for row in rows)

    def record_protection_conflict(
        self, *, manifest_id: str, conflict: str, now: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO economic_exit_protection_conflicts VALUES (?, ?, ?)",
                (
                    _text(manifest_id, "manifest_id"),
                    _text(conflict, "conflict"),
                    _time(now, "now"),
                ),
            )

    def record_finality(
        self, *, manifest_id: str, executor_id: str, now: float, proof: dict
    ) -> None:
        manifest = self.load(manifest_id)
        if manifest is None or executor_id != manifest.sell_executor_id:
            raise RuntimeError("economic exit finality executor identity changed")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            journal = conn.execute(
                "SELECT * FROM economic_exit_fills WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchall()
            _validate_whole_terminal_proof(proof, manifest, journal)
            current = exact_decimal_sum(
                Decimal(row[0])
                for row in conn.execute(
                    "SELECT base_remaining FROM economic_lots WHERE position_id = ? AND ownership='ACTIVE_TRADE'",
                    (manifest.position_id,),
                )
            )
            if current != Decimal(proof["remaining_base"]):
                raise RuntimeError(
                    "whole terminal inventory changed before finality claim"
                )
            conn.execute(
                "INSERT INTO economic_exit_finality (manifest_id, executor_id, observed_at, proof_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(manifest_id) DO UPDATE SET "
                "executor_id=excluded.executor_id, observed_at=excluded.observed_at, proof_json=excluded.proof_json",
                (
                    manifest_id,
                    executor_id,
                    _time(now, "now"),
                    json.dumps(proof, sort_keys=True),
                ),
            )

    def finality_confirmed(self, manifest_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT proof_json FROM economic_exit_finality WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            if row is None or row["proof_json"] is None:
                return False
            manifest_row = conn.execute(
                "SELECT * FROM economic_exit_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            journal = conn.execute(
                "SELECT * FROM economic_exit_fills WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchall()
            _validate_whole_terminal_proof(
                json.loads(row["proof_json"]), self._from_row(manifest_row), journal
            )
            return True

    def fill_recorded(self, trade_id: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM economic_exit_fills WHERE trade_id = ?",
                    (_text(trade_id, "trade_id"),),
                ).fetchone()
                is not None
            )

    def record_fill(
        self,
        *,
        trade_id: str,
        manifest_id: str,
        order_id: str,
        base_sold: Decimal,
        gross_proceeds_quote: Decimal,
        exit_fee_quote: Decimal,
        allocated_basis_quote: Decimal,
        allocated_entry_fees_quote: Decimal,
        realized_pnl_quote: Decimal,
        price: Decimal,
        occurred_at: float,
    ) -> None:
        tid = _text(trade_id, "trade_id")
        fields = (
            "manifest_id",
            "order_id",
            "base_sold",
            "gross_proceeds_quote",
            "exit_fee_quote",
            "allocated_basis_quote",
            "allocated_entry_fees_quote",
            "realized_pnl_quote",
            "price",
            "occurred_at",
        )
        values = (
            _text(manifest_id, "manifest_id"),
            _text(order_id, "order_id"),
            str(_decimal(base_sold, "base_sold", positive=True)),
            str(
                _decimal(
                    gross_proceeds_quote, "gross_proceeds_quote", non_negative=True
                )
            ),
            str(_decimal(exit_fee_quote, "exit_fee_quote", non_negative=True)),
            str(
                _decimal(
                    allocated_basis_quote, "allocated_basis_quote", non_negative=True
                )
            ),
            str(
                _decimal(
                    allocated_entry_fees_quote,
                    "allocated_entry_fees_quote",
                    non_negative=True,
                )
            ),
            str(_decimal(realized_pnl_quote, "realized_pnl_quote")),
            str(_decimal(price, "price", positive=True)),
            _time(occurred_at, "occurred_at"),
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM economic_exit_fills WHERE trade_id = ?", (tid,)
            ).fetchone()
            if existing is not None:
                if tuple(existing[field] for field in fields) != values:
                    raise RuntimeError("economic exit fill replay conflicts")
                return
            conn.execute(
                "INSERT INTO economic_exit_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, *values),
            )

    def validate_fill_coverage(
        self,
        manifest: EconomicPositionExitManifest,
        *,
        remaining: Decimal,
        now: float | None,
    ) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM economic_exit_fills WHERE manifest_id = ?",
                (manifest.manifest_id,),
            ).fetchall()
        if exact_decimal_sum(
            (Decimal(row["base_sold"]) for row in rows)
        ) != exact_decimal_difference(manifest.target_active_base, remaining):
            raise RuntimeError(
                "economic exit disposed inventory lacks exact durable fill coverage"
            )
        with localcontext() as context:
            context.prec = 80
            for row in rows:
                basis = Decimal(row["allocated_basis_quote"]) + Decimal(
                    row["allocated_entry_fees_quote"]
                )
                required = basis * manifest.target_net_profit_bps / BPS
                if (
                    Decimal(row["price"]) < manifest.protected_limit_price
                    or Decimal(row["realized_pnl_quote"]) < required
                ):
                    if now is None:
                        raise RuntimeError(
                            "economic exit profit protection conflict requires resolution"
                        )
                    self.record_protection_conflict(
                        manifest_id=manifest.manifest_id,
                        conflict=f"profit_floor_breached:{row['trade_id']}",
                        now=now,
                    )
        if self.protection_conflicts(manifest.manifest_id):
            raise RuntimeError(
                "economic exit profit protection conflict requires resolution"
            )

    def mark_completed(
        self,
        manifest_id: str,
        *,
        completed_at: float,
    ) -> EconomicPositionExitManifest:
        mid = _text(manifest_id, "manifest_id")
        timestamp = _time(completed_at, "completed_at")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE economic_exit_manifests
                SET status = ?, completed_at = COALESCE(completed_at, ?)
                WHERE manifest_id = ?
                """,
                (
                    EconomicExitManifestStatus.COMPLETED.value,
                    timestamp,
                    mid,
                ),
            )
        result = self.load(mid)
        if result is None:
            raise ValueError(f"unknown economic exit manifest: {mid}")
        return result


def _validate_whole_terminal_proof(proof, manifest, journal):
    """Validate exact terminal SELL coverage without trusting aggregate balances."""
    if not isinstance(proof, dict):
        raise RuntimeError("whole terminal proof is not an object")
    body = {key: value for key, value in proof.items() if key != "sha256"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        proof.get("sha256") != digest
        or proof.get("schema") != "spectra-whole-terminal-v1"
        or proof.get("manifest_id") != manifest.manifest_id
        or proof.get("executor_id") != manifest.sell_executor_id
        or proof.get("executor_status") != "TERMINATED"
    ):
        raise RuntimeError("whole terminal proof identity conflicts")
    orders, trades = proof.get("orders"), proof.get("trades")
    if not isinstance(orders, list) or not orders or not isinstance(trades, list):
        raise RuntimeError("whole terminal proof lacks exact order/trade coverage")
    by_order = {row["order_id"]: row for row in orders}
    by_trade = {row["trade_id"]: row for row in trades}
    actual = {str(row["trade_id"]): row for row in journal}
    if (
        len(by_order) != len(orders)
        or len(by_trade) != len(trades)
        or set(by_trade) != set(actual)
    ):
        raise RuntimeError("whole terminal proof repeats or omits durable fills")
    volume = _decimal(
        Decimal(str(proof.get("executor_filled_quote"))),
        "terminal_executor_volume",
        non_negative=True,
    )
    if Fraction(volume) != sum(
        (
            Fraction(Decimal(trade["amount_base"])) * Fraction(Decimal(trade["price"]))
            for trade in trades
        ),
        Fraction(0),
    ):
        raise RuntimeError(
            "whole terminal proof executor volume differs from exact trades"
        )
    remaining = _decimal(
        Decimal(str(proof.get("remaining_base"))),
        "terminal_remaining_base",
        non_negative=True,
    )
    if exact_decimal_sum(
        Decimal(row["base_sold"]) for row in journal
    ) != exact_decimal_difference(manifest.target_active_base, remaining):
        raise RuntimeError(
            "whole terminal proof inventory lacks exact disposed fill coverage"
        )
    terminal = {"FILLED", "CANCELED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}
    for order_id, order in by_order.items():
        amount = _decimal(
            Decimal(order["amount"]), "terminal_order_amount", positive=True
        )
        filled = _decimal(
            Decimal(order["filled_amount"]), "terminal_order_fill", non_negative=True
        )
        if (
            order["status"] not in terminal
            or order["side"] != "SELL"
            or order["order_type"] != "LIMIT"
            or _decimal(Decimal(order["price"]), "terminal_order_price", positive=True)
            < manifest.protected_limit_price
            or filled > amount
            or (order["status"] == "FILLED" and filled != amount)
            or filled
            != exact_decimal_sum(
                Decimal(trade["amount_base"])
                for trade in trades
                if trade["order_id"] == order_id
            )
        ):
            raise RuntimeError(
                "whole terminal proof includes live, unprotected, or incompletely covered orders"
            )
    scopes = {(owner.account_name, owner.connector_name) for owner in manifest.owners}
    if len(scopes) != 1:
        raise RuntimeError("whole terminal proof has ambiguous owner scope")
    account, connector = next(iter(scopes))
    base_asset, quote_asset = manifest.pair.rsplit("-", 1)
    for trade_id, trade in by_trade.items():
        stored = actual[trade_id]
        if (
            trade["order_id"] not in by_order
            or trade["side"] != "SELL"
            or (trade["pair"], trade["account_name"], trade["connector_name"])
            != (manifest.pair, account, connector)
            or str(stored["order_id"]) != trade["order_id"]
        ):
            raise RuntimeError("whole terminal trade crosses immutable scope")
        amount = _decimal(
            Decimal(trade["amount_base"]), "terminal_trade_amount", positive=True
        )
        price = _decimal(Decimal(trade["price"]), "terminal_trade_price", positive=True)
        fee = _decimal(
            Decimal(trade["fee_paid"]), "terminal_trade_fee", non_negative=True
        )
        fee_currency = trade["fee_currency"]
        if fee > ZERO and fee_currency not in {base_asset, quote_asset}:
            raise RuntimeError("whole terminal trade fee has unproven valuation")
        sold = Fraction(amount) + (Fraction(fee) if fee_currency == base_asset else 0)
        gross = Fraction(amount) * Fraction(price)
        exit_fee = Fraction(fee) if fee_currency == quote_asset else 0
        if (
            Fraction(Decimal(stored["base_sold"])) != sold
            or Fraction(Decimal(stored["gross_proceeds_quote"])) != gross
            or Fraction(Decimal(stored["exit_fee_quote"])) != exit_fee
            or Decimal(stored["price"]) != price
            or float(stored["occurred_at"])
            != _time(trade["occurred_at"], "terminal_trade_time")
        ):
            raise RuntimeError(
                "whole terminal proof conflicts with exact durable fills"
            )
        invested = Fraction(Decimal(stored["allocated_basis_quote"])) + Fraction(
            Decimal(stored["allocated_entry_fees_quote"])
        )
        if (
            price < manifest.protected_limit_price
            or Fraction(Decimal(stored["realized_pnl_quote"]))
            < invested * Fraction(manifest.target_net_profit_bps) / 10000
        ):
            raise RuntimeError(
                "whole terminal proof violates immutable profit protection"
            )


def economic_exit_terminal_certificate(connection, manifest_id, *, schema="main"):
    """Read a complete protected whole SELL proof under the ownership lock."""
    if (
        not isinstance(schema, str)
        or not schema.isidentifier()
        or schema
        not in {str(row[1]) for row in connection.execute("PRAGMA database_list")}
    ):
        raise ValueError("invalid whole certificate database schema")
    tables = {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
        )
    }
    required = {
        "economic_exit_manifests",
        "economic_exit_dispatch_claims",
        "economic_exit_finality",
        "economic_exit_fills",
    }
    if not required.issubset(tables):
        return None
    if not connection.execute(
        f"SELECT 1 FROM {schema}.economic_exit_dispatch_claims WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone():
        return None
    columns = {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA {schema}.table_info(economic_exit_finality)"
        )
    }
    if "proof_json" not in columns:
        return None
    row = connection.execute(
        f"SELECT * FROM {schema}.economic_exit_finality WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    if row is None or row["proof_json"] is None:
        return None
    manifest_row = connection.execute(
        f"SELECT * FROM {schema}.economic_exit_manifests WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    if manifest_row is None:
        raise RuntimeError("whole terminal certificate lost its manifest")
    manifest = EconomicExitManifestStore._from_row(manifest_row)
    if (
        manifest.cash_proceeds_route != "PRODUCTIVE"
        or manifest.sell_executor_id != row["executor_id"]
    ):
        raise RuntimeError(
            "whole terminal certificate changed its execution or cash route"
        )
    if (
        "economic_exit_protection_conflicts" in tables
        and connection.execute(
            f"SELECT 1 FROM {schema}.economic_exit_protection_conflicts WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
    ):
        raise RuntimeError(
            "whole terminal certificate has unresolved profit protection conflict"
        )
    journal = connection.execute(
        f"SELECT * FROM {schema}.economic_exit_fills WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchall()
    proof = json.loads(row["proof_json"])
    _validate_whole_terminal_proof(proof, manifest, journal)
    lots = connection.execute(
        f"SELECT base_remaining FROM {schema}.economic_lots WHERE position_id = ? AND ownership = 'ACTIVE_TRADE'",
        (manifest.position_id,),
    ).fetchall()
    remaining = exact_decimal_sum(Decimal(lot[0]) for lot in lots)
    disposed = exact_decimal_sum(Decimal(fill["base_sold"]) for fill in journal)
    if disposed != exact_decimal_difference(
        manifest.target_active_base, Decimal(proof["remaining_base"])
    ) or remaining > Decimal(proof["remaining_base"]):
        raise RuntimeError(
            "whole terminal certificate inventory lacks exact fill coverage"
        )
    return proof


def _manifest_id(
    *,
    position_id: str,
    pair: str,
    owners: tuple[EconomicExitOwner, ...],
) -> str:
    payload = EconomicExitManifestStore._owners_json(owners)
    digest = hashlib.sha256(
        f"{position_id}|{pair}|{payload}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{position_id}:exit:{digest}"


def _group_owners(
    *,
    lots: tuple[EconomicLot, ...],
    governor: CapitalGovernor,
    pair: str,
) -> tuple[EconomicExitOwner, ...]:
    grouped: dict[tuple[str, str], list[EconomicLot]] = {}
    executor_owner: dict[str, str] = {}

    for lot in lots:
        if lot.proposal_id is None or lot.executor_id is None:
            raise ValueError(
                f"active lot {lot.lot_id} lacks exact proposal/executor ownership"
            )
        conflicts = getattr(governor, "get_protected_buy_conflicts", lambda _: ())(
            lot.proposal_id
        )
        if conflicts:
            raise ValueError(
                "economic exit owner has unresolved protected BUY conflict"
            )

        provenance = governor.get_execution_provenance(lot.proposal_id)
        receipt = governor.get_execution_receipt(lot.proposal_id)
        if provenance is None or receipt != lot.executor_id:
            raise ValueError(f"active lot {lot.lot_id} lost exact execution provenance")
        if provenance.executor_id != lot.executor_id:
            raise ValueError(f"active lot {lot.lot_id} executor ownership changed")
        if provenance.pair.strip().upper() != pair:
            raise ValueError(f"active lot {lot.lot_id} crosses exit pair")
        if provenance.intent not in {IntentKind.ENTRY, IntentKind.RECOVERY}:
            raise ValueError(f"active lot {lot.lot_id} is not owned trade capital")
        if provenance.account_name is None or provenance.connector_name is None:
            raise ValueError(
                f"active lot {lot.lot_id} has unscoped execution provenance"
            )

        prior = executor_owner.get(lot.executor_id)
        if prior is not None and prior != lot.proposal_id:
            raise ValueError("one executor cannot own multiple live capital proposals")
        executor_owner[lot.executor_id] = lot.proposal_id
        grouped.setdefault(
            (lot.proposal_id, lot.executor_id),
            [],
        ).append(lot)

    owners: list[EconomicExitOwner] = []
    for proposal_id, executor_id in sorted(grouped):
        owner_lots = grouped[(proposal_id, executor_id)]
        provenance = governor.get_execution_provenance(proposal_id)
        if provenance is None:
            raise RuntimeError("exit owner provenance disappeared")

        owners.append(
            EconomicExitOwner(
                proposal_id=proposal_id,
                executor_id=executor_id,
                intent=provenance.intent,
                account_name=str(provenance.account_name),
                connector_name=str(provenance.connector_name).lower(),
                lot_ids=tuple(lot.lot_id for lot in owner_lots),
                target_base=exact_decimal_sum(
                    (lot.base_remaining for lot in owner_lots)
                ),
                target_cost_basis_quote=exact_decimal_sum(
                    (lot.cost_basis_remaining_quote for lot in owner_lots)
                ),
                target_entry_fees_quote=exact_decimal_sum(
                    (lot.entry_fees_remaining_quote for lot in owner_lots)
                ),
            )
        )

    if not owners:
        raise ValueError("economic position has no exact active trade owners")
    return tuple(owners)


def _validate_sellability(
    owners: tuple[EconomicExitOwner, ...],
    *,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
) -> None:
    price = _decimal(current_price, "current_price", positive=True)
    min_base = trading_rule.min_order_size
    min_quote = trading_rule.effective_min_quote

    for owner in owners:
        if owner.target_base < min_base:
            raise ValueError(
                f"exit owner {owner.proposal_id} is below Kraken min order size"
            )
        if owner.target_base * price < min_quote:
            raise ValueError(
                f"exit owner {owner.proposal_id} is below Kraken min notional"
            )


def validate_manifest_against_ledger(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
    allow_additional_inventory: bool = False,
    governor: CapitalGovernor | None = None,
) -> None:
    if manifest.cash_proceeds_route not in {None, "PRODUCTIVE"}:
        raise RuntimeError("whole exit cash route must preserve PRODUCTIVE purpose")
    durable = EconomicExitManifestStore(ledger.path).load(manifest.manifest_id)
    if durable is None or durable != replace(
        manifest, status=durable.status, completed_at=durable.completed_at
    ):
        raise RuntimeError(
            "economic exit immutable manifest differs from durable state"
        )
    protected_fields = (
        manifest.target_net_profit_bps,
        manifest.expected_exit_cost_bps,
        manifest.protected_limit_price,
        manifest.sell_executor_id,
    )
    if any(value is not None for value in protected_fields):
        if any(value is None for value in protected_fields):
            raise RuntimeError("economic exit profit protection is incomplete")
        price = _decimal(
            manifest.protected_limit_price, "protected_limit_price", positive=True
        )
        target = _decimal(
            manifest.target_net_profit_bps, "target_net_profit_bps", non_negative=True
        )
        costs = _decimal(
            manifest.expected_exit_cost_bps, "expected_exit_cost_bps", non_negative=True
        )
        if costs >= BPS:
            raise RuntimeError("economic exit costs invalidate protection")
        with localcontext() as context:
            context.prec = 80
            invested = (
                manifest.target_cost_basis_quote + manifest.target_entry_fees_quote
            )
            if price * manifest.target_active_base * (1 - costs / BPS) < invested * (
                1 + target / BPS
            ):
                raise RuntimeError(
                    "economic exit limit violates configured profit floor"
                )
    snapshot = ledger.snapshot(manifest.position_id)
    if snapshot.pair != manifest.pair:
        raise RuntimeError("economic exit manifest pair changed")
    if (
        snapshot.strategic_retained_base_same_pair
        < manifest.strategic_retained_base_at_creation
    ):
        raise RuntimeError(
            "strategic retained inventory decreased during economic exit"
        )

    allowed_lot_ids = set(manifest.lot_ids)
    current_lots = ledger.list_active_lots(manifest.position_id)
    unexpected = [
        lot.lot_id for lot in current_lots if lot.lot_id not in allowed_lot_ids
    ]
    if unexpected and not allow_additional_inventory:
        raise RuntimeError(
            "new ACTIVE_TRADE inventory appeared after exit manifest creation"
        )
    if unexpected:
        if governor is None:
            raise RuntimeError(
                "additional inventory reconciliation requires exact capital authority"
            )
        scopes = {
            (owner.account_name, owner.connector_name) for owner in manifest.owners
        }
        if len(scopes) != 1:
            raise RuntimeError(
                "additional inventory has ambiguous economic owner scope"
            )
        scope = next(iter(scopes))
        for lot in current_lots:
            if lot.lot_id in allowed_lot_ids:
                continue
            provenance = (
                None
                if lot.proposal_id is None
                else governor.get_execution_provenance(lot.proposal_id)
            )
            if (
                provenance is None
                or lot.executor_id != provenance.executor_id
                or governor.get_execution_receipt(lot.proposal_id)
                != provenance.executor_id
                or provenance.pair.strip().upper() != manifest.pair
                or (provenance.account_name, (provenance.connector_name or "").lower())
                != scope
                or provenance.intent not in {IntentKind.ENTRY, IntentKind.RECOVERY}
            ):
                raise RuntimeError(
                    "additional inventory lacks exact scoped execution ownership"
                )
        # A previously dispatched SELL may only consume its original manifest
        # lot IDs; subsequent confirmed BUY lots stay outside that parcel.
        current_lots = [lot for lot in current_lots if lot.lot_id in allowed_lot_ids]

    owner_targets = {
        (owner.proposal_id, owner.executor_id): owner.target_base
        for owner in manifest.owners
    }
    current_by_owner: dict[tuple[str, str], Decimal] = {}
    for lot in current_lots:
        if lot.proposal_id is None or lot.executor_id is None:
            raise RuntimeError("active inventory lost exact ownership during exit")
        key = (lot.proposal_id, lot.executor_id)
        if key not in owner_targets:
            raise RuntimeError(
                "active inventory owner is outside immutable exit manifest"
            )
        current_by_owner[key] = exact_decimal_sum(
            (current_by_owner.get(key, ZERO), lot.base_remaining)
        )

    for key, current in current_by_owner.items():
        if current > owner_targets[key]:
            raise RuntimeError(
                "active inventory increased after exit manifest creation"
            )


def create_or_load_position_exit_manifest(
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    position_id: str,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    now: float,
    store: EconomicExitManifestStore | None = None,
    target_net_profit_bps: Decimal | None = None,
    expected_exit_cost_bps: Decimal | None = None,
    protected_floor_price: Decimal | None = None,
) -> EconomicPositionExitManifest:
    pid = _text(position_id, "position_id")
    timestamp = _time(now, "now")
    manifest_store = store or EconomicExitManifestStore(ledger.path)

    if ledger.snapshot(pid).status == EconomicPositionStatus.RESIDUAL:
        raise ValueError("retired residual ownership cannot dispatch another exit")

    existing = manifest_store.open_for_position(pid)
    if existing is not None:
        validate_manifest_against_ledger(existing, ledger=ledger)
        return existing

    snapshot = ledger.snapshot(pid)
    if snapshot.status != EconomicPositionStatus.ACTIVE:
        raise ValueError("cannot manifest exit for closed economic position")
    if snapshot.active_base <= ZERO:
        raise ValueError("economic position has no active trade inventory")
    if trading_rule.trading_pair.strip().upper() != snapshot.pair:
        raise ValueError("exit trading rule pair does not match position")

    lots = ledger.list_active_lots(pid)
    owners = _group_owners(
        lots=lots,
        governor=governor,
        pair=snapshot.pair,
    )
    protected = target_net_profit_bps is not None or expected_exit_cost_bps is not None
    if protected and (target_net_profit_bps is None or expected_exit_cost_bps is None):
        raise ValueError("economic exit requires explicit target and exit costs")
    if not protected:
        if protected_floor_price is not None:
            raise ValueError("economic exit floor requires explicit target and costs")
        _validate_sellability(
            owners, trading_rule=trading_rule, current_price=current_price
        )

    target_base = exact_decimal_sum((owner.target_base for owner in owners))
    target_basis = exact_decimal_sum(
        (owner.target_cost_basis_quote for owner in owners)
    )
    target_fees = exact_decimal_sum((owner.target_entry_fees_quote for owner in owners))
    if (
        target_base != snapshot.active_base
        or target_basis != snapshot.active_cost_basis_quote
        or target_fees != snapshot.active_entry_fees_quote
    ):
        raise RuntimeError(
            "exit owner manifest does not exactly cover ACTIVE_TRADE geometry"
        )

    protected_price = None
    if protected:
        # Use the already-tested exact-parcel algebra. Whole-position disposal
        # follows the same proportional allocation, including ENTRY owners.
        protected_price = protected_partial_recycle_price(
            target_base=target_base,
            target_cost_basis_quote=target_basis,
            target_entry_fees_quote=target_fees,
            target_net_profit_bps=target_net_profit_bps,
            expected_exit_cost_bps=expected_exit_cost_bps,
            trading_rule=trading_rule,
        )
        if protected_floor_price is not None:
            floor = _decimal(
                protected_floor_price, "protected_floor_price", positive=True
            )
            with localcontext() as context:
                context.prec = 80
                tick = trading_rule.min_price_increment
                protected_price = max(
                    protected_price,
                    (floor / tick).to_integral_value(rounding=ROUND_CEILING) * tick,
                )

    identity = _manifest_id(position_id=pid, pair=snapshot.pair, owners=owners)

    manifest = EconomicPositionExitManifest(
        manifest_id=identity,
        position_id=pid,
        pair=snapshot.pair,
        status=EconomicExitManifestStatus.OPEN,
        target_active_base=target_base,
        target_cost_basis_quote=target_basis,
        target_entry_fees_quote=target_fees,
        reference_price=_decimal(
            current_price,
            "current_price",
            positive=True,
        ),
        min_order_size=trading_rule.min_order_size,
        min_notional_quote=trading_rule.effective_min_quote,
        realized_pnl_before_quote=snapshot.realized_pnl_quote,
        strategic_retained_base_at_creation=(
            snapshot.strategic_retained_base_same_pair
        ),
        unattributed_base_at_creation=(snapshot.unattributed_base_same_pair),
        owners=owners,
        created_at=timestamp,
        completed_at=None,
        target_net_profit_bps=target_net_profit_bps,
        expected_exit_cost_bps=expected_exit_cost_bps,
        protected_limit_price=protected_price,
        sell_executor_id=(
            None
            if not protected
            else "spectra-whole-exit-" + identity.rsplit(":", 1)[-1]
        ),
        cash_proceeds_route="PRODUCTIVE" if protected else None,
    )
    created = manifest_store.create(manifest)
    validate_manifest_against_ledger(created, ledger=ledger)
    return created


def remaining_manifest_base(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
) -> Decimal:
    validate_manifest_against_ledger(manifest, ledger=ledger)
    return ledger.snapshot(manifest.position_id).active_base


def realized_pnl_for_manifest(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
) -> Decimal:
    return exact_decimal_difference(
        ledger.snapshot(manifest.position_id).realized_pnl_quote,
        manifest.realized_pnl_before_quote,
    )


def assess_position_exit_manifest(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
    store: EconomicExitManifestStore | None = None,
    now: float | None = None,
) -> EconomicExitProgress:
    validate_manifest_against_ledger(manifest, ledger=ledger)
    snapshot = ledger.snapshot(manifest.position_id)
    if manifest.sell_executor_id is not None:
        manifest_store = store or EconomicExitManifestStore(ledger.path)
        if manifest_store.protection_conflicts(manifest.manifest_id):
            raise RuntimeError(
                "economic exit profit protection conflict requires resolution"
            )
        manifest_store.validate_fill_coverage(
            manifest, remaining=snapshot.active_base, now=now
        )

    realized = exact_decimal_difference(
        snapshot.realized_pnl_quote, manifest.realized_pnl_before_quote
    )
    retained = snapshot.strategic_retained_base_same_pair

    if snapshot.status == EconomicPositionStatus.RESIDUAL:
        return EconomicExitProgress(
            manifest_id=manifest.manifest_id,
            state=EconomicExitProgressState.RESIDUAL_RETAINED,
            remaining_active_base=snapshot.active_base,
            realized_pnl_quote=realized,
            strategic_retained_base=retained,
            reasons=(
                "terminal_residual_exact_ownership_retained",
                "no_capital_released_for_residual",
            ),
        )

    if snapshot.active_base == ZERO:
        if snapshot.status != EconomicPositionStatus.CLOSED:
            raise RuntimeError("zero active base did not close economic position")
        if (
            manifest.sell_executor_id is not None
            and not manifest_store.finality_confirmed(manifest.manifest_id)
        ):
            return EconomicExitProgress(
                manifest_id=manifest.manifest_id,
                state=EconomicExitProgressState.PENDING,
                remaining_active_base=ZERO,
                realized_pnl_quote=realized,
                strategic_retained_base=retained,
                reasons=("confirmed_inventory_flat_awaiting_sell_executor_finality",),
            )
        if store is not None:
            if now is None:
                raise ValueError("now is required to complete exit manifest")
            store.mark_completed(
                manifest.manifest_id,
                completed_at=_time(now, "now"),
            )
        return EconomicExitProgress(
            manifest_id=manifest.manifest_id,
            state=EconomicExitProgressState.COMPLETED,
            remaining_active_base=ZERO,
            realized_pnl_quote=realized,
            strategic_retained_base=retained,
            reasons=(
                "economic_position_active_trade_flat",
                "strategic_retained_inventory_untouched",
            ),
        )

    remaining = snapshot.active_base
    if (
        manifest.sell_executor_id is not None
        and manifest_store.dispatch_claimed(manifest.manifest_id)
        and not manifest_store.finality_confirmed(manifest.manifest_id)
    ):
        return EconomicExitProgress(
            manifest_id=manifest.manifest_id,
            state=EconomicExitProgressState.PENDING,
            remaining_active_base=remaining,
            realized_pnl_quote=realized,
            strategic_retained_base=retained,
            reasons=("protected_sell_pending_remainder_remains_owned",),
        )
    if (
        remaining < manifest.min_order_size
        or remaining * (manifest.protected_limit_price or manifest.reference_price)
        < manifest.min_notional_quote
    ):
        return EconomicExitProgress(
            manifest_id=manifest.manifest_id,
            state=EconomicExitProgressState.DUST_BLOCKED,
            remaining_active_base=remaining,
            realized_pnl_quote=realized,
            strategic_retained_base=retained,
            reasons=(
                "remaining_active_trade_below_exchange_minimum",
                "retained_inventory_cannot_satisfy_exit_deficit",
            ),
        )

    return EconomicExitProgress(
        manifest_id=manifest.manifest_id,
        state=EconomicExitProgressState.PENDING,
        remaining_active_base=remaining,
        realized_pnl_quote=realized,
        strategic_retained_base=retained,
        reasons=("economic_position_exit_still_reconciling",),
    )
