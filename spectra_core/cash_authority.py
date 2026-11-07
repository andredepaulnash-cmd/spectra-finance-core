from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from .decimal_math import exact_decimal_difference, exact_decimal_sum

ZERO = Decimal("0")


class CashSalePurpose(StrEnum):
    RECOVERY_PARTIAL = "RECOVERY_PARTIAL"
    WHOLE_EXIT = "WHOLE_EXIT"


@dataclass(frozen=True)
class CashAuthoritySnapshot:
    account_name: str
    connector_name: str
    quote_asset: str
    productive_quote: Decimal
    parked_quote: Decimal
    held_quote: Decimal
    blocked: bool
    reason: str
    activated_at: float
    entry_fee_buffer_bps: Decimal


def text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if value < ZERO or (positive and value == ZERO):
        raise ValueError(
            f"{field} must be {'positive' if positive else 'non-negative'}"
        )
    return value


def timestamp(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite and positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


def scope(
    account_name: object, connector_name: object, quote_asset: object
) -> tuple[str, str, str]:
    return (
        text(account_name, "account_name"),
        text(connector_name, "connector_name").lower(),
        text(quote_asset, "quote_asset").upper(),
    )


def payload_json(values: dict) -> str:
    def normalize(value):
        if isinstance(value, Decimal):
            if not value:
                return "0"
            result = format(value, "f")
            return result.rstrip("0").rstrip(".") if "." in result else result
        return value

    return json.dumps(
        {key: normalize(value) for key, value in values.items()},
        sort_keys=True,
        separators=(",", ":"),
    )


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cash_authority (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            activation_id TEXT NOT NULL UNIQUE,
            account_name TEXT NOT NULL,
            connector_name TEXT NOT NULL,
            quote_asset TEXT NOT NULL,
            opening_productive_quote TEXT NOT NULL,
            opening_parked_quote TEXT NOT NULL,
            activated_at REAL NOT NULL,
            entry_fee_buffer_bps TEXT NOT NULL DEFAULT '0',
            last_venue_free_quote TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_sale_bindings (
            manifest_id TEXT PRIMARY KEY,
            sell_executor_id TEXT NOT NULL UNIQUE,
            pair TEXT NOT NULL,
            sale_purpose TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_sale_fills (
            event_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL UNIQUE,
            manifest_id TEXT NOT NULL,
            net_proceeds_quote TEXT NOT NULL,
            sale_purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            settlement_evidence TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_buy_burns (
            proposal_id TEXT PRIMARY KEY,
            amount_quote TEXT NOT NULL,
            excess_quote TEXT NOT NULL DEFAULT '0'
        );
        CREATE TABLE IF NOT EXISTS cash_buy_fills (
            trade_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            spent_quote TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_authority_conflicts (
            conflict_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL
        );
    """)
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(cash_authority)").fetchall()
    }
    for name, sql_type in (
        ("entry_fee_buffer_bps", "TEXT NOT NULL DEFAULT '0'"),
        ("last_venue_free_quote", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE cash_authority ADD COLUMN {name} {sql_type}")


def authorized_cash(amount_quote: Decimal, entry_fee_buffer_bps: Decimal) -> Decimal:
    """Exact finite notional plus configured quote-fee headroom."""
    digits = entry_fee_buffer_bps.as_tuple()
    rate = Decimal((digits.sign, digits.digits, digits.exponent - 4))
    multiplier = exact_decimal_sum((Decimal("1"), rate))
    with localcontext() as context:
        context.prec = max(
            28,
            len(amount_quote.as_tuple().digits) + len(multiplier.as_tuple().digits) + 2,
        )
        return amount_quote * multiplier


def binding(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cash_authority WHERE singleton=1").fetchone()


def record_conflict(conn: sqlite3.Connection, conflict_id: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cash_authority_conflicts(conflict_id, reason) VALUES (?, ?)",
        (text(conflict_id, "conflict_id"), text(reason, "reason")),
    )


def validate_scope(
    bound: sqlite3.Row,
    account_name: object,
    connector_name: object,
    quote_asset: object,
    *,
    pair: str | None = None,
) -> None:
    actual = scope(account_name, connector_name, quote_asset)
    expected = (bound["account_name"], bound["connector_name"], bound["quote_asset"])
    if actual != expected:
        raise ValueError("cash authority account/connector/quote scope mismatch")
    if pair is not None and text(pair, "pair").upper().rsplit("-", 1)[-1] != actual[2]:
        raise ValueError("pair quote does not match cash authority")


def snapshot(conn: sqlite3.Connection) -> CashAuthoritySnapshot | None:
    bound = binding(conn)
    if bound is None:
        return None
    burns = conn.execute(
        "SELECT amount_quote, excess_quote FROM cash_buy_burns"
    ).fetchall()
    settled = conn.execute(
        "SELECT net_proceeds_quote, sale_purpose FROM cash_sale_fills WHERE status='SETTLED'"
    ).fetchall()
    pending = conn.execute(
        "SELECT net_proceeds_quote FROM cash_sale_fills WHERE status='PENDING'"
    ).fetchall()
    productive = exact_decimal_sum(
        (
            Decimal(bound["opening_productive_quote"]),
            *(Decimal(row["amount_quote"]).copy_negate() for row in burns),
            *(Decimal(row["excess_quote"]).copy_negate() for row in burns),
            *(
                Decimal(row["net_proceeds_quote"])
                for row in settled
                if row["sale_purpose"] == CashSalePurpose.WHOLE_EXIT
            ),
        )
    )
    parked = exact_decimal_sum(
        (
            Decimal(bound["opening_parked_quote"]),
            *(
                Decimal(row["net_proceeds_quote"])
                for row in settled
                if row["sale_purpose"] == CashSalePurpose.RECOVERY_PARTIAL
            ),
        )
    )
    held = exact_decimal_sum(Decimal(row["net_proceeds_quote"]) for row in pending)
    conflict = conn.execute(
        "SELECT reason FROM cash_authority_conflicts ORDER BY conflict_id LIMIT 1"
    ).fetchone()
    reason = ""
    if conflict is not None:
        reason = str(conflict["reason"])
    elif pending:
        reason = "cash fill settlement is pending"
    elif productive < ZERO:
        reason = "productive cash authority is overdrawn"
    return CashAuthoritySnapshot(
        account_name=str(bound["account_name"]),
        connector_name=str(bound["connector_name"]),
        quote_asset=str(bound["quote_asset"]),
        productive_quote=productive,
        parked_quote=parked,
        held_quote=held,
        blocked=bool(reason),
        reason=reason,
        activated_at=float(bound["activated_at"]),
        entry_fee_buffer_bps=Decimal(bound["entry_fee_buffer_bps"]),
    )


def authority_rejection(
    conn: sqlite3.Connection, proposal, capital_snapshot=None
) -> str:
    bound = binding(conn)
    if bound is None:
        return ""
    try:
        validate_scope(
            bound,
            proposal.account_name,
            proposal.connector_name,
            proposal.quote_asset,
            pair=proposal.pair,
        )
        if capital_snapshot is not None:
            validate_scope(
                bound,
                capital_snapshot.account_name,
                capital_snapshot.connector_name,
                capital_snapshot.quote_asset,
            )
    except ValueError as exc:
        return str(exc)
    state = snapshot(conn)
    assert state is not None
    return state.reason if state.blocked else ""


def reservation_scope_rejection(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    bound = binding(conn)
    if bound is None:
        return ""
    try:
        validate_scope(
            bound,
            row["account_name"],
            row["connector_name"],
            row["quote_asset"],
            pair=row["pair"],
        )
    except ValueError as exc:
        return str(exc)
    state = snapshot(conn)
    assert state is not None
    return state.reason if state.blocked else ""


def spend_headroom(
    conn: sqlite3.Connection,
    *,
    reserved_cash_quote: Decimal,
    protected_cash_quote: Decimal,
    reserve_floor_quote: Decimal,
) -> bool:
    state = snapshot(conn)
    if state is None:
        return True
    if state.blocked:
        return False
    # RESERVED claims include this proposal; COMMITTED claims were already
    # burned, so they must not be subtracted from productive authority again.
    if exact_decimal_difference(state.productive_quote, reserved_cash_quote) < ZERO:
        return False
    bound = binding(conn)
    assert bound is not None
    venue = bound["last_venue_free_quote"]
    if venue is None:
        return False
    physical_after = exact_decimal_difference(Decimal(venue), protected_cash_quote)
    if physical_after < reserve_floor_quote:
        return False
    if exact_decimal_difference(physical_after, state.parked_quote) < ZERO:
        return False
    return True


def burn(
    conn: sqlite3.Connection,
    proposal_id: str,
    amount_quote: Decimal,
    *,
    reserved_cash_quote: Decimal,
    protected_cash_quote: Decimal,
    reserve_floor_quote: Decimal,
) -> bool:
    state = snapshot(conn)
    if state is None:
        return True
    if not spend_headroom(
        conn,
        reserved_cash_quote=reserved_cash_quote,
        protected_cash_quote=protected_cash_quote,
        reserve_floor_quote=reserve_floor_quote,
    ):
        return False
    amount_quote = authorized_cash(amount_quote, state.entry_fee_buffer_bps)
    existing = conn.execute(
        "SELECT amount_quote FROM cash_buy_burns WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    if existing is not None:
        return Decimal(existing["amount_quote"]) == amount_quote
    conn.execute(
        "INSERT INTO cash_buy_burns(proposal_id, amount_quote) VALUES (?, ?)",
        (proposal_id, str(amount_quote)),
    )
    return True


def evidence_hash(value: object) -> str:
    result = text(value, "settlement_evidence").lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError("settlement_evidence must be a SHA-256 journal proof hash")
    return result
