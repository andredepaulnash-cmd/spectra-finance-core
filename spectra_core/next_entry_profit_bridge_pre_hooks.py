"""Default-off paper settled-profit bridge; no order or live cash API.

Cash remains in the paper receipt database. The outbox and balanced reclassification
journal share that database's transaction. Native ledger data are read-only inputs.
The returned integration callbacks MUST replace the original adapter cash projection
and gate every new spend; merely constructing this bridge does not install them.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Callable

from .accounting_health import AccountingHealthPolicy
from .fill_reconciliation import trade_fill_evidence_from_raw
from .next_entry_allocator import Allocator, Member, MembershipSnapshot, SettledProfitEvent


class ProfitBridgeBlocked(ValueError):
    """Evidence or backing is unresolved; no new spending is permitted."""


ZERO = Decimal(0)
ESCROW_POOL = "next_entry_profit_escrow"
PENDING = {"PREPARING", "OPEN", "PARTIAL", "CANCEL_REQUESTED"}


def _json(value):
    def default(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _check(condition, reason):
    if not condition:
        raise ProfitBridgeBlocked(reason)


def _decimal(value):
    _check(not isinstance(value, (bool, float)), "Inexact monetary evidence")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProfitBridgeBlocked("Invalid monetary evidence") from exc
    _check(result.is_finite() and len(result.as_tuple().digits) <= 80
           and abs(result.as_tuple().exponent) <= 80, "Unbounded monetary evidence")
    return result


def _time(value):
    _check(not isinstance(value, bool) and isinstance(value, (int, float))
           and isfinite(value) and value >= 0 and value == int(value), "Whole UTC seconds required")
    return int(value)


def _identifier(value):
    _check(isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip(),
           "Invalid evidence identifier")
    return value


@dataclass(frozen=True)
class CashProjection:
    """Full cash includes escrow; base budgets must use available_base_pools only."""
    cash_pools: dict[str, Decimal]
    available_base_pools: dict[str, Decimal]
    profit_escrow: Decimal
    total_physical_cash: Decimal
    protected_cash: Decimal
    pending_trade_ids: tuple[str, ...]
    new_spending_allowed: bool


@dataclass(frozen=True)
class IntegrationCallbacks:
    cash_projection: Callable[[], CashProjection]
    before_new_spending: Callable[[], CashProjection]


class NativeSettledProfitBridge:
    """Verify a complete native disposal and stage one immutable paper receipt.

    The caller must serialize this bridge with the paper adapter's account writer
    lock. The bridge never writes the adapter state or native tables. Its SQLite
    transactions serialize receipt-domain changes; there is no cross-store atomic
    commit with the allocator. Replaying the frozen outbox closes that gap.
    """

    def __init__(self, *, paper_db: str | Path, native_db: str | Path,
                 allocator: Allocator, account_id: str, connector: str,
                 owner_id: str, enabled: bool = False,
                 fault_hook: Callable[[str], None] | None = None):
        self.paper_db = Path(paper_db).resolve()
        self.native_db = Path(native_db).resolve()
        self.allocator = allocator
        self.account_id = _identifier(account_id)
        self.connector = _identifier(connector)
        self.owner_id = _identifier(owner_id)
        self.enabled = enabled is True
        self.fault_hook = fault_hook
        self._identity = None
        if not self.enabled:
            return
        _check(self.paper_db.is_file() and self.native_db.is_file(), "Existing authority databases required")
        _check(self.paper_db != self.native_db and self.paper_db != allocator.path.resolve()
               and self.native_db != allocator.path.resolve(), "Authority databases must be separate")
        _check(allocator.account_id == account_id and allocator.currency == "USD", "Allocator scope mismatch")
        with self._paper() as db:
            state = self._state(db)
            self._check_identity(state)
            self._identity = _json({"schema": 1, "paper_identity": state["identity"],
                                   "native_path": str(self.native_db), "owner_id": owner_id,
                                   "account_id": account_id, "connector": connector,
                                   "allocator_path": str(allocator.path.resolve()),
                                   "allocator_policy": asdict(allocator.policy)})
            db.executescript("""
                CREATE TABLE IF NOT EXISTS next_profit_meta (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS next_profit_snapshots (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS next_profit_outbox (
                    trade_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                    source_payload TEXT NOT NULL, source_digest TEXT NOT NULL,
                    snapshot_payload TEXT NOT NULL, event_payload TEXT,
                    payload_digest TEXT NOT NULL, status TEXT NOT NULL,
                    staged_at INTEGER NOT NULL, acknowledgement TEXT);
                CREATE TABLE IF NOT EXISTS next_profit_cash (
                    trade_id TEXT PRIMARY KEY, source_pool TEXT NOT NULL,
                    source_debit TEXT NOT NULL, escrow_credit TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS next_profit_discrepancies (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT,
                    at INTEGER NOT NULL, reason TEXT NOT NULL, observed TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT identity FROM next_profit_meta WHERE id=1").fetchone()
            if previous is None:
                db.execute("INSERT INTO next_profit_meta VALUES (1,?)", (self._identity,))
            else:
                _check(previous[0] == self._identity, "Bridge identity changed on restart")
            db.commit()

    @contextmanager
    def _paper(self):
        db = sqlite3.connect(self.paper_db, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def _native(self):
        db = sqlite3.connect(self.native_db.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def _require_enabled(self):
        _check(self.enabled, "Settled-profit bridge is disabled")

    def _fault(self, point):
        if self.fault_hook:
            self.fault_hook(point)

    @staticmethod
    def _state(db):
        row = db.execute("SELECT value FROM state WHERE id=1").fetchone()
        _check(row is not None, "Missing paper state")
        return json.loads(row[0])

    def _check_identity(self, state):
        config = state["identity"]["config"]
        _check(config["account"] == self.account_id and config["connector"] == self.connector
               and config["owner"] == self.owner_id, "Foreign paper authority")
        _check(_decimal(config["initial_cash"]) == 2000 and _decimal(config["protected_cash"]) == 500,
               "Unsupported paper cash genesis")
        _check(state["identity"]["schema"] == 1, "Unsupported paper schema")
        if self._identity is not None:
            _check(state["identity"] == json.loads(self._identity)["paper_identity"], "Paper identity changed")

    @staticmethod
    def _one(db, table, key, value):
        # Table/column names are hard-coded by this module, never supplied externally.
        row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        _check(row is not None, "Missing " + table + " evidence")
        return dict(row)

    def _clean(self, db, state):
        self._check_identity(state)
        _check(not state.get("quarantine") and state.get("breaker") == "NORMAL", "Paper accounting quarantine")
        _check(db.execute("SELECT 1 FROM receipts WHERE applied != 1 LIMIT 1").fetchone() is None,
               "Unapplied paper receipt")
        _check(db.execute("SELECT 1 FROM next_profit_discrepancies LIMIT 1").fetchone() is None,
               "Unresolved bridge discrepancy")

    def _record_discrepancy(self, trade_id, now, reason):
        # Do not rewrite original source/outbox bytes, even when verification fails.
        with self._paper() as db:
            row = db.execute("SELECT payload FROM receipts WHERE id=?", (trade_id,)).fetchone()
            observed = _json({"paper_receipt": None if row is None else row[0]})
            db.execute("INSERT INTO next_profit_discrepancies(trade_id,at,reason,observed) VALUES (?,?,?,?)",
                       (trade_id, now, str(reason), observed))

    def _snapshot_payload(self, snapshot, settled_at):
        _check(isinstance(snapshot, MembershipSnapshot), "Explicit membership snapshot required")
        _identifier(snapshot.snapshot_id)
        _check(snapshot.account_id == self.account_id and snapshot.currency == "USD", "Foreign membership")
        _check(_time(snapshot.effective_from) <= settled_at <= _time(snapshot.effective_until),
               "Settlement outside membership interval")
        _check(isinstance(snapshot.members, tuple) and all(isinstance(item, Member) for item in snapshot.members),
               "Frozen explicit members required")
        members = sorted(snapshot.members, key=lambda item: item.allocation_id)
        _check(len(members) >= 2 and len({item.allocation_id for item in members}) == len(members),
               "Invalid membership identifiers")
        for member in members:
            _identifier(member.allocation_id)
            _identifier(member.owner_id)
            _check(member.role in {"POSITION_ALLOCATION", "VCAP"}, "Unknown member role")
        _check(sum(item.role == "VCAP" for item in members) == 1
               and any(item.role == "POSITION_ALLOCATION" for item in members), "Explicit VCAP and position members required")
        result = asdict(snapshot)
        result["members"] = [asdict(member) for member in members]
        result["effective_from"] = _time(snapshot.effective_from)
        result["effective_until"] = _time(snapshot.effective_until)
        return _json(result)

    def _verify_source(self, db, trade_id, now):
        """Return only immutable source fields, including every disposal allocation."""
        state = self._state(db)
        self._clean(db, state)
        _check(_time(state["now"]) <= now, "Future paper state")
        receipt = self._one(db, "receipts", "id", trade_id)
        raw = json.loads(receipt["payload"])
        fill = trade_fill_evidence_from_raw(raw)
        _check(receipt["id"] == fill.trade_id == trade_id and receipt["order_id"] == fill.order_id,
               "Paper receipt identity mismatch")
        _check(fill.side == "SELL", "Only executed sales can produce profit")
        _check(fill.account_name == self.account_id and fill.connector_name == self.connector,
               "Foreign receipt scope")
        _check(fill.pair.split("-")[1] == "USD" and fill.fee_currency in {None, "USD"},
               "Only exactly valued quote-currency fees are supported")
        order = state["orders"][fill.order_id]
        _check(order["id"] == fill.order_id and order["side"] == "SELL"
               and order["pair"] == fill.pair, "Paper order binding mismatch")
        _check(order["status"] == order["terminal"] and order["terminal"] in {"FILLED", "CANCELED"},
               "Sale order has no terminal cash settlement")
        _check(order["pool"] not in {"protected", ESCROW_POOL}, "Protected/escrow cash cannot be a source")
        terminal = []
        for item in db.execute("SELECT * FROM audit_events WHERE kind='terminal_reconciliation' ORDER BY seq"):
            payload = json.loads(item["payload"])
            if payload.get("order") == fill.order_id:
                terminal.append(dict(item))
        _check(len(terminal) == 1 and json.loads(terminal[0]["payload"])["status"] == order["terminal"],
               "Missing or conflicting terminal cash receipt")
        settled_at = _time(terminal[0]["at"])
        occurred_at = _time(fill.occurred_at)
        _check(_time(order["decision_at"]) <= occurred_at <= settled_at <= _time(state["now"]) <= now,
               "Future or predecision sale evidence")
        order_fills = []
        for row in db.execute("SELECT * FROM receipts WHERE order_id=? ORDER BY id", (fill.order_id,)):
            evidence = trade_fill_evidence_from_raw(json.loads(row["payload"]))
            _check(evidence.order_id == fill.order_id and evidence.side == "SELL"
                   and evidence.account_name == self.account_id and evidence.connector_name == self.connector
                   and evidence.pair == fill.pair and evidence.fee_currency in {None, "USD"}
                   and evidence.trade_id == row["id"] and row["applied"] == 1
                   and _time(order["decision_at"]) <= _time(evidence.occurred_at) <= settled_at,
                   "Conflicting order receipt")
            order_fills.append(evidence)
        quantity = sum((item.amount_base for item in order_fills), ZERO)
        _check(quantity <= _decimal(order["quantity"]) and
               (order["terminal"] != "FILLED" or quantity == _decimal(order["quantity"])),
               "Terminal filled quantity mismatch")
        with self._native() as native:
            binding = self._one(native, "economic_fill_bindings", "order_id", fill.order_id)
            reconciled = self._one(native, "economic_trade_fill_reconciliation", "trade_id", trade_id)
            event_id = "trade-fill:" + trade_id
            event = self._one(native, "economic_position_events", "event_id", event_id)
            provenance = self._one(native, "execution_provenance", "proposal_id", binding["proposal_id"])
            execution = self._one(native, "execution_receipts", "proposal_id", binding["proposal_id"])
            position = self._one(native, "economic_positions", "position_id", binding["position_id"])
            _check(binding["role"] == reconciled["role"] == "POSITION_EXIT", "Not an owned position exit")
            for name, expected in (("order_id", fill.order_id), ("account_name", self.account_id),
                                   ("connector_name", self.connector), ("pair", fill.pair),
                                   ("position_id", order["position"])):
                _check(binding[name] == reconciled[name] == expected, "Native binding mismatch: " + name)
            _check(binding["proposal_id"] == order["proposal"] == provenance["proposal_id"]
                   and binding["executor_id"] == order["executor"] == provenance["executor_id"] == execution["executor_id"],
                   "Native executor ownership mismatch")
            _check(provenance["account_name"] == self.account_id and provenance["connector_name"] == self.connector
                   and provenance["controller_id"] == self.owner_id and provenance["pair"] == fill.pair
                   and provenance["intent"] in {"ENTRY", "RECOVERY"}, "Foreign governor provenance")
            _check(position["pair"] == fill.pair and reconciled["result_id"] == event_id
                   and reconciled["side"] == "SELL", "Native realization identity mismatch")
            for name, expected in (("amount_base", fill.amount_base), ("price", fill.price), ("fee_paid", fill.fee_paid)):
                _check(_decimal(reconciled[name]) == expected, "Native fill mismatch: " + name)
            _check(reconciled["fee_currency"] == fill.fee_currency, "Native fee currency mismatch")
            _check(_time(reconciled["occurred_at"]) == occurred_at == _time(event["occurred_at"])
                   and occurred_at <= _time(reconciled["reconciled_at"]) <= settled_at
                   and _time(binding["created_at"]) <= occurred_at
                   and _time(provenance["created_at"]) <= occurred_at
                   and _time(execution["created_at"]) <= occurred_at, "Native evidence chronology mismatch")
            _check(event["position_id"] == order["position"] and event["event_type"] in {"PARTIAL_EXIT", "FINAL_EXIT"}
                   and _decimal(event["base_amount"]) == fill.amount_base
                   and _decimal(event["quote_amount"]) == fill.quote_amount
                   and _decimal(event["fee_quote"]) == fill.fee_paid, "Native event/receipt mismatch")
            allocations = [dict(row) for row in native.execute(
                "SELECT * FROM economic_exit_allocations WHERE event_id=? ORDER BY lot_id", (event_id,))]
            _check(bool(allocations), "Missing exact disposal allocations")
            lots = []
            acquisitions = []
            disposal_histories = []
            allowed = order["eligible_lot_ids"]
            _check(len(set(allowed)) == len(allowed), "Duplicate allowed lot identity")
            for allocation in allocations:
                lot = self._one(native, "economic_lots", "lot_id", allocation["lot_id"])
                _check(allocation["lot_id"] in allowed, "Disposal exceeded immutable sell scope")
                for name, expected in (("position_id", order["position"]), ("proposal_id", binding["proposal_id"]),
                                       ("executor_id", binding["executor_id"]), ("ownership", "ACTIVE_TRADE")):
                    _check(allocation[name] == lot[name] == expected, "Foreign disposal owner: " + name)
                _check(lot["pair"] == fill.pair and lot["source"] in {"BASE_ENTRY", "VCAP_RECOVERY"}, "Foreign disposed lot")
                _check(lot["lot_id"].startswith("trade-fill:"), "Unsupported inherited acquisition")
                acquisition_id = lot["lot_id"][len("trade-fill:"):]
                acquired_receipt = self._one(db, "receipts", "id", acquisition_id)
                acquired = trade_fill_evidence_from_raw(json.loads(acquired_receipt["payload"]))
                acquired_order = state["orders"][acquired.order_id]
                acquired_native = self._one(native, "economic_trade_fill_reconciliation", "trade_id", acquisition_id)
                acquired_binding = self._one(native, "economic_fill_bindings", "order_id", acquired.order_id)
                _check(acquired_receipt["applied"] == 1 and acquired.trade_id == acquisition_id
                       and acquired_receipt["order_id"] == acquired.order_id and acquired.side == "BUY"
                       and acquired.account_name == self.account_id and acquired.connector_name == self.connector
                       and acquired.pair == fill.pair and acquired.fee_currency in {None, "USD"},
                       "Original acquisition has no exact applied paper debit")
                _check(acquired_native["result_id"] == lot["lot_id"] and acquired_native["side"] == "BUY"
                       and acquired_native["order_id"] == acquired.order_id
                       and acquired_native["fee_currency"] == acquired.fee_currency,
                       "Original acquisition native receipt mismatch")
                for name, expected in (("amount_base", acquired.amount_base), ("price", acquired.price),
                                       ("fee_paid", acquired.fee_paid)):
                    _check(_decimal(acquired_native[name]) == expected, "Original acquisition fill mismatch")
                for name, expected in (("account_name", self.account_id), ("connector_name", self.connector),
                                       ("pair", fill.pair), ("position_id", order["position"]),
                                       ("role", "BASE_ENTRY" if lot["source"] == "BASE_ENTRY" else "VCAP_RECOVERY")):
                    _check(acquired_binding[name] == acquired_native[name] == expected,
                           "Original acquisition ownership mismatch")
                _check(acquired_binding["proposal_id"] == lot["proposal_id"] == acquired_order["proposal"]
                       and acquired_binding["executor_id"] == lot["executor_id"] == acquired_order["executor"]
                       and acquired_order["pool"] == order["pool"]
                       and acquired_order["position"] == order["position"] and acquired_order["pair"] == fill.pair
                       and acquired_order["side"] == "BUY" and acquired_order["id"] == acquired.order_id,
                       "Original acquisition source pool/owner was reassigned")
                _check(_decimal(lot["base_original"]) == acquired.amount_base
                       and _decimal(lot["cost_basis_original_quote"]) == acquired.quote_amount
                       and _decimal(lot["entry_fees_original_quote"]) == acquired.fee_paid,
                       "Original lot differs from confirmed acquisition cost/fees")
                acquired_at = _time(acquired.occurred_at)
                _check(_time(acquired_order["decision_at"]) <= acquired_at == _time(lot["created_at"])
                       == _time(acquired_native["occurred_at"])
                       and acquired_at <= _time(acquired_native["reconciled_at"]) <= occurred_at
                       and _time(acquired_binding["created_at"]) <= acquired_at,
                       "Original acquisition chronology mismatch")
                acquisitions.append({"receipt": acquired_receipt, "binding": acquired_binding,
                    "reconciliation": acquired_native,
                    "order": {key: acquired_order[key] for key in ("id", "side", "pair", "position", "proposal",
                               "executor", "pool", "quantity", "decision_at")}})
                _check(_decimal(allocation["base_sold"]) > 0
                       and _decimal(allocation["allocated_cost_basis_quote"]) >= 0
                       and _decimal(allocation["allocated_entry_fees_quote"]) >= 0, "Invalid disposal amounts")
                # Native allocations, including older exits, plus the current remainder
                # must conserve the original lot. Do not accept a rewritten cost basis.
                all_allocations = list(native.execute(
                    "SELECT a.*, e.rowid AS event_sequence FROM economic_exit_allocations a "
                    "LEFT JOIN economic_position_events e ON e.event_id=a.event_id "
                    "WHERE a.lot_id=? ORDER BY e.rowid", (lot["lot_id"],)))
                replay_base = _decimal(lot["base_original"])
                replay_basis = _decimal(lot["cost_basis_original_quote"])
                replay_fees = _decimal(lot["entry_fees_original_quote"])
                frozen_history = []
                reached_current = False
                for historical in all_allocations:
                    for name in ("position_id", "proposal_id", "executor_id", "ownership"):
                        _check(historical[name] == lot[name], "Historical disposal owner mismatch")
                    _check(_decimal(historical["base_sold"]) > 0
                           and _decimal(historical["allocated_cost_basis_quote"]) >= 0
                           and _decimal(historical["allocated_entry_fees_quote"]) >= 0,
                           "Invalid historical disposal allocation")
                    historical_event = self._one(native, "economic_position_events", "event_id", historical["event_id"])
                    _check(historical["event_sequence"] is not None, "Orphan historical allocation")
                    sold_piece = _decimal(historical["base_sold"])
                    _check(0 < sold_piece <= replay_base, "Historical disposal overdraws original lot")
                    # Reproduce the pinned native ledger's Decimal(28) proportional
                    # allocation, in durable event insertion order. Conservation
                    # alone permits moving principal into the unsold remainder.
                    with localcontext() as native_context:
                        native_context.prec = 28
                        ratio = sold_piece / replay_base
                        basis_piece = replay_basis * ratio
                        fee_piece = replay_fees * ratio
                        _check(_decimal(historical["allocated_cost_basis_quote"]) == basis_piece
                               and _decimal(historical["allocated_entry_fees_quote"]) == fee_piece,
                               "Native proportional disposal cost/fee replay mismatch")
                        replay_base -= sold_piece
                        replay_basis -= basis_piece
                        replay_fees -= fee_piece
                    if not reached_current:
                        frozen_history.append({"allocation": dict(historical), "event": historical_event})
                    if historical["event_id"] == event_id:
                        reached_current = True
                    _check(historical_event["position_id"] == lot["position_id"]
                           and historical_event["event_type"] in {"PARTIAL_EXIT", "FINAL_EXIT"}
                           and acquired_at <= _time(historical_event["occurred_at"]) <= _time(state["now"]),
                           "Historical disposal event mismatch")
                _check(reached_current and replay_base == _decimal(lot["base_remaining"])
                       and replay_basis == _decimal(lot["cost_basis_remaining_quote"])
                       and replay_fees == _decimal(lot["entry_fees_remaining_quote"]),
                       "Native remaining lot disagrees with allocation replay")
                disposal_histories.append({"lot_id": lot["lot_id"], "history": frozen_history})
                for allocated, remaining, original in (
                    ("base_sold", "base_remaining", "base_original"),
                    ("allocated_cost_basis_quote", "cost_basis_remaining_quote", "cost_basis_original_quote"),
                    ("allocated_entry_fees_quote", "entry_fees_remaining_quote", "entry_fees_original_quote")):
                    used = sum((_decimal(item[allocated]) for item in all_allocations), ZERO)
                    left = _decimal(lot[remaining])
                    _check(left >= 0 and used + left == _decimal(lot[original]),
                           "Native original lot conservation failed")
                # Remaining quantities are mutable; original ownership and basis are not.
                lots.append({key: lot[key] for key in ("lot_id", "position_id", "pair", "ownership", "source",
                    "base_original", "cost_basis_original_quote", "entry_fees_original_quote", "proposal_id", "executor_id", "created_at")})
            for allocation_field, event_field in (("base_sold", "base_amount"),
                    ("allocated_cost_basis_quote", "allocated_cost_basis_quote"),
                    ("allocated_entry_fees_quote", "allocated_entry_fees_quote")):
                total = sum((_decimal(item[allocation_field]) for item in allocations), ZERO)
                _check(total == _decimal(event[event_field]), "Incomplete native disposal aggregation")
            principal = _decimal(event["allocated_cost_basis_quote"])
            fees = _decimal(event["allocated_entry_fees_quote"]) + fill.fee_paid
            profit = fill.quote_amount - principal - fees
            _check(profit == _decimal(event["realized_pnl_quote"]), "Native aggregate realized P&L mismatch")
        frozen_order = {key: order[key] for key in ("id", "side", "pair", "position", "proposal", "executor",
                       "pool", "quantity", "decision_at", "status", "terminal", "eligible_lot_ids")}
        source = {"receipt": receipt, "order": frozen_order, "terminal": terminal[0],
                  "binding": binding, "reconciliation": reconciled, "event": event,
                  "provenance": provenance, "execution": execution, "allocations": allocations, "lots": lots,
                  "acquisitions": acquisitions, "disposal_histories": disposal_histories,
                  "order_receipts": [dict(row) for row in db.execute(
                      "SELECT * FROM receipts WHERE order_id=? ORDER BY id", (fill.order_id,))]}
        return _json(source), settled_at, fill.quote_amount, principal, fees, profit, order["pool"]

    def _allocator_backing(self, outbox):
        """No allocator event may exist without this bridge's exact cash backing."""
        state = self.allocator.export_research_state()  # Includes allocator invariant checks.
        expected = {}
        acknowledged = set()
        money = ("gross_realized_proceeds", "returned_principal", "total_attributable_fees",
                 "fee_net_realized_profit", "earmarked_profit_cash")
        for row in outbox:
            if row["event_payload"] is None:
                continue
            event = json.loads(row["event_payload"])
            for name in money:
                atoms = _decimal(event[name]) / self.allocator.policy.accounting_quantum
                _check(atoms == atoms.to_integral_value(), "Invalid frozen accounting precision")
                event[name] = int(atoms)
            event["source_realization_ids"] = sorted(event["source_realization_ids"])
            expected[event["event_id"]] = (event, json.loads(row["snapshot_payload"]), row)
            if row["status"] == "ACKNOWLEDGED":
                acknowledged.add(event["event_id"])
        actual = set(state["events"])
        _check(acknowledged <= actual <= set(expected), "Allocator event/cash backing coverage mismatch")
        for event_id, record in state["events"].items():
            event, snapshot, row = expected[event_id]
            _check(record["event"] == event and record["snapshot"] == snapshot,
                   "Allocator grant differs from frozen cash-backed evidence")
            if row["status"] == "ACKNOWLEDGED":
                ack = json.loads(row["acknowledgement"])
                _check(ack.get("operation_id") == row["operation_id"]
                       and {key: value for key, value in ack.items() if key not in {"operation_id", "sequence"}}
                       == record["receipt"], "Allocator acknowledgement mismatch")
        # This bridge does not implement the native debit/reservation half of an entry.
        _check(not state["reservations"] and not state["commits"],
               "Profit entry spending requires a separately reviewed native debit bridge")

    def _cash_native_coverage(self, db, state):
        # Every applied receipt and scoped native fill must describe the same cash
        # history. A missing historical BUY must never manufacture spendable cash.
        paper = {row["id"]: dict(row) for row in db.execute("SELECT * FROM receipts")}
        with self._native() as native:
            if native.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='economic_accounting_health'").fetchone():
                for health in native.execute("SELECT * FROM economic_accounting_health"):
                    _check(health["recorded_state"] == "HEALTHY"
                           and not json.loads(health["unresolved_order_ids_json"])
                           and not json.loads(health["unresolved_trade_ids_json"])
                           and health["proposal_count"] > 0
                           and health["proposal_count"] == health["fully_reconciled_proposals"],
                           "Native accounting conflict or hold")
                    _check(health["last_success_at"] is not None
                           and _time(health["last_success_at"]) <= _time(health["last_attempt_at"]) <= _time(state["now"])
                           and _time(state["now"]) - _time(health["last_success_at"]) <= AccountingHealthPolicy().max_age_seconds,
                           "Native accounting evidence stale or future")
            native_rows = {row["trade_id"]: dict(row) for row in native.execute(
                "SELECT * FROM economic_trade_fill_reconciliation WHERE account_name=? AND connector_name=?",
                (self.account_id, self.connector))}
            _check({key for key, row in paper.items() if row["applied"] == 1} == set(native_rows),
                   "Paper/native cash history coverage mismatch")
            for trade_id, reconciled in native_rows.items():
                fill = trade_fill_evidence_from_raw(json.loads(paper[trade_id]["payload"]))
                _check(fill.trade_id == trade_id and reconciled["order_id"] == fill.order_id
                       and reconciled["pair"] == fill.pair and reconciled["side"] == fill.side
                       and reconciled["fee_currency"] == fill.fee_currency
                       and _time(reconciled["occurred_at"]) == _time(fill.occurred_at),
                       "Paper/native cash history identity mismatch")
                for name, expected in (("amount_base", fill.amount_base), ("price", fill.price), ("fee_paid", fill.fee_paid)):
                    _check(_decimal(reconciled[name]) == expected, "Paper/native cash history amount mismatch")
                _check(_time(fill.occurred_at) <= _time(reconciled["reconciled_at"]) <= _time(state["now"]),
                       "Future native cash history")

            positions = {row["position_id"] for row in native_rows.values()}
            buys = {row["result_id"]: row for row in native_rows.values() if row["side"] == "BUY"}
            sells = {row["result_id"]: row for row in native_rows.values() if row["side"] == "SELL"}
            lots = {row["lot_id"]: dict(row) for row in native.execute("SELECT * FROM economic_lots")
                    if row["position_id"] in positions and row["ownership"] == "ACTIVE_TRADE"}
            events = {row["event_id"]: dict(row) for row in native.execute("SELECT * FROM economic_position_events")
                      if row["position_id"] in positions}
            _check(set(lots) == set(buys) and set(events) == set(sells),
                   "Native economic history lacks exact applied cash receipts")
            for event_id, event in events.items():
                sale = sells[event_id]
                _check(event_id == "trade-fill:" + sale["trade_id"]
                       and event["position_id"] == sale["position_id"]
                       and _decimal(event["base_amount"]) == _decimal(sale["amount_base"])
                       and _decimal(event["quote_amount"]) == _decimal(sale["amount_base"]) * _decimal(sale["price"])
                       and _decimal(event["fee_quote"]) == _decimal(sale["fee_paid"])
                       and _time(event["occurred_at"]) == _time(sale["occurred_at"]),
                       "Historical disposal differs from confirmed cash receipt")
                allocations = list(native.execute("SELECT * FROM economic_exit_allocations WHERE event_id=?", (event_id,)))
                _check(bool(allocations), "Historical disposal lacks allocations")
                for field, event_field in (("base_sold", "base_amount"),
                    ("allocated_cost_basis_quote", "allocated_cost_basis_quote"),
                    ("allocated_entry_fees_quote", "allocated_entry_fees_quote")):
                    _check(sum((_decimal(item[field]) for item in allocations), ZERO) == _decimal(event[event_field]),
                           "Historical disposal aggregate mismatch")
                _check(_decimal(event["quote_amount"]) - _decimal(event["fee_quote"])
                       - _decimal(event["allocated_cost_basis_quote"]) - _decimal(event["allocated_entry_fees_quote"])
                       == _decimal(event["realized_pnl_quote"]), "Historical realized P&L mismatch")
            basis = sum((_decimal(lot["cost_basis_remaining_quote"]) + _decimal(lot["entry_fees_remaining_quote"])
                         for lot in lots.values()), ZERO)
            realized = sum((_decimal(event["realized_pnl_quote"]) for event in events.values()), ZERO)
            return basis, realized

    def _project(self, db, state):
        basis, realized = self._cash_native_coverage(db, state)
        pools = {"protected": Decimal(500), "seed": Decimal(1000), "repair": Decimal(500)}
        holds = {}
        unsettled = {}
        by_order = {}
        for row in db.execute("SELECT * FROM receipts ORDER BY id"):
            fill = trade_fill_evidence_from_raw(json.loads(row["payload"]))
            order = state["orders"][fill.order_id]
            _check(fill.trade_id == row["id"] and fill.order_id == row["order_id"]
                   and fill.side == order["side"] and fill.pair == order["pair"]
                   and fill.account_name == self.account_id and fill.connector_name == self.connector
                   and fill.fee_currency in {None, "USD"} and fill.pair.endswith("-USD"), "Cash receipt scope mismatch")
            pool = order["pool"]
            _check(pool not in {"protected", ESCROW_POOL}, "Receipt attempted protected/escrow mutation")
            gross, fee = fill.quote_amount, fill.fee_paid
            pools[pool] = pools.get(pool, ZERO) + (gross - fee if fill.side == "SELL" else -gross - fee)
            prior = by_order.get(fill.order_id, (ZERO, ZERO))
            by_order[fill.order_id] = (prior[0] + gross, prior[1] + fee)
        physical = sum(pools.values(), ZERO)
        _check(physical + basis == Decimal(2000) + realized, "Paper cash/native basis/realized P&L do not conserve")
        for order in state["orders"].values():
            if order["status"] not in PENDING:
                continue
            gross, fee = by_order.get(order["id"], (ZERO, ZERO))
            pool = order["pool"]
            _check(pool not in {"protected", ESCROW_POOL} and order["side"] in {"BUY", "SELL"},
                   "Invalid pending order source pool or side")
            pools.setdefault(pool, ZERO)
            if order["side"] == "BUY":
                target = _decimal(order["target"])
                _check(target >= 0 and target >= gross + fee, "Pending buy debit exceeds target")
                holds[pool] = holds.get(pool, ZERO) + target - gross - fee
            else:
                unsettled[pool] = unsettled.get(pool, ZERO) + gross - fee
        escrow = ZERO
        entries = {row["trade_id"]: dict(row) for row in db.execute("SELECT * FROM next_profit_cash")}
        outbox = list(db.execute("SELECT * FROM next_profit_outbox ORDER BY trade_id"))
        self._allocator_backing(outbox)
        expected = {row["trade_id"] for row in outbox if row["event_payload"] is not None}
        _check(set(entries) == expected, "Cash journal/outbox coverage mismatch")
        for row in outbox:
            snapshot = json.loads(row["snapshot_payload"])
            stored_snapshot = self._one(db, "next_profit_snapshots", "id", snapshot["snapshot_id"])
            _check(stored_snapshot["payload"] == row["snapshot_payload"], "Frozen membership store mismatch")
            _check(_digest(row["source_payload"]) == row["source_digest"], "Frozen source digest mismatch")
            _check(_digest(row["snapshot_payload"] + "\n" + (row["event_payload"] or "null")) == row["payload_digest"],
                   "Frozen attestation digest mismatch")
            if row["event_payload"] is None:
                _check(row["status"] == "INELIGIBLE", "Invalid loss outbox state")
                continue
            _check(row["status"] in {"STAGED", "ACKNOWLEDGED"}, "Invalid outbox state")
            event = json.loads(row["event_payload"])
            entry = entries[row["trade_id"]]
            debit, credit = _decimal(entry["source_debit"]), _decimal(entry["escrow_credit"])
            source_pool = json.loads(row["source_payload"])["order"]["pool"]
            _check(entry["source_pool"] == source_pool and source_pool not in {"protected", ESCROW_POOL}
                   and debit == credit == _decimal(event["earmarked_profit_cash"]) > 0,
                   "Unbalanced or foreign cash earmark")
            pools[source_pool] = pools.get(source_pool, ZERO) - debit
            escrow += credit
        available = {key: value - holds.get(key, ZERO) - unsettled.get(key, ZERO)
                     for key, value in pools.items() if key != "protected"}
        _check(all(value >= 0 for value in available.values()), "Cash backing deficit or competing hold")
        pools[ESCROW_POOL] = escrow
        _check(pools["protected"] == 500 and sum(pools.values(), ZERO) == physical
               and physical - sum(holds.values(), ZERO) - sum(unsettled.values(), ZERO) >= 500,
               "Cash conservation or protected reserve failure")
        known = {row["trade_id"] for row in outbox}
        unstaged = {row["id"] for row in db.execute("SELECT id,order_id FROM receipts")
                    if state["orders"][row["order_id"]]["side"] == "SELL"
                    and state["orders"][row["order_id"]]["status"] not in PENDING
                    and row["id"] not in known}
        pending = tuple(sorted(unstaged | {row["trade_id"] for row in outbox if row["status"] == "STAGED"}))
        discrepancy = db.execute("SELECT 1 FROM next_profit_discrepancies LIMIT 1").fetchone()
        unapplied = db.execute("SELECT 1 FROM receipts WHERE applied != 1 LIMIT 1").fetchone()
        allowed = not pending and not discrepancy and not unapplied and not state.get("quarantine") and state.get("breaker") == "NORMAL"
        return CashProjection(pools, available, escrow, physical, Decimal(500), pending, allowed)

    def stage(self, *, trade_id: str, snapshot: MembershipSnapshot, now: int):
        """Durably freeze evidence, membership, grant payload and balanced cash legs."""
        self._require_enabled()
        _identifier(trade_id)
        now = _time(now)
        try:
            with localcontext() as ctx, self._paper() as db:
                ctx.prec = 400
                db.execute("BEGIN IMMEDIATE")
                source, settled, gross, principal, fees, profit, pool = self._verify_source(db, trade_id, now)
                membership = self._snapshot_payload(snapshot, settled)
                existing = db.execute("SELECT * FROM next_profit_outbox WHERE trade_id=?", (trade_id,)).fetchone()
                if existing is not None:
                    _check(existing["source_payload"] == source and existing["snapshot_payload"] == membership,
                           "Retry changed frozen source or membership")
                    self._project(db, self._state(db))
                    return dict(existing)
                policy = self.allocator.policy
                _check(now - settled <= policy.max_profit_age_seconds, "Stale new settlement")
                last = db.execute("SELECT MAX(staged_at) FROM next_profit_outbox").fetchone()[0]
                _check(last is None or now >= last, "Bridge clock moved backwards")
                previous = db.execute("SELECT payload FROM next_profit_snapshots WHERE id=?", (snapshot.snapshot_id,)).fetchone()
                _check(previous is None or previous[0] == membership, "Snapshot ID changed membership")
                db.execute("INSERT OR IGNORE INTO next_profit_snapshots VALUES (?,?)", (snapshot.snapshot_id, membership))
                key = _digest(_json([self.account_id, self.connector, self.owner_id, trade_id]))
                operation = "native-profit:" + key
                event_payload = None
                if profit > 0:
                    event = SettledProfitEvent(
                        event_id="realized:" + key, account_id=self.account_id, currency="USD",
                        source_owner_id=self.owner_id, source_realization_ids=("trade-fill:" + trade_id,),
                        cash_backing_id="paper-cash:" + key, proof_id="settled-profit-proof:" + key,
                        gross_realized_proceeds=gross, returned_principal=principal, total_attributable_fees=fees,
                        fee_net_realized_profit=profit, earmarked_profit_cash=profit, settled_at=settled,
                        verified_at=now, expires_at=now + policy.max_proof_age_seconds,
                        confirmed=True, settled=True, final=True, verified=True)
                    # Reject sub-atom amounts before locking backing in an outbox.
                    for amount in (gross, principal, fees, profit):
                        atoms = amount / policy.accounting_quantum
                        _check(atoms == atoms.to_integral_value(), "Source amount below allocator accounting precision")
                    event_payload = _json(asdict(event))
                status = "STAGED" if event_payload else "INELIGIBLE"
                db.execute("INSERT INTO next_profit_outbox VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                           (trade_id, operation, source, _digest(source), membership, event_payload,
                            _digest(membership + "\n" + (event_payload or "null")), status, now))
                if event_payload:
                    db.execute("INSERT INTO next_profit_cash VALUES (?,?,?,?)", (trade_id, pool, str(profit), str(profit)))
                self._project(db, self._state(db))
                self._fault("before_stage_commit")
                db.commit()
                self._fault("after_stage_commit")
                return dict(db.execute("SELECT * FROM next_profit_outbox WHERE trade_id=?", (trade_id,)).fetchone())
        except (ProfitBridgeBlocked, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            self._record_discrepancy(trade_id, now, exc)
            if isinstance(exc, ProfitBridgeBlocked):
                raise
            raise ProfitBridgeBlocked(str(exc)) from exc

    @staticmethod
    def _decode_payload(row):
        membership = json.loads(row["snapshot_payload"])
        membership["members"] = tuple(Member(**item) for item in membership["members"])
        event = json.loads(row["event_payload"])
        event["source_realization_ids"] = tuple(event["source_realization_ids"])
        return MembershipSnapshot(**membership), SettledProfitEvent(**event)

    def dispatch(self, *, trade_id: str, now: int):
        """Replay the original allocator payload; acknowledge only after grant commit."""
        self._require_enabled()
        now = _time(now)
        try:
            with localcontext() as ctx, self._paper() as db:
                ctx.prec = 400
                db.execute("BEGIN IMMEDIATE")
                row = self._one(db, "next_profit_outbox", "trade_id", trade_id)
                source, *_ = self._verify_source(db, trade_id, now)
                _check(source == row["source_payload"], "Native source changed after durable staging")
                self._project(db, self._state(db))
                if row["status"] == "INELIGIBLE":
                    return None
                if row["status"] == "ACKNOWLEDGED":
                    return json.loads(row["acknowledgement"])
                snapshot, event = self._decode_payload(row)
                db.commit()
            receipt = self.allocator.apply_profit(operation_id=row["operation_id"], snapshot=snapshot, event=event, now=now)
            self._fault("after_allocator_commit")
            with localcontext() as ctx, self._paper() as db:
                ctx.prec = 400
                db.execute("BEGIN IMMEDIATE")
                current = self._one(db, "next_profit_outbox", "trade_id", trade_id)
                _check(all(current[key] == row[key] for key in row if key not in {"status", "acknowledgement"}),
                       "Outbox changed during dispatch")
                source, *_ = self._verify_source(db, trade_id, now)
                _check(source == row["source_payload"], "Native source changed before acknowledgement")
                if current["acknowledgement"] is not None:
                    _check(current["acknowledgement"] == _json(receipt), "Changed allocator acknowledgement")
                db.execute("UPDATE next_profit_outbox SET status='ACKNOWLEDGED',acknowledgement=? WHERE trade_id=?",
                           (_json(receipt), trade_id))
                self._project(db, self._state(db))
                self._fault("before_ack_commit")
                db.commit()
                self._fault("after_ack_commit")
                return receipt
        except (ProfitBridgeBlocked, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            self._record_discrepancy(trade_id, now, exc)
            if isinstance(exc, ProfitBridgeBlocked):
                raise
            raise ProfitBridgeBlocked(str(exc)) from exc

    def cash_projection(self):
        self._require_enabled()
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            self._check_identity(state)
            return self._project(db, state)

    def before_new_spending(self):
        """Required adapter gate. Never returns permission while an outbox is pending."""
        self._require_enabled()
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            self._clean(db, state)
            for row in db.execute("SELECT * FROM next_profit_outbox ORDER BY trade_id").fetchall():
                source, *_ = self._verify_source(db, row["trade_id"], _time(state["now"]))
                _check(source == row["source_payload"], "Backing evidence changed after grant")
            projection = self._project(db, state)
            _check(projection.new_spending_allowed, "Unresolved profit outbox blocks new spending")
            return projection

    def integration_callbacks(self):
        return IntegrationCallbacks(self.cash_projection, self.before_new_spending)
