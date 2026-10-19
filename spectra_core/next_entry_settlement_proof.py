"""Read-only exact BUY and terminal proofs for the opt-in paper funding saga.

No caller attestations, database initialization, native mutation, or order APIs live
here. The account writer must serialize paper/native writes around these reads.
Proofs retain original receipt bytes and immutable native acquisition fields;
mutable remaining inventory is deliberately not used as original cost evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation, localcontext
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3

from .accounting_health import AccountingHealthPolicy
from .fill_reconciliation import trade_fill_evidence_from_raw
from .next_entry_profit_bridge import ProfitBridgeBlocked

ZERO = Decimal(0)
PENDING = frozenset({"PREPARING", "OPEN", "PARTIAL", "CANCEL_REQUESTED"})
TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED"})


def _check(condition, reason):
    if not condition:
        raise ProfitBridgeBlocked(reason)


def _decimal(value):
    _check(not isinstance(value, (bool, float)), "Inexact monetary evidence")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProfitBridgeBlocked("Invalid monetary evidence") from exc
    _check(result.is_finite() and len(result.as_tuple().digits) <= 80
           and abs(result.as_tuple().exponent) <= 80, "Unbounded monetary evidence")
    return result


def _time(value):
    _check(not isinstance(value, bool) and isinstance(value, (int, float))
           and 0 <= value <= 253402300799 and isfinite(value) and int(value) == value,
           "Whole UTC seconds required")
    return int(value)


def _identifier(value):
    _check(isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip(),
           "Invalid evidence identifier")
    return value


def canonical_json(value):
    """Canonical detached encoding. Monetary decimals are exact JSON strings."""
    def default(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(source_payload):
    _check(isinstance(source_payload, str), "Canonical source text required")
    return hashlib.sha256(source_payload.encode("utf-8")).hexdigest()


def _decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            _check(key not in result, "Duplicate JSON evidence key")
            result[key] = value
        return result
    def invalid(value):
        raise ProfitBridgeBlocked("Nonfinite JSON evidence")
    try:
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid)
    except (TypeError, ValueError) as exc:
        raise ProfitBridgeBlocked("Invalid JSON evidence") from exc


@dataclass(frozen=True)
class EntrySettlementContext:
    account_id: str
    connector: str
    controller_id: str
    entry_id: str
    order_id: str
    pair: str
    position_id: str
    proposal_id: str
    executor_id: str
    native_reservation_id: str
    entry_pool: str
    base_budget: Decimal
    profit_budget: Decimal
    authorized_quantity: Decimal
    reference_price: Decimal
    reserved_at: int
    funding_owner: str
    snapshot_id: str | None = None
    allocation_id: str | None = None
    owner_id: str | None = None
    allocation_role: str | None = None
    source_pool: str | None = None

    def __post_init__(self):
        for field in ("account_id", "connector", "controller_id", "entry_id", "order_id", "pair",
                      "position_id", "proposal_id", "executor_id", "native_reservation_id", "entry_pool"):
            _identifier(getattr(self, field))
        _check(self.connector == self.connector.lower(), "Connector must be canonical")
        _check(self.pair == self.pair.upper() and self.pair.count("-") == 1
               and self.pair.split("-")[0] and self.pair.split("-")[1] == "USD",
               "Only exact USD quote pairs supported")
        _check(self.native_reservation_id == self.proposal_id, "Native reservation/proposal mismatch")
        _check(self.entry_pool == "next_entry:" + self.entry_id, "Invalid funded entry pool")
        _check(self.funding_owner in {"seed", "repair"}, "Explicit paper funding owner required")
        for field in ("base_budget", "profit_budget", "authorized_quantity", "reference_price"):
            _check(isinstance(getattr(self, field), Decimal), "Context amounts must be Decimal")
            _decimal(getattr(self, field))
        _check(self.base_budget >= ZERO and self.profit_budget > ZERO
               and self.authorized_quantity > ZERO and self.reference_price > ZERO,
               "Invalid funded entry amounts")
        _check(type(self.reserved_at) is int, "Context reserved_at must be integer UTC seconds")
        _time(self.reserved_at)
        for field in ("snapshot_id", "allocation_id", "owner_id", "source_pool"):
            if getattr(self, field) is not None:
                _identifier(getattr(self, field))
        _check(self.allocation_role in {None, "POSITION_ALLOCATION", "VCAP"}, "Unknown allocation role")

    @property
    def full_budget(self):
        with localcontext(Context(prec=512)) as ctx:
            ctx.prec = 512
            ctx.traps[Inexact] = True
            return self.base_budget + self.profit_budget

    @property
    def source_payload(self):
        return canonical_json(asdict(self))

    @property
    def source_digest(self):
        return fingerprint(self.source_payload)

    def as_dict(self):
        return _decode(self.source_payload)


@dataclass(frozen=True)
class VerifiedEntryReceipts:
    settlement_ids: tuple[str, ...]
    receipts: tuple[str, ...]
    total_quantity: Decimal
    total_fee_inclusive_debit: Decimal
    latest_reconciled_at: int | None
    source_payload: str
    source_digest: str

    def as_dict(self):
        result = _decode(self.source_payload)
        result["source_payload"] = self.source_payload
        result["source_digest"] = self.source_digest
        return result


@dataclass(frozen=True)
class VerifiedTerminalClosure:
    batch: VerifiedEntryReceipts
    terminal_status: str
    terminal_at: int
    verified_at: int
    source_payload: str
    source_digest: str

    def as_dict(self):
        result = _decode(self.source_payload)
        result["source_payload"] = self.source_payload
        result["source_digest"] = self.source_digest
        return result


def identity():
    """Hash pins for the proof implementation and every consulted native contract."""
    root = Path(__file__).resolve().parent
    names = (Path(__file__).name, "next_entry_profit_bridge.py", "next_entry_allocator.py",
             "economic_position.py", "fill_reconciliation.py", "capital_governor.py",
             "accounting_health.py")
    return {"implementation": "exact-paper-entry-settlement-proof-v2", "schema": 1,
            "sources": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names},
            "health_max_age_seconds": AccountingHealthPolicy().max_age_seconds}


@contextmanager
def _native(native_db):
    path = Path(native_db).resolve()
    _check(path.is_file(), "Existing native authority database required")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _rows(db, query, args=()):
    cursor = db.execute(query, args)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(db, table, key, value):
    # Table and column identifiers originate exclusively in hard-coded call sites.
    rows = _rows(db, f"SELECT * FROM {table} WHERE {key}=?", (value,))
    _check(len(rows) == 1, "Missing or duplicate " + table + " evidence")
    return rows[0]


def _paper(paper, context, now):
    row = _one(paper, "state", "id", 1)
    state = _decode(row["value"])
    config = state["identity"]["config"]
    _check(state["identity"]["schema"] == 1 and config["account"] == context.account_id
           and config["connector"] == context.connector and config["owner"] == context.controller_id,
           "Foreign paper authority")
    _check(not state.get("quarantine") and state.get("breaker") == "NORMAL", "Paper accounting quarantine")
    _check(_time(state["now"]) <= now, "Future paper state")
    order = state["orders"].get(context.order_id)
    _check(isinstance(order, dict), "Missing exact paper entry order")
    for key, expected in (("id", context.order_id), ("side", "BUY"), ("pair", context.pair),
                          ("position", context.position_id), ("proposal", context.proposal_id),
                          ("executor", context.executor_id), ("pool", context.entry_pool),
                          ("funding_owner", context.funding_owner)):
        _check(order.get(key) == expected, "Paper entry binding mismatch: " + key)
    intent = "ENTRY" if context.funding_owner == "seed" else "RECOVERY"
    _check(order.get("intent") == intent, "Paper entry intent mismatch")
    _check(order.get("entry_id") == context.entry_id, "Paper entry identity mismatch")
    for key, expected in (("base_budget", context.base_budget), ("profit_budget", context.profit_budget)):
        _check(_decimal(order[key]) == expected, "Paper entry funding budget mismatch: " + key)
    for key, expected in (("snapshot_id", context.snapshot_id), ("allocation_id", context.allocation_id),
                          ("allocation_owner_id", context.owner_id), ("allocation_role", context.allocation_role),
                          ("source_pool", context.source_pool)):
        if expected is not None:
            _check(order.get(key) == expected, "Paper entry allocation mismatch: " + key)
    for key, expected in (("target", context.full_budget), ("quantity", context.authorized_quantity),
                          ("reference", context.reference_price)):
        _check(_decimal(order[key]) == expected, "Paper full target/quantity/reference mismatch: " + key)
    principal = _decimal(order["principal"])
    _check(ZERO < principal <= context.full_budget
           and context.authorized_quantity * context.reference_price <= principal,
           "Paper principal/quantity exceeds native authorization")
    _check(_time(order["decision_at"]) <= context.reserved_at <= _time(state["now"])
           and _time(order["decision_at"]) <= _time(order["created_at"]) <= context.reserved_at,
           "Paper decision/reservation chronology mismatch")
    _check(order["status"] in PENDING | TERMINAL, "Unsupported entry order status")
    _check((order["status"] in TERMINAL and order.get("terminal") == order["status"])
           or (order["status"] in PENDING and order.get("terminal") is None),
           "Conflicting paper terminal status")
    return state, order


def _reservation(native, context, order, paper_now):
    reservation = _one(native, "capital_reservations", "proposal_id", context.native_reservation_id)
    _check(reservation["proposal_id"] == context.proposal_id and reservation["pair"] == context.pair
           and reservation["intent"] == order["intent"]
           and reservation["status"] in {"COMMITTED", "RECONCILED"},
           "Native reservation scope/status mismatch")
    _check(_decimal(reservation["amount_quote"]) == _decimal(order["principal"])
           and _decimal(reservation["leverage"]) == 1, "Native reservation amount/leverage mismatch")
    _check(_time(order["decision_at"]) <= _time(reservation["created_at"]) <= context.reserved_at,
           "Native reservation chronology mismatch")
    _check(_time(reservation["created_at"]) <= _time(reservation["updated_at"]) <= paper_now,
           "Native reservation update chronology mismatch")
    return reservation


def _order_receipts(paper, context):
    # Scan raw identities too: moving a row to a foreign SQL order_id cannot hide it.
    result = []
    for row in _rows(paper, "SELECT rowid AS receipt_sequence,* FROM receipts ORDER BY rowid"):
        raw = _decode(row["payload"])
        _check(isinstance(raw, dict), "Invalid raw paper receipt")
        if row["order_id"] == context.order_id or raw.get("order_id") == context.order_id:
            _check(row["order_id"] == raw.get("order_id") == context.order_id,
                   "Paper receipt order identity mismatch")
            _check(row["applied"] == 1, "Unapplied paper entry receipt")
            result.append((row, raw))
    return result


def _authority_chronology(order, reservation, binding, provenance, execution, position, boundary):
    """Validate authorization independently of whether the order has any fills."""
    binding_at = _time(binding["created_at"])
    provenance_at = _time(provenance["created_at"])
    _check(_time(order["created_at"]) <= binding_at <= boundary
           and _time(reservation["created_at"]) <= provenance_at == _time(execution["created_at"])
           <= binding_at <= boundary
           and _time(position["created_at"]) <= binding_at,
           "Native authority chronology exceeds paper/terminal boundary or is inconsistent")


def _verify(paper, native, context, now):
    state, order = _paper(paper, context, now)
    reservation = _reservation(native, context, order, _time(state["now"]))
    rows = _order_receipts(paper, context)
    native_rows = _rows(native, "SELECT * FROM economic_trade_fill_reconciliation WHERE order_id=?",
                        (context.order_id,))
    _check({row["trade_id"] for row in native_rows} == {row["id"] for row, _ in rows},
           "Paper/native entry receipt coverage mismatch")
    native_lots = _rows(native, "SELECT lot_id FROM economic_lots WHERE proposal_id=? OR executor_id=?",
                        (context.proposal_id, context.executor_id))
    _check({row["lot_id"] for row in native_lots} == {row["result_id"] for row in native_rows},
           "Native acquisition has no exact order receipt")
    receipts = []
    quantity = ZERO
    debit = ZERO
    gross = ZERO
    latest = None
    if rows or order["status"] in TERMINAL:
        _check(not rows or order.get("execution_frozen") is True, "Entry execution quantity was not frozen")
        _check(reservation["status"] == "RECONCILED", "Filled or terminal entry native reservation is not reconciled")
        binding = _one(native, "economic_fill_bindings", "order_id", context.order_id)
        provenance = _one(native, "execution_provenance", "proposal_id", context.proposal_id)
        execution = _one(native, "execution_receipts", "proposal_id", context.proposal_id)
        position = _one(native, "economic_positions", "position_id", context.position_id)
        role = "BASE_ENTRY" if context.funding_owner == "seed" else "VCAP_RECOVERY"
        for key, expected in (("order_id", context.order_id), ("position_id", context.position_id),
                              ("role", role), ("pair", context.pair), ("account_name", context.account_id),
                              ("connector_name", context.connector), ("proposal_id", context.proposal_id),
                              ("executor_id", context.executor_id)):
            _check(binding[key] == expected, "Native entry binding mismatch: " + key)
        for key, expected in (("proposal_id", context.proposal_id), ("executor_id", context.executor_id),
                              ("pair", context.pair), ("intent", order["intent"]),
                              ("account_name", context.account_id), ("connector_name", context.connector),
                              ("controller_id", context.controller_id)):
            _check(provenance[key] == expected, "Native execution provenance mismatch: " + key)
        _check(execution["proposal_id"] == context.proposal_id and execution["executor_id"] == context.executor_id,
               "Native execution receipt identity mismatch")
        _check(_decimal(provenance["authorized_quote"]) == _decimal(reservation["amount_quote"])
               and _decimal(provenance["leverage"]) == _decimal(reservation["leverage"]),
               "Native provenance authorization mismatch")
        _check(position["pair"] == context.pair and position["base_asset"] == context.pair.split("-")[0],
               "Native economic position mismatch")
        _authority_chronology(order, reservation, binding, provenance, execution, position, _time(state["now"]))
        # These watermarks are mutable as the same native executor continues
        # accounting after BUY closure. Only immutable authority is terminal-bound.
        _check(_time(provenance["created_at"]) <= _time(reservation["updated_at"]) <= _time(state["now"]),
               "Native reservation update chronology mismatch")
        _check(_time(position["created_at"]) <= _time(position["updated_at"]) <= _time(state["now"]),
               "Native position update chronology mismatch")
        immutable_order = {key: order[key] for key in ("id", "pair", "position", "proposal", "executor", "side",
            "pool", "funding_owner", "intent", "target", "principal", "quantity", "reference", "decision_at", "created_at",
            "entry_id", "base_budget", "profit_budget")}
        for key in ("source_pool", "snapshot_id", "allocation_id", "allocation_owner_id", "allocation_role",
                    "decision_quantity", "decision_reference"):
            if key in order:
                immutable_order[key] = order[key]
        immutable_reservation = {key: reservation[key] for key in
            ("proposal_id", "pair", "intent", "amount_quote", "leverage", "created_at")}
        for row, raw in rows:
            for name in ("amount", "price", "fee_paid"):
                _decimal(raw.get(name))  # The native parser accepts floats; this proof must not.
            fill = trade_fill_evidence_from_raw(raw)
            _identifier(fill.trade_id)
            _check(fill.trade_id == row["id"] and fill.order_id == context.order_id
                   and fill.account_name == context.account_id and fill.connector_name == context.connector
                   and fill.pair == context.pair and fill.side == "BUY", "Foreign/mismatched paper BUY receipt")
            _check(fill.fee_currency in {None, "USD"} and (fill.fee_paid == ZERO or fill.fee_currency == "USD"),
                   "Only exactly valued USD quote fees supported")
            _check(fill.price <= context.reference_price, "Fill exceeds frozen reference price")
            reconciled = _one(native, "economic_trade_fill_reconciliation", "trade_id", fill.trade_id)
            for key, expected in (("order_id", context.order_id), ("account_name", context.account_id),
                ("connector_name", context.connector), ("pair", context.pair), ("side", "BUY"),
                ("role", role), ("position_id", context.position_id), ("result_id", "trade-fill:" + fill.trade_id),
                ("fee_currency", fill.fee_currency)):
                _check(reconciled[key] == expected, "Native fill identity mismatch: " + key)
            for key, expected in (("amount_base", fill.amount_base), ("price", fill.price), ("fee_paid", fill.fee_paid)):
                _check(_decimal(reconciled[key]) == expected, "Native fill amount mismatch: " + key)
            acquired = _one(native, "economic_lots", "lot_id", reconciled["result_id"])
            for key, expected in (("position_id", context.position_id), ("pair", context.pair),
                ("ownership", "ACTIVE_TRADE"), ("source", role), ("proposal_id", context.proposal_id),
                ("executor_id", context.executor_id)):
                _check(acquired[key] == expected, "Original acquired lot ownership mismatch: " + key)
            for key, expected in (("base_original", fill.amount_base), ("cost_basis_original_quote", fill.quote_amount),
                                  ("entry_fees_original_quote", fill.fee_paid)):
                _check(_decimal(acquired[key]) == expected, "Original acquired lot cost/fees mismatch: " + key)
            occurred = _time(fill.occurred_at)
            reconciled_at = _time(reconciled["reconciled_at"])
            _check(context.reserved_at <= occurred == _time(reconciled["occurred_at"]) == _time(acquired["created_at"])
                   and occurred <= reconciled_at <= _time(state["now"]), "Native receipt chronology mismatch")
            _check(_time(position["created_at"]) <= occurred
                   and _time(order["created_at"]) <= _time(binding["created_at"]) <= occurred
                   and _time(reservation["created_at"]) <= _time(provenance["created_at"])
                   <= _time(binding["created_at"]) <= occurred
                   and _time(provenance["created_at"]) == _time(execution["created_at"]),
                   "Native execution/binding chronology mismatch")
            lot = {key: acquired[key] for key in ("lot_id", "position_id", "pair", "ownership", "source",
                "base_original", "cost_basis_original_quote", "entry_fees_original_quote", "proposal_id", "executor_id", "created_at")}
            amount = fill.quote_amount + fill.fee_paid
            quantity += fill.amount_base
            gross += fill.quote_amount
            debit += amount
            latest = max(latest or 0, reconciled_at)
            receipts.append(canonical_json({"settlement_id": fill.trade_id, "quantity": fill.amount_base,
                "gross_quote": fill.quote_amount, "fee_quote": fill.fee_paid, "fee_inclusive_debit": amount,
                "occurred_at": occurred, "reconciled_at": reconciled_at, "receipt_sequence": row["receipt_sequence"],
                "context": context.as_dict(), "paper_receipt": row, "order": immutable_order,
                "native_binding": binding, "native_provenance": provenance, "native_execution": execution,
                "native_reservation": immutable_reservation, "native_reconciliation": reconciled, "original_lot": lot}))
    _check(quantity <= context.authorized_quantity, "Entry quantity overfill")
    _check(debit <= context.full_budget and gross <= _decimal(order["principal"]), "Entry fee-inclusive overbudget")
    payload = canonical_json({"schema": 1, "context": context.as_dict(),
        "settlement_ids": [row["id"] for row, _ in rows], "receipts": [_decode(item) for item in receipts],
        "total_quantity": quantity, "total_fee_inclusive_debit": debit, "latest_reconciled_at": latest})
    batch = VerifiedEntryReceipts(tuple(row["id"] for row, _ in rows), tuple(receipts), quantity, debit,
                                  latest, payload, fingerprint(payload))
    if order["status"] in TERMINAL:
        terminal, _ = _terminal_audit(paper, context, state, order, batch)
        _authority_chronology(order, reservation, binding, provenance, execution, position, _time(terminal["at"]))
    return batch, state, order


def verify_entry_receipts(paper_connection, native_db, context, now):
    """Verify every applied exact-order BUY; return an immutable empty batch if none."""
    _check(isinstance(context, EntrySettlementContext), "Immutable entry context required")
    clock = _time(now)
    try:
        with localcontext(Context(prec=512)) as ctx, _native(native_db) as native:
            ctx.prec = 512
            ctx.traps[Inexact] = True
            return _verify(paper_connection, native, context, clock)[0]
    except (sqlite3.Error, KeyError, TypeError, AttributeError, OverflowError, InvalidOperation, Inexact) as exc:
        raise ProfitBridgeBlocked("Incomplete or invalid entry settlement evidence: " + str(exc)) from exc
    except ValueError as exc:
        if isinstance(exc, ProfitBridgeBlocked):
            raise
        raise ProfitBridgeBlocked("Invalid native BUY evidence: " + str(exc)) from exc


def _terminal_audit(paper, context, state, order, batch):
    terminal = []
    for row in _rows(paper, "SELECT * FROM audit_events WHERE kind='terminal_reconciliation' ORDER BY seq"):
        raw = _decode(row["payload"])
        if raw.get("order") == context.order_id:
            terminal.append(row)
    _check(len(terminal) == 1 and _decode(terminal[0]["payload"]).get("status") == order["terminal"],
           "Missing or conflicting terminal paper audit")
    terminal_at = _time(terminal[0]["at"])
    _check(context.reserved_at <= terminal_at <= _time(state["now"]), "Terminal paper audit chronology mismatch")
    _check(order["terminal"] != "FILLED" or batch.total_quantity == context.authorized_quantity,
           "FILLED terminal quantity mismatch")
    _check(order["terminal"] != "REJECTED" or
           (not batch.settlement_ids and not batch.receipts and batch.total_quantity == ZERO
            and batch.total_fee_inclusive_debit == ZERO), "REJECTED terminal has actual fills or cash debit")
    facts = {_decode(item)["settlement_id"]: _decode(item) for item in batch.receipts}
    _check(all(item["occurred_at"] <= terminal_at and item["reconciled_at"] <= terminal_at
               for item in facts.values()), "Late receipt after terminal paper audit")
    # Applied flags alone cannot establish insertion chronology across tables.
    # Real adapter application audits are written before applied=1 and closure.
    seen = set()
    audits = []
    for row in _rows(paper, "SELECT * FROM audit_events WHERE kind='receipt_reconciled' ORDER BY seq"):
        raw = _decode(row["payload"])
        trade = raw.get("trade_id")
        if trade in facts:
            _check(row["seq"] < terminal[0]["seq"]
                   and facts[trade]["reconciled_at"] <= _time(row["at"]) <= terminal_at,
                   "Late or pre-reconciliation receipt audit after terminal closure")
            seen.add(trade)
            audits.append(row)
    _check(seen == set(facts), "Missing applied receipt audit before terminal closure")
    return terminal[0], tuple(audits)


def _health_coverage(paper, native, context, state, health):
    bindings = _rows(native, "SELECT * FROM economic_fill_bindings WHERE position_id=?", (context.position_id,))
    bound_order_ids = {row["order_id"] for row in bindings}
    orders = [order for order in state["orders"].values() if order["position"] == context.position_id]
    # Pre-admission rejected candidates have no native authority. An actually
    # bound, later rejected zero-fill executor remains in exact health coverage.
    proposals = {order["proposal"] for order in orders
                 if order["status"] != "REJECTED" or order["id"] in bound_order_ids}
    orders = [order for order in orders if order["proposal"] in proposals]
    order_ids = {order["id"] for order in orders}
    _check(context.order_id in order_ids and len(order_ids) == len(orders), "Terminal paper order coverage mismatch")
    _check({row["order_id"] for row in bindings} == order_ids, "Terminal native binding coverage mismatch")
    for row in bindings:
        order = state["orders"].get(row["order_id"])
        _check(order is not None and row["proposal_id"] == order["proposal"]
               and row["executor_id"] == order["executor"] and row["pair"] == order["pair"]
               and row["account_name"] == context.account_id and row["connector_name"] == context.connector,
               "Terminal native binding owner mismatch")
    paper_ids = set()
    for row in _rows(paper, "SELECT * FROM receipts"):
        raw = _decode(row["payload"])
        if row["order_id"] in order_ids or raw.get("order_id") in order_ids:
            _check(row["applied"] == 1 and row["order_id"] == raw.get("order_id")
                   and row["id"] == raw.get("trade_id"), "Terminal has unapplied/unbound receipt")
            paper_ids.add(row["id"])
    native_rows = _rows(native, "SELECT * FROM economic_trade_fill_reconciliation WHERE position_id=?",
                        (context.position_id,))
    _check({row["trade_id"] for row in native_rows} == paper_ids
           and all(row["order_id"] in order_ids for row in native_rows), "Terminal native receipt coverage mismatch")
    _check(health["proposal_count"] == health["fully_reconciled_proposals"] == len(proposals) > 0
           and health["known_orders"] == health["bound_orders"] == len(order_ids)
           and health["confirmed_fills"] == health["ledgered_fills"] == len(paper_ids),
           "Native health counts disagree with current exact receipts/orders")
    _check(all(_time(row["reconciled_at"]) <= _time(health["last_success_at"]) for row in native_rows),
           "Native health predates latest reconciled fill")


def verify_terminal_closure(paper_connection, native_db, context, now, minimum_verified_after=None):
    """Derive terminal closure from a real paper audit plus a fresh native watermark.

    ``minimum_verified_after`` is strict: wall-clock passage alone cannot authorize
    release after a partial commit. A genuinely later native observation is needed.
    """
    _check(isinstance(context, EntrySettlementContext), "Immutable entry context required")
    clock = _time(now)
    minimum = None if minimum_verified_after is None else _time(minimum_verified_after)
    try:
        with localcontext(Context(prec=512)) as ctx, _native(native_db) as native:
            ctx.prec = 512
            ctx.traps[Inexact] = True
            batch, state, order = _verify(paper_connection, native, context, clock)
            _check(order["status"] == order["terminal"] and order["terminal"] in TERMINAL,
                   "Entry has remaining order liability")
            terminal, receipt_audits = _terminal_audit(paper_connection, context, state, order, batch)
            terminal_at = _time(terminal["at"])
            health = _one(native, "economic_accounting_health", "position_id", context.position_id)
            _check(health["recorded_state"] == "HEALTHY"
                   and _decode(health["unresolved_order_ids_json"]) == []
                   and _decode(health["unresolved_trade_ids_json"]) == [], "Native accounting hold/conflict")
            _check(health["last_success_at"] is not None, "Missing native health success watermark")
            verified_at = _time(health["last_success_at"])
            _check(terminal_at <= verified_at == _time(health["last_attempt_at"]) <= _time(state["now"]) <= clock
                   and clock - verified_at <= AccountingHealthPolicy().max_age_seconds,
                   "Native health evidence stale/future or predates terminal")
            _check(minimum is None or verified_at > minimum,
                   "Release requires genuinely later native health observation")
            _health_coverage(paper_connection, native, context, state, health)
            authority = {
                "binding": _one(native, "economic_fill_bindings", "order_id", context.order_id),
                "provenance": _one(native, "execution_provenance", "proposal_id", context.proposal_id),
                "execution": _one(native, "execution_receipts", "proposal_id", context.proposal_id),
                "reservation": _one(native, "capital_reservations", "proposal_id", context.proposal_id),
            }
            authority["reservation"] = {key: authority["reservation"][key] for key in
                ("proposal_id", "pair", "intent", "amount_quote", "leverage", "created_at")}
            payload = canonical_json({"schema": 1, "context": context.as_dict(), "batch": batch.as_dict(),
                "terminal_status": order["terminal"], "terminal_at": terminal_at, "verified_at": verified_at,
                "terminal_audit": terminal, "receipt_audits": receipt_audits, "native_health": health,
                "native_authority": authority})
            return VerifiedTerminalClosure(batch, order["terminal"], terminal_at, verified_at,
                                           payload, fingerprint(payload))
    except (sqlite3.Error, KeyError, TypeError, AttributeError, OverflowError, InvalidOperation, Inexact) as exc:
        raise ProfitBridgeBlocked("Incomplete or invalid terminal closure evidence: " + str(exc)) from exc
    except ValueError as exc:
        if isinstance(exc, ProfitBridgeBlocked):
            raise
        raise ProfitBridgeBlocked("Invalid terminal closure evidence: " + str(exc)) from exc
