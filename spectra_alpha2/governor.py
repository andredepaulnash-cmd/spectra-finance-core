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
from .policy import (
    BUY_FEE_RATE,
    SELL_FEE_RATE,
    INITIAL_ENTRY_BUDGET,
    INITIAL_ENTRY_NOTIONAL,
    MAX_OPEN_POSITIONS,
    VCAP_RECOVERY_POOL,
    gross_sell_notional_for_net_target,
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


@dataclass(frozen=True, slots=True)
class ExitAuthorization:
    authorization_id: str
    idempotency_key: str
    position_id: str
    asset_id: str
    lot_id: str
    quantity: Decimal
    minimum_price: Decimal
    expected_gross_proceeds: Decimal
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
                    strategy_origin TEXT NOT NULL DEFAULT 'TOP_GAINER',
                    reference_price TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gross_committed TEXT NOT NULL,
                    manual_review_required INTEGER NOT NULL DEFAULT 0,
                    manual_review_reason TEXT,
                    manual_review_at REAL,
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
                    strategy_origin TEXT NOT NULL DEFAULT 'TOP_GAINER',
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
                    reserved_exit_quantity TEXT NOT NULL DEFAULT '0',
                    gross_sell_proceeds TEXT NOT NULL DEFAULT '0',
                    sell_fee_quote TEXT NOT NULL DEFAULT '0',
                    realized_pnl_quote TEXT NOT NULL DEFAULT '0',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_position_lots_position_status
                    ON position_lots(position_id,status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_unique_order
                    ON reservations(order_id) WHERE order_id IS NOT NULL;
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
                    reservation_id TEXT UNIQUE REFERENCES reservations(reservation_id),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vcap_shards_asset_status
                    ON vcap_shards(asset_id, status);
                CREATE TABLE IF NOT EXISTS exit_authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    position_id TEXT NOT NULL REFERENCES positions(position_id),
                    asset_id TEXT NOT NULL,
                    lot_id TEXT NOT NULL REFERENCES position_lots(lot_id),
                    quantity TEXT NOT NULL,
                    minimum_price TEXT NOT NULL,
                    expected_gross_proceeds TEXT NOT NULL,
                    fee_reserve TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    order_id TEXT,
                    actual_quantity TEXT,
                    actual_gross TEXT,
                    actual_fee TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_exit_authorizations_unique_order
                    ON exit_authorizations(order_id) WHERE order_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                """
            )
            vcap_columns={row[1]:row for row in conn.execute("PRAGMA table_info(vcap_shards)")}
            if vcap_columns.get("reservation_id") is not None and vcap_columns["reservation_id"][3] == 1:
                conn.execute("ALTER TABLE vcap_shards RENAME TO vcap_shards_legacy")
                conn.execute("""CREATE TABLE vcap_shards (
                    shard_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES positions(position_id),
                    asset_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reservation_id TEXT UNIQUE REFERENCES reservations(reservation_id),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )""")
                conn.execute("INSERT INTO vcap_shards SELECT * FROM vcap_shards_legacy")
                conn.execute("DROP TABLE vcap_shards_legacy")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vcap_shards_asset_status ON vcap_shards(asset_id,status)")
            position_columns={row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            for column,declaration in (
                ("strategy_origin","TEXT NOT NULL DEFAULT 'TOP_GAINER'"),
                ("manual_review_required","INTEGER NOT NULL DEFAULT 0"),
                ("manual_review_reason","TEXT"),
                ("manual_review_at","REAL"),
            ):
                if column not in position_columns:
                    conn.execute(f"ALTER TABLE positions ADD COLUMN {column} {declaration}")
            reservation_columns={row[1] for row in conn.execute("PRAGMA table_info(reservations)")}
            if "strategy_origin" not in reservation_columns:
                conn.execute("ALTER TABLE reservations ADD COLUMN strategy_origin TEXT NOT NULL DEFAULT 'TOP_GAINER'")
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
        strategy_origin: str = "TOP_GAINER",
    ) -> CapitalAuthorization:
        self._assert_paper()
        if strategy_origin not in {"TOP_GAINER","TOP_LOSER_REVERSAL"}:
            raise ValueError("strategy_origin must be TOP_GAINER or TOP_LOSER_REVERSAL")
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
                    """INSERT INTO positions(position_id,asset_id,strategy_origin,reference_price,status,gross_committed,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (position_id, asset_id, strategy_origin, str(price), "RESERVED", str(amount), now, now),
                )
                conn.execute(
                    """INSERT INTO reservations
                    (reservation_id,idempotency_key,position_id,asset_id,strategy_origin,action_id,pool_id,
                     gross_notional,fee_reserve,status,expires_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (reservation_id,idempotency_key,position_id,asset_id,strategy_origin,"INITIAL",
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
                    (str(Decimal(conn.execute("SELECT reserved FROM pools WHERE pool_id=?", (CapitalPool.INITIAL.value,)).fetchone()["reserved"]) + amount + fee), CapitalPool.INITIAL.value),
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
        """Fail closed until the Architect-approved VCAP shard policy is configured."""
        self._assert_paper()
        if action_type != "VCAP":
            raise CapitalDenied("DCA purchases are not part of the Alpha 2.0 recovery strategy")
        raise CapitalDenied(
            "VCAP shard selection, batch sizing, drawdown triggers, and recycling policy are not yet approved/configured"
        )
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
        from .dispatch_receipt import _issue_executor_dispatch_receipt

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
        if request_body.get("strategy_origin") != row["strategy_origin"]:
            raise CapitalDenied("order strategy origin does not match the persisted position reservation")
        if request_body.get("strategy_origin") not in {"TOP_GAINER","TOP_LOSER_REVERSAL"}:
            raise CapitalDenied("order strategy origin is unsupported")
        if request_body.get("trade_type") != "BUY" or request_body.get("position_action") != "OPEN":
            raise CapitalDenied("this reservation authorizes only an opening paper buy")
        if request_body.get("order_type") != "MARKET" or request_body.get("price") not in (None, ""):
            raise CapitalDenied("Alpha 2.0 entries and recovery purchases must be MARKET BUY orders without a limit price")
        try:
            base_amount=Decimal(str(request_body.get("amount")))
        except Exception as exc:
            raise CapitalDenied("market-buy base amount must be a decimal value") from exc
        if not base_amount.is_finite() or base_amount <= ZERO:
            raise CapitalDenied("market-buy base amount must be positive")
        try:
            maximum_execution_price=Decimal(str(request_body.get("max_execution_price")))
        except Exception as exc:
            raise CapitalDenied("market-buy maximum execution price must be a decimal value") from exc
        if not maximum_execution_price.is_finite() or maximum_execution_price <= ZERO:
            raise CapitalDenied("market-buy maximum execution price must be positive")
        if base_amount * maximum_execution_price > Decimal(row["gross_notional"]):
            raise CapitalDenied("market-buy quantity at its signed price ceiling exceeds the reserved quote amount")
        # Actual gross plus fee is checked again during fill settlement.
        intent="ENTRY" if row["action_id"] == "INITIAL" else "RECOVERY"
        return _issue_executor_dispatch_receipt(
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


    def bind_order_id(self, authorization_id: str, *, order_id: str) -> None:
        """Persist the Hummingbot order ID before any fill reconciliation."""
        self._assert_paper()
        if not order_id or not str(order_id).strip():
            raise ValueError("order_id is required")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT status,order_id FROM reservations WHERE reservation_id=?",(authorization_id,)).fetchone()
                if row is None or row["status"] != ReservationStatus.SUBMITTED.value:
                    raise CapitalDenied("order ID can only be bound to a submitted authorization")
                if row["order_id"] and row["order_id"] != order_id:
                    raise CapitalDenied("authorization is already bound to a different Hummingbot order")
                conn.execute("UPDATE reservations SET order_id=?,updated_at=? WHERE reservation_id=?",(order_id,now,authorization_id))
                self._event(conn,"ORDER_ID_BOUND",authorization_id,{"order_id":order_id})
                conn.commit()
            except Exception:
                conn.rollback()
                raise


    def authorize_exit(
        self,
        lot_id: str,
        *,
        quantity: Decimal,
        limit_price: Decimal,
        idempotency_key: str,
    ) -> ExitAuthorization:
        """Authorize the confirmed fixed net take-profit for TOP_GAINER lots only."""
        self._assert_paper()
        qty=self._positive(quantity,"quantity")
        price=self._positive(limit_price,"limit_price")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        now=time.time()
        authorization_id=str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                lot=conn.execute("SELECT * FROM position_lots WHERE lot_id=?",(lot_id,)).fetchone()
                if lot is None or lot["status"] != "ACTIVE":
                    raise CapitalDenied("only an active lot can receive a take-profit authorization")
                position=conn.execute("SELECT strategy_origin FROM positions WHERE position_id=?",(lot["position_id"],)).fetchone()
                if position is None or position["strategy_origin"] != "TOP_GAINER":
                    raise CapitalDenied("fixed +1.74% net take-profit is reserved for TOP_GAINER; TOP_LOSER_REVERSAL requires DTTP/DTSL")
                remaining=Decimal(lot["remaining_quantity"])
                reserved_exit=Decimal(lot["reserved_exit_quantity"])
                available=remaining-reserved_exit
                if qty>available:
                    raise CapitalDenied("exit quantity exceeds unreserved remaining lot quantity")
                basis=Decimal(lot["remaining_cost_basis_quote"])*qty/remaining
                required_gross=gross_sell_notional_for_net_target(basis)
                minimum_price=required_gross/qty
                if price<minimum_price:
                    raise CapitalDenied("sell limit price does not achieve the +1.74% net take-profit after fees")
                expected_gross=qty*price
                fee_reserve=expected_gross*SELL_FEE_RATE
                conn.execute("UPDATE position_lots SET reserved_exit_quantity=?,updated_at=? WHERE lot_id=?",(str(reserved_exit+qty),now,lot_id))
                conn.execute(
                    """INSERT INTO exit_authorizations
                    (authorization_id,idempotency_key,position_id,asset_id,lot_id,quantity,
                     minimum_price,expected_gross_proceeds,fee_reserve,status,expires_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (authorization_id,idempotency_key,lot["position_id"],lot["asset_id"],lot_id,
                     str(qty),str(minimum_price),str(expected_gross),str(fee_reserve),
                     ReservationStatus.RESERVED.value,now+self.reservation_ttl_seconds,now,now),
                )
                self._event(conn,"EXIT_QUANTITY_RESERVED",authorization_id,{"lot_id":lot_id,"quantity":str(qty),"minimum_price":str(minimum_price),"expected_gross_proceeds":str(expected_gross)})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ExitAuthorization(authorization_id,idempotency_key,lot["position_id"],lot["asset_id"],lot_id,qty,minimum_price,expected_gross,fee_reserve)
    def consume_exit_authorization(
        self,
        authorization_id: str,
        *,
        idempotency_key: str,
        connector: str,
    ) -> ExitAuthorization:
        self._assert_paper()
        try:
            self.safety.validate_order_boundary(connector,authorization_id)
        except Exception as exc:
            raise CapitalDenied(str(exc)) from exc
        if connector != PAPER_CONNECTOR:
            raise CapitalDenied("only kraken_paper_trade is permitted")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT e.*,p.strategy_origin FROM exit_authorizations e JOIN positions p ON p.position_id=e.position_id WHERE e.authorization_id=?",(authorization_id,)).fetchone()
                if row is None or row["status"] != ReservationStatus.RESERVED.value:
                    raise CapitalDenied("exit authorization is unknown or already consumed")
                if row["idempotency_key"] != idempotency_key:
                    raise CapitalDenied("exit authorization idempotency key mismatch")
                if row["expires_at"]<now:
                    raise CapitalDenied("exit authorization expired")
                conn.execute("UPDATE exit_authorizations SET status=?,updated_at=? WHERE authorization_id=?",(ReservationStatus.SUBMITTED.value,now,authorization_id))
                self._event(conn,"EXIT_AUTHORIZATION_CONSUMED",authorization_id,{"connector":connector})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ExitAuthorization(
            row["authorization_id"],row["idempotency_key"],row["position_id"],row["asset_id"],
            row["lot_id"],Decimal(row["quantity"]),Decimal(row["minimum_price"]),
            Decimal(row["expected_gross_proceeds"]),Decimal(row["fee_reserve"]),connector,
        )

    def issue_exit_dispatch_receipt(
        self,
        authorization_id: str,
        *,
        request_body: dict,
        now: float,
        ttl_seconds: int=30,
    ):
        self._assert_paper()
        from .dispatch_receipt import _issue_executor_dispatch_receipt
        with self._connect() as conn:
            row=conn.execute("SELECT e.*,p.strategy_origin FROM exit_authorizations e JOIN positions p ON p.position_id=e.position_id WHERE e.authorization_id=?",(authorization_id,)).fetchone()
        if row is None or row["status"] != ReservationStatus.SUBMITTED.value:
            raise CapitalDenied("a submitted CapitalGovernor exit authorization is required")
        if request_body.get("account_name")!="master_account" or request_body.get("connector_name")!=PAPER_CONNECTOR:
            raise CapitalDenied("exit order is restricted to the master Kraken paper account")
        if request_body.get("trading_pair")!=row["asset_id"]:
            raise CapitalDenied("exit market does not match the authorized lot")
        if request_body.get("strategy_origin")!=row["strategy_origin"]:
            raise CapitalDenied("exit strategy origin does not match the persisted position")
        if request_body.get("trade_type")!="SELL" or request_body.get("position_action")!="OPEN":
            raise CapitalDenied("exit authorization requires a Kraken spot SELL order")
        if request_body.get("order_type")!="LIMIT":
            raise CapitalDenied("exit authorization requires a LIMIT order")
        amount=Decimal(str(request_body.get("amount")))
        price=Decimal(str(request_body.get("price")))
        if amount != Decimal(row["quantity"]) or price < Decimal(row["minimum_price"]):
            raise CapitalDenied("sell quantity or limit price differs from the authorized take-profit")
        return _issue_executor_dispatch_receipt(
            request_body=request_body,proposal_id=authorization_id,intent="EXIT",
            amount_quote=Decimal(row["expected_gross_proceeds"]),now=now,
            ttl_seconds=ttl_seconds,operation="place_trade",method="POST",
            path="/trading/orders",account_name="master_account",
            connector_name=PAPER_CONNECTOR,trading_pair=row["asset_id"],
        )

    def bind_exit_order_id(self, authorization_id: str, *, order_id: str) -> None:
        self._assert_paper()
        if not order_id:
            raise ValueError("order_id is required")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT status,order_id FROM exit_authorizations WHERE authorization_id=?",(authorization_id,)).fetchone()
                if row is None or row["status"] != ReservationStatus.SUBMITTED.value:
                    raise CapitalDenied("exit order ID can only bind to a submitted exit authorization")
                if row["order_id"] and row["order_id"]!=order_id:
                    raise CapitalDenied("exit authorization is already bound to another order")
                conn.execute("UPDATE exit_authorizations SET order_id=?,updated_at=? WHERE authorization_id=?",(order_id,now,authorization_id))
                self._event(conn,"EXIT_ORDER_ID_BOUND",authorization_id,{"order_id":order_id})
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def settle_exit_fill(
        self,
        authorization_id: str,
        *,
        actual_quantity: Decimal,
        average_price: Decimal,
        actual_fee: Decimal,
    ) -> Decimal:
        """Settle a terminal sell order and credit only actual net proceeds."""
        self._assert_paper()
        qty=self._positive(actual_quantity,"actual_quantity")
        price=self._positive(average_price,"average_price")
        if not isinstance(actual_fee,Decimal) or not actual_fee.is_finite() or actual_fee<ZERO:
            raise ValueError("actual_fee must be a finite non-negative Decimal")
        gross=qty*price
        if actual_fee>gross:
            raise CapitalDenied("sell fee exceeds gross proceeds")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                auth=conn.execute("SELECT e.*,p.strategy_origin FROM exit_authorizations e JOIN positions p ON p.position_id=e.position_id WHERE e.authorization_id=?",(authorization_id,)).fetchone()
                if auth is None or auth["status"] != ReservationStatus.SUBMITTED.value or not auth["order_id"]:
                    raise CapitalDenied("submitted exit authorization with bound order ID is required")
                authorized_qty=Decimal(auth["quantity"])
                if qty>authorized_qty:
                    raise CapitalDenied("actual sell quantity exceeds authorized quantity")
                lot=conn.execute("SELECT * FROM position_lots WHERE lot_id=?",(auth["lot_id"],)).fetchone()
                remaining=Decimal(lot["remaining_quantity"])
                basis=Decimal(lot["remaining_cost_basis_quote"])
                allocated_basis=basis*qty/remaining
                minimum_price=Decimal(auth["minimum_price"])
                if price<minimum_price:
                    raise CapitalDenied("settled sell fill is below its signed adaptive-exit minimum price")
                if actual_fee>gross*SELL_FEE_RATE*Decimal("1.05"):
                    raise CapitalDenied("actual sell fee exceeds the estimated Kraken fee allowance")
                reserved_exit=Decimal(lot["reserved_exit_quantity"])-authorized_qty
                remaining_qty=remaining-qty
                remaining_basis=basis-allocated_basis
                net=gross-actual_fee
                realized=net-allocated_basis
                lot_status="RESOLVED" if remaining_qty==ZERO else "ACTIVE"
                conn.execute(
                    """UPDATE position_lots SET status=?,reserved_exit_quantity=?,remaining_quantity=?,
                    remaining_cost_basis_quote=?,gross_sell_proceeds=?,sell_fee_quote=?,realized_pnl_quote=?,updated_at=?
                    WHERE lot_id=?""",
                    (lot_status,str(reserved_exit),str(remaining_qty),str(remaining_basis),
                     str(Decimal(lot["gross_sell_proceeds"])+gross),
                     str(Decimal(lot["sell_fee_quote"])+actual_fee),
                     str(Decimal(lot["realized_pnl_quote"])+realized),now,auth["lot_id"]),
                )
                pool=CapitalPool(lot["pool_id"])
                poolrow=conn.execute("SELECT recovered FROM pools WHERE pool_id=?",(pool.value,)).fetchone()
                conn.execute("UPDATE pools SET recovered=? WHERE pool_id=?",(str(Decimal(poolrow["recovered"])+net),pool.value))
                conn.execute(
                    """UPDATE exit_authorizations SET status=?,actual_quantity=?,actual_gross=?,actual_fee=?,updated_at=?
                    WHERE authorization_id=?""",
                    (ReservationStatus.SETTLED.value,str(qty),str(gross),str(actual_fee),now,authorization_id),
                )
                if lot_status=="RESOLVED":
                    shard=conn.execute("SELECT shard_id FROM vcap_shards WHERE shard_id=?",(auth["lot_id"],)).fetchone()
                    if shard is not None:
                        conn.execute("UPDATE vcap_shards SET status='RESOLVED',updated_at=? WHERE shard_id=?",(now,auth["lot_id"]))
                        conn.execute("UPDATE position_steps SET status='RESOLVED',updated_at=? WHERE position_id=? AND step_id=?",(now,lot["position_id"],lot["action_id"]))
                        self._event(conn,"VCAP_SHARD_RESOLVED",auth["lot_id"],{"remaining_quantity":"0"})
                        conn.execute("UPDATE vcap_shards SET status='RECYCLED',updated_at=? WHERE shard_id=?",(now,auth["lot_id"]))
                        conn.execute("UPDATE position_steps SET status='RECYCLED',updated_at=? WHERE position_id=? AND step_id=?",(now,lot["position_id"],lot["action_id"]))
                        self._event(conn,"VCAP_SHARD_RECYCLED",auth["lot_id"],{"reusable_capital_credited":str(net)})
                self._event(conn,"AUTHORIZED_EXIT_SETTLED",authorization_id,{
                    "lot_id":auth["lot_id"],"quantity":str(qty),"gross_proceeds":str(gross),
                    "sell_fee_quote":str(actual_fee),"net_proceeds_credited":str(net),
                    "realized_pnl_quote":str(realized),"remaining_quantity":str(remaining_qty),
                })
                conn.commit()
                return realized
            except Exception:
                conn.rollback()
                raise

    def release_exit_authorization(self,authorization_id: str,*,confirmed_no_fill: bool=False) -> None:
        self._assert_paper()
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                auth=conn.execute("SELECT e.*,p.strategy_origin FROM exit_authorizations e JOIN positions p ON p.position_id=e.position_id WHERE e.authorization_id=?",(authorization_id,)).fetchone()
                if auth is None or auth["status"] not in {ReservationStatus.RESERVED.value,ReservationStatus.SUBMITTED.value}:
                    raise CapitalDenied("exit authorization is not releasable")
                if auth["status"]==ReservationStatus.SUBMITTED.value and not confirmed_no_fill:
                    raise CapitalDenied("submitted exit order must be confirmed unfilled/cancelled before release")
                lot=conn.execute("SELECT reserved_exit_quantity FROM position_lots WHERE lot_id=?",(auth["lot_id"],)).fetchone()
                new_reserved=Decimal(lot["reserved_exit_quantity"])-Decimal(auth["quantity"])
                conn.execute("UPDATE position_lots SET reserved_exit_quantity=?,updated_at=? WHERE lot_id=?",(str(new_reserved),now,auth["lot_id"]))
                conn.execute("UPDATE exit_authorizations SET status=?,updated_at=? WHERE authorization_id=?",(ReservationStatus.RELEASED.value,now,authorization_id))
                self._event(conn,"EXIT_AUTHORIZATION_RELEASED",authorization_id,{"confirmed_no_fill":confirmed_no_fill})
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def settle_fill(
        self,
        authorization_id: str,
        *,
        actual_gross_notional: Decimal,
        actual_fee: Decimal,
        actual_quantity: Decimal,
        average_price: Decimal,
    ) -> None:
        self._assert_paper()
        actual=self._positive(actual_gross_notional,"actual_gross_notional")
        quantity=self._positive(actual_quantity,"actual_quantity")
        price=self._positive(average_price,"average_price")
        if not isinstance(actual_fee,Decimal) or not actual_fee.is_finite() or actual_fee < ZERO:
            raise ValueError("actual_fee must be a finite non-negative Decimal")
        if abs(quantity*price-actual) > max(Decimal("0.00000001"),actual*Decimal("0.00000001")):
            raise CapitalDenied("fill quantity and average price do not reconcile to gross notional")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT * FROM reservations WHERE reservation_id=?",(authorization_id,)).fetchone()
                if row is None or row["status"] != ReservationStatus.SUBMITTED.value:
                    raise CapitalDenied("only a submitted authorization can be settled")
                if not row["order_id"]:
                    raise CapitalDenied("Hummingbot order ID must be persisted before fill settlement")
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
                lot=conn.execute("SELECT lot_id FROM position_lots WHERE reservation_id=?",(authorization_id,)).fetchone()
                if lot is None:
                    raise CapitalDenied("independent position lot is missing for this reservation")
                lot_status="FILLED" if row["action_id"].startswith("VCAP_") else "ACTIVE"
                conn.execute(
                    """UPDATE position_lots SET status=?,gross_buy_notional=?,buy_fee_quote=?,
                    filled_quantity=?,remaining_quantity=?,remaining_cost_basis_quote=?,updated_at=?
                    WHERE reservation_id=?""",
                    (lot_status,str(actual),str(actual_fee),str(quantity),str(quantity),str(actual+actual_fee),now,authorization_id),
                )
                conn.execute(
                    "UPDATE reservations SET status=?,actual_gross=?,actual_fee=?,updated_at=? WHERE reservation_id=?",
                    (ReservationStatus.SETTLED.value,str(actual),str(actual_fee),now,authorization_id),
                )
                conn.execute("UPDATE vcap_shards SET status='FILLED',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                conn.execute("UPDATE position_steps SET status='FILLED',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                self._event(conn,"FILL_SETTLED",authorization_id,{"actual_gross_notional":str(actual),"actual_fee":str(actual_fee),"actual_quantity":str(quantity),"average_price":str(price),"pool":pool.value})
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
                conn.execute("UPDATE position_lots SET status='ACTIVE',updated_at=? WHERE lot_id=?",(now,shard_id))
                self._event(conn,"VCAP_SHARD_ACTIVATED",shard_id,{"status":"ACTIVE"})
                conn.commit()
            except Exception:
                conn.rollback()
                raise


    def resolve_vcap_shard(self, shard_id: str) -> None:
        """Verify a VCAP slot was released by a fully reconciled sell fill."""
        self._assert_paper()
        with self._connect() as conn:
            row=conn.execute(
                """SELECT s.status,l.remaining_quantity
                FROM vcap_shards s JOIN position_lots l ON l.lot_id=s.shard_id
                WHERE s.shard_id=?""",
                (shard_id,),
            ).fetchone()
        if row is None or row["status"] != "RECYCLED" or Decimal(row["remaining_quantity"]) != ZERO:
            raise CapitalDenied("VCAP shard can only be recycled after its fully reconciled exit")

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
                conn.execute("UPDATE vcap_shards SET status='AVAILABLE',reservation_id=NULL,updated_at=? WHERE reservation_id=?",(now,authorization_id))
                conn.execute("UPDATE position_steps SET status='AVAILABLE',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                conn.execute("UPDATE position_lots SET status='AVAILABLE',updated_at=? WHERE reservation_id=?",(now,authorization_id))
                self._event(conn,"RESERVATION_RELEASED",authorization_id,{"confirmed_no_fill":confirmed_no_fill})
                conn.commit()
            except Exception:
                conn.rollback()
                raise


    def close_position(
        self,
        position_id: str,
        *,
        confirmed_all_exits_reconciled: bool = False,
    ) -> Decimal:
        """Close a position only after every lot exit has been reconciled."""

        self._assert_paper()
        if not confirmed_all_exits_reconciled:
            raise CapitalDenied("position closure requires confirmed exit reconciliation")
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
                lots=conn.execute("SELECT remaining_quantity FROM position_lots WHERE position_id=?",(position_id,)).fetchall()
                if any(Decimal(lot["remaining_quantity"]) > ZERO for lot in lots):
                    raise CapitalDenied("all position lots must be fully exited before position closure")
                lot_rows=conn.execute("SELECT realized_pnl_quote FROM position_lots WHERE position_id=?",(position_id,)).fetchall()
                realized_pnl=sum((Decimal(lot["realized_pnl_quote"]) for lot in lot_rows),ZERO)
                conn.execute("UPDATE positions SET status='CLOSED',updated_at=? WHERE position_id=?",(now,position_id))
                self._event(conn,"POSITION_CLOSED",position_id,{"realized_pnl_quote":str(realized_pnl),"exit_reconciled":True})
                conn.commit()
                return realized_pnl
            except Exception:
                conn.rollback()
                raise



    def list_pending_authorizations(self) -> list[dict]:
        """Return submitted buy and exit orders requiring restart reconciliation."""
        self._assert_paper()
        with self._connect() as conn:
            buys=conn.execute(
                """SELECT r.*,l.lot_id,s.shard_id
                FROM reservations r
                LEFT JOIN position_lots l ON l.reservation_id=r.reservation_id
                LEFT JOIN vcap_shards s ON s.reservation_id=r.reservation_id
                WHERE r.status=? ORDER BY r.created_at""",
                (ReservationStatus.SUBMITTED.value,),
            ).fetchall()
            exits=conn.execute(
                """SELECT x.*,s.shard_id
                FROM exit_authorizations x
                LEFT JOIN vcap_shards s ON s.shard_id=x.lot_id
                WHERE x.status=? ORDER BY x.created_at""",
                (ReservationStatus.SUBMITTED.value,),
            ).fetchall()
        result=[
            {
                "kind":"BUY","authorization_id":row["reservation_id"],
                "idempotency_key":row["idempotency_key"],"position_id":row["position_id"],
                "asset_id":row["asset_id"],"action_id":row["action_id"],
                "pool":CapitalPool(row["pool_id"]),"gross_notional":Decimal(row["gross_notional"]),
                "fee_reserve":Decimal(row["fee_reserve"]),"order_id":row["order_id"],
                "lot_id":row["lot_id"],"shard_id":row["shard_id"],"expires_at":row["expires_at"],
            } for row in buys
        ]
        result.extend(
            {
                "kind":"EXIT","authorization_id":row["authorization_id"],
                "idempotency_key":row["idempotency_key"],"position_id":row["position_id"],
                "asset_id":row["asset_id"],"action_id":"EXIT","pool":None,
                "gross_notional":Decimal(row["expected_gross_proceeds"]),
                "fee_reserve":Decimal(row["fee_reserve"]),"order_id":row["order_id"],
                "lot_id":row["lot_id"],"shard_id":row["shard_id"],"expires_at":row["expires_at"],
                "quantity":Decimal(row["quantity"]),"minimum_price":Decimal(row["minimum_price"]),
            } for row in exits
        )
        return result


    def list_positions(self) -> list[dict]:
        """List reserved/open positions so pending entries occupy the 74-position ceiling."""
        self._assert_paper()
        with self._connect() as conn:
            rows=conn.execute(
                "SELECT position_id,asset_id,strategy_origin,reference_price,status,gross_committed,manual_review_required,manual_review_reason,manual_review_at,created_at,updated_at FROM positions WHERE status IN ('RESERVED','OPEN') ORDER BY created_at"
            ).fetchall()
        return [
            {
                "position_id":row["position_id"],"asset_id":row["asset_id"],
                "strategy_origin":row["strategy_origin"],
                "reference_price":Decimal(row["reference_price"]),
                "status":row["status"],"gross_committed":Decimal(row["gross_committed"]),
                "manual_review_required":bool(row["manual_review_required"]),
                "manual_review_reason":row["manual_review_reason"],"manual_review_at":row["manual_review_at"],
                "created_at":row["created_at"],"updated_at":row["updated_at"],
            } for row in rows
        ]


    def get_position_state(self,position_id:str) -> dict | None:
        """Return immutable reference, completed ladder steps, and independent active lots."""
        self._assert_paper()
        with self._connect() as conn:
            position=conn.execute("SELECT * FROM positions WHERE position_id=?",(position_id,)).fetchone()
            if position is None:
                return None
            steps=conn.execute("SELECT step_id,action_type,status FROM position_steps WHERE position_id=?",(position_id,)).fetchall()
            lots=conn.execute(
                """SELECT lot_id,action_id,pool_id,status,remaining_quantity,
                remaining_cost_basis_quote,reserved_exit_quantity,realized_pnl_quote
                FROM position_lots WHERE position_id=? AND status IN ('ACTIVE','FILLED')
                ORDER BY created_at""",
                (position_id,),
            ).fetchall()
        completed_dca={
            row["step_id"] for row in steps
            if row["action_type"]=="DCA" and row["status"] in {"SUBMITTED","FILLED","ACTIVE","RESOLVED","RECYCLED"}
        }
        completed_vcap={
            row["step_id"] for row in steps
            if row["action_type"]=="VCAP" and row["status"] in {"SUBMITTED","FILLED","ACTIVE","RESOLVED","RECYCLED"}
        }
        active_lots=[
            {
                "lot_id":row["lot_id"],"action_id":row["action_id"],"pool_id":CapitalPool(row["pool_id"]),
                "status":row["status"],"remaining_quantity":Decimal(row["remaining_quantity"]),
                "remaining_cost_basis_quote":Decimal(row["remaining_cost_basis_quote"]),
                "reserved_exit_quantity":Decimal(row["reserved_exit_quantity"]),
                "realized_pnl_quote":Decimal(row["realized_pnl_quote"]),
            } for row in lots if Decimal(row["remaining_quantity"])>ZERO
        ]
        return {
            "position_id":position["position_id"],"asset_id":position["asset_id"],
            "strategy_origin":position["strategy_origin"],
            "reference_price":Decimal(position["reference_price"]),
            "status":position["status"],"gross_committed":Decimal(position["gross_committed"]),
            "manual_review_required":bool(position["manual_review_required"]),
            "manual_review_reason":position["manual_review_reason"],
            "completed_dca_steps":completed_dca,"completed_vcap_steps":completed_vcap,
            "active_lots":active_lots,
            "total_quantity":sum((lot["remaining_quantity"] for lot in active_lots),ZERO),
            "total_cost_basis_quote":sum((lot["remaining_cost_basis_quote"] for lot in active_lots),ZERO),
        }

    def flag_manual_review(self,position_id:str,*,drawdown:Decimal,reason:str) -> None:
        """Stop all automatic additions without closing or repricing the position."""
        self._assert_paper()
        if not isinstance(drawdown,Decimal) or not drawdown.is_finite():
            raise ValueError("drawdown must be a finite Decimal")
        if not reason:
            raise ValueError("manual-review reason is required")
        now=time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row=conn.execute("SELECT status,manual_review_required FROM positions WHERE position_id=?",(position_id,)).fetchone()
                if row is None or row["status"]!="OPEN":
                    raise CapitalDenied("only an open position can be flagged for manual review")
                conn.execute(
                    "UPDATE positions SET manual_review_required=1,manual_review_reason=?,manual_review_at=?,updated_at=? WHERE position_id=?",
                    (reason,now,now,position_id),
                )
                if not row["manual_review_required"]:
                    self._event(conn,"POSITION_FLAGGED_MANUAL_REVIEW",position_id,{
                        "drawdown":str(drawdown),"reason":reason,"reference_price_reset":False,
                        "automatic_patching_disabled":True,
                    })
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
                    "recovered":Decimal(row["recovered"]),
                    "available":Decimal(row["budget"])-Decimal(row["reserved"])-Decimal(row["spent"])+Decimal(row["recovered"]),
                }
            open_count=conn.execute("SELECT COUNT(*) AS n FROM positions WHERE status IN ('RESERVED','OPEN')").fetchone()["n"]
            positions=conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
            open_lots=conn.execute("SELECT COUNT(*) AS n FROM position_lots WHERE status IN ('ACTIVE','FILLED')").fetchone()["n"]
            active_shards=conn.execute("SELECT COUNT(*) AS n FROM vcap_shards WHERE status IN ('RESERVED','SUBMITTED','FILLED','ACTIVE')").fetchone()["n"]
            manual_review_count=conn.execute("SELECT COUNT(*) AS n FROM positions WHERE manual_review_required=1 AND status='OPEN'").fetchone()["n"]
            realized_rows=conn.execute("SELECT realized_pnl_quote FROM position_lots").fetchall()
            realized_pnl=sum((Decimal(row["realized_pnl_quote"]) for row in realized_rows),ZERO)
            return {"pools":pools,"open_positions":open_count,"total_positions":positions,"open_lots":open_lots,"active_vcap_shards":active_shards,"manual_review_positions":manual_review_count,"realized_pnl_quote":realized_pnl}

