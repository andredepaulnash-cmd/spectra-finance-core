"""Synthetic accounting tests only. No prices, strategy runs or external services."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, localcontext
import itertools
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from spectra_core.next_entry_allocator import (
    Allocator, Blocked, CommitProof, EntryPermit, Member,
    MembershipSnapshot, Policy, ReleaseProof, SettledProfitEvent,
)

D = Decimal
POLICY = Policy(D("0.00000001"), D("0.01"), 300, 60)


def membership(snapshot_id="members-1", *, count=2):
    return MembershipSnapshot(snapshot_id, "paper-account", "USD", 900, 2000,
                              tuple(Member(f"position-{n}", f"owner-{n}", "POSITION_ALLOCATION")
                                    for n in range(count)) + (Member("vcap", "vcap-owner", "VCAP"),))


def profit(event_id="profit-1", amount="10.00", *, when=1000):
    amount = D(amount)
    return SettledProfitEvent(
        event_id, "paper-account", "USD", "source-owner", ("realization-" + event_id,),
        "cash-" + event_id, "proof-" + event_id, D("100") + D("2") + amount,
        D("100"), D("2"), amount, amount, when, when, when + 60,
        True, True, True, True,
    )


def permit(permit_id="entry-1", amount="2.00", *, when=1001, allocation_id="position-0", owner_id="owner-0", role="POSITION_ALLOCATION"):
    amount = D(amount)
    return EntryPermit(
        permit_id, "native-" + permit_id, permit_id, "paper-account", "USD",
        allocation_id, owner_id, role, "proof-" + permit_id,
        D("50"), amount, D("50") + amount, when, when + 60,
        True, True, True, True, True, True, True,
    )


def commit_proof(entry=None, amount="1.00", *, proof_id="fill-1", when=1002):
    entry = entry or permit()
    amount = D(amount)
    return CommitProof(
        proof_id, entry.native_reservation_id, entry.entry_id, entry.account_id, entry.currency,
        entry.allocation_id, entry.owner_id, ("settlement-" + proof_id,),
        amount, D("20"), D("20") + amount, when, when, when + 60, True, True, True,
    )


def release_proof(entry=None, *, when=1003):
    entry = entry or permit()
    return ReleaseProof("release-proof-" + entry.entry_id, entry.native_reservation_id,
                        entry.entry_id, entry.account_id, entry.currency, entry.allocation_id,
                        entry.owner_id, when, when + 60, True, True, True, True)


class AllocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "allocator.sqlite3"
        self.ledger = self.open()

    def tearDown(self):
        self.temp.cleanup()

    def open(self, *, fault_hook=None, path=None):
        return Allocator(path or self.path, account_id="paper-account", currency="USD", policy=POLICY, fault_hook=fault_hook)

    def apply(self, event=None, snapshot=None, *, now=1000, operation_id=None):
        event = event or profit()
        return self.ledger.apply_profit(operation_id=operation_id or "apply-" + event.event_id,
                                        snapshot=snapshot or membership(), event=event, now=now)

    def reserve(self, entry=None, *, reservation_id="r1", now=1001, operation_id=None):
        entry = entry or permit()
        return self.ledger.reserve(operation_id=operation_id or "reserve-" + reservation_id,
                                   reservation_id=reservation_id, snapshot_id="members-1", permit=entry, now=now)

    def assert_unmutated(self, action):
        before = self.ledger.audit()
        with self.assertRaises((Blocked, TypeError)):
            action()
        self.assertEqual(before, self.ledger.audit())

    def test_equal_share_including_vcap_not_seventy_five_twenty_five(self):
        result = self.apply()
        self.assertEqual(result["equal_share"], "3.33000000")
        self.assertEqual(result["carry_after"], "0.01000000")
        self.assertEqual(len(result["grants"]), 3)
        self.assertEqual({g["amount"] for g in result["grants"]}, {"3.33000000"})
        self.assertEqual(result["grants"][-1]["role"], "VCAP")
        audit = self.ledger.audit()
        self.assertEqual(audit["eligible_profit"], "10.00000000")
        self.assertEqual(audit["available_entitlements"], "9.99000000")
        self.assertEqual(audit["cash_created"], "0")
        self.assertTrue(audit["conserved"])

    def test_returned_principal_and_fees_excluded(self):
        result = self.apply()
        self.assertEqual(result["returned_principal_excluded"], "100.00000000")
        self.assertNotEqual(self.ledger.audit()["eligible_profit"], "112.00000000")
        self.assert_unmutated(lambda: self.apply(replace(profit("p2"), fee_net_realized_profit=D("112"))))

    def test_carry_funds_later_equal_shares_with_source_attribution(self):
        self.apply(profit("p1", "0.01"))
        self.apply(profit("p2", "0.01"))
        third = self.apply(profit("p3", "0.01"))
        self.assertEqual(third["equal_share"], "0.01000000")
        self.assertEqual(third["carry_before"], "0.02000000")
        self.assertEqual(third["carry_after"], "0.00000000")
        self.assertEqual([g["source_lots"][0]["event_id"] for g in third["grants"]], ["p1", "p2", "p3"])
        self.assertTrue(self.ledger.audit()["conserved"])

    def test_carry_cannot_migrate_to_changed_membership(self):
        self.apply(profit("p1", "0.01"))
        self.apply(profit("p2", "0.03"), membership("members-2"))
        self.assertEqual(self.ledger.audit()["carry"], "0.01000000")
        state = self.ledger.export_research_state()
        self.assertEqual(state["carry"]["members-1"][0]["event_id"], "p1")
        self.assertEqual(state["carry"]["members-2"], [])

    def test_no_standalone_penny_orders_and_vcap_entitlements_are_inert(self):
        self.apply(profit(amount="0.03"))
        self.assertEqual(self.ledger.audit()["reservations"], {})
        entry = replace(permit(amount="0.01"), base_entry_budget=D("0"), full_fee_inclusive_budget=D("0.01"))
        self.assert_unmutated(lambda: self.reserve(entry))
        vcap = permit("vcap-entry", "0.01", allocation_id="vcap", owner_id="vcap-owner", role="VCAP")
        self.assertFalse(self.reserve(vcap)["order_permission"])

    def test_membership_caller_supplied_immutable_and_unique(self):
        self.apply()
        self.assert_unmutated(lambda: self.apply(profit("p2"), membership(count=3)))
        for bad in [replace(membership(), members=list(membership().members)),
                    replace(membership(), members=()),
                    replace(membership(), members=membership().members + (membership().members[0],)),
                    replace(membership(), members=(Member("p", "o", "POSITION_ALLOCATION"),)),
                    replace(membership(), effective_until=899)]:
            self.assert_unmutated(lambda bad=bad: self.apply(profit("p3"), bad))

    def test_profit_same_operation_exactly_once_across_restart_and_stale_retry(self):
        receipt = self.apply()
        before = self.ledger.audit()
        self.ledger = self.open()
        repeated = self.apply(now=5000)
        self.assertEqual(receipt, repeated)
        self.assertEqual(before, self.ledger.audit())

    def test_profit_event_new_delivery_id_does_not_allocate_twice(self):
        self.apply()
        self.apply(operation_id="redelivery", now=5000)
        self.assertEqual(self.ledger.audit()["eligible_profit"], "10.00000000")
        self.assertEqual(self.ledger.audit()["event_count"], 1)

    def test_changed_idempotency_payload_fails_closed(self):
        self.apply()
        self.assert_unmutated(lambda: self.apply(profit(amount="11")))
        self.assert_unmutated(lambda: self.apply(profit(amount="11"), operation_id="redelivery"))

    def test_overlapping_realization_and_reused_cash_backing_rejected(self):
        self.apply()
        self.assert_unmutated(lambda: self.apply(replace(profit("p2"), source_realization_ids=("realization-profit-1",))))
        self.assert_unmutated(lambda: self.apply(replace(profit("p2"), cash_backing_id="cash-profit-1")))
        self.assert_unmutated(lambda: self.apply(replace(profit("p2"), source_realization_ids=("a", "a"))))

    def test_deposits_unrealized_unconfirmed_stale_and_wrong_currency_rejected(self):
        bad_events = [replace(profit(), kind="DEPOSIT"), replace(profit(), kind="UNREALIZED_PNL"),
                      replace(profit(), confirmed=False), replace(profit(), settled=False),
                      replace(profit(), final=False), replace(profit(), verified=False),
                      replace(profit(), verified=1), replace(profit(), currency="EUR"),
                      replace(profit(), account_id="another-account"),
                      replace(profit(), earmarked_profit_cash=D("9")),
                      replace(profit(), verified_at=1001), replace(profit(), expires_at=999),
                      replace(profit(), settled_at=899), profit(amount="0")]
        for event in bad_events:
            with self.subTest(event=event):
                self.assert_unmutated(lambda event=event: self.apply(event))
        self.assert_unmutated(lambda: self.apply(now=1061))
        stale = replace(profit(when=1000), verified_at=1400, expires_at=1460)
        self.assert_unmutated(lambda: self.apply(stale, now=1400))

    def test_exact_amounts_no_float_nan_infinity_or_silent_rounding(self):
        for amount in [10.0, True, D("NaN"), D("Infinity"), D("-1"), D("0.000000001"), "garbage", D("1e1000")]:
            self.assert_unmutated(lambda amount=amount: self.apply(replace(profit(), fee_net_realized_profit=amount)))

    def test_decimal_context_cannot_round_accounting(self):
        event = profit(amount="123456789012345678.12345678")
        # Construct totals under normal precision first, then deliberately lower it.
        with localcontext() as context:
            context.prec = 3
            self.apply(event)
            audit = self.ledger.audit()
        self.assertEqual(audit["eligible_profit"], "123456789012345678.12345678")
        self.assertTrue(audit["conserved"])

    def test_no_high_water_mark_gate(self):
        # An upstream loss creates no allocation and is not a gate on later profit.
        self.assert_unmutated(lambda: self.apply(replace(profit("loss"), fee_net_realized_profit=D("-1"))))
        self.apply(profit("later-profit", "0.03"))
        self.assertEqual(self.ledger.audit()["available_entitlements"], "0.03000000")

    def test_reserve_commit_release_conserve_exactly(self):
        self.apply()
        self.reserve()
        self.assertEqual(self.ledger.audit()["reserved_entitlements"], "2.00000000")
        self.ledger.commit(operation_id="commit1", reservation_id="r1", proof=commit_proof(), now=1002)
        self.assertEqual(self.ledger.audit()["committed_profit_cash"], "1.00000000")
        self.ledger.release(operation_id="release1", reservation_id="r1", proof=release_proof(), now=1003)
        audit = self.ledger.audit()
        self.assertEqual(audit["available_entitlements"], "8.99000000")
        self.assertEqual(audit["reserved_entitlements"], "0.00000000")
        self.assertEqual(audit["reservations"], {"r1": "RELEASED"})
        self.assertTrue(audit["conserved"])

    def test_full_commit_is_terminal_and_exact_retries_are_noops(self):
        self.apply()
        reserve_receipt = self.reserve()
        receipt = self.ledger.commit(operation_id="commit1", reservation_id="r1", proof=commit_proof(amount="2"), now=1002)
        before = self.ledger.audit()
        self.ledger = self.open()
        self.assertEqual(self.reserve(now=5000), reserve_receipt)
        self.assertEqual(self.ledger.commit(operation_id="commit1", reservation_id="r1", proof=commit_proof(amount="2"), now=5000), receipt)
        self.assertEqual(before, self.ledger.audit())
        self.assert_unmutated(lambda: self.ledger.release(operation_id="release1", reservation_id="r1", proof=release_proof(), now=1003))

    def test_release_retry_after_restart_and_late_commit_fails_closed(self):
        self.apply()
        self.reserve()
        released = self.ledger.release(operation_id="release1", reservation_id="r1", proof=release_proof(), now=1003)
        before = self.ledger.audit()
        self.ledger = self.open()
        self.assertEqual(self.ledger.release(operation_id="release1", reservation_id="r1", proof=release_proof(), now=5000), released)
        self.assertEqual(before, self.ledger.audit())
        self.assert_unmutated(lambda: self.ledger.commit(operation_id="late", reservation_id="r1", proof=commit_proof(when=1004), now=1004))

    def test_all_native_guards_required_and_topups_rejected(self):
        self.apply()
        for name in ("native_cash_reserved", "signal_eligible", "reserve_guard_passed", "minimum_order_guard_passed",
                     "risk_guard_passed", "sizing_guard_passed", "balance_guard_passed"):
            self.assert_unmutated(lambda name=name: self.reserve(replace(permit(), **{name: False})))
        self.assert_unmutated(lambda: self.reserve(replace(permit(), held_position_topup=True)))
        self.assert_unmutated(lambda: self.reserve(replace(permit(), kind="TOP_UP")))
        self.assert_unmutated(lambda: self.reserve(replace(permit(), owner_id="wrong-owner")))
        self.assert_unmutated(lambda: self.reserve(replace(permit(), role="VCAP")))
        self.assert_unmutated(lambda: self.reserve(replace(permit(), allocation_id="unknown")))
        self.assert_unmutated(lambda: self.reserve(permit(amount="3.34")))
        self.assert_unmutated(lambda: self.reserve(permit(amount="0.001")))
        self.assert_unmutated(lambda: self.reserve(replace(permit(), full_fee_inclusive_budget=D("51"))))
        self.assert_unmutated(lambda: self.reserve(permit(when=999)))
        self.assert_unmutated(lambda: self.reserve(now=1062))

    def test_native_identity_cannot_fund_two_reservations(self):
        self.apply()
        self.reserve()
        self.assert_unmutated(lambda: self.reserve(reservation_id="r2"))
        for field in ("permit_id", "native_reservation_id", "entry_id"):
            duplicate = replace(permit("entry-2", "0.50"), **{field: getattr(permit(), field)})
            self.assert_unmutated(lambda duplicate=duplicate: self.reserve(duplicate, reservation_id="r2"))

    def test_commit_ordering_provenance_and_confirmations_fail_closed(self):
        self.assert_unmutated(lambda: self.ledger.commit(operation_id="c", reservation_id="r1", proof=commit_proof(), now=1002))
        self.assert_unmutated(lambda: self.ledger.release(operation_id="x", reservation_id="r1", proof=release_proof(), now=1003))
        self.apply()
        self.reserve()
        bad_proofs = [replace(commit_proof(), owner_id="wrong"), replace(commit_proof(), settled=False),
                      replace(commit_proof(), confirmed=False), replace(commit_proof(), verified=False),
                      replace(commit_proof(), settled_at=1000), replace(commit_proof(), settled_at=1003),
                      replace(commit_proof(), native_reservation_id="wrong"),
                      replace(commit_proof(), total_fee_inclusive_cash_debit=D("1")),
                      commit_proof(amount="2.01"),
                      replace(commit_proof(), base_cash_used=D("51"), total_fee_inclusive_cash_debit=D("52"))]
        for bad in bad_proofs:
            self.assert_unmutated(lambda bad=bad: self.ledger.commit(operation_id="c", reservation_id="r1", proof=bad, now=1002))
        self.assert_unmutated(lambda: self.ledger.commit(operation_id="c", reservation_id="r1", proof=commit_proof(), now=1063))

    def test_duplicate_settlement_under_new_operation_cannot_spend_twice(self):
        self.apply()
        self.reserve()
        self.ledger.commit(operation_id="c1", reservation_id="r1", proof=commit_proof(), now=1002)
        self.assert_unmutated(lambda: self.ledger.commit(operation_id="c2", reservation_id="r1", proof=commit_proof(), now=1002))

    def test_cumulative_base_debits_cannot_exceed_native_budget(self):
        self.apply()
        self.reserve()
        first = replace(commit_proof(amount="0.5"), base_cash_used=D("40"), total_fee_inclusive_cash_debit=D("40.5"))
        self.ledger.commit(operation_id="c1", reservation_id="r1", proof=first, now=1002)
        self.assert_unmutated(lambda: self.ledger.commit(operation_id="c2", reservation_id="r1", proof=commit_proof(proof_id="fill-2"), now=1002))

    def test_release_requires_native_terminal_reconciliation_and_does_not_expire_automatically(self):
        self.apply()
        self.reserve()
        for field in ("native_reservation_closed", "no_unreconciled_fills", "no_remaining_order_liability", "verified"):
            self.assert_unmutated(lambda field=field: self.ledger.release(operation_id="release", reservation_id="r1", proof=replace(release_proof(), **{field: False}), now=1003))
        self.assert_unmutated(lambda: self.ledger.release(operation_id="release", reservation_id="r1", proof=release_proof(), now=5000))
        self.assertEqual(self.ledger.audit()["reserved_entitlements"], "2.00000000")

    def test_cross_action_operation_collision_rejected_and_clock_is_monotone(self):
        self.apply()
        self.assert_unmutated(lambda: self.reserve(operation_id="apply-profit-1"))
        self.assert_unmutated(lambda: self.apply(profit("p2", when=999), now=999))

    def test_restart_rejects_changed_account_or_policy(self):
        self.apply()
        for params in [dict(account_id="other", currency="USD", policy=POLICY),
                       dict(account_id="paper-account", currency="EUR", policy=POLICY),
                       dict(account_id="paper-account", currency="USD", policy=replace(POLICY, allocation_quantum=D("0.001")))]:
            with self.assertRaises(Blocked):
                Allocator(self.path, **params)

    def test_crash_boundaries_are_atomic_for_every_mutation(self):
        for action, stage in itertools.product(("profit", "reserve", "commit", "release"),
                                                ("before_write", "after_write_before_commit", "after_commit")):
            with self.subTest(action=action, stage=stage):
                path = Path(self.temp.name) / f"{action}-{stage}.db"
                self.ledger = self.open(path=path)
                if action != "profit":
                    self.apply()
                if action in ("commit", "release"):
                    self.reserve()
                before = self.ledger.audit()
                def crash(boundary):
                    if boundary == stage:
                        raise RuntimeError("simulated process loss")
                self.ledger = self.open(path=path, fault_hook=crash)
                def perform():
                    if action == "profit":
                        return self.apply()
                    if action == "reserve":
                        return self.reserve()
                    if action == "commit":
                        return self.ledger.commit(operation_id="c", reservation_id="r1", proof=commit_proof(), now=1002)
                    return self.ledger.release(operation_id="x", reservation_id="r1", proof=release_proof(), now=1003)
                with self.assertRaises(RuntimeError):
                    perform()
                self.ledger = self.open(path=path)
                after_crash = self.ledger.audit()
                if stage != "after_commit":
                    self.assertEqual(before, after_crash)
                else:
                    self.assertEqual(before["sequence"] + 1, after_crash["sequence"])
                result = perform()
                final = self.ledger.audit()
                self.assertEqual(final["sequence"], before["sequence"] + 1)
                self.assertEqual(result, perform())
                self.assertEqual(final, self.ledger.audit())
                self.assertTrue(final["conserved"])

    def test_parallel_repeated_event_delivery_allocates_once(self):
        def deliver(_):
            worker = self.open()
            return worker.apply_profit(operation_id="apply1", snapshot=membership(), event=profit(), now=1000)
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(deliver, range(24)))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual(self.ledger.audit()["event_count"], 1)

    def test_actual_process_exit_at_every_transaction_boundary_recovers_once(self):
        module_dir = str(Path(__file__).resolve().parent)
        for action, stage in itertools.product(("profit", "reserve", "commit", "release"),
                                                ("before_write", "after_write_before_commit", "after_commit")):
            with self.subTest(action=action, stage=stage):
                path = Path(self.temp.name) / f"process-{action}-{stage}.db"
                self.ledger = self.open(path=path)
                if action != "profit":
                    self.apply()
                if action in ("commit", "release"):
                    self.reserve()
                before = self.ledger.audit()
                expressions = {
                    "profit": "ledger.apply_profit(operation_id='apply-profit-1', snapshot=membership(), event=profit(), now=1000)",
                    "reserve": "ledger.reserve(operation_id='reserve-r1', reservation_id='r1', snapshot_id='members-1', permit=permit(), now=1001)",
                    "commit": "ledger.commit(operation_id='c', reservation_id='r1', proof=commit_proof(), now=1002)",
                    "release": "ledger.release(operation_id='x', reservation_id='r1', proof=release_proof(), now=1003)",
                }
                code = ("import os, sys\n" + f"sys.path.insert(0, {module_dir!r})\n" +
                        "from test_next_entry_allocator import *\n" +
                        f"def crash(phase):\n    if phase == {stage!r}: os._exit(73)\n" +
                        f"ledger = Allocator({str(path)!r}, account_id='paper-account', currency='USD', policy=POLICY, fault_hook=crash)\n" +
                        expressions[action] + "\n")
                child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
                self.assertEqual(child.returncode, 73, child.stderr)
                self.ledger = self.open(path=path)
                if stage != "after_commit":
                    self.assertEqual(before, self.ledger.audit())
                result = eval(expressions[action], globals(), {"ledger": self.ledger})
                final = self.ledger.audit()
                self.assertEqual(final["sequence"], before["sequence"] + 1)
                self.assertEqual(result, eval(expressions[action], globals(), {"ledger": self.ledger}))
                self.assertEqual(final, self.ledger.audit())
                self.assertTrue(final["conserved"])

    def test_parallel_reservations_cannot_overspend_member(self):
        self.apply()
        def attempt(n):
            worker = self.open()
            try:
                worker.reserve(operation_id=f"reserve-{n}", reservation_id=f"r{n}", snapshot_id="members-1", permit=permit(f"e{n}", "2"), now=1001)
                return True
            except Blocked:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.ledger.audit()["reserved_entitlements"], "2.00000000")

    def test_event_arrival_order_changes_provenance_but_not_equal_totals(self):
        outputs = []
        for idx, order in enumerate(itertools.permutations(["0.014", "0.023", "0.037"])):
            ledger = self.open(path=Path(self.temp.name) / f"order-{idx}.db")
            for n, amount in enumerate(order):
                ledger.apply_profit(operation_id=f"p{n}", snapshot=membership(), event=profit(f"p{n}", amount), now=1000)
            audit = ledger.audit()
            outputs.append((audit["available_entitlements"], audit["carry"], tuple(b["available"] for b in audit["balances"])))
        self.assertEqual(len(set(outputs)), 1)

    def test_seeded_synthetic_events_and_cash_movements_preserve_invariants(self):
        rng = random.Random(7319)
        for n in range(60):
            now = 1000 + n * 4
            amount = D(rng.randint(1, 100000)) / D("10000")
            self.apply(profit(f"p{n}", amount, when=now), now=now)
            balance = next(b for b in self.ledger.audit()["balances"] if b["allocation_id"] == "position-0")
            if D(balance["available"]) >= 1:
                entry = permit(f"entry-{n}", "1", when=now + 1)
                rid = f"r{n}"
                self.reserve(entry, reservation_id=rid, now=now + 1)
                if n % 2:
                    self.ledger.commit(operation_id=f"c{n}", reservation_id=rid, proof=commit_proof(entry, "0.413579", proof_id=f"fill-{n}", when=now + 2), now=now + 2)
                self.ledger.release(operation_id=f"x{n}", reservation_id=rid, proof=release_proof(entry, when=now + 3), now=now + 3)
            if n % 5 == 0:
                self.ledger = self.open()
            self.assertTrue(self.ledger.audit()["conserved"])

    def test_external_mutation_of_audit_copy_cannot_change_ledger(self):
        self.apply()
        before = self.ledger.audit()
        copy = self.ledger.export_research_state()
        copy["events"].clear()
        self.assertEqual(before, self.ledger.audit())

    def test_reused_proof_cannot_duplicate_profit_reserve_commit_or_release(self):
        self.apply()
        self.assert_unmutated(lambda: self.apply(replace(profit("p2"), proof_id=profit().proof_id)))
        self.reserve()
        self.assert_unmutated(lambda: self.reserve(replace(permit("e2", "0.50"), proof_id=permit().proof_id), reservation_id="r2"))
        self.ledger.commit(operation_id="c1", reservation_id="r1", proof=commit_proof(), now=1002)
        repeated_proof = replace(commit_proof(proof_id="different-fill"), proof_id=commit_proof().proof_id)
        self.assert_unmutated(lambda: self.ledger.commit(operation_id="c2", reservation_id="r1", proof=repeated_proof, now=1002))
        self.assert_unmutated(lambda: self.ledger.release(operation_id="x", reservation_id="r1", proof=replace(release_proof(), proof_id=profit().proof_id), now=1003))
        self.ledger = self.open()
        self.assert_unmutated(lambda: self.apply(replace(profit("p3", when=1003), proof_id=permit().proof_id), now=1003))

    def test_release_must_follow_latest_debit_verification_including_restart(self):
        self.apply()
        self.reserve()
        self.ledger.commit(operation_id="c", reservation_id="r1", proof=commit_proof(when=1005), now=1005)
        self.ledger = self.open()
        for when in (1003, 1005):
            self.assert_unmutated(lambda when=when: self.ledger.release(operation_id="x", reservation_id="r1", proof=release_proof(when=when), now=1006))
        self.ledger.release(operation_id="x", reservation_id="r1", proof=release_proof(when=1006), now=1006)
        self.assertEqual(self.ledger.audit()["reservations"]["r1"], "RELEASED")

    def test_same_source_cash_cannot_be_silently_moved_between_owners(self):
        self.apply()
        with sqlite3.connect(self.path) as conn:
            state = json.loads(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            buckets = list(state["buckets"].values())
            buckets[0][0]["units"] -= 1
            buckets[1][0]["units"] += 1
            conn.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state),))
        with self.assertRaisesRegex(Blocked, "beneficiary"):
            self.open()

    def test_each_deduplication_index_is_reconstructed_on_restart(self):
        self.apply()
        self.reserve()
        self.ledger.commit(operation_id="c", reservation_id="r1", proof=commit_proof(), now=1002)
        original = self.ledger.export_research_state()
        for index in ("source_ids", "backing_ids", "proof_ids", "permit_ids", "entry_ids", "native_reservation_ids", "settlement_ids"):
            with self.subTest(index=index):
                corrupt = json.loads(json.dumps(original))
                key = next(iter(corrupt[index]))
                corrupt[index][key] = "wrong-target"
                with sqlite3.connect(self.path) as conn:
                    conn.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(corrupt),))
                with self.assertRaises(Blocked):
                    self.open()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(original),))
        self.assertTrue(self.open().audit()["conserved"])

    def test_profit_fingerprint_binds_membership_and_immutable_economics(self):
        first = self.apply()
        self.assertEqual(len(first["input_fingerprint"]), 64)
        second = self.apply(operation_id="redelivery", now=1100)
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])
        self.assert_unmutated(lambda: self.apply(replace(profit(), source_owner_id="different-owner"), operation_id="changed", now=1100))

    def test_fixed_fifteen_assets_plus_vcap_gives_sixteen_equal_shares(self):
        result = self.apply(profit(amount="16"), membership(count=15))
        self.assertEqual(result["member_count"], 16)
        self.assertEqual(result["equal_share"], "1.00000000")
        self.assertEqual({grant["amount"] for grant in result["grants"]}, {"1.00000000"})

    def test_carry_source_age_cannot_authorize_entry_before_its_grant(self):
        self.apply(profit("p1", "0.01", when=1000), membership(count=1), now=1000)
        self.apply(profit("p2", "0.01", when=1040), membership(count=1), now=1040)
        for approved_at in (1020, 1040):
            self.assert_unmutated(lambda approved_at=approved_at: self.reserve(permit(amount="0.01", when=approved_at), now=1041))
        result = self.reserve(permit(amount="0.01", when=1041), now=1041)
        self.assertEqual(result["source_lots"], [{"event_id": "p1", "grant_event_id": "p2", "amount": "0.01000000"}])

    def test_same_source_retains_distinct_grant_ages_and_release_restores_oldest(self):
        self.apply(profit("p1", "0.03", when=1000), membership(count=1), now=1000)
        old = permit("old-entry", "0.01", when=1001)
        self.reserve(old)
        self.apply(profit("p2", "0.01", when=1040), membership(count=1), now=1040)
        self.ledger.release(operation_id="release-old", reservation_id="r1", proof=release_proof(old, when=1041), now=1041)
        self.ledger = self.open()
        result = self.reserve(permit("valid-old-grant", "0.01", when=1020), reservation_id="r2", now=1042)
        self.assertEqual(result["source_lots"][0]["event_id"], "p1")
        self.assertEqual(result["source_lots"][0]["grant_event_id"], "p1")
        self.assert_unmutated(lambda: self.reserve(permit("invalid-new-grant", "0.01", when=1020), reservation_id="r3", now=1042))
        self.reserve(permit("valid-new-grant", "0.01", when=1042), reservation_id="r3", now=1042)

    def test_corrupt_cash_state_is_detected_before_new_mutation(self):
        self.apply()
        with sqlite3.connect(self.path) as conn:
            state = json.loads(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            next(iter(state["buckets"].values()))[0]["units"] += 1
            conn.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state),))
        with self.assertRaises(Blocked):
            self.ledger.audit()
        with self.assertRaises(Blocked):
            self.apply(profit("p2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
