"""Read-only native admission for the default-off weekly next-entry integration.

This module cannot reserve, fund, create a position or dispatch an order. The
NEW ordinary reentry gate is a later completed positive hour above EMA8. Repair
continues to use the original native full-double feasibility rule. Proofs bind
all immutable identities and the complete order, never just a governor status.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import sqlite3

from spectra_core.next_entry_allocator import Allocator, EntryPermit
from spectra_core.reversal_signals import HourBar, IndicatorHistory
from .owned_spot.adapter import OwnedSpotAdapter, encoded, serial
from .owned_spot.model import Blocked, D, ZERO, ONE
from .owned_spot import native as N

SIGNAL_LABEL = "NEW_NEXT_ENTRY_POSITIVE_HOUR_ABOVE_EMA8"
PENDING = {"PREPARING", "OPEN", "PARTIAL", "CANCEL_REQUESTED", "FUNDING_PREPARED", "ENTRY_PREPARED"}
ORDER_FIELDS = (
    "id", "entry_id", "pair", "position", "cohort", "cohort_generation",
    "previous_cohort_generation", "kind", "side", "pool", "funding_owner",
    "proposal", "executor", "intent", "target", "principal", "decision_quantity",
    "decision_reference", "decision_at", "created_at", "capacity", "decision_bar",
    "repair_rung", "repair_retry", "source_pool", "source_owner", "base_budget", "profit_budget", "snapshot_id", "allocation_id",
    "allocation_owner_id", "allocation_role",
)
NATIVE_FIELDS = ("proposal_id", "pair", "intent", "amount_quote", "leverage", "created_at")


class AdmissionBlocked(Blocked):
    """Stable reason codes support explicit gate-blocked counts."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise AdmissionBlocked(code, message)


