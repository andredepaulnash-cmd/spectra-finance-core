"""Isolated paper-research entitlement ledger. This module cannot place orders.

Native receipts remain the cash authority. The immutable inputs below are
attestations from a future reviewed adapter, not independently verified evidence.
All arithmetic uses integer accounting atoms with exact Decimal boundaries.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable


class Blocked(ValueError):
    """Fail-closed rejection; the transaction has not changed the ledger."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise Blocked(f"{name}: nonempty, trimmed identifier of at most 256 characters required")
    return value


def _time(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise Blocked(f"{name}: nonnegative integer UTC epoch seconds required")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, str, int)):
        raise Blocked(f"{name}: exact Decimal, decimal string or integer required")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise Blocked(f"{name}: invalid decimal") from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 80:
        raise Blocked(f"{name}: finite decimal with at most 80 digits required")
    return result


def _quantum_exponent(value: object, name: str) -> int:
    value = _decimal(value, name)
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if sign or digits != [1] or not -30 <= exponent <= 0:
        raise Blocked(f"{name}: a power of ten between 1 and 1e-30 required")
    return exponent


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    accounting_quantum: Decimal
    allocation_quantum: Decimal
    max_profit_age_seconds: int
    max_proof_age_seconds: int


@dataclass(frozen=True)
class Member:
    allocation_id: str
    owner_id: str
    role: str  # POSITION_ALLOCATION or VCAP; every member receives one equal share.


@dataclass(frozen=True)
class MembershipSnapshot:
    snapshot_id: str
    account_id: str
    currency: str
    effective_from: int
    effective_until: int
    members: tuple[Member, ...]


@dataclass(frozen=True)
class SettledProfitEvent:
    event_id: str
    account_id: str
    currency: str
    source_owner_id: str
    source_realization_ids: tuple[str, ...]
    cash_backing_id: str
    proof_id: str
    gross_realized_proceeds: Decimal
    returned_principal: Decimal  # Realized cost basis, excluding allocated entry fees.
    total_attributable_fees: Decimal  # Allocated entry fees plus all exit/settlement costs.
    fee_net_realized_profit: Decimal
    earmarked_profit_cash: Decimal
    settled_at: int
    verified_at: int
    expires_at: int
    confirmed: bool
    settled: bool
    final: bool
    verified: bool
    kind: str = "REALIZED_SETTLED_PROFIT"


@dataclass(frozen=True)
class EntryPermit:
    permit_id: str
    native_reservation_id: str
    entry_id: str
    account_id: str
    currency: str
    allocation_id: str
    owner_id: str
    role: str
    proof_id: str
    base_entry_budget: Decimal
    approved_profit_increment: Decimal
    full_fee_inclusive_budget: Decimal
    approved_at: int
    expires_at: int
    native_cash_reserved: bool
    signal_eligible: bool
    reserve_guard_passed: bool
    minimum_order_guard_passed: bool
    risk_guard_passed: bool
    sizing_guard_passed: bool
    balance_guard_passed: bool
    kind: str = "NEXT_ELIGIBLE_ENTRY"
    held_position_topup: bool = False


@dataclass(frozen=True)
class CommitProof:
    proof_id: str
    native_reservation_id: str
    entry_id: str
    account_id: str
    currency: str
    allocation_id: str
    owner_id: str
    settlement_ids: tuple[str, ...]
    profit_cash_used: Decimal
    base_cash_used: Decimal
    total_fee_inclusive_cash_debit: Decimal
    settled_at: int
    verified_at: int
    expires_at: int
    confirmed: bool
    settled: bool
    verified: bool


@dataclass(frozen=True)
class ReleaseProof:
    proof_id: str
    native_reservation_id: str
    entry_id: str
    account_id: str
    currency: str
    allocation_id: str
    owner_id: str
    verified_at: int
    expires_at: int
    native_reservation_closed: bool
    no_unreconciled_fills: bool
    no_remaining_order_liability: bool
    verified: bool


_MONEY_FIELDS = {
    "gross_realized_proceeds", "returned_principal", "total_attributable_fees",
    "fee_net_realized_profit", "earmarked_profit_cash", "base_entry_budget",
    "approved_profit_increment", "full_fee_inclusive_budget", "profit_cash_used",
    "base_cash_used", "total_fee_inclusive_cash_debit",
}
_TIME_FIELDS = {
    "effective_from", "effective_until", "settled_at", "verified_at",
    "expires_at", "approved_at",
}
_BOOL_FIELDS = {
    "confirmed", "settled", "final", "verified", "native_cash_reserved",
    "signal_eligible", "reserve_guard_passed", "minimum_order_guard_passed",
    "risk_guard_passed", "sizing_guard_passed", "balance_guard_passed",
    "held_position_topup", "native_reservation_closed", "no_unreconciled_fills",
    "no_remaining_order_liability",
}


