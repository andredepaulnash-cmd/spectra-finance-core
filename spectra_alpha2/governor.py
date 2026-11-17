"""Transactional SQLite CapitalGovernor for isolated paper capital."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .safety import PaperSafetySettings
from .strategy import PlannedActionType, calculate_drawdown, plan_next_action
from .policy import (
    BUY_FEE_RATE,
    INITIAL_ENTRY_BUDGET,
    INITIAL_ENTRY_NOTIONAL,
    MAX_ACTIVE_VCAP_SHARDS_PER_ASSET,
    MAX_ASSET_GROSS_NOTIONAL,
    MAX_OPEN_POSITIONS,
    VCAP_RECOVERY_POOL,
)


ZERO = Decimal("0")
PAPER_CONNECTOR = "kraken_paper_trade"


class CapitalDenied(RuntimeError):
    pass


class CapitalPool(StrEnum):
    INITIAL = "INITIAL"
    RECOVERY = "RECOVERY"


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class CapitalAuthorization:
    authorization_id: str
    idempotency_key: str
    position_id: str
    asset_id: str
    action_id: str
    pool: CapitalPool
    gross_notional: Decimal
    fee_reserve: Decimal
    connector: str = PAPER_CONNECTOR


class CapitalGovernor:
    """Single-writer transactional capital and order-authorization boundary."""

    def __init__(self, database_path: str | Path, *, environment: Mapping[str, str], reservation_ttl_seconds: int = 90):
        if isinstance(reservation_ttl_seconds, bool) or not isinstance(reservation_ttl_seconds, int) or reservation_ttl_seconds <= 0:
            raise ValueError("reservation_ttl_seconds must be a positive integer")
        self.safety = PaperSafetySettings.from_environment(environment)
        self.safety.assert_paper_mode()
        self.database_path = str(database_path)
        self.reservation_ttl_seconds = reservation_ttl_seconds
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _assert_paper(self) -> None:
        self.safety.assert_paper_mode()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if self.database_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pools (
                    pool_id TEXT PRIMARY KEY,
                    budget TEXT NOT NULL,
                    reserved TEXT NOT NULL DEFAULT '0',
                    spent TEXT NOT NULL DEFAULT '0',
                    recovered TEXT NOT NULL DEFAULT '0'
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gross_committed TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_active_asset
                    ON positions(asset_id) WHERE status IN ('RESERVED','OPEN');
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    position_id TEXT NOT NULL REFERENCES positions(position_id),
                    asset_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
                    gross_notional TEXT NOT NULL,
                    fee_reserve TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    order_id TEXT,
                    actual_gross TEXT,
                    actual_fee TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_lots (
                    lot_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES positions(position_id),
                    asset_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
                    reservation_id TEXT NOT NULL UNIQUE REFERENCES reservations(reservation_id),
                    status TEXT NOT NULL,
                    gross_buy_notional TEXT NOT NULL DEFAULT '0',
                    buy_fee_quote TEXT NOT NULL DEFAULT '0',
                    filled_quantity TEXT NOT NULL DEFAULT '0',
                    remaining_quantity TEXT NOT NULL DEFAULT '0',
                    remaining_cost_basis_quote TEXT NOT NULL DEFAULT '0',
                    gross_sell_proceeds TEXT NOT NULL DEFAULT '0',
                    sell_fee_quote TEXT NOT NULL DEFAULT '0',
                    realized_pnl_quote TEXT NOT NULL DEFAULT '0',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_position_lots_position_status
                    ON position_lots(position_id,status);
                CREATE TABLE IF NOT EXISTS position_steps (
                    position_id TEXT NOT NULL REFERENCES positions(position_id),
                    step_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reservation_id TEXT,
                    drawdown TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(position_id,step_id)
                );
                CREATE TABLE IF NOT EXISTS vcap_shards (
                    shard_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES positions(position_id),
                    asset_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reservation_id TEXT NOT NULL UNIQUE REFERENCES reservations(reservation_id),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vcap_shards_asset_status
                    ON vcap_shards(asset_id, status);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO pools(pool_id,budget,reserved,spent,recovered) VALUES(?,?,?,?,?)",
                (CapitalPool.INITIAL.value, str(INITIAL_ENTRY_BUDGET), "0", "0", "0"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO pools(pool_id,budget,reserved,spent,recovered) VALUES(?,?,?,?,?)",
                (CapitalPool.RECOVERY.value, str(VCAP_RECOVERY_POOL), "0", "0", "0"),
            )

    @staticmethod
    def _positive(value: Decimal, name: str) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
            raise ValueError(f"{name} must be a finite positive Decimal")
        return value

    @staticmethod
    def _event(conn: sqlite3.Connection, event_type: str, entity_id: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO events(event_type,entity_id,payload,occurred_at) VALUES(?,?,?,?)",
            (event_type, entity_id, json.dumps(payload, sort_keys=True), time.time()),
        )

    @staticmethod
    def _available_pool(conn: sqlite3.Connection, pool: CapitalPool) -> Decimal:
        row = conn.execute("SELECT budget,reserved,spent,recovered FROM pools WHERE pool_id=?", (pool.value,)).fetchone()
        return Decimal(row["budget"]) - Decimal(row["reserved"]) - Decimal(row["spent"]) + Decimal(row["recovered"])

    def reserve_initial_position(
        self,
        *,
        position_id: str,
        asset_id: str,
        reference_price: Decimal,
        idempotency_key: str,
    ) -> CapitalAuthorization:
        self._assert_paper()
        price = self._positive(reference_price, "reference_price")
        if not all((position_id, asset_id, idempotency_key)):
            raise ValueError("position_id, asset_id and idempotency_key are required")
        amount = INITIAL_ENTRY_NOTIONAL
        fee = amount * BUY_FEE_RATE
        now = time.time()
        reservation_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM positions WHERE status IN ('RESERVED','OPEN')"
                ).fetchone()["n"]
                if count >= MAX_OPEN_POSITIONS:
                    raise CapitalDenied("74-position hard ceiling reached")
                if conn.execute("SELECT 1 FROM positions WHERE asset_id=? AND status IN ('RESERVED','OPEN')", (asset_id,)).fetchone():
                    raise CapitalDenied("asset already has a position")
                if self._available_pool(conn, CapitalPool.INITIAL) < amount + fee:
                    raise CapitalDenied("initial-entry pool has insufficient available capital")
                conn.execute(
                    "INSERT INTO positions VALUES(?,?,?,?,?,?,?)",
                    (position_id, asset_id, str(price), "RESERVED", str(amount), now, now),
                )
                conn.execute(
                    """INSERT INTO reservations
                    (reservation_id,idempotency_key,position_id,asset_id,action_id,pool_id,
                     gross_notional,fee_reserve,status,expires_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (reservation_id,idempotency_key,position_id,asset_id,"INITIAL",
                     CapitalPool.INITIAL.value,str(amount),str(fee),ReservationStatus.RESERVED.value,
                     now+self.reservation_ttl_seconds,now,now),
                )
                conn.execute(
                    """INSERT INTO position_lots
                    (lot_id,position_id,asset_id,action_id,pool_id,reservation_id,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (f"{position_id}:INITIAL",position_id,asset_id,"INITIAL",CapitalPool.INITIAL.value,reservation_id,"RESERVED",now,now),
                )
                conn.execute(
                    "UPDATE pools SET reserved=? WHERE pool_id=?",
                    (str(Decimal("0") + (Decimal(conn.execute("SELECT reserved FROM pools WHERE pool_id=?", (CapitalPool.INITIAL.value,)).fetchone()["reserved"]) + amount + fee)), CapitalPool.INITIAL.value),
                )
                self._event(conn,"POSITION_RESERVED",position_id,{"asset_id":asset_id,"gross_notional":str(amount),"fee_reserve":str(fee)})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return CapitalAuthorization(reservation_id,idempotency_key,position_id,asset_id,"INITIAL",CapitalPool.INITIAL,amount,fee)

    def reserve_addition(
        self,
        *,
        position_id: str,
        action_id: str,
        gross_notional: Decimal,
        current_price: Decimal,
        idempotency_key: str,
        action_type: str,
        shard_id: str | None = None,
    ) -> CapitalAuthorization:
        self._assert_paper()
        amount = self._positive(gross_notional, "gross_notional")
        if action_type not in {"DCA","VCAP"}:
            raise ValueError("action_type must be DCA or VCAP")
        if not all((position_id, action_id, idempotency_key)):
            raise ValueError("position_id, action_id and idempotency_key are required")
        if action_type == "VCAP" and not shard_id:
            raise ValueError("VCAP additions require an independent shard_id")
        pool = CapitalPool.INITIAL if action_type == "DCA" else CapitalPool.RECOVERY
        fee = amount * BUY_FEE_RATE
        now = time.time()
        reservation_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                position = conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
                if position is None or position["status"] != "OPEN":
                    raise CapitalDenied("position must be OPEN before adding capital")
                price = self._positive(current_price, "current_price")
                drawdown = calculate_drawdown(Decimal(position["reference_price"]), price)
                step_rows = conn.execute(
                    "SELECT step_id,action_type,status FROM position_steps WHERE position_id=?",
                    (position_id,),
                ).fetchall()
                completed_dca = {
                    row["step_id"] for row in step_rows
                    if row["action_type"] == "DCA" and row["status"] == "FILLED"
                }
                completed_vcap = {
                    row["step_id"] for row in step_rows
                    if row["action_type"] == "VCAP"
                    and row["status"] in {"SUBMITTED","FILLED","ACTIVE","RESOLVED","RECYCLED"}
                }
                planned = plan_next_action(
                    drawdown,
                    completed_dca_steps=completed_dca,
                    completed_vcap_steps=completed_vcap,
                )
                expected_type = PlannedActionType.DCA if action_type == "DCA" else PlannedActionType.VCAP
                if planned.action_type != expected_type or planned.step is None:
                    raise CapitalDenied("no automatic capital addition is eligible at the current drawdown")
                if planned.step.step_id != action_id or planned.step.addition != amount:
                    raise CapitalDenied("action does not match the next exact DCA/VCAP ladder step")
                prior = conn.execute(
                    "SELECT status FROM position_steps WHERE position_id=? AND step_id=?",
                    (position_id,action_id),
                ).fetchone()
                if prior is not None and prior["status"] != "AVAILABLE":
                    raise CapitalDenied("this ladder step already has an outstanding or completed reservation")
                committed = Decimal(position["gross_committed"])
                if committed + amount > MAX_ASSET_GROSS_NOTIONAL:
                    raise CapitalDenied("per-asset $1,000 gross-notional cap exceeded")
                if action_type == "VCAP":
                    occupied = conn.execute(
                        """SELECT COUNT(*) AS n FROM vcap_shards
                        WHERE asset_id=? AND status IN ('RESERVED','SUBMITTED','FILLED','ACTIVE')""",
                        (position["asset_id"],),
                    ).fetchone()["n"]
                    if occupied >= MAX_ACTIVE_VCAP_SHARDS_PER_ASSET:
                        raise CapitalDenied("per-asset two-shard concurrency limit reached")
                    if conn.execute("SELECT 1 FROM vcap_shards WHERE shard_id=?", (shard_id,)).fetchone():
                        raise CapitalDenied("shard_id already exists")
                if self._available_pool(conn,pool) < amount + fee:
                    raise CapitalDenied(f"{pool.value.lower()} pool has insufficient available capital")
                conn.execute(
                    "UPDATE positions SET gross_committed=?,updated_at=? WHERE position_id=?",
                    (str(committed+amount),now,position_id),
                )
                conn.execute(
                    """INSERT INTO reservations
                    (reservation_id,idempotency_key,position_id,asset_id,action_id,pool_id,
                     gross_notional,fee_reserve,status,expires_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (reservation_id,idempotency_key,position_id,position["asset_id"],action_id,pool.value,
                     str(amount),str(fee),ReservationStatus.RESERVED.value,now+self.reservation_ttl_seconds,now,now),
                )
                lot_id = shard_id if action_type == "VCAP" else f"{position_id}:{action_id}"
                conn.execute(
                    """INSERT INTO position_lots
                    (lot_id,position_id,asset_id,action_id,pool_id,reservation_id,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(lot_id) DO UPDATE SET
                    reservation_id=excluded.reservation_id,status='RESERVED',
                    gross_buy_notional='0',buy_fee_quote='0',filled_quantity='0',
                    remaining_quantity='0',remaining_cost_basis_quote='0',
                    gross_sell_proceeds='0',sell_fee_quote='0',realized_pnl_quote='0',
                    updated_at=excluded.updated_at""",
                    (lot_id,position_id,position["asset_id"],action_id,pool.value,reservation_id,"RESERVED",now,now),
                )
                conn.execute(
                    """INSERT INTO position_steps(position_id,step_id,action_type,status,reservation_id,drawdown,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(position_id,step_id) DO UPDATE SET
                    status='RESERVED',reservation_id=excluded.reservation_id,
                    drawdown=excluded.drawdown,updated_at=excluded.updated_at""",
                    (position_id,action_id,action_type,"RESERVED",reservation_id,str(drawdown),now),
                )
                if action_type == "VCAP":
                    conn.execute(
                        "INSERT INTO vcap_shards VALUES(?,?,?,?,?,?,?,?)",
                        (shard_id,position_id,position["asset_id"],action_id,"RESERVED",reservation_id,now,now),
                    )
                row = conn.execute("SELECT reserved FROM pools WHERE pool_id=?", (pool.value,)).fetchone()
                conn.execute("UPDATE pools SET reserved=? WHERE pool_id=?", (str(Decimal(row["reserved"])+amount+fee),pool.value))
                self._event(conn,"CAPITAL_RESERVED",reservation_id,{"position_id":position_id,"action_id":action_id,"action_type":action_type,"gross_notional":str(amount),"fee_reserve":str(fee),"pool":pool.value,"shard_id":shard_id})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return CapitalAuthorization(reservation_id,idempotency_key,position_id,position["asset_id"],action_id,pool,amount,fee)

    def consume_authorization(
        self, authorization_id: str, *, idempotency_key: str, connector: str
    ) -> CapitalAuthorization:
        self._assert_paper()
        try:
            self.safety.validate_order_boundary(connector, authorization_id)
        except Exception as exc:
            raise CapitalDenied(str(exc)) from exc
        if connector != PAPER_CONNECTOR:
            raise CapitalDenied("only kraken_paper_trade is permitted")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM reservations WHERE reservation_id=?", (authorization_id,)).fetchone()
                if row is None:
                    raise CapitalDenied("unknown CapitalGovernor authorization")
                if row["idempotency_key"] != idempotency_key:
                    raise CapitalDenied("authorization idempotency key mismatch")
                if row["status"] != ReservationStatus.RESERVED.value:
                    raise CapitalDenied("authorization is not unused")
                if row["expires_at"] < now:
                    raise CapitalDenied("authorization expired")
                conn.execute(
                    "UPDATE reservations SET status=?,updated_at=? WHERE reservation_id=?",
                    (ReservationStatus.SUBMITTED.value,now,authorization_id),
                )
                conn.execute(
                    "UPDATE vcap_shards SET status='SUBMITTED',updated_at=? WHERE reservation_id=?",
                    (now,authorization_id),
                )
                conn.execute(
                    "UPDATE position_steps SET status='SUBMITTED',updated_at=? WHERE reservation_id=?",
                    (now,authorization_id),
                )
                self._event(conn,"AUTHORIZATION_CONSUMED",authorization_id,{"connector":connector,"idempotency_key":idempotency_key})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return CapitalAuthorization(
            row["reservation_id"],row["idempotency_key"],row["position_id"],row["asset_id"],
            row["action_id"],CapitalPool(row["pool_id"]),Decimal(row["gross_notional"]),Decimal(row["fee_reserve"]),connector
        )


    def issue_dispatch_receipt(
        self,
        authorization_id: str,
        *,
        request_body: dict,
        now: float,
        ttl_seconds: int = 30,
    ):
        """Sign an order only after its matching Governor reservation is submitted."""
        self._assert_paper()
        from .dispatch_receipt import issue_executor_dispatch_receipt

        if not isinstance(request_body, dict):
            raise CapitalDenied("paper order body must be an object")
        with self._connect() as conn:
            row=conn.execute(
                "SELECT * FROM reservations WHERE reservation_id=?",
                (authorization_id,),
            ).fetchone()
        if row is None or row["status"] != ReservationStatus.SUBMITTED.value:
            raise CapitalDenied("a submitted CapitalGovernor reservation is required to sign an order")
        if request_body.get("account_name") != "master_account":
            raise CapitalDenied("only the master paper account is authorized")
        if request_body.get("connector_name") != PAPER_CONNECTOR:
            raise CapitalDenied("only kraken_paper_trade is authorized")
        if request_body.get("trading_pair") != row["asset_id"]:
            raise CapitalDenied("order market does not match the reserved asset")
        if request_body.get("trade_type") != "BUY" or request_body.get("position_action") != "OPEN":
            raise CapitalDenied("this reservation authorizes only an opening paper buy")
        if request_body.get("order_type") != "LIMIT" or request_body.get("price") in (None, ""):
            raise CapitalDenied("Alpha 2.0 entries/recovery must use a marketable LIMIT order")
        try:
            base_amount=Decimal(str(request_body.get("amount")))
            limit_price=Decimal(str(request_body.get("price")))
        except Exception as exc:
            raise CapitalDenied("order amount and limit price must be decimal values") from exc
        if not base_amount.is_finite() or base_amount <= ZERO or not limit_price.is_finite() or limit_price <= ZERO:
            raise CapitalDenied("order amount and limit price must be positive")
        quote_notional=base_amount*limit_price
        if quote_notional > Decimal(row["gross_notional"]):
            raise CapitalDenied("order quote notional exceeds the reserved CapitalGovernor amount")
        intent="ENTRY" if row["action_id"] == "INITIAL" else "RECOVERY"
        return issue_executor_dispatch_receipt(
            request_body=request_body,
            proposal_id=authorization_id,
            intent=intent,
            amount_quote=Decimal(row["gross_notional"]),
            now=now,
            ttl_seconds=ttl_seconds,
            operation="place_trade",
            method="POST",
            path="/trading/orders",
            account_name="master_account",
            connector_name=PAPER_CONNECTOR,
            trading_pair=row["asset_id"],
        )

    def settle_fill(self, authorization_id: str, *, actual_gross_notional: Decimal, actual_fee: Decimal) -> None:
        self._assert_paper()
        actual = self._positive(actual_gross_notional,"actual_gross_notional")
        if not isinstance(actual_fee,Decimal) or not actual_fee.is_finite() or actual_fee < ZERO:
            raise ValueError("actual_fee must be a finite non-negative Decimal")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT * FROM reservations WHERE reservation_id=?",(authorization_id,)).fetchone()
                if row is None or row["status"] != ReservationStatus.SUBMITTED.value:
                    raise CapitalDenied("only a submitted authorization can be settled")
                reserved_gross=Decimal(row["gross_notional"])
                fee_reserve=Decimal(row["fee_reserve"])
                if actual > reserved_gross or actual_fee > fee_reserve:
                    raise CapitalDenied("actual fill or fee exceeds authorized reserve")
                pool=CapitalPool(row["pool_id"])
                poolrow=conn.execute("SELECT reserved,spent FROM pools WHERE pool_id=?",(pool.value,)).fetchone()
                new_reserved=Decimal(poolrow["reserved"])-reserved_gross-fee_reserve
                new_spent=Decimal(poolrow["spent"])+actual+actual_fee
                conn.execute("UPDATE pools SET reserved=?,spent=? WHERE pool_id=?",(str(new_reserved),str(new_spent),pool.value))
                position=conn.execute("SELECT gross_committed FROM positions WHERE position_id=?",(row["position_id"],)).fetchone()
                adjusted=Decimal(position["gross_committed"])-reserved_gross+actual
                conn.execute("UPDATE positions SET gross_committed=?,status=CASE WHEN ?='INITIAL' THEN 'OPEN' ELSE status END,updated_at=? WHERE position_id=?",(str(adjusted),row["action_id"],now,row["position_id"]))
                conn.execute(
                    "UPDATE reservations SET status=?,actual_gross=?,actual_fee=?,updated_at=? WHERE reservation_id=?",
                    (ReservationStatus.SETTLED.value,str(actual),str(actual_fee),now,authorization_id),
                )
                conn.execute("UPDATE vcap_shards SET status='FILLED',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                conn.execute("UPDATE position_steps SET status='FILLED',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                self._event(conn,"FILL_SETTLED",authorization_id,{"actual_gross_notional":str(actual),"actual_fee":str(actual_fee),"pool":pool.value})
                conn.commit()
            except Exception:
                conn.rollback()
                raise


    def activate_vcap_shard(self, shard_id: str) -> None:
        self._assert_paper()
        """Move a reconciled filled shard into the active position lifecycle."""
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT status FROM vcap_shards WHERE shard_id=?",(shard_id,)).fetchone()
                if row is None or row["status"] != "FILLED":
                    raise CapitalDenied("only a filled shard can become active")
                conn.execute("UPDATE vcap_shards SET status='ACTIVE',updated_at=? WHERE shard_id=?",(now,shard_id))
                conn.execute("UPDATE position_steps SET status='ACTIVE',updated_at=? WHERE reservation_id=(SELECT reservation_id FROM vcap_shards WHERE shard_id=?)",(now,shard_id))
                self._event(conn,"VCAP_SHARD_ACTIVATED",shard_id,{"status":"ACTIVE"})
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def resolve_vcap_shard(self, shard_id: str) -> None:
        self._assert_paper()
        """Release a shard slot only after the caller has reconciled its full exit."""
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT status FROM vcap_shards WHERE shard_id=?",(shard_id,)).fetchone()
                if row is None or row["status"] != "ACTIVE":
                    raise CapitalDenied("only an active shard can be resolved")
                conn.execute("UPDATE vcap_shards SET status='RESOLVED',updated_at=? WHERE shard_id=?",(now,shard_id))
                conn.execute("UPDATE position_steps SET status='RESOLVED',updated_at=? WHERE reservation_id=(SELECT reservation_id FROM vcap_shards WHERE shard_id=?)",(now,shard_id))
                self._event(conn,"VCAP_SHARD_RESOLVED",shard_id,{"slot_released":True})
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def release_unfilled_reservation(self, authorization_id: str, *, confirmed_no_fill: bool = False) -> None:
        self._assert_paper()
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT * FROM reservations WHERE reservation_id=?",(authorization_id,)).fetchone()
                if row is None or row["status"] not in {ReservationStatus.RESERVED.value,ReservationStatus.SUBMITTED.value}:
                    raise CapitalDenied("reservation is not releasable")
                if row["status"] == ReservationStatus.SUBMITTED.value and not confirmed_no_fill:
                    raise CapitalDenied("submitted order must be confirmed unfilled/cancelled before release")
                pool=CapitalPool(row["pool_id"])
                poolrow=conn.execute("SELECT reserved FROM pools WHERE pool_id=?",(pool.value,)).fetchone()
                reserved=Decimal(row["gross_notional"])+Decimal(row["fee_reserve"])
                conn.execute("UPDATE pools SET reserved=? WHERE pool_id=?",(str(Decimal(poolrow["reserved"])-reserved),pool.value))
                pos=conn.execute("SELECT gross_committed,status FROM positions WHERE position_id=?",(row["position_id"],)).fetchone()
                adjusted=Decimal(pos["gross_committed"])-Decimal(row["gross_notional"])
                new_status="CANCELLED" if row["action_id"]=="INITIAL" else pos["status"]
                conn.execute("UPDATE positions SET gross_committed=?,status=?,updated_at=? WHERE position_id=?",(str(adjusted),new_status,now,row["position_id"]))
                conn.execute("UPDATE reservations SET status=?,updated_at=? WHERE reservation_id=?",(ReservationStatus.RELEASED.value,now,authorization_id))
                conn.execute("UPDATE vcap_shards SET status='AVAILABLE',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                conn.execute("UPDATE position_steps SET status='AVAILABLE',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                self._event(conn,"RESERVATION_RELEASED",authorization_id,{"confirmed_no_fill":confirmed_no_fill})
                conn.commit()
            except Exception:
                conn.rollback()
                raise


    def close_position(
        self,
        position_id: str,
        *,
        realized_pnl_quote: Decimal,
        confirmed_all_exits_reconciled: bool = False,
    ) -> None:
        """Close a position only after every exit and shard is reconciled.

        Sale proceeds are not credited to a capital pool by this method. A
        separate settlement operation must reconcile actual proceeds first.
        """
        self._assert_paper()
        if not confirmed_all_exits_reconciled:
            raise CapitalDenied("position closure requires confirmed exit reconciliation")
        if not isinstance(realized_pnl_quote, Decimal) or not realized_pnl_quote.is_finite():
            raise ValueError("realized_pnl_quote must be a finite Decimal")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                position=conn.execute("SELECT status FROM positions WHERE position_id=?",(position_id,)).fetchone()
                if position is None or position["status"] != "OPEN":
                    raise CapitalDenied("only an OPEN position can be closed")
                active=conn.execute(
                    "SELECT COUNT(*) AS n FROM vcap_shards WHERE position_id=? AND status IN ('RESERVED','SUBMITTED','FILLED','ACTIVE')",
                    (position_id,),
                ).fetchone()["n"]
                if active:
                    raise CapitalDenied("all VCAP shards must be resolved before position closure")
                conn.execute("UPDATE positions SET status='CLOSED',updated_at=? WHERE position_id=?",(now,position_id))
                self._event(conn,"POSITION_CLOSED",position_id,{"realized_pnl_quote":str(realized_pnl_quote),"exit_reconciled":True})
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def snapshot(self) -> dict:
        self._assert_paper()
        with self._connect() as conn:
            pools={}
            for row in conn.execute("SELECT * FROM pools").fetchall():
                pools[row["pool_id"]] = {
                    "budget":Decimal(row["budget"]),
                    "reserved":Decimal(row["reserved"]),
                    "spent":Decimal(row["spent"]),
                    "available":Decimal(row["budget"])-Decimal(row["reserved"])-Decimal(row["spent"]),
                }
            open_count=conn.execute("SELECT COUNT(*) AS n FROM positions WHERE status IN ('RESERVED','OPEN')").fetchone()["n"]
            positions=conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
            return {"pools":pools,"open_positions":open_count,"total_positions":positions}