def _hash(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _money(value):
    _require(not isinstance(value, (float, bool)), "EXACT_MONEY", "Exact monetary input required")
    try:
        value = D(value)
    except Exception as exc:
        raise AdmissionBlocked("EXACT_MONEY", "Invalid exact monetary input") from exc
    _require(value.is_finite(), "EXACT_MONEY", "Finite monetary input required")
    return value


def _time(value):
    _require(not isinstance(value, bool) and isinstance(value, (int, float))
             and value > 0 and value == int(value), "TIME", "Whole positive UTC seconds required")
    return int(value)


def helper_identity():
    """Pin helper and its actual implementations, not a caller-supplied version."""
    root = Path(__file__).resolve().parents[2]
    paths = ("research/weekly_reversal/entry_admission.py",
             "research/weekly_reversal/compounding_adapter.py",
             "research/weekly_reversal/owned_spot/adapter.py",
             "research/weekly_reversal/owned_spot/model.py",
             "research/weekly_reversal/owned_spot/psm_bridge.py",
             "research/weekly_reversal/psm_kernel.py",
             "spectra_core/reversal_signals.py", "spectra_core/next_entry_allocator.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}


class NativeEntryAdmission:
    """Recompute guards using authoritative SQLite and a detached adapter view.

    ``cash_bridge`` is the reviewed native cash projection, never a free cash
    number. The owner must hold the existing adapter account writer lock. The
    optional ``paper_connection`` allows reads within the bridge's current
    transaction. ``state`` is checked against that connection, never trusted as
    a caller attestation. No methods here persist or change adapter state.
    """
    def __init__(self, adapter, *, allocator, snapshot_id, cash_bridge=None):
        _require(isinstance(adapter, OwnedSpotAdapter) and type(allocator) is Allocator,
                 "AUTHORITY", "Actual native adapter and concrete allocator required")
        self.adapter = adapter
        self.allocator = allocator
        self.snapshot_id = snapshot_id
        self.cash_bridge = cash_bridge

    def identity(self):
        return helper_identity()

    @contextmanager
    def _paper(self, connection=None):
        if connection is not None:
            yield connection
            return
        db = sqlite3.connect(self.adapter.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _native(self):
        db = sqlite3.connect((self.adapter.root / "native.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            yield db
        finally:
            db.close()

    def _state(self, db, expected):
        databases = db.execute("PRAGMA database_list").fetchall()
        main_path = next((row[2] for row in databases if row[1] == "main"), None)
        _require(main_path is not None and Path(main_path).resolve() == self.adapter.db_path.resolve(),
                 "AUTHORITY", "Paper transaction belongs to another authority database")
        row = db.execute("SELECT value FROM state WHERE id=1").fetchone()
        _require(row is not None, "AUTHORITY", "Missing durable paper state")
        state = json.loads(row[0])
        _require(expected is None or expected == state, "AUTHORITY", "Detached paper state differs from current authority")
        _require(not self.adapter.lock.closed, "WRITER_LOCK", "Account writer is closed")
        _require(state["identity"]["config"] == serial(asdict(self.adapter.config)),
                 "CONFIG", "Adapter configuration differs from durable identity")
        _require(state["identity"]["sources"] == N.verify_sources(), "NATIVE_IDENTITY", "Pinned native identity changed")
        c = self.adapter.config
        _require((c.initial_cash, c.protected_cash, c.total_budget_cap, c.pair_budget_cap,
                  c.recovery_cap, c.fee_rate) == (D(2000), D(500), D(1500), D(750), D(500), D(".008")),
                 "CONFIG", "Frozen capital limits and actual 80bps fees required")
        return state

    def _view(self, state, now, projection, *, db=None):
        # Avoid subclass spending hooks, clock changes, seed-completion writes and
        # cached evidence. Read-only original methods operate on this detached view.
        view = object.__new__(OwnedSpotAdapter)
        view.__dict__ = dict(self.adapter.__dict__)
        view.state, view.now, view._read_snapshot = deepcopy(state), now, None
        view.cash_pools = lambda: dict(projection.cash_pools)
        view.available_pool = lambda pool: projection.available_base_pools.get(pool, ZERO)
        # Audit checks nonnegative source claims. Receipt-only funding_cash cannot
        # see balanced profit transfers; use the independently verified cash
        # partition, retaining stricter per-pool rather than net owner balances.
        view.funding_cash = lambda: dict(projection.cash_pools)
        # Never let detached guards reenter the live adapter cash projection:
        # prove can already hold its exact paper write transaction. The reviewed
        # root balances use that same snapshot rather than a second connection.
        view._psm_bridge = copy(self.adapter.psm)
        view.psm.adapter = view
        if db is not None and getattr(self.cash_bridge, "root_cash_enabled", False):
            view.recovery_psm_cash = lambda: sum((amount for pair in state.get("psm", {})
                for key, amount in self.cash_bridge._root_balances(db, state, pair).items()
                if key.endswith("|repair")), ZERO)
        else:
            view.recovery_psm_cash = lambda: view.psm.recovery_cash()
        def no_write(*args, **kwargs):
            raise AdmissionBlocked("READ_ONLY", "Admission attempted a state write")
        view._save = no_write
        return view

    def _native_reservation(self, native, order, *, replay=False, required=True):
        row = native.execute("SELECT * FROM capital_reservations WHERE proposal_id=?", (order["proposal"],)).fetchone()
        if row is None and not required:
            others = list(native.execute("SELECT proposal_id FROM capital_reservations WHERE status IN ('RESERVED','COMMITTED')"))
            _require(not others, "PENDING_NATIVE", "Another unresolved native reservation blocks admission")
            return None
        _require(row is not None, "NATIVE_RESERVATION", "Exact native proposal is missing")
        row = dict(row)
        _require(row["pair"] == order["pair"] and row["intent"] == order["intent"]
                 and _money(row["amount_quote"]) == _money(order["principal"])
                 and _money(row["leverage"]) == ONE and _time(row["created_at"]) == _time(order["created_at"]),
                 "NATIVE_RESERVATION", "Native proposal scope or amount differs")
        allowed = {"COMMITTED", "RECONCILED", "RELEASED"} if replay else {"COMMITTED"}
        _require(row["status"] in allowed, "NATIVE_RESERVATION", "Native cash reservation is not committed")
        if not replay:
            others = list(native.execute("SELECT proposal_id FROM capital_reservations WHERE status IN ('RESERVED','COMMITTED') AND proposal_id<>?", (order["proposal"],)))
            _require(not others, "PENDING_NATIVE", "Another unresolved native reservation blocks admission")
        execution = native.execute("SELECT executor_id FROM execution_receipts WHERE proposal_id=?", (order["proposal"],)).fetchone()
        _require(execution is None or execution[0] == order["executor"],
                 "NATIVE_PROVENANCE", "Candidate execution receipt belongs to another executor")
        provenance = native.execute("SELECT * FROM execution_provenance WHERE proposal_id=?", (order["proposal"],)).fetchone()
        if provenance is not None:
            provenance = dict(provenance)
            c = self.adapter.config
            _require(provenance["executor_id"] == order["executor"] and provenance["pair"] == order["pair"]
                     and provenance["intent"] == order["intent"] and provenance["account_name"] == c.account
                     and provenance["connector_name"] == c.connector and provenance["controller_id"] == c.owner
                     and _money(provenance["authorized_quote"]) == _money(order["principal"]),
                     "NATIVE_PROVENANCE", "Candidate executor provenance differs from frozen authority")
        return {key: row[key] for key in NATIVE_FIELDS}

    def _membership(self, state, order, now, *, db=None, base_only=False):
        ledger = self.allocator.export_research_state()
        snapshot = ledger["snapshots"].get(self.snapshot_id)
        if snapshot is None and base_only and db is not None:
            row = db.execute("SELECT payload FROM next_profit_snapshots WHERE id=?", (self.snapshot_id,)).fetchone()
            if row is not None:
                snapshot = json.loads(row[0])
            if snapshot is None and order.get("repair_retry"):
                snapshot = deepcopy(state.get("weekly_compounding_membership"))
        _require(snapshot is not None, "MEMBERSHIP", "Frozen allocator membership missing")
        config = state["identity"]["config"]
        expected = [{"allocation_id": p, "owner_id": config["owner"] + ":" + p, "role": "POSITION_ALLOCATION"}
                    for p in sorted(config["rules"])]
        expected.append({"allocation_id": "VCAP", "owner_id": config["owner"] + ":VCAP", "role": "VCAP"})
        _require(snapshot["account_id"] == config["account"] == self.allocator.account_id
                 and snapshot["currency"] == self.allocator.currency == "USD"
                 and sorted(snapshot["members"], key=lambda x: x["allocation_id"]) == sorted(expected, key=lambda x: x["allocation_id"]),
                 "MEMBERSHIP", "Allocator membership does not match the complete frozen asset/VCAP scope")
        _require(snapshot["effective_from"] <= now <= snapshot["effective_until"], "MEMBERSHIP_STALE", "Decision outside frozen membership interval")
        allocation = "VCAP" if order["kind"] == "REPAIR" else order["pair"]
        _require(order.get("snapshot_id", self.snapshot_id) == self.snapshot_id
                 and order.get("allocation_id", allocation) == allocation,
                 "ALLOCATION_SCOPE", "Order attempts another membership or allocation")
        member = next(item for item in expected if item["allocation_id"] == allocation)
        _require(order.get("allocation_owner_id") == member["owner_id"]
                 and order.get("allocation_role") == member["role"],
                 "ALLOCATION_SCOPE", "Order owner or role differs from exact frozen member")
        key = json.dumps([self.snapshot_id, allocation], separators=(",", ":"))
        lots = deepcopy(ledger["buckets"].get(key, []))
        available_units = sum(item["units"] for item in lots)
        spendable_units = (available_units // self.allocator.quantum) * self.allocator.quantum
        amount = D(self.allocator._amount(spendable_units))
        if base_only:
            _require(amount == ZERO, "PROFIT_AVAILABLE", "Spendable allocation-quantum profit must accompany the next eligible entry")
        else:
            _require(amount > ZERO, "NO_PROFIT", "Allocation has no spendable allocation-quantum settled profit")
        events = {eid: deepcopy(ledger["events"][eid]) for item in lots for eid in (item["event_id"], item["grant_event_id"])}
        _require(all(ledger["events"][item["grant_event_id"]]["applied_at"] < now for item in lots),
                 "SAME_TIME_GRANT", "Entry must follow its entitlement grants")
        return snapshot, member, lots, amount, events

    def _psm(self, db, state, pair):
        meta = state.get("psm", {}).get(pair)
        if meta is None:
            return None
        _require(meta.get("mode") == "REPAIR" and meta.get("action") is None,
                 "PSM_OWNERSHIP", "Active PSM ownership blocks next-entry capital")
        archives = [row for row in meta.get("archived_episodes", []) if row.get("episode") == meta.get("episode")]
        _require(len(archives) == 1, "PSM_HANDBACK", "Completed archived PSM handback is required")
        archive = archives[0]
        from .owned_spot.psm_bridge import PSM, SOURCE_SHA256, _lineage_digest
        _require(meta.get("source_sha256") == archive.get("source_sha256") == SOURCE_SHA256
                 and meta.get("campaign_lineage_sha256") == _lineage_digest(meta),
                 "PSM_HANDBACK", "PSM source/campaign lineage changed")
        try:
            position = PSM.ResearchBook.loads(archive["journal"]).positions[archive["position"]]
        except Exception as exc:
            raise AdmissionBlocked("PSM_HANDBACK", "Archived PSM cannot replay") from exc
        _require(position["phase"] == "COMPLETE" and not position.get("quarantine")
                 and all(item["status"] != "PENDING" for item in position["orders"].values()),
                 "PSM_HANDBACK", "Archived PSM has not completed all children")
        _require(hashlib.sha256(archive["journal"].encode()).hexdigest() == archive["journal_sha256"],
                 "PSM_HANDBACK", "Archived PSM journal identity changed")
        receipts = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE kind='psm_episode_handback'")
                    if json.loads(row["payload"]) == {"pair": pair, "episode": meta["episode"]}]
        _require(len(receipts) == 1, "PSM_HANDBACK", "Native PSM handback audit receipt missing")
        return {"archive": archive, "receipt": receipts[0]}

    def _empty_root_attempt(self, db, native, state, cohort):
        """Prove an archived zero-fill attempt shell; never count it as acquired."""
        from .psm_root_cash import archive
        orders = [o for o in state["orders"].values() if o.get("cohort") == cohort["id"]]
        _require(orders and not cohort.get("lot_ids") and cohort.get("anchor") is None
                 and not cohort.get("anchor_history"), "ROOT_SUCCESSOR_LINEAGE_UNAVAILABLE", "Empty successor lacks exact zero-fill history")
        evidence = []
        for order in orders:
            _require(order["kind"] == "PSM_REBUILD" and order["side"] == "BUY"
                     and order["status"] == order.get("terminal") and order["terminal"] in {"CANCELED", "REJECTED"}
                     and order["pool"] == "psm:" + cohort["pair"]
                     and order.get("psm_root_cohort") == cohort["root_cohort_id"]
                     and db.execute("SELECT 1 FROM receipts WHERE order_id=?", (order["id"],)).fetchone() is None
                     and native.execute("SELECT 1 FROM economic_lots WHERE proposal_id=?", (order["proposal"],)).fetchone() is None,
                     "ROOT_EMPTY_ATTEMPT", "Successor attempt has fills, inventory, pending or foreign authority")
            reservation = self._native_reservation(native, order, replay=True)
            status = native.execute("SELECT status FROM capital_reservations WHERE proposal_id=?", (order["proposal"],)).fetchone()[0]
            binding = native.execute("SELECT * FROM economic_fill_bindings WHERE order_id=?", (order["id"],)).fetchone()
            execution = native.execute("SELECT * FROM execution_receipts WHERE proposal_id=?", (order["proposal"],)).fetchone()
            c = self.adapter.config
            _require(status in {"RECONCILED", "RELEASED"} and binding is not None and execution is not None
                     and dict(binding) == {"order_id": order["id"], "position_id": order["position"],
                         "role": "BASE_ENTRY" if order["funding_owner"] == "seed" else "VCAP_RECOVERY",
                         "pair": order["pair"], "account_name": c.account, "connector_name": c.connector,
                         "proposal_id": order["proposal"], "executor_id": order["executor"], "created_at": binding["created_at"]}
                     and execution["executor_id"] == order["executor"],
                     "ROOT_EMPTY_ATTEMPT", "Zero-fill successor lacks exact terminal native binding")
            terminal = [dict(r) for r in db.execute("SELECT * FROM audit_events WHERE kind='terminal_reconciliation'")
                        if json.loads(r["payload"]).get("order") == order["id"]]
            _require(len(terminal) == 1 and json.loads(terminal[0]["payload"])["status"] == order["terminal"],
                     "ROOT_EMPTY_ATTEMPT", "Zero-fill successor lacks exact terminal audit")
            record, book = archive(state, order["pair"], order["psm_position"])
            actions = [a for a in state["psm"][order["pair"]]["actions"] if a["order"] == order["psm_order"]]
            _require(len(actions) == 1, "ROOT_EMPTY_ATTEMPT", "Zero-fill successor lost archived action")
            action = actions[0]
            allocations = [a for a in action["allocations"] if a.get("child") == order["id"]]
            parent = book.positions[record["position"]]["orders"][action["order"]]
            reconciles = [e for e in book.journal if e["kind"] == "reconcile" and e["order"] == action["order"]]
            _require(action["completed"] is True and action["sealed"] is True and action["side"] == "buy"
                     and action["terminal"] in {"CANCELED", "REJECTED"} and order["id"] in action["children"]
                     and len(allocations) == 1 and allocations[0]["cohort"] == cohort["id"]
                     and allocations[0]["root"] == cohort["root_cohort_id"]
                     and allocations[0]["funding_owner"] == order["funding_owner"]
                     and _money(allocations[0]["target"]) == _money(order["target"])
                     and _money(allocations[0]["quantity"]) == _money(order["decision_quantity"])
                     and parent["status"] == action["terminal"] and len(reconciles) == 1
                     and reconciles[0]["complete_fill_history"] is True
                     and terminal[0]["at"] <= record["completed_at"],
                     "ROOT_EMPTY_ATTEMPT", "Zero-fill successor archive/child economics changed")
            evidence.append({"order": deepcopy(order), "reservation": reservation, "terminal": terminal[0],
                             "native_binding": dict(binding), "native_execution": dict(execution),
                             "archive": record, "action": deepcopy(action), "reconcile": reconciles[0]})
        return serial({"cohort": deepcopy(cohort), "children": evidence})

    def _root_empty(self, db, native, view, cohort, *, candidate=None):
        """Independent root-wide native inventory and exclusive-action guard."""
        root, pair = cohort["id"], cohort["pair"]
        members = [c for c in view.state["cohorts"].values()
                   if c["id"] == root or c.get("root_cohort_id") == root]
        ids = {c["id"] for c in members}
        _require(all(c["pair"] == pair for c in members), "ROOT_SCOPE", "Root successor changed asset")
        orders = [o for o in view.state["orders"].values() if o.get("cohort") in ids]
        proposals = {o["proposal"] for o in orders if o["side"] == "BUY"}
        _require(not any(view._cohort_lots(c) for c in members if c["id"] != root)
                 and not any(l.proposal_id in proposals and l.base_remaining > ZERO for l in view._lots(pair)
                             if l.lot_id not in set(cohort["lot_ids"])),
                 "ROOT_SUCCESSOR_HELD", "Original slot still owns native successor units, including dust")
        _require(not view._cohort_lots(cohort), "HELD_POSITION_TOPUP", "Original slot is not fully sold")
        _require(not any(o["status"] in PENDING and o["id"] != candidate for o in orders),
                 "ROOT_SUCCESSOR_HELD", "Root successor has unresolved order authority")
        own = self._state(db, None)["orders"].get(candidate, {}) if candidate else {}
        _require(native.execute("SELECT 1 FROM capital_reservations WHERE status IN ('RESERVED','COMMITTED') AND proposal_id<>?",
                                (own.get("proposal", ""),)).fetchone() is None,
                 "PENDING_NATIVE", "Another native commitment blocks root mutation")
        acquired = []
        for order in orders:
            if order["side"] != "BUY" or order.get("funding_owner") != "seed" or order["kind"] not in {"SEED", "REENTRY", "PSM_REBUILD"}:
                continue
            receipts = [json.loads(row[0]) for row in db.execute("SELECT payload FROM receipts WHERE order_id=? AND applied=1", (order["id"],))]
            if not any(_money(r["amount"]) > ZERO for r in receipts):
                continue
            generation = order.get("cohort_generation", 1)
            if order["kind"] == "PSM_REBUILD":
                member = view.state["cohorts"][order["cohort"]]
                anchors = [a for a in member.get("anchor_history", []) if order["id"] in a.get("receipt_orders", [])]
                _require(len(anchors) == 1, "ROOT_SUCCESSOR_LINEAGE_UNAVAILABLE", "Exact completed PSM acquisition generation anchor missing")
                generation = anchors[0]["generation"]
            acquired.append((generation, order))
        _require(acquired, "SOURCE_LINEAGE", "Root has no actual acquired seed capital")
        empty_attempts = []
        for member in members:
            if member["id"] != root and not any(o["cohort"] == member["id"] and o["kind"] == "PSM_REBUILD" for _, o in acquired):
                empty_attempts.append(self._empty_root_attempt(db, native, view.state, member))
        generation = max(g for g, _ in acquired)
        latest = [o for g, o in acquired if g == generation]
        return members, generation, latest, empty_attempts

    def _root_principal(self, db, state, cohort, source_pool):
        """Exact executable-source capital; never aggregate seed/PSM cash."""
        root, pair = cohort["id"], cohort["pair"]
        fields = ("id", "pair", "cohort", "kind", "side", "pool", "funding_owner", "proposal", "executor")
        balances = {"seed": self.adapter.config.seed_ticket}
        receipts, earmarks, transfers = [], [], []
        def owns(order):
            member = state["cohorts"].get(order.get("cohort"))
            return (member is not None and member.get("root_cohort_id", member["id"]) == root
                    and order["pair"] == pair and order.get("funding_owner") == "seed")
        with localcontext() as ctx:
            ctx.prec = 400
            for row in db.execute("SELECT * FROM receipts ORDER BY id"):
                order = state["orders"][row["order_id"]]
                if not owns(order):
                    continue
                _require(row["applied"] == 1, "ROOT_PRINCIPAL", "Root cash has an unapplied native receipt")
                raw = json.loads(row["payload"])
                gross, fee = _money(raw["amount"]) * _money(raw["price"]), _money(raw["fee_paid"])
                delta = gross - fee if order["side"] == "SELL" else -gross - fee
                balances[order["pool"]] = balances.get(order["pool"], ZERO) + delta
                receipts.append({"receipt": dict(row), "order": {k: order.get(k) for k in fields}})
            for row in db.execute("SELECT * FROM next_profit_cash ORDER BY trade_id"):
                receipt = db.execute("SELECT order_id FROM receipts WHERE id=?", (row["trade_id"],)).fetchone()
                _require(receipt is not None, "ROOT_PRINCIPAL", "Profit cash lost its original receipt")
                order = state["orders"][receipt[0]]
                if not owns(order):
                    continue
                _require(row["source_pool"] == order["pool"] and _money(row["source_debit"]) == _money(row["escrow_credit"]) > ZERO,
                         "ROOT_PRINCIPAL", "Root profit debit lost its original same-owner cash")
                balances[order["pool"]] = balances.get(order["pool"], ZERO) - _money(row["source_debit"])
                earmarks.append(dict(row))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='next_entry_cash_transfers'").fetchone():
                for row in db.execute("SELECT * FROM next_entry_cash_transfers ORDER BY transfer_id"):
                    payload = json.loads(row["payload"])
                    _require(hashlib.sha256(row["payload"].encode()).hexdigest() == row["payload_digest"],
                             "ROOT_PRINCIPAL", "Root funding cash transfer changed")
                    funding, proof = self.cash_bridge._funding_row(db, row["entry_id"])
                    order = state["orders"][funding["order_id"]]
                    if not owns(order):
                        continue
                    _require(payload["kind"] in {"FUND", "RETURN"}
                             and payload["source_pool"] == proof["source_pool"]
                             and payload["entry_pool"] == proof["entry_pool"] == order["pool"]
                             and payload["admission_sha256"] == proof["proof_sha256"],
                             "ROOT_PRINCIPAL", "Root transfer differs from its immutable admitted owner")
                    base, profit = _money(payload["base"]), _money(payload["profit"])
                    direction = ONE if payload["kind"] == "FUND" else -ONE
                    balances[payload["source_pool"]] = balances.get(payload["source_pool"], ZERO) - direction * base
                    balances[payload["entry_pool"]] = balances.get(payload["entry_pool"], ZERO) + direction * (base + profit)
                    transfers.append({"transfer": dict(row), "order": {k: order.get(k) for k in fields},
                                      "admission_sha256": proof["proof_sha256"]})
            _require(all(value >= ZERO for value in balances.values()), "ROOT_PRINCIPAL", "A root source cash claim is overdrawn")
            available = balances.get(source_pool, ZERO)
            unavailable = [{"source_pool": pool, "amount": amount} for pool, amount in sorted(balances.items())
                           if pool != source_pool and amount > ZERO]
            proof = serial({"schema": 1, "kind": "SAME_ROOT_SOURCE_PRINCIPAL", "root_cohort_id": root,
                "pair": pair, "funding_owner": "seed", "source_pool": source_pool,
                "initial_seed_allocation": self.adapter.config.seed_ticket, "balances": balances,
                "receipts": receipts, "profit_earmarks": earmarks, "cash_transfers": transfers,
                "available_base_cash": available, "unavailable_to_this_entry": unavailable})
            proof["proof_sha256"] = _hash(proof)
            return proof

    def _principal_totals(self, principal):
        """Replay exact frozen row economics, independent of claimed totals."""
        balances = {"seed": _money(principal["initial_seed_allocation"])}
        receipts = {}
        with localcontext() as ctx:
            ctx.prec = 400
            for item in principal["receipts"]:
                row, order = item["receipt"], item["order"]
                raw = json.loads(row["payload"])
                _require(row["id"] not in receipts and row["id"] == raw["trade_id"]
                         and row["order_id"] == raw["order_id"] == order["id"]
                         and row["applied"] == 1 and order["funding_owner"] == "seed"
                         and order["pair"] == principal["pair"] and raw["trade_type"] == order["side"],
                         "ROOT_PRINCIPAL", "Frozen root receipt scope is duplicate or inconsistent")
                receipts[row["id"]] = item
                gross, fee = _money(raw["amount"]) * _money(raw["price"]), _money(raw["fee_paid"])
                _require(order["side"] in {"BUY", "SELL"} and gross > ZERO and fee >= ZERO,
                         "ROOT_PRINCIPAL", "Frozen root receipt economics are invalid")
                delta = gross - fee if order["side"] == "SELL" else -gross - fee
                balances[order["pool"]] = balances.get(order["pool"], ZERO) + delta
            seen = set()
            for row in principal["profit_earmarks"]:
                item = receipts.get(row["trade_id"])
                _require(item is not None and row["trade_id"] not in seen
                         and item["order"]["side"] == "SELL" and item["order"]["pool"] == row["source_pool"]
                         and _money(row["source_debit"]) == _money(row["escrow_credit"]) > ZERO,
                         "ROOT_PRINCIPAL", "Frozen root profit debit lacks its exact own sale")
                seen.add(row["trade_id"])
                balances[row["source_pool"]] = balances.get(row["source_pool"], ZERO) - _money(row["source_debit"])
            seen = set()
            for item in principal["cash_transfers"]:
                row, order = item["transfer"], item["order"]
                payload = json.loads(row["payload"])
                _require(row["transfer_id"] not in seen and hashlib.sha256(row["payload"].encode()).hexdigest() == row["payload_digest"]
                         and payload["entry_id"] == row["entry_id"] and payload["kind"] in {"FUND", "RETURN"}
                         and payload["entry_pool"] == order["pool"] and order["funding_owner"] == "seed"
                         and order["pair"] == principal["pair"] and payload["admission_sha256"] == item["admission_sha256"]
                         and payload["source_pool"] != payload["entry_pool"],
                         "ROOT_PRINCIPAL", "Frozen root transfer is duplicate or inconsistent")
                seen.add(row["transfer_id"])
                base, profit = _money(payload["base"]), _money(payload["profit"])
                _require(base >= ZERO and profit >= ZERO, "ROOT_PRINCIPAL", "Frozen root transfer is negative")
                direction = ONE if payload["kind"] == "FUND" else -ONE
                balances[payload["source_pool"]] = balances.get(payload["source_pool"], ZERO) - direction * base
                balances[payload["entry_pool"]] = balances.get(payload["entry_pool"], ZERO) + direction * (base + profit)
            _require(all(v >= ZERO for v in balances.values()), "ROOT_PRINCIPAL", "Frozen root source is overdrawn")
            available = balances.get(principal["source_pool"], ZERO)
            unavailable = [{"source_pool": pool, "amount": amount} for pool, amount in sorted(balances.items())
                           if pool != principal["source_pool"] and amount > ZERO]
            _require(serial(balances) == principal["balances"] and _money(principal["available_base_cash"]) == available
                     and serial(unavailable) == principal["unavailable_to_this_entry"],
                     "ROOT_PRINCIPAL", "Frozen root cash totals differ from exact original row economics")
        return available

    def _replay_root_principal(self, proof, db, state):
        original = proof["evidence"].get("root_principal")
        if original is None:
            _require(proof["evidence"]["order"]["kind"] != "REENTRY", "ROOT_PRINCIPAL", "Reentry requires frozen source-specific principal")
            return
        frozen = deepcopy(original); digest = frozen.pop("proof_sha256", None)
        _require(digest == _hash(frozen) and original["source_pool"] == proof["source_pool"]
                 and original["root_cohort_id"] == proof["evidence"]["order"]["cohort"]
                 and _money(original["initial_seed_allocation"]) == self.adapter.config.seed_ticket,
                 "ROOT_PRINCIPAL", "Frozen root principal identity changed")
        self._principal_totals(original)
        for item in original["receipts"]:
            row = db.execute("SELECT * FROM receipts WHERE id=?", (item["receipt"]["id"],)).fetchone()
            order = state["orders"].get(item["order"]["id"])
            _require(row is not None and dict(row) == item["receipt"] and order is not None
                     and {k: order.get(k) for k in item["order"]} == item["order"]
                     and state["cohorts"][order["cohort"]].get("root_cohort_id", order["cohort"]) == original["root_cohort_id"],
                     "ROOT_PRINCIPAL", "Original root cash receipt or owner changed")
        for item in original["profit_earmarks"]:
            row = db.execute("SELECT * FROM next_profit_cash WHERE trade_id=?", (item["trade_id"],)).fetchone()
            _require(row is not None and dict(row) == item, "ROOT_PRINCIPAL", "Original root profit cash debit changed")
        for item in original["cash_transfers"]:
            row = db.execute("SELECT * FROM next_entry_cash_transfers WHERE transfer_id=?", (item["transfer"]["transfer_id"],)).fetchone()
            order = state["orders"].get(item["order"]["id"])
            _require(row is not None and dict(row) == item["transfer"] and order is not None
                     and {k: order.get(k) for k in item["order"]} == item["order"]
                     and state["cohorts"][order["cohort"]].get("root_cohort_id", order["cohort"]) == original["root_cohort_id"],
                     "ROOT_PRINCIPAL", "Original same-root base transfer changed")
            _, admission = self.cash_bridge._funding_row(db, item["transfer"]["entry_id"])
            _require(admission["proof_sha256"] == item["admission_sha256"], "ROOT_PRINCIPAL", "Original transfer admission changed")
        entry = proof.get("permit") or proof["base_entry"]
        _require(_money(entry["base_entry_budget"]) == min(self.adapter.config.seed_ticket, _money(original["available_base_cash"])) > ZERO,
                 "ROOT_PRINCIPAL", "Base differs from frozen executable-source principal cap")

    def _root_source(self, db, native, view, cohort, now, *, candidate=None):
        members, generation, latest, empty_attempts = self._root_empty(db, native, view, cohort, candidate=candidate)
        pools = {o["pool"] for o in latest}
        _require(len(pools) == 1, "SOURCE_LINEAGE", "Latest actual root acquisition has ambiguous source pools")
        source = next(iter(pools))
        principal = self._root_principal(db, view.state, cohort, source)
        common = {"root_principal": principal, "available_base_cash": _money(principal["available_base_cash"]),
                  "base_budget": min(view.config.seed_ticket, _money(principal["available_base_cash"])),
                  "unavailable_to_this_entry": principal["unavailable_to_this_entry"]}
        if source != "psm:" + cohort["pair"]:
            _require(all(o["cohort"] == cohort["id"] and o["kind"] in {"SEED", "REENTRY"} for o in latest)
                     and generation == cohort.get("generation", 1),
                     "ROOT_SUCCESSOR_LINEAGE_UNAVAILABLE", "Latest acquisition cannot reuse stale original-slot cash")
            return {"source_pool": source, "source_owner": None, "latest_generation": generation,
                    "latest_cohort_ids": [cohort["id"]], "root_source": None, "empty_attempts": empty_attempts, **common}
        _require(hasattr(self.cash_bridge, "root_source_proof"), "ROOT_SUCCESSOR_LINEAGE_UNAVAILABLE", "Reviewed root cash bridge unavailable")
        result = self.cash_bridge.root_source_proof(cohort["id"], now=now, paper_connection=db)
        proof, owner = result["ownership_proof"], result["source_owner"]
        frozen = deepcopy(proof); digest = frozen.pop("proof_sha256", None)
        expected = {"schema": 1, "pair": cohort["pair"], "root_cohort_id": cohort["id"],
                    "funding_owner": "seed", "source_pool": source, "latest_generation": generation,
                    "ownership_proof_sha256": digest}
        _require(digest == _hash(frozen) and owner == expected
                 and proof["latest_generation"] == generation
                 and sorted(proof["latest_cohort_ids"]) == sorted({o["cohort"] for o in latest})
                 and _time(proof["latest_sold_at"]) <= now,
                 "ROOT_SOURCE", "Root source owner, actual generation or completed sale boundary changed")
        _require(self.cash_bridge.validate_root_source_proof(proof, now=now, paper_connection=db) == proof,
                 "ROOT_SOURCE", "Frozen root native/receipt/handback proof failed replay")
        _require(_money(result["available_base_cash"]) == common["available_base_cash"],
                 "ROOT_PRINCIPAL", "Independent root principal and PSM subcash proofs disagree")
        return {"source_pool": source, "source_owner": owner, "latest_generation": generation,
                "latest_cohort_ids": proof["latest_cohort_ids"], "latest_sold_at": proof["latest_sold_at"],
                "root_source": proof, "empty_attempts": empty_attempts, **common}

    def root_reentry_source(self, cohort_id, *, now, state=None, paper_connection=None):
        """Read-only root/source diagnostics; never an order or capital permit."""
        now = _time(now)
        with self._paper(paper_connection) as db, self._native() as native:
            current = self._state(db, state)
            _require(_time(current["now"]) == now, "CLOCK", "Root derivation requires current durable clock")
            cohort = current["cohorts"].get(cohort_id)
            _require(cohort is not None and cohort.get("origin") == "SEED"
                     and cohort["id"] == f'{cohort["pair"]}:seed:{cohort["ordinal"]:02d}',
                     "ORIGINAL_SLOT", "Root source requires the original configured slot")
            self._psm(db, current, cohort["pair"])
            projection = self.cash_bridge._project(db, current)
            view = self._view(current, now, projection, db=db)
            result = self._root_source(db, native, view, cohort, now)
            return serial({"order_permission": False, **result})

    def _reentry(self, db, native, view, order, cohort, now):
        pair = order["pair"]
        _require(cohort.get("origin") == "SEED" and cohort["id"] == f'{pair}:seed:{cohort["ordinal"]:02d}'
                 and 1 <= cohort["ordinal"] <= view.config.seed_count,
                 "ORIGINAL_SLOT", "Reentry requires an original configured partial slot")
        root_source = self._root_source(db, native, view, cohort, now, candidate=order["id"])
        _require(order["source_pool"] == root_source["source_pool"] and order.get("source_owner") == root_source["source_owner"],
                 "ROOT_SOURCE", "Candidate source differs from actual latest root acquisition")
        _require(cohort["status"] == "SOLD", "HELD_POSITION_TOPUP", "Original slot is not fully sold")
        generation = root_source["latest_generation"]
        _require(order["cohort_generation"] == generation + 1
                 and order.get("previous_cohort_generation") == generation,
                 "COHORT_GENERATION", "Reentry must bind the actual latest sold root generation")
        _require(view._asset(pair).get("selected") in {None, cohort["id"]},
                 "WAITING_COHORT", "Another selected partial owns asset mutation authority")
        if root_source["root_source"] is not None:
            sold_receipts = []
            sold_at = root_source["latest_sold_at"]
        else:
            sale_orders = [o for o in view.state["orders"].values() if o.get("cohort") == cohort["id"]
                           and o.get("cohort_generation", 1) == generation and o["side"] == "SELL"]
            _require(sale_orders and all(o["terminal"] in {"FILLED", "CANCELED"}
                     and o["status"] == o["terminal"] for o in sale_orders),
                     "SOLD_COMPLETION", "All exact predecessor sale orders must be settled")
            sold_receipts = []
            for sale in sale_orders:
                rows = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE kind='terminal_reconciliation'")
                        if json.loads(row["payload"]).get("order") == sale["id"]]
                _require(len(rows) == 1 and json.loads(rows[0]["payload"])["status"] == sale["terminal"],
                         "SOLD_COMPLETION", "Exact terminal sale completion receipt required")
                sold_receipts.extend(rows)
            sold_at = max(_time(row["at"]) for row in sold_receipts)
        raw_hours = view.state.get("next_entry_completed_hours", {}).get(pair, [])
        _require(len(raw_hours) >= 2, "NEW_ENTRY_HOUR_MISSING", "At least two observed completed hours required for the new reentry gate")
        history = IndicatorHistory(pair)
        try:
            for raw in raw_hours:
                bar = HourBar(**raw)
                _require(bar.opened_at + 3600 <= now, "NEW_ENTRY_HOUR_FUTURE", "Incomplete future hour supplied")
                observation = history.push(bar, observed_at=bar.opened_at + 3600)
        except AdmissionBlocked:
            raise
        except Exception as exc:
            raise AdmissionBlocked("NEW_ENTRY_HOUR_INVALID", str(exc)) from exc
        latest = raw_hours[-1]
        _require(observation.consecutive_hours >= 2 and observation.completed_at <= now < observation.completed_at + 3600,
                 "NEW_ENTRY_HOUR_STALE", "Current contiguous completed-hour evidence required")
        _require(latest["opened_at"] >= sold_at and observation.completed_at > sold_at,
                 "NEW_ENTRY_HOUR_PRE_SALE", "New entry hour must begin after sold-slot completion")
        flags = set(observation.reversal_evidence)
        _require("positive_completed_hour" in flags, "NEW_ENTRY_HOUR_NOT_POSITIVE", "New entry requires positive completed close-to-close return")
        _require("above_ema8" in flags, "NEW_ENTRY_BELOW_EMA8", "New entry requires completed-hour close above EMA8")
        return {"label": SIGNAL_LABEL, "sold_at": sold_at, "sold_receipts": sold_receipts,
                "completed_hours": raw_hours, "observation": asdict(observation), "root_source": root_source}

    def _repair_retry_rule(self, db, native, view, pair, now):
        """Read the original rung and invoke its unchanged native retry guards."""
        asset = view._asset(pair)
        cid = asset.get("selected")
        _require(cid is not None, "RETRY_NO_SELECTED", "Exact selected repair cohort required")
        cohort = view.state["cohorts"][cid]
        rung = cohort.get("rung")
        _require(rung is not None and rung["status"] in {"INCOMPLETE", "BLOCKED"},
                 "RETRY_NO_INCOMPLETE", "Only an already admitted incomplete rung may retry")
        prior_ids = rung.get("orders", [])
        _require(prior_ids and len(set(prior_ids)) == len(prior_ids), "RETRY_SCOPE", "Original unique rung order lineage required")
        previous = [view.state["orders"].get(oid) for oid in prior_ids]
        _require(all(o is not None and o["kind"] == "REPAIR" and o["side"] == "BUY"
                     and o["pair"] == pair and o["cohort"] == cid and o["cohort_generation"] == cohort["generation"]
                     and o["repair_rung"] == rung["level"] and o["intent"] == "RECOVERY"
                     and o["funding_owner"] == "repair" and o.get("terminal") in {"FILLED", "CANCELED", "REJECTED"}
                     and o["status"] == o["terminal"] for o in previous),
                 "RETRY_SCOPE", "Every prior same-generation rung attempt must be exactly terminal")
        root = previous[0]
        requested = _money(rung["requested_target"])
        _require(not root.get("repair_retry") and requested == _money(root["target"]) == _money(rung["start_basis"]),
                 "RETRY_TARGET", "Retry cannot replace the original independently admitted full-double target")
        actual = ZERO
        terminal_audits, receipts = [], []
        for prior in previous:
            audits = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE kind='terminal_reconciliation'")
                      if json.loads(row["payload"]) == {"order": prior["id"], "status": prior["terminal"]}]
            _require(len(audits) == 1 and audits[0]["at"] < now, "RETRY_TERMINAL", "Retry needs an earlier exact terminal attempt")
            terminal_audits.extend(audits)
            self._native_reservation(native, prior, replay=True)
            rows = view._receipts(prior["id"])
            _require(rows == view._receipts(prior["id"], applied=True), "RETRY_RECEIPTS", "Prior rung has unapplied receipts")
            native_rows = [dict(row) for row in native.execute("SELECT * FROM economic_trade_fill_reconciliation WHERE order_id=?", (prior["id"],))]
            _require({r["trade_id"] for r in native_rows} == {r["trade_id"] for r in rows},
                     "RETRY_RECEIPTS", "Prior rung paper/native receipt coverage differs")
            for receipt in rows:
                fill = next(r for r in native_rows if r["trade_id"] == receipt["trade_id"])
                q, price, fee = (_money(receipt[k]) for k in ("amount", "price", "fee_paid"))
                lot = view.ledger.get_lot("trade-fill:" + receipt["trade_id"])
                _require(q > ZERO and price > ZERO and fee >= ZERO and fill["side"] == "BUY"
                         and fill["result_id"] == lot.lot_id and fill["position_id"] == prior["position"]
                         and _money(fill["amount_base"]) == q and _money(fill["price"]) == price
                         and _money(fill["fee_paid"]) == fee and lot.proposal_id == prior["proposal"]
                         and lot.executor_id == prior["executor"] and lot.base_original == q
                         and lot.cost_basis_original_quote == q * price and lot.entry_fees_original_quote == fee,
                         "RETRY_RECEIPTS", "Original repair debit differs from exact native ownership")
                actual += q * price + fee
                receipts.append(receipt)
            if prior.get("entry_id") and _money(prior.get("profit_budget", "0")) > ZERO:
                entry, prior_proof = self.cash_bridge._funding_row(db, prior["entry_id"])
                _require(entry["status"] == "CLOSED", "RETRY_FUNDING", "Prior funded attempt must finish its exact return/release")
                self.validate(prior_proof, prior, now=now, replay=True, paper_connection=db)
            elif prior.get("base_entry_proof"):
                self.validate_base_entry(prior["base_entry_proof"], prior, now=now, replay=True, paper_connection=db)
        other_filled = [o["id"] for o in view.state["orders"].values() if o["kind"] == "REPAIR"
                        and o.get("cohort") == cid and o.get("cohort_generation") == cohort["generation"]
                        and o.get("repair_rung") == rung["level"] and view._receipts(o["id"])]
        _require(set(other_filled) <= set(prior_ids), "RETRY_SCOPE", "A real prior rung acquisition was omitted")
        remaining = requested - actual
        _require(actual == _money(rung["actual_added"]) and remaining == _money(rung["shortfall"])
                 and remaining > ZERO and view.cohort_basis(cid) == requested + actual,
                 "RETRY_TARGET", "Exact original target, remaining budget or unchanged owned basis differs")
        bar = view._bar(pair)
        _require(bar.completed_at == now and cohort.get("acquired_at") is not None
                 and bar.opened_at >= cohort["acquired_at"], "FRESH_BAR", "Retry needs a current post-acquisition completed bar")
        lots = view._cohort_lots(cohort)
        N.economic_exit._group_owners(lots=lots, governor=view.governor, pair=pair)
        quantity = sum((lot.base_remaining for lot in lots), ZERO)
        principal = sum((lot.cost_basis_remaining_quote for lot in lots), ZERO)
        fees = sum((lot.entry_fees_remaining_quote for lot in lots), ZERO)
        key = f'{view.config.owner}:{cid}:generation:{cohort["generation"]}:lifecycle'
        original_state = view.lifecycle.load(key)
        lifecycle = original_state or N.vcap_state.VCAPLifecycleState(envelope_proposal_id=key, pair=pair,
            next_sequence=1, remaining_authorized_quote=ZERO, recovery_proposal_ids=(), position_base=quantity,
            cost_basis_quote=principal, last_known_price=bar.close, profit_state=None,
            status=N.vcap_state.VCAPLifecycleStatus.ACTIVE, updated_at=now)
        class CaptureOnly:
            def save(self, value):
                self.value = value
        decision = N.vcap_lifecycle.plan_vcap_lifecycle(state=lifecycle, store=CaptureOnly(),
            position=N.recovery_fingerprint.VCAPPositionState(pair=pair, position_base=quantity,
                cost_basis_quote=principal, current_price=bar.close), fingerprint=view._fingerprint(pair),
            trading_rule=view._native_rule(pair), seed_quote=view.config.seed_ticket,
            costs=N.vcap_profit_exit.VCAPExitCostModel(accrued_fees_quote=fees,
                expected_exit_fee_bps=view.config.fee_rate * D(10000),
                slippage_allowance_bps=view.config.expected_slippage_bps), now=now,
            profit_policy=N.vcap_profit_exit.VCAPProfitExitPolicy(target_net_profit_bps=view.config.target_net_profit_bps))
        _require(decision.action.value not in {"EXIT", "PROTECT_PROFIT"}, "RETRY_PROFIT_PROTECTED", "Native profit lifecycle blocks same-target retry")
        view.state["cohorts"][cid]["profit_state"] = None if decision.state.profit_state is None else serial(asdict(decision.state.profit_state))
        _require(not view.cohort_profit(cid, now=now).profit_floor_reached,
                 "RETRY_PROFIT_PROTECTED", "Selected cohort profit floor blocks same-target retry")
        scope = {"root_order_id": root["id"], "prior_order_ids": prior_ids,
                 "requested_target": rung["requested_target"], "start_basis": rung["start_basis"],
                 "prior_fee_inclusive_debit": str(actual), "remaining": str(remaining), "level": rung["level"]}
        return {"cohort_id": cid, "generation": cohort["generation"], "next_rung": rung["level"],
                "target": str(remaining), "repair_retry": scope, "terminal_audits": terminal_audits,
                "prior_receipts": receipts, "native_action": decision.action.value,
                "native_lifecycle_before": None if original_state is None else serial(asdict(original_state)),
                "native_lifecycle_after": serial(asdict(decision.state))}

    def repair_retry_feasibility(self, pair, *, now, state=None, paper_connection=None):
        """Nonauthorizing candidate diagnostics for the existing rung's exact residual."""
        now = _time(now)
        result = {"eligible": False, "order_permission": False, "pair": pair, "at": now}
        try:
            with self._paper(paper_connection) as db, self._native() as native:
                current = self._state(db, state)
                _require(_time(current["now"]) == now, "CLOCK", "Current durable completed boundary required")
                _require(not any(o["status"] in PENDING for o in current["orders"].values())
                         and not any(a.get("exit") for a in current["assets"].values()), "PENDING", "Pending action blocks repair retry")
                _require(current.get("seed_completion_receipt"), "SEED_INCOMPLETE", "Original seed barrier required")
                self._psm(db, current, pair)
                self.cash_bridge._clean(db, current)
                with localcontext() as context:
                    context.prec = 400
                    projection = self.cash_bridge._project(db, current)
                _require(not projection.pending_trade_ids, "RETRY_FUNDING", "Prior cash/funding reconciliation is pending")
                view = self._view(current, now, projection, db=db)
                view._assert_clean(pair)
                for asset in current["assets"].values():
                    for position in asset["positions"]:
                        health = view.health.assess(position_id=position, now=now)
                        _require(health.allows_live_mutation(now=now) and health.fully_reconciled,
                                 "ACCOUNTING_HEALTH", "Fresh whole-account accounting required")
                rule = self._repair_retry_rule(db, native, view, pair, now)
                result.update(rule)
                key = json.dumps([self.snapshot_id, "VCAP"], separators=(",", ":"))
                atoms = sum(item["units"] for item in self.allocator.export_research_state()["buckets"].get(key, []))
                profit = D(self.allocator._amount((atoms // self.allocator.quantum) * self.allocator.quantum))
                order = {"kind": "REPAIR", "pair": pair, "snapshot_id": self.snapshot_id,
                         "allocation_id": "VCAP", "allocation_owner_id": view.config.owner + ":VCAP",
                         "allocation_role": "VCAP", "repair_retry": rule["repair_retry"],
                         "source_pool": "repair", "funding_owner": "repair", "target": rule["target"]}
                self._membership(current, order, now, db=db, base_only=profit == ZERO)
                target = _money(rule["target"]); base = target - profit
                _require(base > ZERO, "PROFIT_EXCEEDS_REPAIR_TARGET", "Spendable VCAP leaves no positive base for the unchanged residual")
                bar = view._bar(pair); venue = view.config.rules[pair]
                q = view.config.quantity_for_budget(pair, target, bar.close)
                _require(q > ZERO and q >= venue.min_base and q * bar.close >= venue.min_quote,
                         "MINIMUM", "Exact residual is below venue minimum and remains locked")
                used = _money(current.get("liquidity", {}).get(f'{pair}:{bar.completed_at}', "0"))
                _require(used + target <= bar.quote_volume * view.config.participation, "LIQUIDITY", "Full exact residual exceeds prior-bar participation")
                _require(projection.available_base_pools.get("repair", ZERO) >= base and projection.profit_escrow >= profit,
                         "OWNER_CASH", "Exact repair base plus settled VCAP profit cannot fund residual")
                _require(projection.cash_pools.get("protected") == D(500)
                         and projection.total_physical_cash - view.holds() - view._unsettled_sales() - target >= D(500),
                         "RESERVE", "Exact residual breaches protected reserve")
                claims = self._capital_claims(db, view, projection, order, base)
                _require(claims["total"] + target <= D(1500), "TOTAL_CAP", "Exact residual breaches total capital cap")
                _require(claims["pairs"][pair] + target <= D(750), "PAIR_CAP", "Exact residual breaches asset capital cap")
                _require(claims["recovery"] + target <= D(500), "RECOVERY_CAP", "Exact residual breaches recovery cap")
                result.update(eligible=True, reason_code="SAME_TARGET_RETRY_CANDIDATE", base_budget=str(base), profit_budget=str(profit))
        except (Blocked, ValueError) as exc:
            result.update(reason_code=getattr(exc, "code", "RETRY_GUARD"), reason=str(exc))
        return serial(result)

    def _capital_claims(self, db, view, projection, order, base):
        original = view.capital_commitments()
        result = {"total": original["total"], "pairs": dict(original["pairs"]),
                  "recovery": original["recovery"], "native": original, "dedicated_claims": []}
        # A funded dedicated pool already backs its outstanding hold. Count
        # actual pool cash once, whether an order is open, terminal, or sold.
        for pending in view._pending():
            if not pending["pool"].startswith("next_entry:"):
                continue
            hold = view._hold(pending)
            result["total"] -= hold
            result["pairs"][pending["pair"]] -= hold
            if pending.get("funding_owner") == "repair":
                result["recovery"] -= hold
        for pool, amount in sorted(projection.cash_pools.items()):
            if not pool.startswith("next_entry:"):
                continue
            entry_id = pool[len("next_entry:"):]
            _require(hasattr(self.cash_bridge, "_funding_row"), "CAPITAL_CLAIM", "Dedicated cash requires reviewed funding authority")
            row, prior = self.cash_bridge._funding_row(db, entry_id)
            original_order = view.state["orders"].get(row["order_id"])
            _require(original_order is not None and original_order["pool"] == prior["entry_pool"] == pool
                     and original_order["entry_id"] == entry_id
                     and original_order["pair"] == prior["evidence"]["order"]["pair"]
                     and original_order["funding_owner"] == prior["evidence"]["order"]["funding_owner"]
                     and original_order["funding_owner"] in {"seed", "repair"},
                     "CAPITAL_CLAIM", "Dedicated cash lost exact asset/source-owner lineage")
            pair, owner = original_order["pair"], original_order["funding_owner"]
            result["total"] += amount
            result["pairs"][pair] += amount
            if owner == "repair":
                result["recovery"] += amount
            result["dedicated_claims"].append({"pool": pool, "amount": amount,
                "pair": pair, "funding_owner": owner, "admission_sha256": prior["proof_sha256"]})
        transfer = base if order["source_pool"].startswith(("next_entry:", "psm:")) else ZERO
        if transfer and order["source_pool"].startswith("psm:"):
            owner = order.get("source_owner")
            _require(owner is not None and owner["pair"] == order["pair"]
                     and owner["root_cohort_id"] == order["cohort"] and owner["funding_owner"] == "seed"
                     and owner["source_pool"] == order["source_pool"],
                     "CAPITAL_CLAIM", "PSM transfer requires exact root seed ownership")
        elif transfer:
            matching = [claim for claim in result["dedicated_claims"] if claim["pool"] == order["source_pool"]]
            _require(len(matching) == 1 and matching[0]["pair"] == order["pair"]
                     and matching[0]["funding_owner"] == order["funding_owner"]
                     and matching[0]["amount"] >= transfer,
                     "CAPITAL_CLAIM", "Transferred source claim is missing, foreign or insufficient")
        result["source_claim_transferred"] = transfer
        result["increment"] = _money(order["target"]) - transfer
        return result

    def _own_prepared_projection(self, db, projection, order):
        entry_id = order["entry_id"]
        _require(set(projection.pending_trade_ids) <= {entry_id}, "FUNDING_PENDING", "An earlier profit or funding saga is unresolved")
        if entry_id not in projection.pending_trade_ids:
            return projection
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='next_entry_funding'").fetchone()
        _require(table is not None, "FUNDING_PENDING", "Unknown pending cash marker")
        row = db.execute("SELECT * FROM next_entry_funding WHERE entry_id=?", (entry_id,)).fetchone()
        if row is not None:
            row = dict(row)
            frozen = json.loads(row["proof"])
            _require(row["status"] == "PREPARED" and row["order_id"] == order["id"]
                     and hashlib.sha256(row["proof"].encode()).hexdigest() == row["proof_digest"]
                     and frozen["entry_id"] == entry_id and frozen["order_id"] == order["id"]
                     and frozen["evidence"]["order"] == {key: order.get(key) for key in ORDER_FIELDS},
                     "FUNDING_BINDING", "Own pending funding row differs from immutable order proof")
            operations = [dict(item) for item in db.execute("SELECT * FROM next_entry_funding_outbox WHERE entry_id=?", (entry_id,))]
            _require(len(operations) == 1 and operations[0]["action"] == "RESERVE"
                     and operations[0]["status"] == "PREPARED"
                     and hashlib.sha256(operations[0]["payload"].encode()).hexdigest() == operations[0]["payload_digest"]
                     and json.loads(operations[0]["payload"]) == {"snapshot_id": self.snapshot_id,
                         "reservation_id": row["reservation_id"], "permit": frozen["permit"]},
                     "FUNDING_BINDING", "Own pending RESERVE outbox differs from original permit")
        else:
            _require(db.execute("SELECT 1 FROM next_entry_funding_outbox WHERE entry_id=?", (entry_id,)).fetchone() is None,
                     "FUNDING_BINDING", "Orphan pending funding operation")
        return replace(projection, pending_trade_ids=(), new_spending_allowed=True)

    def _source_lineage(self, db, state, order, cohort):
        generation = cohort.get("generation", 1)
        acquired = [o for o in state["orders"].values() if o.get("cohort") == cohort["id"]
                    and o.get("cohort_generation", 1) == generation and o["side"] == "BUY"
                    and o["intent"] == "ENTRY" and o.get("kind") in {"SEED", "REENTRY"}
                    and any(_money(json.loads(row[0])["amount"]) > ZERO for row in
                            db.execute("SELECT payload FROM receipts WHERE order_id=? AND applied=1", (o["id"],)))]
        _require(acquired and {o["pool"] for o in acquired} == {order["source_pool"]}
                 and all(o.get("funding_owner") == "seed" for o in acquired),
                 "SOURCE_LINEAGE", "Base cash must come from this exact predecessor entry pool")
        lineage = []
        for buy in acquired:
            sales = [o for o in state["orders"].values() if o.get("cohort") == cohort["id"]
                     and o["side"] == "SELL" and o["proposal"] == buy["proposal"]]
            for sale in sales:
                for row in db.execute("SELECT id FROM receipts WHERE order_id=?", (sale["id"],)):
                    value = self.cash_bridge._source_pool_proof(state, sale, buy, row[0])
                    if order["source_pool"].startswith("next_entry:"):
                        _require(value is not None, "SOURCE_LINEAGE", "Prior dedicated pool requires reviewed funding lineage")
                    lineage.append({"acquisition": {k: buy.get(k) for k in ORDER_FIELDS},
                                    "sale_id": sale["id"], "trade_id": row[0], "funding": value})
        _require(lineage, "SOURCE_LINEAGE", "Missing exact predecessor disposal lineage")
        return lineage

    def _execution_binding(self, order):
        c = self.adapter.config
        if not order.get("execution_frozen"):
            _require(_money(order["quantity"]) == _money(order["decision_quantity"])
                     and _money(order["reference"]) == _money(order["decision_reference"]),
                     "ORDER_BINDING", "Unexecuted decision sizing changed")
            return
        _require(order["status"] != "FUNDING_PREPARED", "NON_EXECUTABLE", "Prepared funding cannot freeze execution")
        q, price, target = (_money(order[k]) for k in ("quantity", "reference", "target"))
        rule = c.rules[order["pair"]]
        _require(price == _money(order["execution_price"]) and price > ZERO
                 and price % rule.price_tick == ZERO and q % rule.base_step == ZERO
                 and q == c.quantity_for_budget(order["pair"], target, price)
                 and q >= rule.min_base and q * price >= rule.min_quote
                 and c.all_in(q, price) <= target,
                 "EXECUTION_SIZING", "Later observed-open sizing violates unchanged budget, fees, tick or minimum")
        _require(order["decision_at"] + c.execution_delay_seconds <= order["execution_observed_at"]
                 <= order["decision_at"] + c.max_order_age_seconds,
                 "EXECUTION_TIME", "Frozen execution falls outside causal delay/expiry")

    def prove_base_entry(self, order_id, *, now, state=None, paper_connection=None):
        """Same real guards for a zero-spendable-profit ordinary reentry.

        Returns no allocator EntryPermit and cannot reserve entitlements. The
        original same-owner source pool pays the unchanged configured base.
        """
        return self._prove(order_id, now=now, approval_time=now, state=state,
                           paper_connection=paper_connection, base_only=True)

    def validate_base_entry(self, proof, order=None, *, now, replay=False, activation=False, state=None, paper_connection=None):
        now = _time(now)
        _require(not activation or replay, "ACTIVATION", "Activation must replay the original admission")
        _require(isinstance(proof, dict) and proof.get("permit") is None
                 and proof.get("allocator_reservation_required") is False
                 and proof.get("base_entry", {}).get("kind") == "BASE_ONLY_NEXT_ENTRY",
                 "BASE_PROOF", "Distinct native base-entry proof required")
        self._proof_identity(proof)
        original = deepcopy(proof)
        digest = original.pop("proof_sha256", None)
        _require(digest == _hash(original) and proof["helper_identity"] == helper_identity(),
                 "PROOF_IDENTITY", "Base proof or implementation identity changed")
        if replay:
            with self._paper(paper_connection) as db, self._native() as native:
                current = self._state(db, state)
                candidate = self._frozen_identity(proof, order, current, db, native, base_only=True)
                _require(candidate.get("base_entry_proof") == proof
                         and candidate["status"] not in {"ENTRY_PREPARED", "FUNDING_PREPARED"},
                         "BASE_REPLAY", "Executed base entry must retain its exact original durable proof")
                anchors = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE kind='base_entry_admitted'")
                           if json.loads(row["payload"]) == {"order": candidate["id"], "proof": proof["proof_sha256"]}]
                _require(len(anchors) == 1 and proof["base_entry"]["approved_at"] <= anchors[0]["at"] <= proof["base_entry"]["expires_at"],
                         "BASE_REPLAY", "Original base-entry admission audit anchor missing")
                if activation:
                    self._activation_identity(proof, candidate, current, db, native, now)
                    return deepcopy(proof)
                receipt = native.execute("SELECT executor_id FROM execution_receipts WHERE proposal_id=?", (candidate["proposal"],)).fetchone()
                _require(receipt is not None and receipt[0] == candidate["executor"],
                         "BASE_REPLAY", "Base entry lacks original native execution receipt")
                return deepcopy(proof)
        _require(now <= proof["base_entry"]["expires_at"], "PROOF_EXPIRED", "Base-entry proof expired")
        with self._paper(paper_connection) as db:
            current = self._state(db, state)
            candidate = current["orders"].get(proof["order_id"])
            _require(candidate is not None and (order is None or candidate == order),
                     "ORDER_BINDING", "Base-entry caller order differs from durable authority")
            recomputed = self._prove(proof["order_id"], now=now,
                approval_time=proof["base_entry"]["approved_at"], state=current,
                paper_connection=db, base_only=True)
            _require(recomputed == proof, "GUARD_CHANGED", "Current base-entry guards differ from frozen proof")
        return deepcopy(proof)

    def _activation_identity(self, proof, order, state, db, native, now):
        entry = proof.get("permit") or proof["base_entry"]
        _require(order["status"] == "PREPARING" and order.get("terminal") is None
                 and not order.get("execution_frozen") and entry["approved_at"] <= now <= entry["expires_at"],
                 "ACTIVATION", "First native execution requires unexpired original PREPARING activation")
        _require(_time(state["now"]) == now, "CLOCK", "Activation requires the current durable paper clock")
        reservation = native.execute("SELECT status FROM capital_reservations WHERE proposal_id=?", (order["proposal"],)).fetchone()
        execution = native.execute("SELECT executor_id FROM execution_receipts WHERE proposal_id=?", (order["proposal"],)).fetchone()
        _require(reservation is not None and (reservation[0] == "COMMITTED" or
                 reservation[0] == "RECONCILED" and execution is not None and execution[0] == order["executor"]),
                 "ACTIVATION", "First execution lost its original committed or exactly bound native authority")
        _require(native.execute("SELECT 1 FROM capital_reservations WHERE status IN ('RESERVED','COMMITTED') AND proposal_id<>?", (order["proposal"],)).fetchone() is None,
                 "ACTIVATION", "Competing native commitment changed exclusive activation authority")
        activation = order.get("activation")
        expected = {"at": activation.get("at") if isinstance(activation, dict) else None,
                    "proof_sha256": proof["proof_sha256"],
                    "liquidity_key": f'{order["pair"]}:{order["decision_bar"]}',
                    "liquidity_debit": order["target"]}
        _require(activation == expected and entry["approved_at"] <= _time(activation["at"]) <= now,
                 "ACTIVATION", "Atomic activation payload changed")
        anchors = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE kind='weekly_entry_activation'")
                   if json.loads(row["payload"]) == {"order": order["id"], "activation": activation}]
        _require(len(anchors) == 1 and anchors[0]["at"] == activation["at"],
                 "ACTIVATION", "Independent original activation audit anchor missing")
        _require(db.execute("SELECT 1 FROM receipts WHERE order_id=?", (order["id"],)).fetchone() is None,
                 "ACTIVATION", "First execution activation already has unexpected fills")
        _require(not any(o["id"] != order["id"] and o["status"] in PENDING for o in state["orders"].values())
                 and not any(asset.get("exit") for asset in state["assets"].values()),
                 "ACTIVATION", "Competing action changed exclusive activation ownership")
        self.cash_bridge._clean(db, state)
        with localcontext() as context:
            context.prec = 400
            projection = self.cash_bridge._project(db, state)
        _require(not projection.pending_trade_ids, "ACTIVATION", "Funding or cash saga is unresolved at activation")
        view = self._view(state, now, projection, db=db)
        view._assert_clean(order["pair"], health=False)
        bar = view._bar(order["pair"])
        _require(serial(asdict(bar)) == proof["evidence"]["completed_bar"], "ACTIVATION", "Original completed decision bar changed")
        _require(serial([asdict(lot) for lot in view._lots()]) == proof["evidence"]["owner_lots"],
                 "ACTIVATION", "Native inventory changed since original admission")
        _require(order["position"] in state["assets"][order["pair"]]["positions"],
                 "ACTIVATION", "Planned native position is not atomically owned by the asset")
        prior_positions = set(proof["evidence"]["asset_authority"]["positions"])
        for asset in state["assets"].values():
            for position in asset["positions"]:
                if position == order["position"] and position not in prior_positions:
                    # The single newly planned empty position cannot have a health
                    # report until its first exact executor binding is established.
                    snapshot = view.ledger.snapshot(position)
                    _require(snapshot.pair == order["pair"] and not snapshot.has_active_inventory,
                             "ACTIVATION", "Planned successor already has unadmitted native inventory")
                    continue
                health = view.health.assess(position_id=position, now=now)
                _require(health.allows_live_mutation(now=now) and health.fully_reconciled,
                         "ACCOUNTING_HEALTH", "Historical/current account health changed before activation")
        cohort = state["cohorts"][order["cohort"]]
        expected_cohort = deepcopy(proof["evidence"]["cohort"])
        expected_asset = deepcopy(proof["evidence"]["asset_authority"])
        if order["position"] not in expected_asset["positions"]:
            expected_asset["positions"].append(order["position"])
        _require(cohort["generation"] == order["cohort_generation"] and cohort["status"] == "OWNED",
                 "ACTIVATION", "Activated cohort generation or ownership changed")
        if order["kind"] == "REENTRY":
            prior = proof["evidence"]["cohort"]
            fields = ("generation", "anchor", "acquired_at", "last_rung", "rung", "status", "profit_state", "lot_ids")
            expected_history = {key: prior.get(key) for key in fields}
            expected_cohort.setdefault("generation_history", []).append(expected_history)
            expected_cohort.update(generation=order["cohort_generation"], anchor=None, acquired_at=None,
                                  last_rung=0, rung=None, status="OWNED", profit_state=None, lot_ids=[])
            _require(cohort.get("generation_history", [])[-1:] == [expected_history]
                     and cohort.get("anchor") is None and cohort.get("acquired_at") is None
                     and cohort.get("last_rung") == 0 and cohort.get("rung") is None
                     and cohort.get("profit_state") is None and not cohort["lot_ids"]
                     and state["assets"][order["pair"]]["selected"] == proof["evidence"]["asset_authority"]["selected"],
                     "ACTIVATION", "Atomic sold-slot generation activation changed")
        else:
            rung = {"level": order["repair_rung"], "requested_target": order["target"],
                    "start_basis": order["target"], "actual_added": "0", "shortfall": order["target"],
                    "status": "PENDING", "decision_bar": order["decision_bar"],
                    "quantity": order["decision_quantity"], "reference": order["decision_reference"],
                    "orders": [order["id"]]}
            if order.get("repair_retry"):
                rung = deepcopy(proof["evidence"]["cohort"]["rung"])
                rung["orders"].append(order["id"])
                rung.update(status="PENDING", quantity=order["decision_quantity"], reference=order["decision_reference"])
            asset = state["assets"][order["pair"]]
            expected_cohort["rung"] = rung
            expected_asset.update(selected=cohort["id"], last_repair_bar=order["decision_bar"], repair_block=None)
            _require(cohort["rung"] == rung and asset["selected"] == cohort["id"]
                     and asset["last_repair_bar"] == order["decision_bar"],
                     "ACTIVATION", "Atomic unchanged full-double rung activation changed")
        _require(cohort == expected_cohort and state["assets"][order["pair"]] == expected_asset,
                 "ACTIVATION", "Complete original cohort history or asset activation authority changed")
        used = _money(state.get("liquidity", {}).get(activation["liquidity_key"], "0"))
        _require(used == _money(proof["evidence"]["prior_liquidity_used"]) + _money(order["target"])
                 and used <= bar.quote_volume * view.config.participation,
                 "ACTIVATION", "Original liquidity admission was lost, duplicated or changed")
        if proof.get("permit") is None and order["kind"] == "REENTRY":
            principal = self._root_principal(db, state, proof["evidence"]["cohort"], order["source_pool"])
            _require(principal == proof["evidence"]["root_principal"], "ROOT_PRINCIPAL", "Current same-root source cash changed before base activation")
        if proof.get("permit") is None and order.get("source_owner") is not None:
            result = self.cash_bridge.root_source_proof(order["cohort"], now=now, paper_connection=db)
            _require(result["ownership_proof"] == proof["evidence"]["root_source"]
                     and result["source_owner"] == order["source_owner"]
                     and _money(result["available_base_cash"]) >= _money(entry["base_entry_budget"]),
                     "ROOT_OWNER_CASH", "Root subcash or frozen ownership changed before base activation")
        claims = self._capital_claims(db, view, projection, order, _money(entry["base_entry_budget"]))
        _require(claims["total"] <= D(1500) and claims["pairs"][order["pair"]] <= D(750)
                 and claims["recovery"] <= D(500), "ACTIVATION", "Current native/claim/hold capital exceeds frozen caps")

    def validate_activation(self, proof, order=None, *, now, state=None, paper_connection=None):
        """Only resume an atomically activated, still-unexpired first execution."""
        now = _time(now)
        if proof.get("permit") is None:
            return self.validate_base_entry(proof, order, now=now, replay=True, activation=True,
                                            state=state, paper_connection=paper_connection)
        self.validate(proof, order, now=now, replay=True, state=state, paper_connection=paper_connection)
        with self._paper(paper_connection) as db, self._native() as native:
            current = self._state(db, state)
            candidate = self._frozen_identity(proof, order, current, db, native)
            row, stored = self.cash_bridge._funding_row(db, proof["entry_id"])
            _require(row["status"] == "FUNDED" and stored == proof, "ACTIVATION", "Exact accepted entry funding is not complete")
            self._activation_identity(proof, candidate, current, db, native, now)
            return deepcopy(proof)

    def _proof_identity(self, proof):
        _require(isinstance(proof, dict), "PROOF", "Frozen admission proof required")
        original = deepcopy(proof)
        digest = original.pop("proof_sha256", None)
        _require(digest == _hash(original) and proof.get("helper_identity") == helper_identity(),
                 "PROOF_IDENTITY", "Proof bytes or reviewed implementation changed")
        expected_id = "entry-admission:" + _hash({"evidence": proof["evidence"],
                                                   "helper_identity": proof["helper_identity"]})
        entry = proof.get("permit") or proof.get("base_entry")
        binding = proof["evidence"]["order"]
        _require(proof["proof_id"] == expected_id and entry is not None
                 and entry["proof_id"] == expected_id and entry["permit_id"] == expected_id + ":permit",
                 "PROOF_IDENTITY", "Derived original proof/permit IDs do not match frozen evidence")
        _require(proof["order_id"] == binding["id"] and proof["entry_id"] == binding["entry_id"] == entry["entry_id"]
                 and proof["source_pool"] == binding["source_pool"] and proof.get("source_owner") == binding.get("source_owner")
                 and proof["entry_pool"] == binding["pool"]
                 and proof["snapshot_id"] == binding["snapshot_id"] == proof["evidence"]["membership"]["snapshot_id"]
                 and entry["native_reservation_id"] == binding["proposal"]
                 and entry["allocation_id"] == binding["allocation_id"]
                 and entry["owner_id"] == binding["allocation_owner_id"]
                 and entry["role"] == binding["allocation_role"]
                 and _money(entry["base_entry_budget"]) == _money(binding["base_budget"])
                 and _money(entry["approved_profit_increment"]) == _money(binding["profit_budget"])
                 and _money(entry["full_fee_inclusive_budget"]) == _money(binding["target"])
                 and entry["approved_at"] == binding["decision_at"],
                 "PROOF_IDENTITY", "Frozen proof/permit envelope differs from original bound evidence")

    def _replay_root_source(self, proof, db, now):
        with self._native() as native:
            state = self._state(db, None)
            self._replay_root_principal(proof, db, state)
            for original in proof["evidence"].get("root_empty_attempts", []):
                cohort = state["cohorts"].get(original["cohort"]["id"])
                _require(cohort is not None and self._empty_root_attempt(db, native, state, cohort) == original,
                         "REPLAY_ROOT_SOURCE", "Original empty terminal successor attempt changed")
        root = proof["evidence"].get("root_source")
        if root is not None:
            _require(hasattr(self.cash_bridge, "validate_root_source_proof")
                     and self.cash_bridge.validate_root_source_proof(root, now=now, paper_connection=db) == root,
                     "REPLAY_ROOT_SOURCE", "Original root cash ownership sources changed")
            owner = proof["evidence"]["order"].get("source_owner")
            _require(owner is not None and owner["ownership_proof_sha256"] == root["proof_sha256"],
                     "REPLAY_ROOT_SOURCE", "Frozen root owner lost its original ownership proof")
        else:
            _require(proof["evidence"]["order"].get("source_owner") is None,
                     "REPLAY_ROOT_SOURCE", "Root owner requires its exact frozen source proof")

    def _frozen_identity(self, proof, order, current, db, native, *, base_only=False):
        self._proof_identity(proof)
        original = deepcopy(proof)
        digest = original.pop("proof_sha256", None)
        _require(digest == _hash(original) and proof.get("helper_identity") == helper_identity(),
                 "PROOF_IDENTITY", "Frozen proof or reviewed implementation changed")
        candidate = current["orders"].get(proof["order_id"])
        _require(candidate is not None and (order is None or order == candidate)
                 and {key: candidate.get(key) for key in ORDER_FIELDS} == proof["evidence"]["order"],
                 "ORDER_BINDING", "Frozen replay order scope or size differs")
        _require(current["identity"] == proof["evidence"]["paper_identity"]
                 and proof["snapshot_id"] == self.snapshot_id,
                 "REPLAY_SCOPE", "Frozen replay configuration or snapshot changed")
        ledger = self.allocator.export_research_state()
        membership = ledger["snapshots"].get(self.snapshot_id)
        if membership is None and base_only:
            row = db.execute("SELECT payload FROM next_profit_snapshots WHERE id=?", (self.snapshot_id,)).fetchone()
            membership = None if row is None else json.loads(row[0])
            if membership is None and proof["evidence"]["order"].get("repair_retry"):
                membership = current.get("weekly_compounding_membership")
        _require(membership == proof["evidence"]["membership"], "REPLAY_SCOPE", "Original frozen membership changed")
        _require(self._native_reservation(native, candidate, replay=True) == proof["evidence"]["native_reservation"],
                 "REPLAY_NATIVE", "Frozen original native proposal changed")
        for event_id, event in proof["evidence"]["source_events"].items():
            _require(ledger["events"].get(event_id) == event, "REPLAY_SOURCE", "Original entitlement source changed")
        for receipt in proof["evidence"]["cash_receipts"]:
            row = db.execute("SELECT * FROM receipts WHERE id=?", (receipt["id"],)).fetchone()
            _require(row is not None and dict(row) == receipt, "REPLAY_SOURCE", "Original frozen cash receipt changed")
        self._replay_root_source(proof, db, current["now"])
        self._execution_binding(candidate)
        return candidate

    def validate_frozen(self, proof, order=None, *, now, state=None, paper_connection=None):
        """Reconciliation-only identity check, never new reservation/OPEN authority.

        Useful for cancellation of a staged unaccepted reservation after expiry.
        Actual terminal/native liability closure still needs its separate proof.
        """
        _time(now)
        with self._paper(paper_connection) as db, self._native() as native:
            current = self._state(db, state)
            self._frozen_identity(proof, order, current, db, native)
            row = db.execute("SELECT proof,proof_digest,order_id FROM next_entry_funding WHERE entry_id=?", (proof["entry_id"],)).fetchone()
            _require(row is not None and json.loads(row[0]) == proof
                     and hashlib.sha256(row[0].encode()).hexdigest() == row[1]
                     and row[2] == proof["order_id"],
                     "REPLAY_FROZEN", "Exact original staged funding proof is missing")
            return {"order_permission": False, "reservation_permission": False,
                    "entry_id": proof["entry_id"], "order_id": proof["order_id"],
                    "proof_id": proof["proof_id"], "proof_sha256": proof["proof_sha256"],
                    "purpose": "RECONCILIATION_ONLY"}

    def preview(self, order_id, *, now, base_only=False, state=None, paper_connection=None):
        """Non-authorizing preflight; absence of native commitment is explicit.

        The candidate must still be durable and all other checks are identical.
        This does not return a reusable proof or any allocator EntryPermit.
        """
        try:
            proof = self._prove(order_id, now=now, approval_time=now, state=state,
                paper_connection=paper_connection, base_only=base_only, require_native=False)
            entry = proof["base_entry"] if base_only else proof["permit"]
            return {"order_id": order_id, "order_permission": False, "eligible_except_native": True,
                    "native_committed": entry["native_cash_reserved"],
                    "native_reason": None if entry["native_cash_reserved"] else "NATIVE_RESERVATION_MISSING",
                    "reason_code": "ADMISSIBLE_PREVIEW", "base_budget": entry["base_entry_budget"],
                    "profit_budget": entry["approved_profit_increment"],
                    "full_target": entry["full_fee_inclusive_budget"],
                    "signal_label": proof["evidence"]["signal"]["label"]}
        except (ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            amounts = {}
            with self._paper(paper_connection) as db:
                candidate = self._state(db, state)["orders"].get(order_id)
                if candidate:
                    amounts = {"base_budget": candidate.get("base_budget"), "profit_budget": candidate.get("profit_budget"),
                               "full_target": candidate.get("target")}
            return {**amounts, "order_id": order_id, "order_permission": False, "eligible_except_native": False,
                    "native_committed": False, "reason_code": getattr(exc, "code", "AUTHORITY"),
                    "reason": str(exc)}

    def prove(self, order_id, *, now, state=None, paper_connection=None):
        return self._prove(order_id, now=now, approval_time=now, state=state, paper_connection=paper_connection)

    def _prove(self, order_id, *, now, approval_time, state=None, paper_connection=None, base_only=False, require_native=True):
        with localcontext() as context:
            context.prec, context.rounding = 28, ROUND_HALF_EVEN
            return self._compute(order_id, now=now, approval_time=approval_time,
                                 state=state, paper_connection=paper_connection, base_only=base_only, require_native=require_native)

    def _compute(self, order_id, *, now, approval_time, state=None, paper_connection=None, base_only=False, require_native=True):
        now, approved_at = _time(now), _time(approval_time)
        _require(self.cash_bridge is not None, "CASH_AUTHORITY", "Reviewed native cash bridge required")
        c = self.adapter.config
        bridge = self.cash_bridge
        _require(bridge.paper_db.resolve() == self.adapter.db_path.resolve()
                 and bridge.native_db.resolve() == (self.adapter.root / "native.sqlite").resolve()
                 and bridge.allocator is self.allocator and bridge.account_id == c.account
                 and bridge.connector == c.connector and bridge.owner_id == c.owner,
                 "CASH_AUTHORITY", "Cash bridge does not match this exact native/account/allocator authority")
        with self._paper(paper_connection) as db, self._native() as native:
            current = self._state(db, state)
            _require(_time(current["now"]) == now, "CLOCK", "Admission requires current durable decision clock")
            order = current["orders"].get(order_id)
            _require(order is not None and order["status"] == ("ENTRY_PREPARED" if base_only else "FUNDING_PREPARED")
                     and order.get("terminal") is None and not order.get("execution_frozen"),
                     "NON_EXECUTABLE", "Exact non-executable prepared order required")
            _require(order["kind"] in ({"REENTRY", "REPAIR"} if not base_only or order.get("repair_retry") else {"REENTRY"}) and order["side"] == "BUY", "ENTRY_PATH", "Only REENTRY or independently due REPAIR is allowed")
            entry_id = order.get("entry_id", order["id"])
            funding_owner = "repair" if order["kind"] == "REPAIR" else "seed"
            source_pool = order.get("source_pool")
            _require(isinstance(source_pool, str) and (source_pool == funding_owner or
                     funding_owner == "seed" and (source_pool.startswith("next_entry:") or
                     source_pool == "psm:" + order["pair"] and order.get("source_owner") is not None)),
                     "SOURCE_POOL", "Unknown or foreign source pool")
            _require(order["pool"] == (source_pool if base_only else "next_entry:" + entry_id) and order["funding_owner"] == funding_owner
                     and order["intent"] == ("RECOVERY" if funding_owner == "repair" else "ENTRY"),
                     "SOURCE_POOL", "Dedicated entry pool and original source-owner scope required")
            _require(not any(o["id"] != order_id and o["status"] in PENDING for o in current["orders"].values()),
                     "PENDING", "Another pending action blocks account spending")
            _require(not any(a.get("exit") for a in current["assets"].values()), "PENDING", "Exit manifest blocks spending")
            _require(db.execute("SELECT 1 FROM receipts WHERE order_id=?", (order_id,)).fetchone() is None,
                     "NON_EXECUTABLE", "Prepared entry already has fills")
            _require(current.get("seed_completion_receipt"), "SEED_INCOMPLETE", "Durable original seed completion barrier required")
            psm = self._psm(db, current, order["pair"])
            self.cash_bridge._clean(db, current)
            with localcontext() as cash_context:
                cash_context.prec = 400
                projection = self.cash_bridge._project(db, current)
            projection = self._own_prepared_projection(db, projection, order)
            view = self._view(current, now, projection, db=db)
            # Keep candidate identity for root native authority exclusion.
            view.state["orders"].pop(order_id)  # This non-executable candidate is not an existing cash hold.
            view._assert_clean(order["pair"])
            for asset in current["assets"].values():
                for position_id in asset["positions"]:
                    health_decision = view.health.assess(position_id=position_id, now=now)
                    _require(health_decision.allows_live_mutation(now=now) and health_decision.fully_reconciled,
                             "ACCOUNTING_HEALTH", "Every historical/current account position requires fresh exact native health")
            bar = view._bar(order["pair"])
            _require(bar.completed_at == approved_at and bar.completed_at - bar.opened_at == 300
                     and order.get("decision_bar") == bar.completed_at
                     and _time(order["decision_at"]) == _time(order["created_at"]) == approved_at,
                     "FRESH_BAR", "Current completed five-minute boundary and exact decision time required")
            reservation = self._native_reservation(native, order, required=require_native)
            snapshot, member, source_lots, profit, events = self._membership(current, order, now, db=db, base_only=base_only)
            asset = current["assets"][order["pair"]]
            existing_position = asset["positions"][-1]
            if existing_position in view.ledger.list_active_position_ids():
                expected_position = existing_position
            else:
                _require(order["kind"] == "REENTRY"
                         and view.ledger.snapshot(existing_position).status == N.economic_position.EconomicPositionStatus.CLOSED,
                         "POSITION_SCOPE", "Only a fully closed same-asset predecessor can open a successor")
                expected_position = f'{view.config.owner}:{order["pair"]}:native:{len(asset["positions"]) + 1}'
            _require(order["position"] == expected_position, "POSITION_SCOPE", "Native position differs from exact current owner or planned successor")
            cohort = current["cohorts"].get(order["cohort"])
            _require(cohort is not None and cohort["pair"] == order["pair"], "COHORT", "Exact same-asset cohort required")
            root_source = None
            if order["kind"] == "REENTRY":
                signal = self._reentry(db, native, view, order, cohort, now)
                root_source = signal.pop("root_source")
                signal["source_lineage"] = ([] if root_source["root_source"] is not None else
                                            self._source_lineage(db, current, order, cohort))
                base = root_source["base_budget"]
                _require(base > ZERO, "BASE_PRINCIPAL_EMPTY", "No positive same-root source principal; profit-only entries remain unsupported")
                target = base + profit
            else:
                view.now = approved_at  # Current health was checked above; replay the original causal decision.
                # Invoke the unchanged original feasibility implementation. Its
                # owner-cash-only block is the sole allowed replacement; all
                # other guards are reproduced below using actual funded cash.
                if order.get("repair_retry"):
                    evidence = self._repair_retry_rule(db, native, view, order["pair"], approved_at)
                    _require(order["repair_retry"] == evidence["repair_retry"], "RETRY_SCOPE", "Original rung target, prior debits or residual changed")
                else:
                    evidence = OwnedSpotAdapter.repair_feasibility(view, order["pair"], now=approved_at, rung=order.get("repair_rung"))
                    required = {"health", "freshness", "pending_clear", "seed_complete", "rung_due", "profit_clear", "ownership", "liquidity_valid"}
                    _require(all(evidence["guards"].get(key) for key in required), "REPAIR_GUARD", str(evidence))
                    _require(evidence["eligible"] or evidence["reason_code"] == "OWNER_CASH", "REPAIR_GUARD", str(evidence))
                _require(evidence["cohort_id"] == order["cohort"] and evidence["generation"] == order["cohort_generation"]
                         and evidence["next_rung"] == order.get("repair_rung"), "WAITING_COHORT", "Exact selected or cursor-selected repair authority required")
                target = _money(evidence["target"])
                base = target - profit
                _require(base > ZERO, "PROFIT_EXCEEDS_REPAIR_TARGET", "Entitlement leaves no positive base portion; full-double target cannot be changed or clipped")
                signal = {"label": "UNCHANGED_NATIVE_SAME_TARGET_RETRY" if order.get("repair_retry") else "UNCHANGED_NATIVE_FULL_DOUBLE_REPAIR", "repair_feasibility": evidence}
            _require(_money(order["target"]) == target and _money(order["base_budget"]) == base
                     and _money(order["profit_budget"]) == profit and base > ZERO,
                     "FULL_TARGET", "Exact base plus entire spendable allocation profit must equal unchanged full target")
            _require(_money(order["principal"]) == target / (ONE + view.config.fee_rate),
                     "FEE_RESERVE", "Native principal must retain the actual fee-inclusive reserve")
            _require(_money(order["decision_reference"]) == _money(order["reference"]) == bar.close, "REFERENCE_PRICE", "Order reference differs from causal completed close")
            rule = view.config.rules[order["pair"]]
            q = view.config.quantity_for_budget(order["pair"], target, bar.close)
            _require(_money(order["decision_quantity"]) == _money(order["quantity"]) == q and q > 0 and q >= rule.min_base and q * bar.close >= rule.min_quote,
                     "MINIMUM", "Full rounded order fails exact step or venue minimum")
            _require(view.config.all_in(q, bar.close) <= target, "SIZING", "Rounded fees exceed full budget")
            capacity = bar.quote_volume * view.config.participation
            used = _money(current.get("liquidity", {}).get(f'{order["pair"]}:{bar.completed_at}', "0"))
            _require(used + target <= capacity and (order.get("capacity") is None or _money(order["capacity"]) == capacity),
                     "LIQUIDITY", "Full un-clipped target exceeds prior completed-bar shared participation")
            if root_source is not None and root_source["root_source"] is not None:
                _require(root_source["available_base_cash"] >= base, "ROOT_OWNER_CASH", "Exact original-root seed subcash cannot fund the configured base ticket")
            _require(projection.available_base_pools.get(source_pool, ZERO) >= base and projection.profit_escrow >= profit,
                     "OWNER_CASH", "Exact source-owner base cash and settled profit escrow are insufficient")
            _require(projection.cash_pools.get("protected") == D(500)
                     and projection.total_physical_cash - view.holds() - view._unsettled_sales() - target >= D(500),
                     "RESERVE", "Full target breaches protected cash reserve")
            commitments = self._capital_claims(db, view, projection, order, base)
            increment = commitments["increment"]
            _require(commitments["total"] + increment <= D(1500), "TOTAL_CAP", "Full target exceeds total basis/cash-claim/hold cap")
            _require(commitments["pairs"][order["pair"]] + increment <= D(750), "PAIR_CAP", "Full target exceeds per-asset cap")
            _require(funding_owner != "repair" or commitments["recovery"] + increment <= D(500), "RECOVERY_CAP", "Full target exceeds recovery cap")
            binding = {key: order.get(key) for key in ORDER_FIELDS}
            health = [dict(row) for row in native.execute("SELECT * FROM economic_accounting_health ORDER BY position_id")]
            lots = [asdict(lot) for lot in view._lots()]
            receipts = [dict(row) for row in db.execute("SELECT * FROM receipts ORDER BY id")]
            evidence = {"order": binding, "native_reservation": reservation,
                        "paper_identity": current["identity"], "membership": snapshot,
                        "source_lots": source_lots, "source_events": events,
                        "allocation_available": D(self.allocator._amount(sum(item["units"] for item in source_lots))),
                        "allocation_spendable": profit,
                        "allocation_remainder": D(self.allocator._amount(sum(item["units"] for item in source_lots) % self.allocator.quantum)),
                        "cohort": deepcopy(cohort), "asset_authority": deepcopy(current["assets"][order["pair"]]),
                        "signal": signal, "root_source": None if root_source is None else root_source["root_source"],
                        "root_empty_attempts": [] if root_source is None else root_source["empty_attempts"],
                        "root_principal": None if root_source is None else root_source["root_principal"],
                        "root_available_base_cash": None if root_source is None else root_source.get("available_base_cash"), "psm_handback": psm, "completed_bar": asdict(bar),
                        "rule": asdict(rule), "health": health, "owner_lots": lots,
                        "cash_receipts": receipts, "cash_projection": asdict(projection),
                        "capital_commitments": commitments, "prior_liquidity_used": used,
                        "rounded_fee": view.config.fee(q * bar.close)}
            identity = helper_identity()
            proof_id = "entry-admission:" + _hash({"evidence": evidence, "helper_identity": identity})
            expires = min(approved_at + self.allocator.policy.max_proof_age_seconds,
                          approved_at + view.config.max_order_age_seconds, approved_at + 30,
                          snapshot["effective_until"])
            permit = EntryPermit(permit_id=proof_id + ":permit", native_reservation_id=order["proposal"],
                entry_id=entry_id, account_id=view.config.account, currency="USD",
                allocation_id=member["allocation_id"], owner_id=member["owner_id"], role=member["role"],
                proof_id=proof_id, base_entry_budget=base, approved_profit_increment=profit,
                full_fee_inclusive_budget=target, approved_at=approved_at, expires_at=expires,
                native_cash_reserved=reservation is not None, signal_eligible=True, reserve_guard_passed=True,
                minimum_order_guard_passed=True, risk_guard_passed=True, sizing_guard_passed=True,
                balance_guard_passed=True)
            proof = serial({"schema": 1, "proof_id": proof_id, "helper_identity": identity,
                "order_id": order_id, "entry_id": entry_id, "source_pool": source_pool, "source_owner": order.get("source_owner"),
                "entry_pool": order["pool"], "snapshot_id": self.snapshot_id,
                "permit": None if base_only else asdict(permit), "evidence": evidence,
                **({"base_entry": {**asdict(permit), "kind": "BASE_ONLY_NEXT_ENTRY"},
                    "allocator_reservation_required": False} if base_only else {})})
            proof["proof_sha256"] = _hash(proof)
            return proof

    def validate(self, proof, order=None, *, now, replay=False, reconciliation_only=False, state=None, paper_connection=None):
        now = _time(now)
        self._proof_identity(proof)
        _require(not reconciliation_only or replay, "RECONCILIATION_ONLY", "Unaccepted reservation cleanup must use validate_frozen")
        _require(proof.get("snapshot_id") == self.snapshot_id, "MEMBERSHIP", "Proof belongs to another membership")
        with self._paper(paper_connection) as db, self._native() as native:
            current = self._state(db, state)
            current_order = current["orders"].get(proof["order_id"])
            _require(current_order is not None and (order is None or order == current_order),
                     "ORDER_BINDING", "Caller order differs from durable authority")
            binding = {key: current_order.get(key) for key in ORDER_FIELDS}
            _require(binding == proof["evidence"]["order"], "ORDER_BINDING", "Immutable order scope or size changed")
            self._execution_binding(current_order)
            self._replay_root_source(proof, db, now)
            if not replay:
                _require(now <= proof["permit"]["expires_at"], "PROOF_EXPIRED", "Admission proof expired")
                recomputed = self._prove(proof["order_id"], now=now, approval_time=proof["permit"]["approved_at"], state=current, paper_connection=db)
                _require(recomputed == proof, "GUARD_CHANGED", "Current entry predicates differ from frozen proof")
                return deepcopy(proof)
            # Replay is reconciliation of an already accepted reservation, never
            # a route to bypass first-admission guards or extend its lifetime.
            ledger = self.allocator.export_research_state()
            reservations = [row for row in ledger["reservations"].values()
                            if row["request"]["permit"]["entry_id"] == proof["entry_id"]]
            _require(len(reservations) == 1, "REPLAY_UNRESERVED", "Replay requires an actual accepted allocator reservation")
            normalized = self.allocator._record(EntryPermit(**proof["permit"]), EntryPermit)
            _require(reservations[0]["request"] == {"snapshot_id": self.snapshot_id, "permit": normalized},
                     "REPLAY_RESERVATION", "Accepted allocator reservation differs from frozen permit")
            _require(current["identity"] == proof["evidence"]["paper_identity"]
                     and ledger["snapshots"].get(self.snapshot_id) == proof["evidence"]["membership"],
                     "REPLAY_SCOPE", "Original configuration or membership changed")
            _require(self._native_reservation(native, current_order, replay=True) == proof["evidence"]["native_reservation"],
                     "REPLAY_NATIVE", "Original native proposal changed")
            for event_id, event in proof["evidence"]["source_events"].items():
                _require(ledger["events"].get(event_id) == event, "REPLAY_SOURCE", "Original settled source attribution changed")
            for receipt in proof["evidence"]["cash_receipts"]:
                row = db.execute("SELECT * FROM receipts WHERE id=?", (receipt["id"],)).fetchone()
                _require(row is not None and dict(row) == receipt, "REPLAY_SOURCE", "Original paper cash receipt changed")
            if reconciliation_only:
                return {"order_permission": False, "reservation_permission": False,
                        "entry_id": proof["entry_id"], "order_id": proof["order_id"],
                        "proof_id": proof["proof_id"], "proof_sha256": proof["proof_sha256"],
                        "purpose": "ACCEPTED_RESERVATION_RECONCILIATION_ONLY"}
            if current_order["status"] == "FUNDING_PREPARED":
                _require(now <= proof["permit"]["expires_at"], "PROOF_EXPIRED", "Expired prepared entry cannot become executable on replay")
            return deepcopy(proof)
