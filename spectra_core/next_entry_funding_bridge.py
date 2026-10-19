"""Default-off paper funding saga over the frozen native settled-profit verifier.

No order is submitted here. FUNDING_PREPARED remains non-executable until the
native admission, allocator reservation and balanced paper cash funding all agree.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3

from .next_entry_allocator import CommitProof, EntryPermit, ReleaseProof
from .next_entry_profit_bridge import (
    ESCROW_POOL, NativeSettledProfitBridge, ProfitBridgeBlocked,
    _check, _decimal, _digest, _identifier, _json, _time,
)

V1_SHA256 = "9e675449f12d771c8825af08ecc591e513e4b5f10ebc3fa892f285e724aefc6a"
PSM_HELPER_SHA256 = "6441b0d99b135a9b483f6722b7b72416b869428a49ed10811d72e40a73521cd2"
SETTLEMENT_HELPER_SHA256 = "38713edc5965caa40a22bee0e20c3d32a37109b596784d2e282c1426a5048a7b"
ZERO = Decimal(0)


class NativeEntryFundingBridge(NativeSettledProfitBridge):
    """Reviewed-code hooks only; no arbitrary callback can approve a permit.

    The owning adapter must retain its single-writer lock, refresh its in-memory
    state after bridge writes, and run its unchanged execution guards before OPEN.
    """

    def __init__(self, *, admission=None, funding_enabled=False, **kwargs):
        self.admission = admission
        self.funding_enabled = funding_enabled is True
        if self.funding_enabled:
            from research.weekly_reversal.entry_admission import NativeEntryAdmission
            _check(type(admission) is NativeEntryAdmission and not ({"prove", "validate", "identity"} & admission.__dict__.keys()), "Pinned native admission helper required")
            admission.cash_bridge = self
        super().__init__(**kwargs)
        if not self.enabled:
            return
        with self._paper() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS next_entry_funding (
                    entry_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
                    proof TEXT NOT NULL, proof_digest TEXT NOT NULL,
                    reservation_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    created_at INTEGER NOT NULL, terminal_payload TEXT);
                CREATE TABLE IF NOT EXISTS next_entry_funding_outbox (
                    operation_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL, acknowledgement TEXT);
                CREATE TABLE IF NOT EXISTS next_entry_cash_transfers (
                    transfer_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS next_entry_execution_contexts (
                    entry_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS next_entry_observed_receipts (
                    trade_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_digest TEXT NOT NULL);
            """)

    def _extension_identity(self):
        from research.weekly_reversal import psm_profit_partition as partition
        actual = hashlib.sha256(Path(__file__).with_name("next_entry_profit_bridge.py").read_bytes()).hexdigest()
        _check(actual == V1_SHA256, "Frozen V1 verifier source changed")
        _check(partition.identity()["helper_sha256"] == PSM_HELPER_SHA256,
               "Pinned PSM partition helper source changed")
        result = {"implementation": "native-entry-funding-v2.2", "v1_sha256": actual,
                  "v2_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "psm_partition": partition.identity(), "funding_enabled": self.funding_enabled}
        if self.funding_enabled:
            from research.weekly_reversal.entry_admission import helper_identity
            from .next_entry_settlement_proof import identity
            settlement_identity = identity()
            _check(settlement_identity["sources"]["next_entry_settlement_proof.py"] == SETTLEMENT_HELPER_SHA256,
                   "Pinned entry settlement helper source changed")
            result.update(admission=helper_identity(), settlement=settlement_identity)
        return result

    def _source_pool_proof(self, state, sale_order, acquired_order, trade_id):
        dedicated = str(acquired_order.get("pool", "")).startswith("next_entry:")
        if sale_order.get("kind") == "PSM_EXIT":
            from research.weekly_reversal.psm_profit_partition import verify_transition
            funding = self._psm_acquisition_funding(state, acquired_order, trade_id) if dedicated else None
            return verify_transition(state, sale_order, acquired_order, trade_id, acquisition_funding=funding)
        ordinary = super()._source_pool_proof(state, sale_order, acquired_order, trade_id)
        if not dedicated:
            return ordinary
        _check(acquired_order["pair"] == sale_order["pair"]
               and acquired_order["cohort"] == sale_order["cohort"]
               and acquired_order.get("cohort_generation") == sale_order.get("cohort_generation")
               and acquired_order.get("funding_owner") == sale_order.get("funding_owner"),
               "Dedicated source root or funding owner changed")
        return self._dedicated_acquisition_proof(state, acquired_order, trade_id)

    def _dedicated_acquisition_proof(self, state, acquired_order, trade_id):
        """Prove acquisition custody independently of a later permitted disposal."""
        _check(self.funding_enabled, "Dedicated acquisition requires its funding journal")
        if acquired_order.get("base_entry_proof") is not None:
            return self._retained_base_pool_proof(state, acquired_order, trade_id)
        with self._paper() as db:
            entry_id = acquired_order.get("entry_id")
            entry, proof = self._funding_row(db, entry_id)
            _check(entry["status"] == "CLOSED" and entry["order_id"] == acquired_order["id"]
                   and proof["entry_pool"] == acquired_order["pool"] == "next_entry:" + entry_id,
                   "Dedicated source is not a closed exact-pool acquisition")
            original = proof["evidence"]["order"]
            _check(original["pair"] == acquired_order["pair"]
                   and original["cohort"] == acquired_order["cohort"]
                   and original["cohort_generation"] == acquired_order.get("cohort_generation")
                   and original["funding_owner"] == acquired_order.get("funding_owner"),
                   "Dedicated source root or funding owner changed")
            persisted = self._state(db)
            self.admission.validate(proof, persisted["orders"][entry["order_id"]],
                now=_time(state["now"]), replay=True, paper_connection=db)
            _, batch = self._verified_batch(db, entry, proof, _time(state["now"]))
            terminal = self._terminal_payload(entry)
            _check(batch.source_payload == terminal["batch_source_payload"], "Closed dedicated acquisition evidence changed")
            transfer = self._one(db, "next_entry_cash_transfers", "transfer_id", self._key("entry-cash-return", entry_id))
            _check(_digest(transfer["payload"]) == transfer["payload_digest"], "Dedicated return journal changed")
            return {"schema": 2, "kind": "CLOSED_SAME_ROOT_ENTRY_POOL", "entry_id": entry_id,
                    "order_id": acquired_order["id"], "pair": original["pair"],
                    "cohort": original["cohort"], "cohort_generation": original["cohort_generation"],
                    "funding_owner": original["funding_owner"], "source_pool": proof["source_pool"],
                    "entry_pool": proof["entry_pool"], "admission_sha256": proof["proof_sha256"],
                    "acquisition_source_sha256": batch.source_digest,
                    "terminal_sha256": _digest(entry["terminal_payload"]),
                    "cash_return_sha256": transfer["payload_digest"]}

    def _retained_base_pool_proof(self, state, acquisition, trade_id):
        """A zero-profit entry retains only its explicitly admitted predecessor pool."""
        from research.weekly_reversal.entry_admission import ORDER_FIELDS
        proof = acquisition["base_entry_proof"]
        original = proof["evidence"]["order"]
        pool = acquisition["pool"]
        _check(proof["source_pool"] == proof["entry_pool"] == pool
               and pool.startswith("next_entry:")
               and original["kind"] == acquisition["kind"] == "REENTRY"
               and original["funding_owner"] == acquisition["funding_owner"] == "seed"
               and original["pair"] == acquisition["pair"]
               and original["cohort"] == acquisition["cohort"]
               and original["cohort_generation"] == acquisition["cohort_generation"]
               and _decimal(proof["base_entry"]["approved_profit_increment"]) == 0,
               "Retained pool acquisition changed root, owner or entry kind")
        with self._paper() as db:
            persisted = self._state(db)
            self.admission.validate_base_entry(proof, persisted["orders"][acquisition["id"]],
                now=_time(state["now"]), replay=True, paper_connection=db)
            root_id = pool[len("next_entry:"):]
            root, root_proof = self._funding_row(db, root_id)
            _check(root["status"] == "CLOSED" and root_proof["entry_pool"] == pool
                   and root_proof["evidence"]["order"]["pair"] == original["pair"]
                   and root_proof["evidence"]["order"]["cohort"] == original["cohort"]
                   and root_proof["evidence"]["order"]["funding_owner"] == "seed",
                   "Retained base pool has no same-root closed original funding")
            lineage = proof["evidence"]["signal"]["source_lineage"]
            _check(bool(lineage), "Retained pool predecessor lineage missing")
            verified = []
            for predecessor in lineage:
                prior = predecessor["acquisition"]
                prior_order = persisted["orders"][prior["id"]]
                prior_sale = persisted["orders"][predecessor["sale_id"]]
                _check({key: prior_order.get(key) for key in ORDER_FIELDS} == prior
                       and prior["cohort"] == original["cohort"] and prior["pair"] == original["pair"]
                       and prior["cohort_generation"] == original["previous_cohort_generation"]
                       < original["cohort_generation"]
                       and prior["pool"] == pool and predecessor["trade_id"] != trade_id
                       and _time(prior_sale["decision_at"]) < _time(acquisition["decision_at"]),
                       "Retained base pool predecessor identity or chronology changed")
                outbox = self._one(db, "next_profit_outbox", "trade_id", predecessor["trade_id"])
                source, *_ = self._verify_source(db, predecessor["trade_id"], _time(state["now"]))
                _check(source == outbox["source_payload"] and outbox["status"] in {"ACKNOWLEDGED", "INELIGIBLE"},
                       "Retained predecessor sale is not durably reconciled")
                sources = [item for item in json.loads(source)["acquisitions"] if item["order"]["id"] == prior["id"]]
                _check(sources and all(item["pool_transition"] == predecessor["funding"] for item in sources),
                       "Retained predecessor funding proof changed")
                verified.append({"trade_id": predecessor["trade_id"], "source_digest": outbox["source_digest"],
                                 "funding": predecessor["funding"]})
            return {"schema": 2, "kind": "RETAINED_ROOT_BASE_ENTRY", "root_entry_id": root_id,
                    "order_id": acquisition["id"], "pair": original["pair"], "cohort": original["cohort"],
                    "cohort_generation": original["cohort_generation"], "funding_owner": "seed",
                    "entry_pool": pool, "base_entry_proof_sha256": proof["proof_sha256"],
                    "root_admission_sha256": root_proof["proof_sha256"], "predecessors": verified}

    def _psm_acquisition_funding(self, state, acquisition, trade_id):
        """Supply binding facts only after exact cash/native/allocator replay.

        The pure partition helper validates these facts against PSM handoff
        ownership. It cannot turn an arbitrary pool string or caller flag into
        funding authority; this method re-reads the actual three durable stores.
        """
        lineage = self._dedicated_acquisition_proof(state, acquisition, trade_id)
        with self._paper() as db:
            persisted = self._state(db)
            actual = persisted["orders"][acquisition["id"]]
            _check(actual == acquisition, "PSM funded acquisition changed outside persisted authority")
            self._clean(db, persisted)
            self._project(db, persisted)
            activation = actual.get("activation")
            _check(isinstance(activation, dict) and isinstance(activation.get("proof_sha256"), str),
                   "PSM dedicated acquisition lacks actual admission activation")
            expected = (lineage["admission_sha256"] if lineage["kind"] == "CLOSED_SAME_ROOT_ENTRY_POOL"
                        else lineage["base_entry_proof_sha256"])
            _check(activation["proof_sha256"] == expected, "PSM acquisition activation/admission mismatch")
            proof = {"schema": 1, "kind": "VERIFIED_DEDICATED_ACQUISITION",
                     "acquisition_order_id": actual["id"], "entry_pool": actual["pool"],
                     "pair": actual["pair"], "cohort": actual["cohort"],
                     "cohort_generation": actual["cohort_generation"], "funding_owner": actual["funding_owner"],
                     "native_position": actual["position"], "proposal_id": actual["proposal"],
                     "executor_id": actual["executor"], "activation_sha256": activation["proof_sha256"],
                     "lineage": lineage, "lineage_sha256": _digest(_json(lineage))}
            proof["proof_sha256"] = _digest(_json(proof))
            return proof

    def _decorate_source(self, state, source):
        if state["orders"][source["order"]["id"]].get("kind") != "PSM_EXIT":
            return source
        from research.weekly_reversal.psm_profit_partition import identity
        proofs = {}
        for acquisition in source["acquisitions"]:
            proof = acquisition["pool_transition"]
            _check(proof is not None, "PSM acquisition lacks verified transition")
            key = proof["acquisition_order_id"]
            _check(key not in proofs or proofs[key] == proof, "Conflicting PSM source ownership")
            proofs[key] = proof
        source["psm_ownership"] = [proofs[key] for key in sorted(proofs)]
        source["psm_helper_identity"] = identity()
        return source

    def _stage_earmark(self, db, source, event, snapshot):
        if source.get("psm_ownership"):
            from research.weekly_reversal.psm_profit_partition import identity, stage_distribution
            _check(source["psm_helper_identity"] == identity(), "PSM helper identity changed")
            state = self._state(db)
            sale = state["orders"][source["order"]["id"]]
            for acquisition in source["acquisitions"]:
                actual = state["orders"][acquisition["order"]["id"]]
                proof = self._source_pool_proof(state, sale, actual, source["receipt"]["id"])
                _check(proof == acquisition["pool_transition"], "PSM source funding evidence changed before distribution")
            updated = stage_distribution(
                state, source, event["fee_net_realized_profit"], event["verified_at"],
                event["event_id"], event["cash_backing_id"], snapshot["snapshot_id"])
            db.execute("UPDATE state SET value=? WHERE id=1", (_json(updated),))
        super()._stage_earmark(db, source, event, snapshot)

    def _require_funding(self):
        self._require_enabled()
        _check(self.funding_enabled and self.admission is not None, "Entry funding is disabled")

    def _key(self, kind, entry_id, extra=None):
        return kind + ":" + _digest(_json([self.account_id, self.connector, self.owner_id, entry_id, extra]))

    def _funding_row(self, db, entry_id):
        row = self._one(db, "next_entry_funding", "entry_id", entry_id)
        _check(_digest(row["proof"]) == row["proof_digest"], "Frozen admission digest changed")
        proof = json.loads(row["proof"])
        _check(_json(proof) == row["proof"], "Noncanonical frozen admission bytes")
        from research.weekly_reversal.entry_admission import _hash
        unsigned = dict(proof)
        signature = unsigned.pop("proof_sha256", None)
        _check(signature == _hash(unsigned), "Frozen admission proof identity changed")
        _check(proof["entry_id"] == entry_id and proof["order_id"] == row["order_id"]
               and row["reservation_id"] == self._key("entry-reservation", entry_id), "Funding identity mismatch")
        return row, proof

    def _outbox(self, db, operation_id):
        row = self._one(db, "next_entry_funding_outbox", "operation_id", operation_id)
        _check(_digest(row["payload"]) == row["payload_digest"], "Funding outbox payload changed")
        _check(row["status"] in {"PREPARED", "ACKNOWLEDGED"}, "Unknown funding outbox status")
        payload = json.loads(row["payload"])
        _check(_json(payload) == row["payload"], "Noncanonical frozen funding payload")
        return row, payload

    def _stage_operation(self, db, entry_id, action, payload, now, *, suffix=None):
        operation = self._key("entry-" + action.lower(), entry_id, suffix)
        encoded = _json(payload)
        existing = db.execute("SELECT * FROM next_entry_funding_outbox WHERE operation_id=?", (operation,)).fetchone()
        if existing is not None:
            _check(existing["payload"] == encoded and existing["action"] == action, "Changed funding operation retry")
        else:
            db.execute("INSERT INTO next_entry_funding_outbox VALUES (?,?,?,?,?,'PREPARED',?,NULL)",
                       (operation, entry_id, action, encoded, _digest(encoded), now))
        return operation

    def _transfer(self, db, entry_id, kind, proof, base, profit, source_evidence):
        _check(base >= 0 and profit >= 0, "Negative entry cash transfer")
        payload = {"kind": kind, "entry_id": entry_id, "source_pool": proof["source_pool"],
                   "entry_pool": proof["entry_pool"], "base": str(base), "profit": str(profit),
                   "admission_sha256": proof["proof_sha256"], "source_evidence": source_evidence}
        transfer_id = self._key("entry-cash-" + kind.lower(), entry_id)
        encoded = _json(payload)
        old = db.execute("SELECT payload FROM next_entry_cash_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        _check(old is None or old[0] == encoded, "Changed entry funding transfer")
        db.execute("INSERT OR IGNORE INTO next_entry_cash_transfers VALUES (?,?,?,?)",
                   (transfer_id, entry_id, encoded, _digest(encoded)))

    def _normalize_allocator_proof(self, proof):
        result = dict(proof)
        money = {"base_entry_budget", "approved_profit_increment", "full_fee_inclusive_budget",
                 "profit_cash_used", "base_cash_used", "total_fee_inclusive_cash_debit"}
        for name in money & result.keys():
            atoms = _decimal(result[name]) / self.allocator.policy.accounting_quantum
            _check(atoms == atoms.to_integral_value(), "Funding amount below accounting precision")
            result[name] = int(atoms)
        if "settlement_ids" in result:
            result["settlement_ids"] = sorted(result["settlement_ids"])
        return result

    def _validate_funding_allocator(self, db, allocator_state):
        if not self.funding_enabled:
            return super()._validate_funding_allocator(db, allocator_state)
        entries = {}
        for candidate in db.execute("SELECT entry_id FROM next_entry_funding"):
            row, proof = self._funding_row(db, candidate[0])
            entries[row["reservation_id"]] = (row, proof)
        _check(set(allocator_state["reservations"]) <= set(entries), "Unbacked foreign allocator reservation")
        operations = {}
        for candidate in db.execute("SELECT operation_id FROM next_entry_funding_outbox"):
            row, payload = self._outbox(db, candidate[0])
            operations[row["operation_id"]] = (row, payload)
        _check(set(allocator_state["commits"]) <= set(operations), "Unknown allocator debit operation")
        with sqlite3.connect(self.allocator.path.resolve().as_uri() + "?mode=ro", uri=True) as allocator_db:
            allocator_db.row_factory = sqlite3.Row
            accepted_operations = {row["operation_id"]: dict(row) for row in allocator_db.execute("SELECT * FROM operations")}
        known_grants = {row[0] for row in db.execute("SELECT operation_id FROM next_profit_outbox WHERE event_payload IS NOT NULL")}
        _check(set(accepted_operations) <= set(operations) | known_grants, "Unknown allocator operation identity")
        for operation_id, (operation, payload) in operations.items():
            entry, _ = self._funding_row(db, operation["entry_id"])
            action = operation["action"]
            accepted_operation = accepted_operations.get(operation_id)
            _check(operation["status"] != "ACKNOWLEDGED" or accepted_operation is not None,
                   "Acknowledged funding operation missing in allocator")
            _check((operation["status"] == "PREPARED") == (operation["acknowledgement"] is None),
                   "Funding acknowledgement status mismatch")
            if accepted_operation is not None:
                expected = {"action": action, "reservation_id": entry["reservation_id"]}
                if action == "RESERVE":
                    expected.update(snapshot_id=payload["snapshot_id"], permit=self._normalize_allocator_proof(payload["permit"]))
                else:
                    expected["proof"] = self._normalize_allocator_proof(payload["proof"])
                _check(json.loads(accepted_operation["payload"]) == expected,
                       "Accepted allocator operation differs from frozen funding payload")
                if operation["status"] == "ACKNOWLEDGED":
                    _check(json.loads(operation["acknowledgement"]) == json.loads(accepted_operation["receipt"]),
                           "Funding acknowledgement differs from allocator operation receipt")

        for reservation_id, (row, proof) in entries.items():
            accepted = allocator_state["reservations"].get(reservation_id)
            if accepted is None:
                _check(row["status"] == "PREPARED", "Funded entry lost allocator reservation")
                continue
            _check(accepted["request"] == {"snapshot_id": proof["snapshot_id"],
                   "permit": self._normalize_allocator_proof(proof["permit"])}, "Allocator/native permit mismatch")
            if accepted["status"] == "RELEASED":
                release = [payload for op, payload in operations.values()
                           if op["entry_id"] == row["entry_id"] and op["action"] == "RELEASE"]
                _check(len(release) == 1 and accepted["release_proof"] == self._normalize_allocator_proof(release[0]["proof"]),
                       "Allocator release lacks exact native closure proof")
        for operation_id, (row, payload) in operations.items():
            if row["action"] == "COMMIT":
                accepted = allocator_state["commits"].get(operation_id)
                _check(row["status"] != "ACKNOWLEDGED" or accepted is not None, "Acknowledged native debit missing in allocator")
                if accepted is not None:
                    entry, _ = self._funding_row(db, row["entry_id"])
                    _check(accepted["reservation_id"] == entry["reservation_id"]
                           and accepted["proof"] == self._normalize_allocator_proof(payload["proof"]),
                           "Allocator commit differs from native debit proof")
            elif row["action"] == "RELEASE" and row["status"] == "ACKNOWLEDGED":
                entry, _ = self._funding_row(db, row["entry_id"])
                accepted = allocator_state["reservations"].get(entry["reservation_id"])
                _check(accepted is not None and accepted["status"] == "RELEASED"
                       and accepted["release_proof"] == self._normalize_allocator_proof(payload["proof"]),
                       "Acknowledged release missing exact accepted allocator closure")
            elif row["action"] not in {"RESERVE", "RELEASE"}:
                raise ProfitBridgeBlocked("Unsupported funding operation")

    def _funding_projection(self, db, state, pools, escrow):
        if not self.funding_enabled:
            return pools, escrow
        result = dict(pools)
        seen = set()
        for row in db.execute("SELECT * FROM next_entry_cash_transfers ORDER BY transfer_id"):
            _check(_digest(row["payload"]) == row["payload_digest"], "Entry cash transfer changed")
            payload = json.loads(row["payload"])
            _check(_json(payload) == row["payload"], "Noncanonical cash transfer evidence")
            entry, proof = self._funding_row(db, row["entry_id"])
            kind = payload["kind"]
            _check(row["transfer_id"] == self._key("entry-cash-" + kind.lower(), entry["entry_id"])
                   and payload["entry_id"] == entry["entry_id"]
                   and kind in {"FUND", "RETURN"} and (entry["entry_id"], kind) not in seen,
                   "Duplicate or unsupported entry cash transfer")
            seen.add((entry["entry_id"], kind))
            _check(payload["source_pool"] == proof["source_pool"]
                   and payload["entry_pool"] == proof["entry_pool"] == "next_entry:" + entry["entry_id"]
                   and payload["admission_sha256"] == proof["proof_sha256"]
                   and proof["source_pool"] not in {ESCROW_POOL, "protected", proof["entry_pool"]},
                   "Foreign entry cash transfer source")
            base, profit = _decimal(payload["base"]), _decimal(payload["profit"])
            _check(base >= 0 and profit >= 0, "Negative entry cash transfer")
            permitted_base = _decimal(proof["permit"]["base_entry_budget"])
            permitted_profit = _decimal(proof["permit"]["approved_profit_increment"])
            if kind == "FUND":
                _check(base == permitted_base and profit == permitted_profit, "Entry funding differs from exact approved budgets")
                _check(payload["source_evidence"] == {"reservation_id": entry["reservation_id"],
                    "operation_id": self._key("entry-reserve", entry["entry_id"])}, "Unbound funding cash evidence")
                direction = Decimal(1)
            else:
                terminal = self._terminal_payload(entry)
                closure = json.loads(terminal["closure_source_payload"])
                spent = _decimal(closure["batch"]["total_fee_inclusive_debit"])
                _check(base == permitted_base-min(permitted_base, spent)
                       and profit == permitted_profit-max(ZERO, spent-permitted_base)
                       and payload["source_evidence"] == terminal,
                       "Entry cash return differs from exact terminal debit")
                direction = Decimal(-1)
            result[proof["source_pool"]] = result.get(proof["source_pool"], ZERO) - direction * base
            result[proof["entry_pool"]] = result.get(proof["entry_pool"], ZERO) + direction * (base + profit)
            escrow -= direction * profit
        for row in db.execute("SELECT * FROM next_entry_funding"):
            if row["status"] in {"FUNDED", "CLOSING", "CLOSED"}:
                _check((row["entry_id"], "FUND") in seen, "Entry lacks atomic funding cash legs")
            elif row["status"] != "PREPARED":
                raise ProfitBridgeBlocked("Unknown entry funding state")
        self._audit_funding_sources(db, state, seen)
        return result, escrow

    @staticmethod
    def _terminal_payload(entry):
        _check(entry["terminal_payload"] is not None, "Terminal funding evidence missing")
        terminal = json.loads(entry["terminal_payload"])
        _check(_json(terminal) == entry["terminal_payload"], "Noncanonical terminal closure bytes")
        _check(_digest(terminal["closure_source_payload"]) == terminal["closure_source_digest"],
               "Frozen terminal closure evidence changed")
        closure = json.loads(terminal["closure_source_payload"])
        _check(closure["batch"]["source_payload"] == terminal["batch_source_payload"]
               and _digest(terminal["batch_source_payload"]) == closure["batch"]["source_digest"]
               and {k: v for k, v in closure["batch"].items() if k not in {"source_payload", "source_digest"}} == json.loads(terminal["batch_source_payload"])
               and closure["terminal_status"] == terminal["terminal_status"]
               and closure["terminal_at"] == terminal["terminal_at"]
               and closure["verified_at"] == terminal["verified_at"], "Terminal closure journal mismatch")
        return terminal

    def _audit_funding_sources(self, db, state, transfers):
        """Re-derive every debit and closed cash return, including acknowledged rows.

        Hashes detect accidental byte changes; native receipts and deterministic
        attribution establish authority. An internally rehashed journal is not a
        substitute for its original paper/native source.
        """
        ledger = self.allocator.export_research_state()
        now = _time(state["now"])
        known_entries = {row[0] for row in db.execute("SELECT entry_id FROM next_entry_funding")}
        for table in ("next_entry_funding_outbox", "next_entry_cash_transfers",
                      "next_entry_execution_contexts", "next_entry_observed_receipts"):
            _check({row[0] for row in db.execute("SELECT DISTINCT entry_id FROM " + table)} <= known_entries,
                   "Orphan funding journal evidence")
        for entry_id in sorted(known_entries):
            entry, admission = self._funding_row(db, entry_id)
            accepted = ledger["reservations"].get(entry["reservation_id"])
            operations = [self._outbox(db, row[0]) for row in db.execute(
                "SELECT operation_id FROM next_entry_funding_outbox WHERE entry_id=? ORDER BY rowid", (entry_id,))]
            reserves = [(row, payload) for row, payload in operations if row["action"] == "RESERVE"]
            _check(len(reserves) == 1 and operations[0] == reserves[0], "Entry lacks unique first reserve operation")
            reserve, payload = reserves[0]
            _check(reserve["operation_id"] == self._key("entry-reserve", entry_id)
                   and payload == {"snapshot_id": admission["snapshot_id"],
                       "reservation_id": entry["reservation_id"], "permit": admission["permit"]},
                   "Reserve operation identity changed")
            if accepted is None:
                _check(entry["status"] == "PREPARED" and len(operations) == 1
                       and reserve["status"] == "PREPARED" and not any(item[0] == entry_id for item in transfers),
                       "Cash funded without allocator reservation")
                continue
            # Replay validation never grants a new order or refreshes a permit.
            self.admission.validate(admission, state["orders"][entry["order_id"]], now=now,
                                    replay=True, reconciliation_only=True, paper_connection=db)
            _check(reserve["status"] != "ACKNOWLEDGED" or
                   {k: v for k, v in json.loads(reserve["acknowledgement"]).items() if k not in {"operation_id", "sequence"}} == accepted["reserve_receipt"],
                   "Reserve acknowledgement changed")
            _check((entry["status"] == "PREPARED") == ((entry_id, "FUND") not in transfers),
                   "Funding state and cash legs disagree")
            if entry["status"] == "PREPARED":
                _check(len(operations) == 1, "Unfunded entry has spending operations")
                continue
            if state["orders"][entry["order_id"]]["status"] == "FUNDING_PREPARED":
                _check(len(operations) == 1 and entry["status"] == "FUNDED"
                       and db.execute("SELECT 1 FROM receipts WHERE order_id=?", (entry["order_id"],)).fetchone() is None
                       and db.execute("SELECT 1 FROM next_entry_execution_contexts WHERE entry_id=?", (entry_id,)).fetchone() is None
                       and entry["terminal_payload"] is None and (entry_id, "RETURN") not in transfers,
                       "Non-executable prepared entry acquired cash evidence")
                continue
            context, batch = self._verified_batch(db, entry, admission, now)
            actual = {json.loads(item)["settlement_id"]: item for item in batch.receipts}
            order_ids = list(batch.settlement_ids)
            consumed = []
            used_base = used_profit = ZERO
            releases = []
            permit = admission["permit"]
            base_budget = _decimal(permit["base_entry_budget"])
            for operation, payload in operations[1:]:
                if operation["action"] == "RELEASE":
                    releases.append((operation, payload))
                    continue
                _check(operation["action"] == "COMMIT" and not releases, "Invalid funding operation ordering")
                facts = payload["receipt_payloads"]
                ids = payload["proof"]["settlement_ids"]
                _check(ids == sorted(set(ids)) and set(ids) == set(facts)
                       and not (set(ids) & set(consumed))
                       and all(actual.get(key) == value for key, value in facts.items()),
                       "Commit native source coverage changed")
                consumed.extend(key for key in order_ids if key in facts)
                _check(consumed == order_ids[:len(consumed)], "Commit skipped earlier native debit")
                spent = sum((_decimal(json.loads(actual[key])["fee_inclusive_debit"]) for key in consumed), ZERO)
                base, profit = min(base_budget, spent), max(ZERO, spent-base_budget)
                raw = payload["proof"]
                verified_at = _time(raw["verified_at"])
                expected = asdict(CommitProof(
                    proof_id=self._key("entry-debit-proof", entry_id, sorted(ids)),
                    native_reservation_id=permit["native_reservation_id"], entry_id=entry_id,
                    account_id=self.account_id, currency="USD", allocation_id=permit["allocation_id"], owner_id=permit["owner_id"],
                    settlement_ids=tuple(sorted(ids)), profit_cash_used=profit-used_profit,
                    base_cash_used=base-used_base, total_fee_inclusive_cash_debit=spent-used_base-used_profit,
                    settled_at=max(json.loads(value)["reconciled_at"] for value in facts.values()),
                    verified_at=verified_at, expires_at=verified_at+self.allocator.policy.max_proof_age_seconds,
                    confirmed=True, settled=True, verified=True))
                _check(_json(raw) == _json(expected) and profit > used_profit
                       and expected["settled_at"] <= verified_at <= now
                       and operation["operation_id"] == self._key("entry-commit", entry_id, sorted(ids)),
                       "Commit attribution is not the exact base-first native debit")
                used_base, used_profit = base, profit
            _check(len(releases) <= 1, "Duplicate terminal release")
            if entry["terminal_payload"] is not None:
                terminal = self._terminal_payload(entry)
                closure = json.loads(terminal["closure_source_payload"])
                _check(batch.source_payload == terminal["batch_source_payload"]
                       and closure["context"] == context.as_dict()
                       and state["orders"][entry["order_id"]]["status"] == terminal["terminal_status"]
                       == state["orders"][entry["order_id"]]["terminal"], "Closed entry source or status changed")
                self._audit_terminal_authority(db, state, context, terminal)
                # Immutable terminal/audit rows are replayed without requiring a
                # fresh watermark forever after a legitimate completed closure.
                terminal_audit = closure["terminal_audit"]
                _check(dict(db.execute("SELECT * FROM audit_events WHERE seq=?", (terminal_audit["seq"],)).fetchone() or {})
                       == terminal_audit, "Frozen terminal audit changed")
                for audit in closure["receipt_audits"]:
                    _check(dict(db.execute("SELECT * FROM audit_events WHERE seq=?", (audit["seq"],)).fetchone() or {}) == audit,
                           "Frozen applied receipt audit changed")
                _check((entry_id, "RETURN") in transfers and entry["status"] in {"CLOSING", "CLOSED"},
                       "Terminal cash return or state missing")
                total_profit = max(ZERO, batch.total_fee_inclusive_debit-base_budget)
                _check(used_profit == total_profit, "Terminal actual profit debit not committed")
                left = _decimal(permit["approved_profit_increment"])-total_profit
                _check(bool(releases) == (left > 0), "Unused profit release coverage mismatch")
                if releases:
                    operation, payload = releases[0]
                    expected = asdict(ReleaseProof(
                        proof_id=self._key("entry-release-proof", entry_id, terminal["closure_source_digest"]),
                        native_reservation_id=permit["native_reservation_id"], entry_id=entry_id,
                        account_id=self.account_id, currency="USD", allocation_id=permit["allocation_id"], owner_id=permit["owner_id"],
                        verified_at=terminal["verified_at"],
                        expires_at=terminal["verified_at"]+self.allocator.policy.max_proof_age_seconds,
                        native_reservation_closed=True, no_unreconciled_fills=True,
                        no_remaining_order_liability=True, verified=True))
                    _check(payload == {**terminal, "proof": expected}
                           and operation["operation_id"] == self._key("entry-release", entry_id),
                           "Release differs from exact frozen terminal evidence")
                _check(entry["status"] != "CLOSED" or
                       all(op["status"] == "ACKNOWLEDGED" for op, _ in operations),
                       "Closed entry has unknown allocator outcome")
            else:
                _check(entry["status"] == "FUNDED" and (entry_id, "RETURN") not in transfers and not releases,
                       "Nonterminal entry attempted a cash return")

    def _audit_terminal_authority(self, db, state, context, terminal):
        closure = json.loads(terminal["closure_source_payload"])
        health = closure["native_health"]
        verified = _time(terminal["verified_at"])
        ended = _time(terminal["terminal_at"])
        minimum = terminal["minimum_verified_after"]
        _check(health["position_id"] == context.position_id
               and health["recorded_state"] == "HEALTHY"
               and json.loads(health["unresolved_order_ids_json"]) == []
               and json.loads(health["unresolved_trade_ids_json"]) == []
               and _time(health["last_success_at"]) == _time(health["last_attempt_at"]) == verified
               and context.reserved_at <= ended <= verified <= _time(state["now"])
               and (minimum is None or verified > _time(minimum)),
               "Frozen terminal native health is not a valid exact closure")
        with self._native() as native:
            authority = {
                "binding": self._one(native, "economic_fill_bindings", "order_id", context.order_id),
                "provenance": self._one(native, "execution_provenance", "proposal_id", context.proposal_id),
                "execution": self._one(native, "execution_receipts", "proposal_id", context.proposal_id),
                "reservation": self._one(native, "capital_reservations", "proposal_id", context.proposal_id),
            }
            authority["reservation"] = {key: authority["reservation"][key] for key in
                ("proposal_id", "pair", "intent", "amount_quote", "leverage", "created_at")}
            _check(authority == closure["native_authority"], "Frozen terminal native authority changed")
            order = state["orders"][context.order_id]
            _check(_time(authority["reservation"]["created_at"])
                   <= _time(authority["provenance"]["created_at"])
                   == _time(authority["execution"]["created_at"])
                   <= _time(authority["binding"]["created_at"]) <= ended
                   and _time(order["created_at"]) <= _time(authority["binding"]["created_at"]),
                   "Terminal native authority chronology changed")
            current = self._one(native, "economic_accounting_health", "position_id", context.position_id)
            if current["last_success_at"] == health["last_success_at"]:
                _check(current == health, "Original terminal health evidence changed")
            # Derive the closed observation's membership from persisted causal
            # timestamps. Later legitimate orders do not rewrite this scope.
            bindings = [dict(row) for row in native.execute(
                "SELECT * FROM economic_fill_bindings WHERE position_id=?", (context.position_id,))
                if _time(row["created_at"]) <= verified]
            bound_order_ids = {row["order_id"] for row in bindings}
            orders = [o for o in state["orders"].values() if o["position"] == context.position_id
                      and (o["status"] != "REJECTED" or o["id"] in bound_order_ids)
                      and _time(o["created_at"]) <= verified]
            order_ids = {o["id"] for o in orders}
            proposals = {o["proposal"] for o in orders}
            fills = [dict(row) for row in native.execute(
                "SELECT * FROM economic_trade_fill_reconciliation WHERE position_id=?", (context.position_id,))
                if _time(row["reconciled_at"]) <= verified]
            _check({b["order_id"] for b in bindings} == order_ids
                   and all(f["order_id"] in order_ids for f in fills)
                   and health["proposal_count"] == health["fully_reconciled_proposals"] == len(proposals) > 0
                   and health["known_orders"] == health["bound_orders"] == len(order_ids)
                   and health["confirmed_fills"] == health["ledgered_fills"] == len(fills),
                   "Frozen terminal health counts differ from causal native coverage")

    def _pending_funding(self, db, state):
        if not self.funding_enabled:
            return ()
        pending = set()
        known = {}
        for row in db.execute("SELECT * FROM next_entry_funding"):
            known[row["order_id"]] = row["entry_id"]
            if row["status"] in {"PREPARED", "CLOSING"}:
                pending.add(row["entry_id"])
            if row["status"] != "CLOSED" and state["orders"][row["order_id"]].get("terminal"):
                pending.add(row["entry_id"])
            for receipt in db.execute("SELECT id FROM receipts WHERE order_id=?", (row["order_id"],)):
                if not db.execute("SELECT 1 FROM next_entry_observed_receipts WHERE trade_id=? AND entry_id=?",
                                  (receipt[0], row["entry_id"])).fetchone():
                    pending.add(row["entry_id"])
        for row in db.execute("SELECT entry_id FROM next_entry_funding_outbox WHERE status != 'ACKNOWLEDGED'"):
            pending.add(row[0])
        for order in state["orders"].values():
            if order["status"] == "FUNDING_PREPARED":
                pending.add(order["entry_id"])
        return tuple(sorted(pending))

    def prepare_entry(self, *, order_id, now):
        self._require_funding()
        now = _time(now)
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            self._clean(db, state)
            existing = db.execute("SELECT entry_id FROM next_entry_funding WHERE order_id=?", (order_id,)).fetchone()
            if existing:
                row, proof = self._funding_row(db, existing[0])
                reservation = self.allocator.export_research_state()["reservations"].get(row["reservation_id"])
                self.admission.validate(proof, state["orders"][order_id], now=now,
                    replay=reservation is not None, paper_connection=db)
                return proof
            proof = self.admission.prove(order_id, now=now, paper_connection=db)
            entry_id = _identifier(proof["entry_id"])
            _check(proof["order_id"] == order_id and proof["entry_pool"] == "next_entry:" + entry_id,
                   "Admission entry pool identity mismatch")
            permit = proof["permit"]
            _check(_decimal(permit["base_entry_budget"]) > 0 and _decimal(permit["approved_profit_increment"]) > 0
                   and _decimal(permit["base_entry_budget"]) + _decimal(permit["approved_profit_increment"])
                   == _decimal(permit["full_fee_inclusive_budget"]), "Invalid exact entry cash budgets")
            encoded = _json(proof)
            reservation_id = self._key("entry-reservation", entry_id)
            db.execute("INSERT INTO next_entry_funding VALUES (?,?,?,?,?,'PREPARED',?,NULL)",
                       (entry_id, order_id, encoded, _digest(encoded), reservation_id, now))
            self._stage_operation(db, entry_id, "RESERVE", {"snapshot_id": proof["snapshot_id"],
                "reservation_id": reservation_id, "permit": permit}, now)
            self._project(db, state)
            self._fault("before_entry_prepare_commit")
            db.commit()
            self._fault("after_entry_prepare_commit")
            return proof

    def _context(self, db, entry, proof, *, freeze=False):
        from .next_entry_settlement_proof import EntrySettlementContext
        state = self._state(db)
        order = state["orders"][entry["order_id"]]
        reserved = self.allocator.export_research_state()["reservations"].get(entry["reservation_id"])
        _check(reserved is not None, "Actual allocator reservation missing")
        stored = db.execute("SELECT * FROM next_entry_execution_contexts WHERE entry_id=?", (entry["entry_id"],)).fetchone()
        permit = proof["permit"]
        context = EntrySettlementContext(
            account_id=self.account_id, connector=self.connector, controller_id=self.owner_id,
            entry_id=entry["entry_id"], order_id=entry["order_id"], pair=order["pair"],
            position_id=order["position"], proposal_id=order["proposal"], executor_id=order["executor"],
            native_reservation_id=permit["native_reservation_id"], entry_pool=proof["entry_pool"],
            base_budget=_decimal(permit["base_entry_budget"]),
            profit_budget=_decimal(permit["approved_profit_increment"]),
            authorized_quantity=_decimal(order["quantity"]), reference_price=_decimal(order["reference"]),
            reserved_at=reserved["reserved_at"], funding_owner=order["funding_owner"],
            snapshot_id=proof["snapshot_id"], allocation_id=permit["allocation_id"],
            owner_id=permit["owner_id"], allocation_role=permit["role"], source_pool=proof["source_pool"])
        if stored is not None:
            _check(_digest(stored["payload"]) == stored["payload_digest"]
                   and stored["payload"] == _json(asdict(context)),
                   "Frozen execution context changed from exact admission/allocator/order")
            return context
        if freeze:
            _check(order.get("execution_frozen") is True, "Entry execution geometry is not frozen")
            payload = _json(asdict(context))
            db.execute("INSERT INTO next_entry_execution_contexts VALUES (?,?,?)",
                       (entry["entry_id"], payload, _digest(payload)))
        return context

    def _verified_batch(self, db, entry, proof, now, *, freeze=False):
        from .next_entry_settlement_proof import verify_entry_receipts
        has_receipts = db.execute("SELECT 1 FROM receipts WHERE order_id=? LIMIT 1", (entry["order_id"],)).fetchone()
        context = self._context(db, entry, proof, freeze=freeze and bool(has_receipts))
        batch = verify_entry_receipts(db, self.native_db, context, now)
        known = {row["trade_id"]: dict(row) for row in db.execute(
            "SELECT * FROM next_entry_observed_receipts WHERE entry_id=?", (entry["entry_id"],))}
        current = {json.loads(payload)["settlement_id"]: payload for payload in batch.receipts}
        _check(set(known) <= set(current), "Previously confirmed entry receipt disappeared")
        for trade_id, row in known.items():
            _check(_digest(row["payload"]) == row["payload_digest"] and row["payload"] == current[trade_id],
                   "Previously confirmed entry evidence changed")
        return context, batch

    def _validate_operation_source(self, db, operation, payload, now):
        entry, proof = self._funding_row(db, operation["entry_id"])
        state = self._state(db)
        self._clean(db, state)
        _check(_time(state["now"]) == now, "Funding verification requires the current paper clock")
        accepted = self.allocator.export_research_state()["reservations"].get(entry["reservation_id"])
        self.admission.validate(proof, state["orders"][entry["order_id"]], now=now,
                                replay=accepted is not None, reconciliation_only=accepted is not None, paper_connection=db)
        if operation["action"] == "RESERVE":
            _check(payload == {"snapshot_id": proof["snapshot_id"], "reservation_id": entry["reservation_id"],
                               "permit": proof["permit"]}, "Frozen reserve payload mismatch")
        else:
            context, batch = self._verified_batch(db, entry, proof, now)
            if operation["action"] == "COMMIT":
                actual = {json.loads(item)["settlement_id"]: item for item in batch.receipts}
                frozen = payload["receipt_payloads"]
                _check(set(frozen) == set(payload["proof"]["settlement_ids"])
                       and all(actual.get(key) == value for key, value in frozen.items()),
                       "Actual native debit changed after staging")
            elif operation["action"] == "RELEASE":
                from .next_entry_settlement_proof import verify_terminal_closure
                closure = verify_terminal_closure(db, self.native_db, context, now,
                    minimum_verified_after=payload["minimum_verified_after"])
                _check(closure.batch.source_payload == payload["batch_source_payload"]
                       and closure.terminal_status == payload["terminal_status"]
                       and closure.terminal_at == payload["terminal_at"]
                       and closure.verified_at >= payload["proof"]["verified_at"],
                       "Native closure changed after staged release")
        return entry, proof

    def _dispatch_operation(self, operation_id, now):
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            operation, payload = self._outbox(db, operation_id)
            entry, admission = self._validate_operation_source(db, operation, payload, now)
            self._project(db, self._state(db))
            if operation["status"] == "ACKNOWLEDGED":
                return json.loads(operation["acknowledgement"])
            db.commit()
        action = operation["action"]
        if action == "RESERVE":
            result = self.allocator.reserve(operation_id=operation_id, reservation_id=entry["reservation_id"],
                snapshot_id=payload["snapshot_id"], permit=EntryPermit(**payload["permit"]), now=now)
        elif action == "COMMIT":
            raw = dict(payload["proof"])
            raw["settlement_ids"] = tuple(raw["settlement_ids"])
            result = self.allocator.commit(operation_id=operation_id, reservation_id=entry["reservation_id"],
                                           proof=CommitProof(**raw), now=now)
        elif action == "RELEASE":
            result = self.allocator.release(operation_id=operation_id, reservation_id=entry["reservation_id"],
                                            proof=ReleaseProof(**payload["proof"]), now=now)
        else:
            raise ProfitBridgeBlocked("Unknown funding dispatch action")
        self._fault("after_allocator_" + action.lower() + "_commit")
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            current, current_payload = self._outbox(db, operation_id)
            _check(current_payload == payload, "Funding payload changed during allocator dispatch")
            entry, admission = self._validate_operation_source(db, current, current_payload, now)
            if current["acknowledgement"] is not None:
                _check(current["acknowledgement"] == _json(result), "Changed funding acknowledgement")
            if action == "RESERVE":
                self._transfer(db, entry["entry_id"], "FUND", admission,
                    _decimal(admission["permit"]["base_entry_budget"]),
                    _decimal(admission["permit"]["approved_profit_increment"]),
                    {"reservation_id": entry["reservation_id"], "operation_id": operation_id})
                db.execute("UPDATE next_entry_funding SET status='FUNDED' WHERE entry_id=? AND status='PREPARED'", (entry["entry_id"],))
            elif action == "RELEASE":
                db.execute("UPDATE next_entry_funding SET status='CLOSED' WHERE entry_id=?", (entry["entry_id"],))
            db.execute("UPDATE next_entry_funding_outbox SET status='ACKNOWLEDGED',acknowledgement=? WHERE operation_id=?",
                       (_json(result), operation_id))
            self._project(db, self._state(db))
            self._fault("before_" + action.lower() + "_ack_commit")
            db.commit()
            self._fault("after_" + action.lower() + "_ack_commit")
        return result

    def fund_entry(self, *, entry_id, now):
        """Return funding readiness only; the original adapter still owns OPEN."""
        self._require_funding()
        now = _time(now)
        operation = self._key("entry-reserve", entry_id)
        result = self._dispatch_operation(operation, now)
        with self._paper() as db:
            row, proof = self._funding_row(db, entry_id)
            _check(row["status"] == "FUNDED", "Entry funding did not reconcile")
            _check(now <= proof["permit"]["expires_at"], "Funded permit expired before execution")
        return {"entry_id": entry_id, "order_id": row["order_id"], "funding_ready": True,
                "order_permission": False, "reservation_receipt": result}

    def _stage_actual_commit(self, db, entry, admission, batch, now):
        acknowledged_base = ZERO
        acknowledged_profit = ZERO
        consumed = set()
        for row in db.execute("SELECT operation_id FROM next_entry_funding_outbox WHERE entry_id=? AND action='COMMIT'", (entry["entry_id"],)):
            operation, payload = self._outbox(db, row[0])
            _check(operation["status"] == "ACKNOWLEDGED", "Prior native debit outbox remains pending")
            acknowledged_base += _decimal(payload["proof"]["base_cash_used"])
            acknowledged_profit += _decimal(payload["proof"]["profit_cash_used"])
            consumed.update(payload["proof"]["settlement_ids"])
        base_budget = _decimal(admission["permit"]["base_entry_budget"])
        profit_used = max(ZERO, batch.total_fee_inclusive_debit - base_budget)
        _check(profit_used >= acknowledged_profit, "Confirmed profit debit moved backwards")
        if profit_used == acknowledged_profit:
            return None
        base_used = min(base_budget, batch.total_fee_inclusive_debit)
        receipts = {json.loads(item)["settlement_id"]: item for item in batch.receipts
                    if json.loads(item)["settlement_id"] not in consumed}
        _check(bool(receipts) and batch.latest_reconciled_at is not None, "Missing confirmed entry cash debits")
        permit = admission["permit"]
        proof = CommitProof(
            proof_id=self._key("entry-debit-proof", entry["entry_id"], sorted(receipts)),
            native_reservation_id=permit["native_reservation_id"], entry_id=entry["entry_id"],
            account_id=self.account_id, currency="USD", allocation_id=permit["allocation_id"], owner_id=permit["owner_id"],
            settlement_ids=tuple(sorted(receipts)), profit_cash_used=profit_used-acknowledged_profit,
            base_cash_used=base_used-acknowledged_base,
            total_fee_inclusive_cash_debit=batch.total_fee_inclusive_debit-acknowledged_base-acknowledged_profit,
            settled_at=batch.latest_reconciled_at, verified_at=now,
            expires_at=now+self.allocator.policy.max_proof_age_seconds,
            confirmed=True, settled=True, verified=True)
        return self._stage_operation(db, entry["entry_id"], "COMMIT",
            {"proof": asdict(proof), "receipt_payloads": receipts}, now, suffix=sorted(receipts))

    def sync_entry(self, *, entry_id, now):
        """Observe exact cash debits, commit actual profit use, then reconcile close."""
        self._require_funding()
        now = _time(now)
        with self._paper() as db:
            pending = [row[0] for row in db.execute(
                "SELECT operation_id FROM next_entry_funding_outbox WHERE entry_id=? AND status='PREPARED' ORDER BY rowid",
                (entry_id,))]
        for operation in pending:
            self._dispatch_operation(operation, now)
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            entry, proof = self._funding_row(db, entry_id)
            _check(entry["status"] in {"FUNDED", "CLOSED"}, "Entry is not durably funded")
            state = self._state(db)
            self._clean(db, state)
            _check(_time(state["now"]) == now, "Entry sync requires current paper clock")
            self.admission.validate(proof, state["orders"][entry["order_id"]], now=now,
                                    replay=True, paper_connection=db)
            context, batch = self._verified_batch(db, entry, proof, now, freeze=True)
            if entry["status"] == "CLOSED":
                _check(batch.source_payload == self._terminal_payload(entry)["batch_source_payload"],
                       "Late cash debit after terminal funding release")
            for payload in batch.receipts:
                trade_id = json.loads(payload)["settlement_id"]
                db.execute("INSERT OR IGNORE INTO next_entry_observed_receipts VALUES (?,?,?,?)",
                           (trade_id, entry_id, payload, _digest(payload)))
            operation = self._stage_actual_commit(db, entry, proof, batch, now)
            self._project(db, state)
            self._fault("before_debit_stage_commit")
            db.commit()
            self._fault("after_debit_stage_commit")
        if operation is not None:
            self._dispatch_operation(operation, now)
        return self._close_if_terminal(entry_id, now)

    def _close_if_terminal(self, entry_id, now):
        from .next_entry_settlement_proof import verify_terminal_closure
        with localcontext() as ctx, self._paper() as db:
            ctx.prec = 400
            db.execute("BEGIN IMMEDIATE")
            entry, admission = self._funding_row(db, entry_id)
            state = self._state(db)
            order = state["orders"][entry["order_id"]]
            if not order.get("terminal"):
                return {"entry_id": entry_id, "status": "FUNDED", "order_permission": False}
            context, batch = self._verified_batch(db, entry, admission, now)
            if entry["status"] == "CLOSED":
                terminal = self._terminal_payload(entry)
                _check(batch.source_payload == terminal["batch_source_payload"], "Late cash debit after terminal funding release")
                return {"entry_id": entry_id, "status": "CLOSED", "order_permission": False}
            commit_times = [json.loads(row[0])["proof"]["verified_at"] for row in db.execute(
                "SELECT payload FROM next_entry_funding_outbox WHERE entry_id=? AND action='COMMIT'", (entry_id,))]
            minimum_after = max(commit_times) if commit_times else None
            # Same-second native closure after a commit is ambiguous by allocator
            # contract. Wait for an actual newer health observation, not a fake clock.
            if minimum_after is not None:
                with self._native() as native:
                    health = native.execute("SELECT last_success_at FROM economic_accounting_health WHERE position_id=?",
                                            (context.position_id,)).fetchone()
                if health is None or health[0] is None or _time(health[0]) <= minimum_after:
                    return {"entry_id": entry_id, "status": "AWAITING_NEWER_NATIVE_CLOSURE", "order_permission": False}
            closure = verify_terminal_closure(db, self.native_db, context, now,
                                              minimum_verified_after=minimum_after)
            permit = admission["permit"]
            base_budget = _decimal(permit["base_entry_budget"])
            profit_budget = _decimal(permit["approved_profit_increment"])
            base_used = min(base_budget, batch.total_fee_inclusive_debit)
            profit_used = max(ZERO, batch.total_fee_inclusive_debit-base_budget)
            base_left, profit_left = base_budget-base_used, profit_budget-profit_used
            terminal = {"closure_source_payload": closure.source_payload,
                        "closure_source_digest": closure.source_digest,
                        "batch_source_payload": batch.source_payload, "terminal_status": closure.terminal_status,
                        "terminal_at": closure.terminal_at, "verified_at": closure.verified_at,
                        "minimum_verified_after": minimum_after}
            db.execute("UPDATE next_entry_funding SET terminal_payload=? WHERE entry_id=?",
                       (_json(terminal), entry_id))
            self._transfer(db, entry_id, "RETURN", admission, base_left, profit_left, terminal)
            if profit_left > 0:
                release = ReleaseProof(
                    proof_id=self._key("entry-release-proof", entry_id, terminal["closure_source_digest"]),
                    native_reservation_id=permit["native_reservation_id"], entry_id=entry_id,
                    account_id=self.account_id, currency="USD", allocation_id=permit["allocation_id"], owner_id=permit["owner_id"],
                    verified_at=closure.verified_at,
                    expires_at=closure.verified_at+self.allocator.policy.max_proof_age_seconds,
                    native_reservation_closed=True, no_unreconciled_fills=True,
                    no_remaining_order_liability=True, verified=True)
                payload = {**terminal, "proof": asdict(release)}
                operation = self._stage_operation(db, entry_id, "RELEASE", payload, now)
                db.execute("UPDATE next_entry_funding SET status='CLOSING' WHERE entry_id=?", (entry_id,))
            else:
                operation = None
                db.execute("UPDATE next_entry_funding SET status='CLOSED' WHERE entry_id=?", (entry_id,))
            self._project(db, state)
            self._fault("before_return_stage_commit")
            db.commit()
            self._fault("after_return_stage_commit")
        if operation is not None:
            self._dispatch_operation(operation, now)
        return {"entry_id": entry_id, "status": "CLOSED", "order_permission": False}