class Allocator:
    """Single-account, single-currency local ledger; never a cash balance API.

    Every mutation and its idempotency receipt commit in one SQLite transaction.
    ``fault_hook`` exists only for deterministic crash tests. Unknown outcomes
    are recovered by replaying the identical operation ID and payload.
    """

    def __init__(self, path: str | Path, *, account_id: str, currency: str,
                 policy: Policy, fault_hook: Callable[[str], None] | None = None):
        self.path = Path(path)
        self.account_id = _text(account_id, "account_id")
        self.currency = _text(currency, "currency")
        if not isinstance(policy, Policy):
            raise Blocked("Policy required")
        self.exponent = _quantum_exponent(policy.accounting_quantum, "accounting_quantum")
        allocation_exponent = _quantum_exponent(policy.allocation_quantum, "allocation_quantum")
        if allocation_exponent < self.exponent:
            raise Blocked("allocation quantum cannot be smaller than accounting quantum")
        self.quantum = 10 ** (allocation_exponent - self.exponent)
        for name in ("max_profit_age_seconds", "max_proof_age_seconds"):
            if _time(getattr(policy, name), name) == 0:
                raise Blocked(f"{name}: positive duration required")
        self.policy = policy
        self.fault_hook = fault_hook
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "schema_version": 1, "account_id": self.account_id, "currency": self.currency,
            "accounting_exponent": self.exponent, "allocation_quantum_atoms": self.quantum,
            "max_profit_age_seconds": policy.max_profit_age_seconds,
            "max_proof_age_seconds": policy.max_proof_age_seconds,
        }
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK (id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, receipt TEXT NOT NULL
                );
            """)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT body FROM metadata WHERE id=1").fetchone()
            if existing is None:
                conn.execute("INSERT INTO metadata VALUES (1, ?)", (_canonical(config),))
                conn.execute("INSERT INTO state VALUES (1, ?)", (_canonical(self._empty_state()),))
            elif existing[0] != _canonical(config):
                raise Blocked("Existing database configuration does not match; no implicit migration")
            self._assert_invariants(self._load(conn))
            conn.commit()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    @staticmethod
    def _empty_state():
        return {"sequence": 0, "last_now": 0, "snapshots": {}, "events": {},
                "buckets": {}, "carry": {}, "reservations": {}, "commits": {},
                "source_ids": {}, "backing_ids": {}, "permit_ids": {},
                "native_reservation_ids": {}, "entry_ids": {}, "settlement_ids": {}, "proof_ids": {}}

    @staticmethod
    def _load(conn):
        row = conn.execute("SELECT body FROM state WHERE id=1").fetchone()
        if row is None:
            raise Blocked("Missing persisted state")
        try:
            return json.loads(row[0])
        except (ValueError, TypeError) as exc:
            raise Blocked("Corrupt persisted state") from exc

    def _units(self, value: object, name: str, *, positive=False) -> int:
        value = _decimal(value, name)
        sign, digits, exponent = value.as_tuple()
        coefficient = int("".join(map(str, digits)))
        shift = exponent - self.exponent
        if abs(shift) > 100:
            raise Blocked(f"{name}: unsupported decimal magnitude")
        if shift < 0:
            coefficient, remainder = divmod(coefficient, 10 ** -shift)
            if remainder:
                raise Blocked(f"{name}: finer than accounting quantum; rounding forbidden")
        else:
            coefficient *= 10 ** shift
        if (sign and coefficient) or (positive and coefficient == 0):
            raise Blocked(f"{name}: {'positive' if positive else 'nonnegative'} amount required")
        return coefficient

    def _amount(self, units: int) -> str:
        return format(Decimal((0, tuple(map(int, str(units))), self.exponent)), "f")

    def _record(self, value, cls):
        if not isinstance(value, cls):
            raise Blocked(f"{cls.__name__} required")
        result = {}
        for field in fields(value):
            name, item = field.name, getattr(value, field.name)
            if name in _MONEY_FIELDS:
                result[name] = self._units(item, name)
            elif name in _TIME_FIELDS:
                result[name] = _time(item, name)
            elif name in _BOOL_FIELDS:
                if type(item) is not bool:
                    raise Blocked(f"{name}: explicit boolean required")
                result[name] = item
            elif name in ("source_realization_ids", "settlement_ids"):
                if not isinstance(item, tuple) or not item:
                    raise Blocked(f"{name}: immutable nonempty tuple required")
                result[name] = sorted(_text(v, name) for v in item)
                if len(set(result[name])) != len(result[name]):
                    raise Blocked(f"{name}: duplicate source identifier")
            else:
                result[name] = _text(item, name)
        return result

    def _snapshot(self, value: MembershipSnapshot):
        if not isinstance(value, MembershipSnapshot) or not isinstance(value.members, tuple) or not value.members:
            raise Blocked("Immutable nonempty membership snapshot required")
        members = [self._record(member, Member) for member in value.members]
        if len({member["allocation_id"] for member in members}) != len(members):
            raise Blocked("Duplicate allocation ID in membership snapshot")
        if any(member["role"] not in {"POSITION_ALLOCATION", "VCAP"} for member in members):
            raise Blocked("Unrecognized member role")
        if sum(member["role"] == "VCAP" for member in members) != 1:
            raise Blocked("Exactly one explicit VCAP member required")
        if not any(member["role"] == "POSITION_ALLOCATION" for member in members):
            raise Blocked("At least one explicit position-allocation member required")
        result = {name: _text(getattr(value, name), name)
                  for name in ("snapshot_id", "account_id", "currency")}
        result.update(effective_from=_time(value.effective_from, "effective_from"),
                      effective_until=_time(value.effective_until, "effective_until"),
                      members=sorted(members, key=lambda m: m["allocation_id"]))
        if result["effective_until"] < result["effective_from"]:
            raise Blocked("Invalid membership effective interval")
        self._scope(result)
        return result

    def _scope(self, item):
        if item["account_id"] != self.account_id or item["currency"] != self.currency:
            raise Blocked("Account or currency mismatch")

    def _fresh(self, proof, now, *, timestamp="verified_at"):
        observed = proof[timestamp]
        if not observed <= now <= proof["expires_at"]:
            raise Blocked("Future or expired proof")
        if now - observed > self.policy.max_proof_age_seconds:
            raise Blocked("Stale proof")

    @staticmethod
    def _require_true(item, *names):
        if any(item[name] is not True for name in names):
            raise Blocked("Unconfirmed or invalid evidence: " + ", ".join(names))

    @staticmethod
    def _claim_proof(state, proof_id, kind, identity):
        if proof_id in state["proof_ids"]:
            raise Blocked("Evidence proof ID has already been used by another economic mutation")
        state["proof_ids"][proof_id] = {"kind": kind, "identity": identity}

    def _mutate(self, operation_id, payload, now, mutation):
        operation_id = _text(operation_id, "operation_id")
        now = _time(now, "now")
        encoded = _canonical(payload)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._load(conn)
            self._assert_invariants(state)
            previous = conn.execute("SELECT payload, receipt FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise Blocked("Idempotency conflict: operation ID reused with a changed payload")
                return json.loads(previous[1])
            if now < state["last_now"]:
                raise Blocked("Logical clock moved backwards; new mutations fail closed")
            state["sequence"] += 1
            result = mutation(state, now)
            state["last_now"] = now
            self._assert_invariants(state)
            result.update(operation_id=operation_id, sequence=state["sequence"])
            if self.fault_hook:
                self.fault_hook("before_write")
            conn.execute("UPDATE state SET body=? WHERE id=1", (_canonical(state),))
            conn.execute("INSERT INTO operations VALUES (?, ?, ?)", (operation_id, encoded, _canonical(result)))
            if self.fault_hook:
                self.fault_hook("after_write_before_commit")
            conn.commit()
            if self.fault_hook:
                self.fault_hook("after_commit")
            return result

    @staticmethod
    def _total(lots):
        return sum(lot["units"] for lot in lots)

    @staticmethod
    def _take(lots, amount):
        if amount <= 0 or Allocator._total(lots) < amount:
            raise Blocked("Insufficient entitlement")
        taken = []
        while amount:
            first = lots[0]
            used = min(first["units"], amount)
            taken.append({**first, "units": used})
            first["units"] -= used
            amount -= used
            if first["units"] == 0:
                lots.pop(0)
        return taken

    @staticmethod
    def _bucket_key(snapshot_id, allocation_id):
        return _canonical([snapshot_id, allocation_id])

    def _member(self, state, snapshot_id, allocation_id):
        if snapshot_id not in state["snapshots"]:
            raise Blocked("Unknown membership snapshot")
        matches = [m for m in state["snapshots"][snapshot_id]["members"] if m["allocation_id"] == allocation_id]
        if len(matches) != 1:
            raise Blocked("Allocation is not a member of that snapshot")
        return matches[0]

    def apply_profit(self, *, operation_id: str, snapshot: MembershipSnapshot,
                     event: SettledProfitEvent, now: int):
        membership = self._snapshot(snapshot)
        profit = self._record(event, SettledProfitEvent)
        self._scope(profit)

        def mutate(state, clock):
            event_id, sid = profit["event_id"], membership["snapshot_id"]
            # Event identity is independently idempotent across different delivery IDs.
            if event_id in state["events"]:
                previous = state["events"][event_id]
                if previous["event"] != profit or previous["snapshot"] != membership:
                    raise Blocked("Profit event ID reused with changed content or membership")
                return dict(previous["receipt"])
            self._require_true(profit, "confirmed", "settled", "final", "verified")
            self._fresh(profit, clock)
            if profit["kind"] != "REALIZED_SETTLED_PROFIT":
                raise Blocked("Only settled fee-net realized profit is eligible")
            if not membership["effective_from"] <= profit["settled_at"] <= membership["effective_until"]:
                raise Blocked("Event outside the supplied membership interval")
            if not profit["settled_at"] <= profit["verified_at"] or clock - profit["settled_at"] > self.policy.max_profit_age_seconds:
                raise Blocked("Future or stale settlement")
            amount = profit["fee_net_realized_profit"]
            if amount <= 0:
                raise Blocked("Zero or negative realized profit creates no entitlement")
            if profit["gross_realized_proceeds"] - profit["returned_principal"] - profit["total_attributable_fees"] != amount:
                raise Blocked("Fee-net profit does not reconcile with realized proceeds, principal and fees")
            if profit["earmarked_profit_cash"] != amount:
                raise Blocked("Cash backing must exactly equal the eligible fee-net profit")
            if profit["cash_backing_id"] in state["backing_ids"]:
                raise Blocked("Profit cash backing already consumed by another event")
            if any(source in state["source_ids"] for source in profit["source_realization_ids"]):
                raise Blocked("Overlapping realization source; refusing duplicate or cumulative profit")
            if sid in state["snapshots"] and state["snapshots"][sid] != membership:
                raise Blocked("Membership snapshot ID is immutable")
            self._claim_proof(state, profit["proof_id"], "PROFIT", event_id)
            state["snapshots"][sid] = membership
            carry = state["carry"].setdefault(sid, [])
            old_carry = self._total(carry)
            carry.append({"event_id": event_id, "units": amount})
            member_count = len(membership["members"])
            each = (old_carry + amount) // (member_count * self.quantum) * self.quantum
            grants = []
            for member in membership["members"]:
                key = self._bucket_key(sid, member["allocation_id"])
                bucket = state["buckets"].setdefault(key, [])
                lots = self._take(carry, each) if each else []
                for lot in lots:
                    lot["grant_event_id"] = event_id
                bucket.extend(lots)
                grants.append({**member, "amount": self._amount(each), "source_lots": self._display_lots(lots)})
            receipt = {"action": "APPLY_PROFIT", "event_id": event_id, "snapshot_id": sid,
                       "eligible_profit": self._amount(amount), "returned_principal_excluded": self._amount(profit["returned_principal"]),
                       "carry_before": self._amount(old_carry), "carry_after": self._amount(self._total(carry)),
                       "equal_share": self._amount(each), "member_count": member_count,
                       "grants": grants, "cash_created": "0",
                       "input_fingerprint": _hash({"snapshot": membership, "event": profit})}
            state["events"][event_id] = {"event": profit, "snapshot": membership, "receipt": receipt,
                                         "sequence": state["sequence"], "applied_at": clock}
            state["backing_ids"][profit["cash_backing_id"]] = event_id
            for source in profit["source_realization_ids"]:
                state["source_ids"][source] = event_id
            return dict(receipt)

        return self._mutate(operation_id, {"action": "APPLY_PROFIT", "snapshot": membership, "event": profit}, now, mutate)

    def reserve(self, *, operation_id: str, reservation_id: str, snapshot_id: str,
                permit: EntryPermit, now: int):
        reservation_id = _text(reservation_id, "reservation_id")
        snapshot_id = _text(snapshot_id, "snapshot_id")
        entry = self._record(permit, EntryPermit)
        self._scope(entry)

        def mutate(state, clock):
            request = {"snapshot_id": snapshot_id, "permit": entry}
            if reservation_id in state["reservations"]:
                previous = state["reservations"][reservation_id]
                if previous["request"] != request:
                    raise Blocked("Reservation ID reused with changed request")
                return dict(previous["reserve_receipt"])
            member = self._member(state, snapshot_id, entry["allocation_id"])
            if entry["owner_id"] != member["owner_id"] or entry["role"] != member["role"]:
                raise Blocked("Entry owner or role does not match immutable entitlement owner")
            self._fresh(entry, clock, timestamp="approved_at")
            self._require_true(entry, "native_cash_reserved", "signal_eligible", "reserve_guard_passed",
                               "minimum_order_guard_passed", "risk_guard_passed", "sizing_guard_passed", "balance_guard_passed")
            if entry["kind"] != "NEXT_ELIGIBLE_ENTRY" or entry["held_position_topup"]:
                raise Blocked("Only a next eligible entry may use an entitlement; held-position top-ups are forbidden")
            amount = entry["approved_profit_increment"]
            if amount <= 0 or amount % self.quantum:
                raise Blocked("Positive allocation-quantum increment required")
            if entry["base_entry_budget"] <= 0:
                raise Blocked("A native independently eligible base entry is required; no profit-only orders")
            if entry["base_entry_budget"] + amount != entry["full_fee_inclusive_budget"]:
                raise Blocked("Native full fee-inclusive budget does not equal base plus profit increment")
            for index, field in (("permit_ids", "permit_id"), ("native_reservation_ids", "native_reservation_id"), ("entry_ids", "entry_id")):
                if entry[field] in state[index]:
                    raise Blocked(f"{field} already used by another entitlement reservation")
            key = self._bucket_key(snapshot_id, entry["allocation_id"])
            lots = self._take(state["buckets"].get(key, []), amount)
            if any(state["events"][lot["grant_event_id"]]["applied_at"] >= entry["approved_at"] for lot in lots):
                raise Blocked("Native authorization must strictly follow each entitlement grant; carry source time is insufficient")
            self._claim_proof(state, entry["proof_id"], "RESERVE", reservation_id)
            receipt = {"action": "RESERVE", "reservation_id": reservation_id, "entry_id": entry["entry_id"],
                       "snapshot_id": snapshot_id, **member, "amount": self._amount(amount),
                       "source_lots": self._display_lots(lots), "order_permission": False}
            state["reservations"][reservation_id] = {"request": request, "reserve_receipt": receipt,
                                                     "remaining_lots": lots, "committed_units": 0, "released_units": 0,
                                                     "status": "RESERVED", "reserved_at": clock,
                                                     "base_committed_units": 0, "last_reconciled_at": entry["approved_at"]}
            for index, field in (("permit_ids", "permit_id"), ("native_reservation_ids", "native_reservation_id"), ("entry_ids", "entry_id")):
                state[index][entry[field]] = reservation_id
            return dict(receipt)

        return self._mutate(operation_id, {"action": "RESERVE", "reservation_id": reservation_id,
                                           "snapshot_id": snapshot_id, "permit": entry}, now, mutate)

    def _reservation_match(self, state, reservation_id, proof):
        if reservation_id not in state["reservations"]:
            raise Blocked("Unknown reservation; commit/release cannot precede reserve")
        reservation = state["reservations"][reservation_id]
        entry = reservation["request"]["permit"]
        for name in ("native_reservation_id", "entry_id", "account_id", "currency", "allocation_id", "owner_id"):
            if proof[name] != entry[name]:
                raise Blocked(f"Proof {name} does not match reservation")
        return reservation

    def commit(self, *, operation_id: str, reservation_id: str, proof: CommitProof, now: int):
        reservation_id = _text(reservation_id, "reservation_id")
        debit = self._record(proof, CommitProof)
        self._scope(debit)

        def mutate(state, clock):
            reservation = self._reservation_match(state, reservation_id, debit)
            if reservation["status"] != "RESERVED":
                raise Blocked("Reservation is no longer open; reconcile any late fill externally")
            self._require_true(debit, "confirmed", "settled", "verified")
            self._fresh(debit, clock)
            if not reservation["reserved_at"] <= debit["settled_at"] <= debit["verified_at"]:
                raise Blocked("Settlement precedes reservation or follows verification")
            if clock - debit["settled_at"] > self.policy.max_proof_age_seconds:
                raise Blocked("Stale entry settlement")
            if any(sid in state["settlement_ids"] for sid in debit["settlement_ids"]):
                raise Blocked("Entry settlement already accounted for")
            amount = debit["profit_cash_used"]
            if amount <= 0 or debit["base_cash_used"] + amount != debit["total_fee_inclusive_cash_debit"]:
                raise Blocked("Invalid fee-inclusive debit attribution")
            entry = reservation["request"]["permit"]
            if reservation["base_committed_units"] + debit["base_cash_used"] > entry["base_entry_budget"]:
                raise Blocked("Base cash debit exceeds the native permitted base budget")
            lots = self._take(reservation["remaining_lots"], amount)
            self._claim_proof(state, debit["proof_id"], "COMMIT", operation_id)
            reservation["committed_units"] += amount
            reservation["base_committed_units"] += debit["base_cash_used"]
            reservation["last_reconciled_at"] = max(reservation["last_reconciled_at"], debit["verified_at"])
            if not reservation["remaining_lots"]:
                reservation["status"] = "COMMITTED"
            state["commits"][operation_id] = {"reservation_id": reservation_id, "proof": debit, "lots": lots}
            for sid in debit["settlement_ids"]:
                state["settlement_ids"][sid] = operation_id
            return {"action": "COMMIT", "reservation_id": reservation_id, "amount": self._amount(amount),
                    "remaining_reserved": self._amount(self._total(reservation["remaining_lots"])),
                    "source_lots": self._display_lots(lots)}

        return self._mutate(operation_id, {"action": "COMMIT", "reservation_id": reservation_id, "proof": debit}, now, mutate)

    def release(self, *, operation_id: str, reservation_id: str, proof: ReleaseProof, now: int):
        reservation_id = _text(reservation_id, "reservation_id")
        evidence = self._record(proof, ReleaseProof)
        self._scope(evidence)

        def mutate(state, clock):
            reservation = self._reservation_match(state, reservation_id, evidence)
            if reservation["status"] != "RESERVED":
                raise Blocked("Reservation is no longer open")
            self._require_true(evidence, "native_reservation_closed", "no_unreconciled_fills", "no_remaining_order_liability", "verified")
            self._fresh(evidence, clock)
            if evidence["verified_at"] < reservation["reserved_at"]:
                raise Blocked("Release proof predates reservation")
            if evidence["verified_at"] < reservation["last_reconciled_at"] or (
                    reservation["committed_units"] and evidence["verified_at"] == reservation["last_reconciled_at"]):
                raise Blocked("Release proof must follow the latest reconciled debit evidence; same-second ordering is ambiguous")
            self._claim_proof(state, evidence["proof_id"], "RELEASE", reservation_id)
            amount = self._total(reservation["remaining_lots"])
            request = reservation["request"]
            key = self._bucket_key(request["snapshot_id"], request["permit"]["allocation_id"])
            bucket = state["buckets"][key]
            bucket.extend(reservation["remaining_lots"])
            bucket.sort(key=lambda lot: (state["events"][lot["grant_event_id"]]["sequence"],
                                         state["events"][lot["event_id"]]["sequence"]))
            reservation["remaining_lots"] = []
            reservation["released_units"] += amount
            reservation["status"] = "RELEASED"
            reservation["release_proof"] = evidence
            return {"action": "RELEASE", "reservation_id": reservation_id, "amount": self._amount(amount),
                    "allocation_id": request["permit"]["allocation_id"], "owner_id": request["permit"]["owner_id"]}

        return self._mutate(operation_id, {"action": "RELEASE", "reservation_id": reservation_id, "proof": evidence}, now, mutate)

    def _display_lots(self, lots):
        return [{"event_id": lot["event_id"], "amount": self._amount(lot["units"]),
                 **({"grant_event_id": lot["grant_event_id"]} if "grant_event_id" in lot else {})} for lot in lots]

    def _assert_invariants(self, state):
        """Reconstruct equal grants; conserve each source event and each beneficiary."""
        expected = {eid: row["event"]["fee_net_realized_profit"] for eid, row in state["events"].items()}
        observed = {eid: 0 for eid in expected}
        expected_by_owner, observed_by_owner = {}, {}
        expected_proofs, expected_sources, expected_backing = {}, {}, {}
        expected_permits, expected_entries, expected_native_reservations, expected_settlements = {}, {}, {}, {}
        reconstructed_carry = {}

        def claim(index, key, value):
            if key in index:
                raise Blocked("Duplicate persisted economic proof or source identifier")
            index[key] = value

        # Reconstruct original destination attribution, independent of current balances.
        for eid, row in sorted(state["events"].items(), key=lambda item: item[1]["sequence"]):
            event, membership = row["event"], row["snapshot"]
            sid = membership["snapshot_id"]
            if state["snapshots"].get(sid) != membership:
                raise Blocked("Persisted membership changed after allocation")
            if event["event_id"] != eid or event["fee_net_realized_profit"] <= 0 or event["earmarked_profit_cash"] != event["fee_net_realized_profit"]:
                raise Blocked("Persisted event cash backing mismatch")
            if event["gross_realized_proceeds"] - event["returned_principal"] - event["total_attributable_fees"] != event["fee_net_realized_profit"]:
                raise Blocked("Persisted event no longer reconciles")
            claim(expected_proofs, event["proof_id"], {"kind": "PROFIT", "identity": eid})
            claim(expected_backing, event["cash_backing_id"], eid)
            for source_id in event["source_realization_ids"]:
                claim(expected_sources, source_id, eid)
            carry = reconstructed_carry.setdefault(sid, [])
            prior_carry = self._total(carry)
            carry.append({"event_id": eid, "units": event["fee_net_realized_profit"]})
            share = self._total(carry) // (len(membership["members"]) * self.quantum) * self.quantum
            reconstructed_grants = []
            for member in membership["members"]:
                lots = self._take(carry, share) if share else []
                for lot in lots:
                    lot["grant_event_id"] = eid
                    key = (sid, member["allocation_id"], lot["event_id"], eid)
                    expected_by_owner[key] = expected_by_owner.get(key, 0) + lot["units"]
                reconstructed_grants.append({**member, "amount": self._amount(share), "source_lots": self._display_lots(lots)})
            receipt = row["receipt"]
            if receipt["grants"] != reconstructed_grants or receipt["carry_before"] != self._amount(prior_carry) or receipt["carry_after"] != self._amount(self._total(carry)):
                raise Blocked("Equal-share source/destination attribution no longer reproduces")
            if receipt["input_fingerprint"] != _hash({"snapshot": membership, "event": event}):
                raise Blocked("Immutable event fingerprint mismatch")
        if reconstructed_carry != state["carry"]:
            raise Blocked("Residual carry source attribution changed")

        def count(lots, snapshot_id=None, allocation_id=None):
            for lot in lots:
                if type(lot["units"]) is not int or lot["units"] <= 0 or lot["event_id"] not in expected:
                    raise Blocked("Corrupt or negative source lot")
                if snapshot_id is not None and state["events"][lot["event_id"]]["snapshot"]["snapshot_id"] != snapshot_id:
                    raise Blocked("Cash attribution crossed membership snapshots")
                observed[lot["event_id"]] += lot["units"]
                if allocation_id is not None:
                    if lot.get("grant_event_id") not in state["events"]:
                        raise Blocked("Missing entitlement grant provenance")
                    key = (snapshot_id, allocation_id, lot["event_id"], lot["grant_event_id"])
                    observed_by_owner[key] = observed_by_owner.get(key, 0) + lot["units"]

        for sid, lots in state["carry"].items():
            count(lots, sid)
            if self._total(lots) >= len(state["snapshots"][sid]["members"]) * self.quantum:
                raise Blocked("Carry exceeds the equal-share rounding bound")
        for key, lots in state["buckets"].items():
            sid, aid = json.loads(key)
            self._member(state, sid, aid)
            count(lots, sid, aid)
        committed_by_reservation = {rid: 0 for rid in state["reservations"]}
        base_by_reservation = {rid: 0 for rid in state["reservations"]}
        horizon_by_reservation = {rid: row["request"]["permit"]["approved_at"] for rid, row in state["reservations"].items()}
        for commit_id, row in state["commits"].items():
            rid = row["reservation_id"]
            reservation = state["reservations"][rid]
            proof = row["proof"]
            self._reservation_match(state, rid, proof)
            self._require_true(proof, "confirmed", "settled", "verified")
            if self._total(row["lots"]) != proof["profit_cash_used"] or proof["base_cash_used"] + proof["profit_cash_used"] != proof["total_fee_inclusive_cash_debit"]:
                raise Blocked("Commit proof cash debit attribution changed")
            if not reservation["reserved_at"] <= proof["settled_at"] <= proof["verified_at"]:
                raise Blocked("Commit proof chronology changed")
            count(row["lots"], reservation["request"]["snapshot_id"], reservation["request"]["permit"]["allocation_id"])
            committed_by_reservation[rid] += self._total(row["lots"])
            base_by_reservation[rid] += proof["base_cash_used"]
            horizon_by_reservation[rid] = max(horizon_by_reservation[rid], proof["verified_at"])
            claim(expected_proofs, proof["proof_id"], {"kind": "COMMIT", "identity": commit_id})
            for settlement_id in proof["settlement_ids"]:
                claim(expected_settlements, settlement_id, commit_id)
        for rid, row in state["reservations"].items():
            request = row["request"]
            member = self._member(state, request["snapshot_id"], request["permit"]["allocation_id"])
            if member["owner_id"] != request["permit"]["owner_id"] or member["role"] != request["permit"]["role"]:
                raise Blocked("Reservation owner attribution mismatch")
            count(row["remaining_lots"], request["snapshot_id"], request["permit"]["allocation_id"])
            claim(expected_proofs, request["permit"]["proof_id"], {"kind": "RESERVE", "identity": rid})
            if "release_proof" in row:
                release = row["release_proof"]
                self._reservation_match(state, rid, release)
                self._require_true(release, "native_reservation_closed", "no_unreconciled_fills", "no_remaining_order_liability", "verified")
                if release["verified_at"] < max(row["reserved_at"], horizon_by_reservation[rid]) or (row["committed_units"] and release["verified_at"] == horizon_by_reservation[rid]):
                    raise Blocked("Release evidence no longer follows latest debit")
                claim(expected_proofs, release["proof_id"], {"kind": "RELEASE", "identity": rid})
            claim(expected_permits, request["permit"]["permit_id"], rid)
            claim(expected_entries, request["permit"]["entry_id"], rid)
            claim(expected_native_reservations, request["permit"]["native_reservation_id"], rid)
            if min(row["committed_units"], row["released_units"], row["base_committed_units"]) < 0:
                raise Blocked("Negative reservation accounting")
            if row["committed_units"] != committed_by_reservation[rid]:
                raise Blocked("Reservation commit total disagrees with source cash ledger")
            if row["base_committed_units"] != base_by_reservation[rid] or row["base_committed_units"] > request["permit"]["base_entry_budget"]:
                raise Blocked("Reservation base-cash budget or debit total mismatch")
            if row["last_reconciled_at"] != horizon_by_reservation[rid]:
                raise Blocked("Reservation evidence horizon mismatch")
            if self._total(row["remaining_lots"]) + row["committed_units"] + row["released_units"] != request["permit"]["approved_profit_increment"]:
                raise Blocked("Reservation amount does not conserve")
            if row["status"] not in {"RESERVED", "COMMITTED", "RELEASED"}:
                raise Blocked("Invalid reservation status")
            if (row["status"] == "RESERVED") != bool(row["remaining_lots"]):
                raise Blocked("Reservation status disagrees with remaining cash")
        if expected != observed:
            raise Blocked("Per-event profit cash conservation failed")
        if expected_by_owner != observed_by_owner:
            raise Blocked("Per-beneficiary source cash conservation failed")
        if expected_backing != state["backing_ids"] or expected_sources != state["source_ids"] or expected_proofs != state["proof_ids"]:
            raise Blocked("Proof, source or cash backing uniqueness index failed")
        if (expected_permits != state["permit_ids"] or expected_entries != state["entry_ids"]
                or expected_native_reservations != state["native_reservation_ids"] or expected_settlements != state["settlement_ids"]):
            raise Blocked("Native reservation or entry-settlement uniqueness index failed")

    def audit(self):
        """Return entitlement totals only; these are not additional spendable cash."""
        with self._connection() as conn:
            state = self._load(conn)
            self._assert_invariants(state)
        total = sum(row["event"]["fee_net_realized_profit"] for row in state["events"].values())
        available = sum(self._total(lots) for lots in state["buckets"].values())
        carry = sum(self._total(lots) for lots in state["carry"].values())
        reserved = sum(self._total(row["remaining_lots"]) for row in state["reservations"].values())
        committed = sum(self._total(row["lots"]) for row in state["commits"].values())
        return {"account_id": self.account_id, "currency": self.currency, "sequence": state["sequence"],
                "eligible_profit": self._amount(total), "available_entitlements": self._amount(available),
                "carry": self._amount(carry), "reserved_entitlements": self._amount(reserved),
                "committed_profit_cash": self._amount(committed), "cash_created": "0",
                "conserved": total == available + carry + reserved + committed,
                "event_count": len(state["events"]),
                "balances": [{"snapshot_id": json.loads(key)[0], "allocation_id": json.loads(key)[1],
                              "owner_id": self._member(state, *json.loads(key))["owner_id"],
                              "available": self._amount(self._total(lots))}
                             for key, lots in sorted(state["buckets"].items())],
                "reservations": {rid: row["status"] for rid, row in sorted(state["reservations"].items())},
                "state_digest": _hash(state)}

    def export_research_state(self):
        """Detached audit copy, including all source and destination attribution."""
        with self._connection() as conn:
            state = self._load(conn)
            self._assert_invariants(state)
            return state
