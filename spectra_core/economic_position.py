from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from pathlib import Path

from .capital_governor import CapitalGovernor
from .decimal_math import exact_decimal_difference, exact_decimal_sum
from .models import IntentKind
from .recovery_fingerprint import VCAPPositionState

ZERO = Decimal("0")


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _pair(value: str) -> str:
    pair = _text(value, "pair").upper()
    if "-" not in pair:
        raise ValueError("pair must be BASE-QUOTE")
    return pair


_BASE_ASSET_ALIASES = {
    "XBT": "BTC",
    "XXBT": "BTC",
    "XDG": "DOGE",
    "XXDG": "DOGE",
    "XETH": "ETH",
    "XXRP": "XRP",
}


def normalized_base_asset(pair: str) -> str:
    """Return the canonical base asset used for one-position-per-asset locking."""
    normalized = _pair(pair)
    base, _ = normalized.rsplit("-", 1)
    return _BASE_ASSET_ALIASES.get(base, base)


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


def _time(value: float, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


class OwnershipBucket(StrEnum):
    ACTIVE_TRADE = "ACTIVE_TRADE"
    STRATEGIC_RETAINED = "STRATEGIC_RETAINED"
    UNATTRIBUTED = "UNATTRIBUTED"


class EconomicLotSource(StrEnum):
    BASE_ENTRY = "BASE_ENTRY"
    VCAP_RECOVERY = "VCAP_RECOVERY"
    STRATEGIC_RETENTION = "STRATEGIC_RETENTION"
    EXTERNAL_UNATTRIBUTED = "EXTERNAL_UNATTRIBUTED"


class EconomicPositionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class EconomicLot:
    lot_id: str
    position_id: str | None
    pair: str
    ownership: OwnershipBucket
    source: EconomicLotSource
    base_original: Decimal
    cost_basis_original_quote: Decimal
    entry_fees_original_quote: Decimal
    base_remaining: Decimal
    cost_basis_remaining_quote: Decimal
    entry_fees_remaining_quote: Decimal
    proposal_id: str | None
    executor_id: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class EconomicPositionSnapshot:
    position_id: str
    pair: str
    status: EconomicPositionStatus
    active_base: Decimal
    active_cost_basis_quote: Decimal
    active_entry_fees_quote: Decimal
    realized_pnl_quote: Decimal
    strategic_retained_base_same_pair: Decimal
    unattributed_base_same_pair: Decimal
    active_lot_ids: tuple[str, ...]
    recovery_proposal_ids: tuple[str, ...]

    @property
    def has_active_inventory(self) -> bool:
        return self.active_base > ZERO

    def to_vcap_position(self, *, current_price: Decimal) -> VCAPPositionState:
        price = _decimal(current_price, "current_price", positive=True)
        if self.status != EconomicPositionStatus.ACTIVE:
            raise ValueError("economic position is closed")
        if self.active_base <= ZERO or self.active_cost_basis_quote <= ZERO:
            raise ValueError("economic position has no active trade inventory")
        return VCAPPositionState(
            pair=self.pair,
            position_base=self.active_base,
            cost_basis_quote=self.active_cost_basis_quote,
            current_price=price,
        )


@dataclass(frozen=True)
class EconomicExitResult:
    event_id: str
    position_id: str
    pair: str
    base_sold: Decimal
    gross_proceeds_quote: Decimal
    exit_fee_quote: Decimal
    allocated_cost_basis_quote: Decimal
    allocated_entry_fees_quote: Decimal
    realized_pnl_quote: Decimal
    active_base_remaining: Decimal
    position_closed: bool


class EconomicPositionLedger:
    """Durable ownership ledger for exact economic trade inventory.

    This ledger intentionally has no API that accepts an aggregate wallet
    balance as ownership evidence. Inventory must enter through an explicit lot
    with an ownership bucket. Recovery exits consume ACTIVE_TRADE lots only.
    STRATEGIC_RETAINED and UNATTRIBUTED inventory are never eligible to satisfy
    or close an active trading position.
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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS economic_positions (
                    position_id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    base_asset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS economic_lots (
                    lot_id TEXT PRIMARY KEY,
                    position_id TEXT,
                    pair TEXT NOT NULL,
                    ownership TEXT NOT NULL,
                    source TEXT NOT NULL,
                    base_original TEXT NOT NULL,
                    cost_basis_original_quote TEXT NOT NULL,
                    entry_fees_original_quote TEXT NOT NULL,
                    base_remaining TEXT NOT NULL,
                    cost_basis_remaining_quote TEXT NOT NULL,
                    entry_fees_remaining_quote TEXT NOT NULL,
                    proposal_id TEXT,
                    executor_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(position_id)
                        REFERENCES economic_positions(position_id)
                );

                DROP INDEX IF EXISTS economic_lots_proposal_unique;
                DROP INDEX IF EXISTS economic_lots_executor_unique;

                CREATE INDEX IF NOT EXISTS
                    economic_lots_proposal_idx
                ON economic_lots(proposal_id)
                WHERE proposal_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS
                    economic_lots_executor_idx
                ON economic_lots(executor_id)
                WHERE executor_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS economic_position_events (
                    event_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    base_amount TEXT NOT NULL,
                    quote_amount TEXT NOT NULL,
                    fee_quote TEXT NOT NULL,
                    allocated_cost_basis_quote TEXT NOT NULL,
                    allocated_entry_fees_quote TEXT NOT NULL,
                    realized_pnl_quote TEXT NOT NULL,
                    active_base_remaining TEXT NOT NULL,
                    position_closed INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    FOREIGN KEY(position_id)
                        REFERENCES economic_positions(position_id)
                );
                CREATE TABLE IF NOT EXISTS economic_exit_lot_allocations (
                    event_id TEXT NOT NULL, lot_id TEXT NOT NULL,
                    base_sold TEXT NOT NULL, cost_basis_quote TEXT NOT NULL,
                    entry_fees_quote TEXT NOT NULL,
                    PRIMARY KEY (event_id, lot_id)
                );
                """)

            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(economic_positions)"
                ).fetchall()
            }
            if "base_asset" not in columns:
                conn.execute(
                    "ALTER TABLE economic_positions ADD COLUMN base_asset TEXT"
                )
                legacy_rows = conn.execute(
                    "SELECT position_id, pair FROM economic_positions"
                ).fetchall()
                for row in legacy_rows:
                    conn.execute(
                        """
                        UPDATE economic_positions
                        SET base_asset = ?
                        WHERE position_id = ?
                        """,
                        (
                            normalized_base_asset(str(row["pair"])),
                            str(row["position_id"]),
                        ),
                    )

            duplicates = conn.execute(
                """
                SELECT base_asset, COUNT(*) AS count
                FROM economic_positions
                WHERE status = ?
                GROUP BY base_asset
                HAVING COUNT(*) > 1
                """,
                (EconomicPositionStatus.ACTIVE.value,),
            ).fetchall()
            if duplicates:
                assets = ", ".join(str(row["base_asset"]) for row in duplicates)
                raise RuntimeError(
                    "legacy ledger contains multiple ACTIVE positions for "
                    f"the same base asset: {assets}"
                )

            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    economic_positions_active_base_unique
                ON economic_positions(base_asset)
                WHERE status = 'ACTIVE'
                """)

    def open_position(
        self,
        *,
        position_id: str,
        pair: str,
        created_at: float,
    ) -> None:
        pid = _text(position_id, "position_id")
        normalized_pair = _pair(pair)
        base_asset = normalized_base_asset(normalized_pair)
        timestamp = _time(created_at, "created_at")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT pair, base_asset
                    FROM economic_positions
                    WHERE position_id = ?
                    """,
                    (pid,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["pair"]) != normalized_pair
                        or str(existing["base_asset"]) != base_asset
                    ):
                        raise RuntimeError(
                            "economic position replay conflicts with existing pair"
                        )
                    conn.rollback()
                    return

                active_same_asset = conn.execute(
                    """
                    SELECT position_id, pair
                    FROM economic_positions
                    WHERE base_asset = ? AND status = ?
                    LIMIT 1
                    """,
                    (
                        base_asset,
                        EconomicPositionStatus.ACTIVE.value,
                    ),
                ).fetchone()
                if active_same_asset is not None:
                    raise ValueError(
                        f"{base_asset} already has ACTIVE economic position "
                        f"{active_same_asset['position_id']} "
                        f"({active_same_asset['pair']})"
                    )

                conn.execute(
                    """
                    INSERT INTO economic_positions (
                        position_id, pair, base_asset, status,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        normalized_pair,
                        base_asset,
                        EconomicPositionStatus.ACTIVE.value,
                        timestamp,
                        timestamp,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _position_row(
        self,
        conn: sqlite3.Connection,
        position_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT *
            FROM economic_positions
            WHERE position_id = ?
            """,
            (position_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown economic position: {position_id}")
        return row

    def _insert_lot(
        self,
        *,
        lot_id: str,
        position_id: str | None,
        pair: str,
        ownership: OwnershipBucket,
        source: EconomicLotSource,
        base_amount: Decimal,
        cost_basis_quote: Decimal,
        entry_fees_quote: Decimal,
        proposal_id: str | None,
        executor_id: str | None,
        occurred_at: float,
        proposal_quote_cap: Decimal | None = None,
    ) -> EconomicLot:
        lid = _text(lot_id, "lot_id")
        normalized_pair = _pair(pair)
        base = _decimal(base_amount, "base_amount", positive=True)
        basis = _decimal(
            cost_basis_quote,
            "cost_basis_quote",
            non_negative=True,
        )
        fees = _decimal(
            entry_fees_quote,
            "entry_fees_quote",
            non_negative=True,
        )
        timestamp = _time(occurred_at, "occurred_at")
        pid = None if position_id is None else _text(position_id, "position_id")
        proposal = (
            None if proposal_id is None else _text(proposal_id, "proposal_id")
        )
        executor = (
            None if executor_id is None else _text(executor_id, "executor_id")
        )

        if ownership == OwnershipBucket.ACTIVE_TRADE:
            if pid is None:
                raise ValueError("ACTIVE_TRADE lot requires position_id")
            if source not in {
                EconomicLotSource.BASE_ENTRY,
                EconomicLotSource.VCAP_RECOVERY,
            }:
                raise ValueError("ACTIVE_TRADE lot has invalid source")
            if basis <= ZERO:
                raise ValueError("ACTIVE_TRADE lot requires positive cost basis")
        elif ownership == OwnershipBucket.STRATEGIC_RETAINED:
            if pid is not None:
                raise ValueError(
                    "STRATEGIC_RETAINED inventory must not belong to active position"
                )
            if source != EconomicLotSource.STRATEGIC_RETENTION:
                raise ValueError("strategic inventory has invalid source")
        else:
            if pid is not None:
                raise ValueError(
                    "UNATTRIBUTED inventory must not belong to active position"
                )
            if source != EconomicLotSource.EXTERNAL_UNATTRIBUTED:
                raise ValueError("unattributed inventory has invalid source")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if pid is not None:
                    position = self._position_row(conn, pid)
                    if str(position["pair"]) != normalized_pair:
                        raise ValueError("lot pair does not match economic position")
                    if (
                        str(position["status"])
                        != EconomicPositionStatus.ACTIVE.value
                    ):
                        raise ValueError("cannot add lot to closed economic position")

                existing = conn.execute(
                    """
                    SELECT *
                    FROM economic_lots
                    WHERE lot_id = ?
                    """,
                    (lid,),
                ).fetchone()

                expected = (
                    pid,
                    normalized_pair,
                    ownership.value,
                    source.value,
                    str(base),
                    str(basis),
                    str(fees),
                    proposal,
                    executor,
                )

                if existing is not None:
                    actual = (
                        existing["position_id"],
                        str(existing["pair"]),
                        str(existing["ownership"]),
                        str(existing["source"]),
                        str(existing["base_original"]),
                        str(existing["cost_basis_original_quote"]),
                        str(existing["entry_fees_original_quote"]),
                        existing["proposal_id"],
                        existing["executor_id"],
                    )
                    if actual != expected:
                        raise RuntimeError(
                            "economic lot replay conflicts with existing lot"
                        )
                    conn.rollback()
                    return self._lot_from_row(existing)

                if proposal is not None and proposal_quote_cap is not None:
                    cap = _decimal(
                        proposal_quote_cap,
                        "proposal_quote_cap",
                        positive=True,
                    )
                    recorded_rows = conn.execute(
                        """
                        SELECT cost_basis_original_quote
                        FROM economic_lots
                        WHERE proposal_id = ?
                        """,
                        (proposal,),
                    ).fetchall()
                    recorded_basis = sum(
                        (
                            Decimal(str(row["cost_basis_original_quote"]))
                            for row in recorded_rows
                        ),
                        ZERO,
                    )
                    if recorded_basis + basis > cap:
                        raise ValueError(
                            "economic lots exceed proposal authorized quote"
                        )

                conn.execute(
                    """
                    INSERT INTO economic_lots (
                        lot_id,
                        position_id,
                        pair,
                        ownership,
                        source,
                        base_original,
                        cost_basis_original_quote,
                        entry_fees_original_quote,
                        base_remaining,
                        cost_basis_remaining_quote,
                        entry_fees_remaining_quote,
                        proposal_id,
                        executor_id,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lid,
                        pid,
                        normalized_pair,
                        ownership.value,
                        source.value,
                        str(base),
                        str(basis),
                        str(fees),
                        str(base),
                        str(basis),
                        str(fees),
                        proposal,
                        executor,
                        timestamp,
                        timestamp,
                    ),
                )
                if pid is not None:
                    conn.execute(
                        """
                        UPDATE economic_positions
                        SET updated_at = ?
                        WHERE position_id = ?
                        """,
                        (timestamp, pid),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return self.get_lot(lid)

    @staticmethod
    def _lot_from_row(row: sqlite3.Row) -> EconomicLot:
        return EconomicLot(
            lot_id=str(row["lot_id"]),
            position_id=(
                None if row["position_id"] is None else str(row["position_id"])
            ),
            pair=str(row["pair"]),
            ownership=OwnershipBucket(str(row["ownership"])),
            source=EconomicLotSource(str(row["source"])),
            base_original=Decimal(str(row["base_original"])),
            cost_basis_original_quote=Decimal(
                str(row["cost_basis_original_quote"])
            ),
            entry_fees_original_quote=Decimal(
                str(row["entry_fees_original_quote"])
            ),
            base_remaining=Decimal(str(row["base_remaining"])),
            cost_basis_remaining_quote=Decimal(
                str(row["cost_basis_remaining_quote"])
            ),
            entry_fees_remaining_quote=Decimal(
                str(row["entry_fees_remaining_quote"])
            ),
            proposal_id=(
                None if row["proposal_id"] is None else str(row["proposal_id"])
            ),
            executor_id=(
                None if row["executor_id"] is None else str(row["executor_id"])
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get_lot(self, lot_id: str) -> EconomicLot:
        lid = _text(lot_id, "lot_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM economic_lots WHERE lot_id = ?",
                (lid,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown economic lot: {lid}")
        return self._lot_from_row(row)

    def record_base_fill(
        self,
        *,
        position_id: str,
        lot_id: str,
        pair: str,
        base_amount: Decimal,
        cost_basis_quote: Decimal,
        entry_fees_quote: Decimal = Decimal("0"),
        occurred_at: float,
    ) -> EconomicLot:
        return self._insert_lot(
            lot_id=lot_id,
            position_id=position_id,
            pair=pair,
            ownership=OwnershipBucket.ACTIVE_TRADE,
            source=EconomicLotSource.BASE_ENTRY,
            base_amount=base_amount,
            cost_basis_quote=cost_basis_quote,
            entry_fees_quote=entry_fees_quote,
            proposal_id=None,
            executor_id=None,
            occurred_at=occurred_at,
        )

    def record_entry_fill(
        self,
        *,
        governor: CapitalGovernor,
        position_id: str,
        lot_id: str,
        proposal_id: str,
        base_amount: Decimal,
        cost_basis_quote: Decimal,
        entry_fees_quote: Decimal = Decimal("0"),
        occurred_at: float,
    ) -> EconomicLot:
        proposal = _text(proposal_id, "proposal_id")
        provenance = governor.get_execution_provenance(proposal)
        if provenance is None:
            raise ValueError("entry lot requires exact execution provenance")
        if provenance.intent != IntentKind.ENTRY:
            raise ValueError("entry lot provenance is not ENTRY intent")
        receipt = governor.get_execution_receipt(proposal)
        if receipt is None or receipt != provenance.executor_id:
            raise ValueError("entry lot requires exact execution receipt")

        return self._insert_lot(
            lot_id=lot_id,
            position_id=position_id,
            pair=provenance.pair,
            ownership=OwnershipBucket.ACTIVE_TRADE,
            source=EconomicLotSource.BASE_ENTRY,
            base_amount=base_amount,
            cost_basis_quote=cost_basis_quote,
            entry_fees_quote=entry_fees_quote,
            proposal_id=proposal,
            executor_id=provenance.executor_id,
            occurred_at=occurred_at,
            proposal_quote_cap=provenance.authorized_quote,
        )

    def record_vcap_fill(
        self,
        *,
        governor: CapitalGovernor,
        position_id: str,
        lot_id: str,
        proposal_id: str,
        base_amount: Decimal,
        cost_basis_quote: Decimal,
        entry_fees_quote: Decimal = Decimal("0"),
        occurred_at: float,
    ) -> EconomicLot:
        proposal = _text(proposal_id, "proposal_id")
        provenance = governor.get_execution_provenance(proposal)
        if provenance is None:
            raise ValueError("VCAP lot requires exact execution provenance")
        if provenance.intent != IntentKind.RECOVERY:
            raise ValueError("VCAP lot provenance is not RECOVERY intent")
        receipt = governor.get_execution_receipt(proposal)
        if receipt is None or receipt != provenance.executor_id:
            raise ValueError("VCAP lot requires exact execution receipt")
        basis = _decimal(
            cost_basis_quote,
            "cost_basis_quote",
            positive=True,
        )
        if basis > provenance.authorized_quote:
            raise ValueError(
                "VCAP lot cost basis exceeds authorized recovery quote"
            )

        return self._insert_lot(
            lot_id=lot_id,
            position_id=position_id,
            pair=provenance.pair,
            ownership=OwnershipBucket.ACTIVE_TRADE,
            source=EconomicLotSource.VCAP_RECOVERY,
            base_amount=base_amount,
            cost_basis_quote=cost_basis_quote,
            entry_fees_quote=entry_fees_quote,
            proposal_id=proposal,
            executor_id=provenance.executor_id,
            occurred_at=occurred_at,
            proposal_quote_cap=provenance.authorized_quote,
        )

    def record_strategic_retained(
        self,
        *,
        lot_id: str,
        pair: str,
        base_amount: Decimal,
        cost_basis_quote: Decimal = Decimal("0"),
        entry_fees_quote: Decimal = Decimal("0"),
        occurred_at: float,
    ) -> EconomicLot:
        return self._insert_lot(
            lot_id=lot_id,
            position_id=None,
            pair=pair,
            ownership=OwnershipBucket.STRATEGIC_RETAINED,
            source=EconomicLotSource.STRATEGIC_RETENTION,
            base_amount=base_amount,
            cost_basis_quote=cost_basis_quote,
            entry_fees_quote=entry_fees_quote,
            proposal_id=None,
            executor_id=None,
            occurred_at=occurred_at,
        )

    def record_unattributed(
        self,
        *,
        lot_id: str,
        pair: str,
        base_amount: Decimal,
        occurred_at: float,
    ) -> EconomicLot:
        return self._insert_lot(
            lot_id=lot_id,
            position_id=None,
            pair=pair,
            ownership=OwnershipBucket.UNATTRIBUTED,
            source=EconomicLotSource.EXTERNAL_UNATTRIBUTED,
            base_amount=base_amount,
            cost_basis_quote=ZERO,
            entry_fees_quote=ZERO,
            proposal_id=None,
            executor_id=None,
            occurred_at=occurred_at,
        )

    def _event_replay(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        position_id: str,
        base_amount: Decimal,
        quote_amount: Decimal,
        fee_quote: Decimal,
    ) -> EconomicExitResult | None:
        row = conn.execute(
            """
            SELECT *
            FROM economic_position_events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None

        expected = (
            position_id,
            str(base_amount),
            str(quote_amount),
            str(fee_quote),
        )
        actual = (
            str(row["position_id"]),
            str(row["base_amount"]),
            str(row["quote_amount"]),
            str(row["fee_quote"]),
        )
        if actual != expected:
            raise RuntimeError(
                "economic exit event replay conflicts with existing event"
            )

        snapshot = self._snapshot_with_conn(conn, position_id)
        return EconomicExitResult(
            event_id=str(row["event_id"]),
            position_id=str(row["position_id"]),
            pair=snapshot.pair,
            base_sold=Decimal(str(row["base_amount"])),
            gross_proceeds_quote=Decimal(str(row["quote_amount"])),
            exit_fee_quote=Decimal(str(row["fee_quote"])),
            allocated_cost_basis_quote=Decimal(
                str(row["allocated_cost_basis_quote"])
            ),
            allocated_entry_fees_quote=Decimal(
                str(row["allocated_entry_fees_quote"])
            ),
            realized_pnl_quote=Decimal(str(row["realized_pnl_quote"])),
            active_base_remaining=Decimal(
                str(row["active_base_remaining"])
            ),
            position_closed=bool(row["position_closed"]),
        )

    def exit_basis_by_lot(self, event_id: str) -> tuple[tuple[str, Decimal], ...]:
        """Actual lot basis consumed in the same transaction as a sale event."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT lot_id, cost_basis_quote FROM economic_exit_lot_allocations "
                "WHERE event_id = ? ORDER BY lot_id",
                (_text(event_id, "event_id"),),
            ).fetchall()
        return tuple(
            (str(row["lot_id"]), Decimal(row["cost_basis_quote"])) for row in rows
        )

    def record_partial_exit(
        self,
        *,
        event_id: str,
        position_id: str,
        base_sold: Decimal,
        gross_proceeds_quote: Decimal,
        exit_fee_quote: Decimal = Decimal("0"),
        occurred_at: float,
        eligible_lot_ids: tuple[str, ...] | None = None,
    ) -> EconomicExitResult:
        eid = _text(event_id, "event_id")
        pid = _text(position_id, "position_id")
        sold = _decimal(base_sold, "base_sold", positive=True)
        proceeds = _decimal(
            gross_proceeds_quote,
            "gross_proceeds_quote",
            non_negative=True,
        )
        exit_fee = _decimal(
            exit_fee_quote,
            "exit_fee_quote",
            non_negative=True,
        )
        timestamp = _time(occurred_at, "occurred_at")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                replay = self._event_replay(
                    conn,
                    event_id=eid,
                    position_id=pid,
                    base_amount=sold,
                    quote_amount=proceeds,
                    fee_quote=exit_fee,
                )
                if replay is not None:
                    conn.rollback()
                    return replay

                position = self._position_row(conn, pid)
                pair = str(position["pair"])
                if str(position["status"]) != EconomicPositionStatus.ACTIVE.value:
                    raise ValueError("cannot exit a closed economic position")

                rows = conn.execute(
                    """
                    SELECT *
                    FROM economic_lots
                    WHERE position_id = ?
                      AND ownership = ?
                    ORDER BY created_at, lot_id
                    """,
                    (pid, OwnershipBucket.ACTIVE_TRADE.value),
                ).fetchall()

                all_active_lots = [
                    self._lot_from_row(row)
                    for row in rows
                    if Decimal(str(row["base_remaining"])) > ZERO
                ]
                total_active_base = exact_decimal_sum(
                    lot.base_remaining for lot in all_active_lots
                )

                if eligible_lot_ids is None:
                    lots = all_active_lots
                else:
                    if not eligible_lot_ids:
                        raise ValueError("eligible_lot_ids cannot be empty")
                    normalized_scope = tuple(
                        _text(lot_id, "eligible_lot_id") for lot_id in eligible_lot_ids
                    )
                    if len(set(normalized_scope)) != len(normalized_scope):
                        raise ValueError("eligible_lot_ids cannot contain duplicates")
                    by_id = {lot.lot_id: lot for lot in all_active_lots}
                    missing = [
                        lot_id for lot_id in normalized_scope if lot_id not in by_id
                    ]
                    if missing:
                        raise ValueError(
                            "exit scope contains inactive or foreign economic lots"
                        )
                    lots = [by_id[lot_id] for lot_id in normalized_scope]

                eligible_base = exact_decimal_sum(lot.base_remaining for lot in lots)
                if sold > eligible_base:
                    message = (
                        "exit base exceeds ACTIVE_TRADE inventory"
                        if eligible_lot_ids is None
                        else "exit base exceeds eligible ACTIVE_TRADE inventory"
                    )
                    raise ValueError(message)

                allocated_basis = ZERO
                allocated_entry_fees = ZERO
                remaining_to_sell = sold
                updates: list[tuple[Decimal, Decimal, Decimal, str]] = []
                allocations = []

                for index, lot in enumerate(lots):
                    if remaining_to_sell <= ZERO:
                        break

                    if sold == eligible_base:
                        lot_sale = lot.base_remaining
                    elif index == len(lots) - 1:
                        lot_sale = remaining_to_sell
                    else:
                        # A rounded proportional proposal must leave a residual
                        # that later owned lots can actually supply. Tiny final
                        # lots cannot absorb the rounding remainder by fiat.
                        future_capacity = exact_decimal_sum(
                            other.base_remaining for other in lots[index + 1 :]
                        )
                        minimum_sale = max(
                            ZERO,
                            exact_decimal_difference(remaining_to_sell, future_capacity),
                        )
                        lot_sale = min(
                            lot.base_remaining,
                            remaining_to_sell,
                            max(minimum_sale, sold * lot.base_remaining / eligible_base),
                        )

                    lot_sale = min(lot_sale, lot.base_remaining)
                    if lot_sale <= ZERO:
                        continue

                    if lot_sale == lot.base_remaining:
                        basis_piece = lot.cost_basis_remaining_quote
                        fee_piece = lot.entry_fees_remaining_quote
                    else:
                        ratio = lot_sale / lot.base_remaining
                        basis_piece = min(
                            lot.cost_basis_remaining_quote,
                            lot.cost_basis_remaining_quote * ratio,
                        )
                        fee_piece = min(
                            lot.entry_fees_remaining_quote,
                            lot.entry_fees_remaining_quote * ratio,
                        )

                    allocated_basis = exact_decimal_sum((allocated_basis, basis_piece))
                    allocated_entry_fees = exact_decimal_sum(
                        (allocated_entry_fees, fee_piece)
                    )
                    remaining_to_sell = exact_decimal_difference(
                        remaining_to_sell, lot_sale
                    )
                    allocations.append(
                        (
                            eid,
                            lot.lot_id,
                            str(lot_sale),
                            str(basis_piece),
                            str(fee_piece),
                        )
                    )

                    updates.append(
                        (
                            exact_decimal_difference(lot.base_remaining, lot_sale),
                            exact_decimal_difference(
                                lot.cost_basis_remaining_quote, basis_piece
                            ),
                            exact_decimal_difference(
                                lot.entry_fees_remaining_quote, fee_piece
                            ),
                            lot.lot_id,
                        )
                    )

                if remaining_to_sell != ZERO:
                    # Decimal arithmetic above should close exactly because the
                    # final lot consumes the residual. Any deviation is safer to
                    # reject than silently reassign inventory.
                    raise RuntimeError("exit allocation did not close exactly")

                for base_left, basis_left, fees_left, lot_id in updates:
                    conn.execute(
                        """
                        UPDATE economic_lots
                        SET
                            base_remaining = ?,
                            cost_basis_remaining_quote = ?,
                            entry_fees_remaining_quote = ?,
                            updated_at = ?
                        WHERE lot_id = ?
                        """,
                        (
                            str(base_left),
                            str(basis_left),
                            str(fees_left),
                            timestamp,
                            lot_id,
                        ),
                    )

                conn.executemany(
                    "INSERT INTO economic_exit_lot_allocations VALUES (?, ?, ?, ?, ?)",
                    allocations,
                )
                realized = exact_decimal_sum(
                    (
                        proceeds,
                        exit_fee.copy_negate(),
                        allocated_basis.copy_negate(),
                        allocated_entry_fees.copy_negate(),
                    )
                )

                active_after = exact_decimal_difference(total_active_base, sold)
                closed = active_after == ZERO

                conn.execute(
                    """
                    INSERT INTO economic_position_events (
                        event_id,
                        position_id,
                        event_type,
                        base_amount,
                        quote_amount,
                        fee_quote,
                        allocated_cost_basis_quote,
                        allocated_entry_fees_quote,
                        realized_pnl_quote,
                        active_base_remaining,
                        position_closed,
                        occurred_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        pid,
                        "PARTIAL_EXIT" if not closed else "FINAL_EXIT",
                        str(sold),
                        str(proceeds),
                        str(exit_fee),
                        str(allocated_basis),
                        str(allocated_entry_fees),
                        str(realized),
                        str(active_after),
                        1 if closed else 0,
                        timestamp,
                    ),
                )

                conn.execute(
                    """
                    UPDATE economic_positions
                    SET status = ?, updated_at = ?
                    WHERE position_id = ?
                    """,
                    (
                        (
                            EconomicPositionStatus.CLOSED.value
                            if closed
                            else EconomicPositionStatus.ACTIVE.value
                        ),
                        timestamp,
                        pid,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return EconomicExitResult(
            event_id=eid,
            position_id=pid,
            pair=pair,
            base_sold=sold,
            gross_proceeds_quote=proceeds,
            exit_fee_quote=exit_fee,
            allocated_cost_basis_quote=allocated_basis,
            allocated_entry_fees_quote=allocated_entry_fees,
            realized_pnl_quote=realized,
            active_base_remaining=active_after,
            position_closed=closed,
        )

    def _snapshot_with_conn(
        self,
        conn: sqlite3.Connection,
        position_id: str,
    ) -> EconomicPositionSnapshot:
        position = self._position_row(conn, position_id)
        pair = str(position["pair"])

        active_rows = conn.execute(
            """
            SELECT *
            FROM economic_lots
            WHERE position_id = ?
              AND ownership = ?
            ORDER BY created_at, lot_id
            """,
            (position_id, OwnershipBucket.ACTIVE_TRADE.value),
        ).fetchall()
        active_lots = [self._lot_from_row(row) for row in active_rows]

        active_base = exact_decimal_sum(lot.base_remaining for lot in active_lots)
        active_basis = exact_decimal_sum(
            lot.cost_basis_remaining_quote for lot in active_lots
        )
        active_fees = exact_decimal_sum(
            lot.entry_fees_remaining_quote for lot in active_lots
        )

        realized_row = conn.execute(
            """
            SELECT realized_pnl_quote
            FROM economic_position_events
            WHERE position_id = ?
            """,
            (position_id,),
        ).fetchall()
        realized = exact_decimal_sum(
            Decimal(str(row["realized_pnl_quote"])) for row in realized_row
        )

        strategic = conn.execute(
            """
            SELECT base_remaining
            FROM economic_lots
            WHERE pair = ?
              AND ownership = ?
            """,
            (pair, OwnershipBucket.STRATEGIC_RETAINED.value),
        ).fetchall()
        strategic_base = exact_decimal_sum(
            Decimal(str(row["base_remaining"])) for row in strategic
        )

        unattributed = conn.execute(
            """
            SELECT base_remaining
            FROM economic_lots
            WHERE pair = ?
              AND ownership = ?
            """,
            (pair, OwnershipBucket.UNATTRIBUTED.value),
        ).fetchall()
        unattributed_base = exact_decimal_sum(
            Decimal(str(row["base_remaining"])) for row in unattributed
        )

        recovery_ids = tuple(
            dict.fromkeys(
                lot.proposal_id
                for lot in active_lots
                if (
                    lot.source == EconomicLotSource.VCAP_RECOVERY
                    and lot.proposal_id is not None
                    and lot.base_remaining > ZERO
                )
            )
        )

        return EconomicPositionSnapshot(
            position_id=position_id,
            pair=pair,
            status=EconomicPositionStatus(str(position["status"])),
            active_base=active_base,
            active_cost_basis_quote=active_basis,
            active_entry_fees_quote=active_fees,
            realized_pnl_quote=realized,
            strategic_retained_base_same_pair=strategic_base,
            unattributed_base_same_pair=unattributed_base,
            active_lot_ids=tuple(
                lot.lot_id for lot in active_lots if lot.base_remaining > ZERO
            ),
            recovery_proposal_ids=recovery_ids,
        )

    def list_active_lots(self, position_id: str) -> tuple[EconomicLot, ...]:
        """Return every remaining ACTIVE_TRADE lot for one position.

        Strategic-retained and unattributed inventory are deliberately absent
        from this surface, so callers cannot accidentally construct an exit
        manifest from aggregate same-asset wallet inventory.
        """
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            self._position_row(conn, pid)
            rows = conn.execute(
                """
                SELECT *
                FROM economic_lots
                WHERE position_id = ?
                  AND ownership = ?
                ORDER BY created_at, lot_id
                """,
                (pid, OwnershipBucket.ACTIVE_TRADE.value),
            ).fetchall()

        return tuple(
            self._lot_from_row(row)
            for row in rows
            if Decimal(str(row["base_remaining"])) > ZERO
        )

    def active_lot_ids_for_provenance(
        self,
        *,
        position_id: str,
        proposal_id: str,
        executor_id: str,
    ) -> tuple[str, ...]:
        pid = _text(position_id, "position_id")
        proposal = _text(proposal_id, "proposal_id")
        executor = _text(executor_id, "executor_id")
        with self._connect() as conn:
            position = self._position_row(conn, pid)
            rows = conn.execute(
                """
                SELECT lot_id, pair, base_remaining
                FROM economic_lots
                WHERE position_id = ?
                  AND ownership = ?
                  AND proposal_id = ?
                  AND executor_id = ?
                ORDER BY created_at, lot_id
                """,
                (
                    pid,
                    OwnershipBucket.ACTIVE_TRADE.value,
                    proposal,
                    executor,
                ),
            ).fetchall()

        if not rows:
            return ()

        pair = str(position["pair"])
        result: list[str] = []
        for row in rows:
            if str(row["pair"]) != pair:
                raise RuntimeError(
                    "economic lot provenance crosses position pair"
                )
            if Decimal(str(row["base_remaining"])) > ZERO:
                result.append(str(row["lot_id"]))
        return tuple(result)

    def list_active_position_ids(self) -> tuple[str, ...]:
        """Return active economic positions in deterministic creation order."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT position_id
                FROM economic_positions
                WHERE status = ?
                ORDER BY created_at, position_id
                """,
                (EconomicPositionStatus.ACTIVE.value,),
            ).fetchall()
        return tuple(str(row["position_id"]) for row in rows)

    def active_proposal_ids(self, position_id: str) -> tuple[str, ...]:
        """Return exact capital proposals still owning active trade inventory."""
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            self._position_row(conn, pid)
            rows = conn.execute(
                """
                SELECT proposal_id, base_remaining
                FROM economic_lots
                WHERE position_id = ?
                  AND ownership = ?
                  AND proposal_id IS NOT NULL
                ORDER BY created_at, lot_id
                """,
                (pid, OwnershipBucket.ACTIVE_TRADE.value),
            ).fetchall()

        proposal_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if Decimal(str(row["base_remaining"])) <= ZERO:
                continue
            proposal_id = str(row["proposal_id"])
            if proposal_id not in seen:
                seen.add(proposal_id)
                proposal_ids.append(proposal_id)
        return tuple(proposal_ids)

    def snapshot(self, position_id: str) -> EconomicPositionSnapshot:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            return self._snapshot_with_conn(conn, pid)
