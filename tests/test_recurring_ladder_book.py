"""Adversarial, synthetic receipt-bound rung recurrence tests.

These tests never load a historical campaign outcome or market tape. Cash,
inventory, sale allocations and releases are recorded by the actual frozen
OwnedCashLedger. Native lot objects supply the separate consumption proof.
"""

from dataclasses import dataclass, replace
from decimal import Decimal as D, localcontext
from fractions import Fraction
import copy
import hashlib
import json
from types import SimpleNamespace
import unittest

from research.danger_room import owned_cash
from research.danger_room.receipt_recurring_ladder import RecurringLadderBook
from research.danger_room.owned_profitability_campaign_v2 import RecoveryLadder, RecoveryRung


@dataclass(frozen=True)
class Fill:
    timestamp: int
    order_id: int
    pair: str
    kind: str
    position_id: int
    quantity: D
    price: D
    gross_quote: D
    fee_quote: D
    basis_released: D = D(0)
    realized_pnl: D = D(0)
    dependency_order_id: int | None = None


@dataclass(frozen=True)
class NativeLot:
    lot_id: str
    position_id: str
    pair: str
    source: str
    base_remaining: D
    cost_basis_remaining_quote: D
    entry_fees_remaining_quote: D


def checkpoint_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


class RecurrenceFixture(unittest.TestCase):
    def setUp(self):
        self.profile = RecoveryLadder("synthetic-explicit-hypothesis", (
            RecoveryRung(D(".10"), D(".5")),
            RecoveryRung(D(".30"), D("1")),
        ))
        self.events = []
        self.book = RecurringLadderBook({"AAA-USD": self.profile}, emit=self.events.append)
        self.owner = owned_cash.OwnedCashLedger()
        self.native = SimpleNamespace(lots={})
        self.floor = SimpleNamespace(profit_floor_reached_at=None,
            peak_net_profit_bps=D("55"), peak_at=17, updated_at=0)
        self.index = 0
        self.next_order = 10
        self.entry = Fill(0, 1, "AAA-USD", "ENTRY", 1, D(".199"), D(100),
                          D("19.9"), D(".1"))
        self._owner_buy(self.entry)
        self.base = self._native_lot(self.entry)
        self.native.lots[self.base.lot_id] = self.base
        self.book.confirmed_entry(1, "AAA-USD", D(100), D(20), fill=self.entry,
            native_lot=self.base, owner_ledger=self.owner)

    def _native_lot(self, fill):
        return NativeLot(f"lot:{fill.order_id}", str(fill.position_id), fill.pair,
            "BASE_ENTRY" if fill.kind == "ENTRY" else "VCAP_RECOVERY",
            fill.quantity, fill.gross_quote, fill.fee_quote)

    def _owner_buy(self, fill, *, limit=None):
        proposal = f"buy:{fill.order_id}"
        self.owner.reserve_buy(proposal, kind=fill.kind,
            parent_id=f"pending-entry:{fill.order_id}" if fill.kind == "ENTRY" else str(fill.position_id),
            pair=fill.pair, all_in_limit=limit or owned_cash.all_in_quote(fill.gross_quote, fill.fee_quote),
            equity_before=D(2000))
        self.owner.confirm_buy(f"fill:{fill.order_id}", proposal,
            lot_id=f"lot:{fill.order_id}", quantity=fill.quantity,
            gross_quote=fill.gross_quote, entry_fee=fill.fee_quote,
            confirmed_parent_id=str(fill.position_id), equity_before=D(2000))
        self.owner.confirm_terminal(f"terminal-filled:{fill.order_id}", proposal,
            terminal_status="FILLED", exact_zero_remaining=True)

    def observe(self, close, *, index=None):
        self.index = self.index + 1 if index is None else index
        self.book.observe_completed_close(1, "AAA-USD", self.index,
            self.index * 300, None if close is None else D(close))

    def offer(self, close, *, index=None, floor=None, known=True, pending=()):
        if index is not None:
            self.index = index
        return self.book.requested_budget(1, "AAA-USD", D(close), index=self.index,
            profit_state=self.floor if floor is None else floor,
            pending_parent_known=known, pending_parent=pending)

    def buy(self, close="80", *, amount=None, reservation_limit=None, order_id=None, index=None):
        self.observe(close, index=index)
        budget, reason = self.offer(close)
        self.assertIsNotNone(budget, reason)
        amount = budget if amount is None else D(amount)
        limit = amount if reservation_limit is None else D(reservation_limit)
        order = self.next_order if order_id is None else order_id
        self.next_order = max(self.next_order, order + 1)
        proposal = f"buy:{order}"
        self.owner.reserve_buy(proposal, kind="RECOVERY", parent_id="1", pair="AAA-USD",
            all_in_limit=limit, equity_before=D(2000))
        self.book.reserve_recovery(order, 1, "AAA-USD", limit,
            owner_ledger=self.owner, index=self.index)
        gross, fee = amount - D(".1"), D(".1")
        fill = Fill((self.index + 1) * 300, order, "AAA-USD", "RECOVERY", 1,
            D(".1"), gross / D(".1"), gross, fee)
        self.owner.confirm_buy(f"fill:{order}", proposal, lot_id=f"lot:{order}",
            quantity=fill.quantity, gross_quote=gross, entry_fee=fee,
            confirmed_parent_id="1", equity_before=D(2000))
        self.owner.confirm_terminal(f"terminal-filled:{order}", proposal,
            terminal_status="FILLED", exact_zero_remaining=True)
        native_lot = self._native_lot(fill)
        self.native.lots[native_lot.lot_id] = native_lot
        self.book.confirmed_recovery(1, "AAA-USD", fill.quantity,
            owned_cash.all_in_quote(gross, fee), order_id=order, fill=fill,
            native_lot=native_lot, owner_ledger=self.owner, index=self.index + 1)
        self.index += 1
        return fill

    def dispose(self, fill, *, profit="2", release=True, sale_kind="PARTIAL",
                residual="0", sale_order=None, index=None, register=True):
        before = self.native.lots[f"lot:{fill.order_id}"]
        owned = self.owner.lots[before.lot_id]
        profit, exit_fee = D(profit), D(".1")
        gross = owned.basis + profit + exit_fee
        order = self.next_order if sale_order is None else sale_order
        self.next_order = max(self.next_order, order + 1)
        execution = self.index + 1 if index is None else index
        sale = Fill(execution * 300, order, "AAA-USD", "PARTIAL_RECYCLE", 1,
            owned.quantity, gross / owned.quantity, gross, exit_fee, owned.basis, profit)
        allocation = owned_cash.SaleAllocation(before.lot_id, owned.quantity,
            owned.gross_basis, owned.entry_fees, gross, exit_fee)
        self.owner.settle_sale(f"sale:{order}", (allocation,), sale_kind=sale_kind,
            deployable=False, fill_gross_proceeds=gross, fill_exit_fee=exit_fee)
        if release:
            self.owner.release_confirmed_partial(f"release:{order}", f"sale:{order}",
                declared_redeploy=True)
        self.native.lots[before.lot_id] = replace(before,
            base_remaining=D(residual), cost_basis_remaining_quote=D(0),
            entry_fees_remaining_quote=D(0))
        if register:
            self.book.confirm_disposal(sale, (before,), self.owner, self.native, execution)
        self.index = execution
        return sale, before

    def reset_cross(self, *, above="95", below="80"):
        self.observe(above)
        self.observe(below)
        return self.offer(below)

    def reserve_only(self, *, close="80", amount="10", order=90):
        budget, reason = self.offer(close)
        self.assertIsNotNone(budget, reason)
        self.owner.reserve_buy(f"buy:{order}", kind="RECOVERY", parent_id="1",
            pair="AAA-USD", all_in_limit=D(amount), equity_before=D(2000))
        self.book.reserve_recovery(order, 1, "AAA-USD", D(amount),
            owner_ledger=self.owner, index=self.index)
        return order

    def exact_zero_rejection(self, order):
        self.owner.confirm_terminal(f"terminal-rejected:{order}", f"buy:{order}",
            terminal_status="REJECTED", exact_zero_remaining=True)
        self.book.rejected_order(order, owner_ledger=self.owner, index=self.index + 1)

    def whole_exit(self, *, order=99):
        selected = tuple(self.owner.lots.values())
        before = tuple(self.native.lots[lot.lot_id] for lot in selected)
        basis = sum((lot.basis for lot in selected), D(0))
        quantity = sum((lot.quantity for lot in selected), D(0))
        gross, fee = basis + D("1.1"), D(".1")
        allocations = owned_cash.allocate_sale_proceeds(selected,
            gross_proceeds=gross, exit_fee=fee)
        fill = Fill((self.index + 1) * 300, order, "AAA-USD", "EXIT", 1,
            quantity, gross / quantity, gross, fee, basis, D(1))
        self.owner.settle_sale(f"sale:{order}", allocations, sale_kind="WHOLE",
            deployable=True, fill_gross_proceeds=gross, fill_exit_fee=fee)
        for lot in before:
            self.native.lots[lot.lot_id] = replace(lot, base_remaining=D(0),
                cost_basis_remaining_quote=D(0), entry_fees_remaining_quote=D(0))
        self.index += 1
        return fill

    def mutate_checkpoint(self, mutator):
        checkpoint = self.book.checkpoint()
        altered = RecurringLadderBook.from_checkpoint(checkpoint,
            expected_sha256=checkpoint["checkpoint_sha256"])
        mutator(altered)
        altered = altered.checkpoint()
        return altered


class RecurringLadderBookTests(RecurrenceFixture):
    def test_entry_binding_freezes_exact_initial_profile_and_base(self):
        parent = self.book.parents[1]
        self.assertEqual(parent["anchor"], D(100))
        self.assertEqual(parent["seed"], D(20))
        self.assertEqual(parent["base_lot_id"], "lot:1")
        self.assertEqual(parent["turnover"], Fraction(0))
        self.assertEqual(parent["consumed"], 0)
        with self.assertRaises(ValueError):
            self.book.confirmed_entry(1, "AAA-USD", D(200), D(40), fill=self.entry,
                native_lot=self.base, owner_ledger=self.owner)

    def test_original_next_rung_has_sequential_gap_priority(self):
        self.observe("50")
        self.assertEqual(self.offer("50")[0], D(10))
        self.assertEqual(self.book.parents[1]["offer"]["rung"], 0)
        self.assertFalse(self.book.parents[1]["offer"]["renewal"])

    def test_live_original_inventory_cannot_renew(self):
        self.buy()
        budget, _ = self.reset_cross()
        self.assertIsNone(budget)
        self.assertEqual(self.book.parents[1]["consumed"], 1)

    def test_profit_sale_without_explicit_release_cannot_renew(self):
        fill = self.buy()
        sale, before = self.dispose(fill, release=False, register=False)
        with self.assertRaises(ValueError):
            self.book.confirm_disposal(sale, (before,), self.owner, self.native, self.index)
        self.assertIsNone(self.reset_cross()[0])
        self.assertGreater(self.owner.parked_cash_total, D(0))

    def test_nonpositive_real_net_profit_cannot_renew(self):
        for profit in ("0", "-1"):
            with self.subTest(profit=profit):
                self.setUp()
                fill = self.buy()
                self.dispose(fill, profit=profit)
                self.assertIsNone(self.reset_cross()[0])

    def test_disposal_alone_has_no_crossing_authority_and_creates_no_cash(self):
        fill = self.buy()
        self.dispose(fill)
        before = self.owner.snapshot()
        self.observe("80")
        self.assertIsNone(self.offer("80")[0])
        self.assertEqual(self.owner.snapshot(), before)
        self.assertEqual(self.reset_cross()[0], D(10))
        self.assertEqual(self.owner.snapshot(), before)

    def test_reset_before_disposal_is_not_fresh_price_evidence(self):
        fill = self.buy()
        self.observe("95")
        self.dispose(fill)
        self.observe("80")
        self.assertIsNone(self.offer("80")[0])
        self.assertEqual(self.reset_cross()[0], D(10))

    def test_missing_completed_close_cannot_reset_or_cross(self):
        fill = self.buy()
        self.dispose(fill)
        with self.assertRaises(ValueError):
            self.observe(None)
        self.observe("80")
        self.assertIsNone(self.offer("80")[0])
        self.observe("95")
        with self.assertRaises(ValueError):
            self.observe(None)
        self.assertIsNone(self.offer("80")[0])

    def test_completed_indices_cannot_replay_or_reverse(self):
        self.observe("95", index=1)
        for index in (1, 0):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.book.observe_completed_close(1, "AAA-USD", index, index * 300, D(80))

    def test_current_pending_or_unknown_parent_state_vetoes_latched_crossing(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        self.assertIsNone(self.offer("80", known=False)[0])
        self.assertIsNone(self.offer("80", pending=("accepted-sell",))[0])
        self.assertEqual(self.offer("80")[0], D(10))

    def test_armed_floor_veto_preserves_peak_and_floor_memory(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        armed = SimpleNamespace(profit_floor_reached_at=300,
            peak_net_profit_bps=D("250"), peak_at=299, updated_at=300)
        before = copy.deepcopy(vars(armed))
        self.assertIsNone(self.offer("80", floor=armed)[0])
        self.assertEqual(vars(armed), before)
        self.assertIsNone(self.offer("80", floor=SimpleNamespace())[0])

    def test_positive_smaller_fill_consumes_an_entire_episode(self):
        fill = self.buy(amount="4", reservation_limit="10")
        self.assertEqual(self.book.parents[1]["consumed"], 1)
        self.assertEqual(self.book.parents[1]["turnover"], Fraction(4))
        self.dispose(fill)
        self.assertEqual(self.reset_cross()[0], D(10))
        self.assertEqual(self.book.parents[1]["offer"]["ordinal"], 2)

    def test_three_episodes_include_original_and_sales_never_refill_turnover(self):
        for ordinal in range(1, 4):
            if ordinal > 1:
                self.reset_cross()
            fill = self.buy()
            self.assertEqual(self.book.parents[1]["turnover"], Fraction(10 * ordinal))
            self.dispose(fill, profit="20")
            self.assertEqual(self.book.parents[1]["turnover"], Fraction(10 * ordinal))
        self.assertIsNone(self.reset_cross()[0])
        self.assertEqual(self.book.parents[1]["consumed"], 1)

    def test_original_priority_blocks_renewal_when_original_is_eligible(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross(above="95", below="70")
        self.assertEqual(self.offer("70")[0], D(20))
        offer = self.book.parents[1]["offer"]
        self.assertEqual((offer["rung"], offer["ordinal"], offer["renewal"]), (1, 1, False))
        # An unavailable current owner reservation cannot select the smaller
        # renewed first rung as an admission fallback.
        self.assertEqual(self.offer("70", pending=("native-pending",))[0], D(20))
        self.assertEqual(self.book.parents[1]["offer"]["rung"], 1)

    def test_checkpoint_roundtrip_preserves_exact_turnover_and_crossing(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        checkpoint = self.book.checkpoint()
        restored = RecurringLadderBook.from_checkpoint(checkpoint,
            expected_sha256=checkpoint["checkpoint_sha256"])
        self.assertEqual(restored.checkpoint(), checkpoint)
        result = restored.requested_budget(1, "AAA-USD", D(80), index=self.index,
            profit_state=self.floor, pending_parent_known=True, pending_parent=())
        self.assertEqual(result[0], D(10))

    def test_checkpoint_requires_external_hash_and_rejects_internal_tampering(self):
        checkpoint = self.book.checkpoint()
        with self.assertRaises(ValueError):
            RecurringLadderBook.from_checkpoint(checkpoint, expected_sha256="0" * 64)
        altered = copy.deepcopy(checkpoint)
        altered["schema_version"] = 999
        altered["checkpoint_sha256"] = checkpoint_hash({k: v for k, v in altered.items()
                                                      if k != "checkpoint_sha256"})
        with self.assertRaises((ValueError, AssertionError)):
            RecurringLadderBook.from_checkpoint(altered,
                expected_sha256=altered["checkpoint_sha256"])

    def test_deepest_used_eligible_rung_wins_without_resizing(self):
        first = self.buy("80")
        second = self.buy("65")
        self.dispose(first)
        self.dispose(second)
        self.assertEqual(self.reset_cross(above="95", below="65")[0], D(20))
        offer = self.book.parents[1]["offer"]
        self.assertEqual((offer["rung"], offer["ordinal"], offer["renewal"]), (1, 2, True))
        self.assertEqual(self.book.parents[1]["seed"], D(20))
        self.assertEqual(self.book.parents[1]["anchor"], D(100))

    def test_same_bar_reset_and_cross_are_denied(self):
        fill = self.buy()
        self.dispose(fill)
        self.observe("95")
        self.assertIsNone(self.offer("80")[0])
        with self.assertRaises(ValueError):
            self.book.observe_completed_close(1, "AAA-USD", self.index,
                self.index * 300, D(80))
        self.observe("80")
        self.assertEqual(self.offer("80")[0], D(10))

    def test_sale_execution_bar_may_reset_but_cross_requires_later_bar(self):
        fill = self.buy()
        self.dispose(fill)
        execution = self.index
        self.observe("95", index=execution)
        self.assertIsNone(self.offer("80")[0])
        self.observe("80")
        self.assertEqual(self.offer("80")[0], D(10))

    def test_zero_fill_fok_rejection_preserves_episode_and_retry_crossing(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        original_turnover = self.book.parents[1]["turnover"]
        order = self.reserve_only()
        self.assertTrue(self.book.is_renewal(order))
        self.assertEqual(self.book.parents[1]["pending"][order], Fraction(10))
        self.exact_zero_rejection(order)
        self.assertEqual(self.book.parents[1]["turnover"], original_turnover)
        self.assertEqual(len(self.book.parents[1]["episodes"][0]), 1)
        self.assertEqual(self.book.parents[1]["pending"], {})
        self.assertIsNone(self.offer("80")[0])
        self.observe("80")
        self.assertEqual(self.offer("80")[0], D(10))
        self.assertEqual(self.book.parents[1]["offer"]["ordinal"], 2)

    def test_rejection_without_actual_terminal_proof_is_atomic(self):
        self.observe("80")
        order = self.reserve_only()
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.rejected_order(order, owner_ledger=self.owner, index=self.index + 1)
        self.assertEqual(self.book.checkpoint(), before)

    def test_nonzero_buy_cannot_be_called_zero_fill_terminal_rejection(self):
        self.observe("80")
        order = self.reserve_only()
        self.owner.confirm_buy(f"partial-fill:{order}", f"buy:{order}", lot_id=f"lot:{order}",
            quantity=D(".01"), gross_quote=D(1), entry_fee=D(".01"),
            confirmed_parent_id="1", equity_before=D(2000))
        self.owner.confirm_terminal(f"terminal-rejected:{order}", f"buy:{order}",
            terminal_status="REJECTED", exact_zero_remaining=True)
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.rejected_order(order, owner_ledger=self.owner, index=self.index + 1)
        self.assertEqual(self.book.checkpoint(), before)

    def test_second_reservation_for_parent_cannot_double_commit(self):
        self.observe("80")
        order = self.reserve_only()
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.reserve_recovery(order, 1, "AAA-USD", D(10),
                owner_ledger=self.owner, index=self.index)
        self.assertEqual(self.book.checkpoint(), before)

    def test_confirmed_buy_replay_is_idempotent_but_changed_kind_is_rejected(self):
        fill = self.buy()
        native = self.native.lots[f"lot:{fill.order_id}"]
        kwargs = dict(order_id=fill.order_id, fill=fill, native_lot=native,
            owner_ledger=self.owner, index=self.index)
        before = self.book.checkpoint()
        self.book.confirmed_recovery(1, "AAA-USD", fill.quantity, D(10), **kwargs)
        self.assertEqual(self.book.checkpoint(), before)
        kwargs["fill"] = replace(fill, kind="ENTRY")
        with self.assertRaises(ValueError):
            self.book.confirmed_recovery(1, "AAA-USD", fill.quantity, D(10), **kwargs)
        self.assertEqual(self.book.checkpoint(), before)

    def test_sale_replay_is_idempotent_but_changed_kind_is_rejected(self):
        fill = self.buy()
        sale, native_before = self.dispose(fill)
        before = self.book.checkpoint()
        self.book.confirm_disposal(sale, (native_before,), self.owner, self.native, self.index)
        self.assertEqual(self.book.checkpoint(), before)
        with self.assertRaises(ValueError):
            self.book.confirm_disposal(replace(sale, kind="EXIT"), (native_before,),
                self.owner, self.native, self.index)
        self.assertEqual(self.book.checkpoint(), before)

    def test_sale_replay_cannot_substitute_native_before_components(self):
        fill = self.buy()
        sale, native_before = self.dispose(fill)
        before = self.book.checkpoint()
        substituted = replace(native_before, cost_basis_remaining_quote=D("9.91"))
        with self.assertRaises(ValueError):
            self.book.confirm_disposal(sale, (substituted,), self.owner, self.native, self.index)
        self.assertEqual(self.book.checkpoint(), before)

    def test_any_positive_native_residual_blocks_full_disposal_proof(self):
        fill = self.buy()
        sale, native_before = self.dispose(fill, residual="1E-29", register=False)
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.confirm_disposal(sale, (native_before,), self.owner, self.native, self.index)
        self.assertEqual(self.book.checkpoint(), before)
        self.assertIsNone(self.reset_cross()[0])

    def test_wrong_parent_pair_and_lot_sale_scope_fail_atomically(self):
        fill = self.buy()
        sale, native_before = self.dispose(fill, register=False)
        before = self.book.checkpoint()
        for wrong_sale, wrong_lot in (
            (replace(sale, position_id=2), native_before),
            (replace(sale, pair="OTHER-USD"), native_before),
            (sale, replace(native_before, lot_id="lot:1")),
            (sale, replace(native_before, position_id="2")),
            (sale, replace(native_before, source="BASE_ENTRY")),
        ):
            with self.subTest(sale=wrong_sale, lot=wrong_lot), self.assertRaises(ValueError):
                self.book.confirm_disposal(wrong_sale, (wrong_lot,),
                    self.owner, self.native, self.index)
            self.assertEqual(self.book.checkpoint(), before)

    def test_fractional_non_native_disposal_fails_closed_without_episode_mutation(self):
        fill = self.buy()
        native_before = self.native.lots[f"lot:{fill.order_id}"]
        owned = self.owner.lots[native_before.lot_id]
        half = owned_cash.SaleAllocation(owned.lot_id, owned.quantity / 2,
            owned.gross_basis / 2, owned.entry_fees / 2, D(6), D(".05"))
        sale = Fill((self.index + 1) * 300, 80, "AAA-USD", "PARTIAL_RECYCLE", 1,
            half.quantity, D(120), half.gross_proceeds, half.exit_fee, owned.basis / 2,
            D(".95"))
        self.owner.settle_sale("sale:80", (half,), sale_kind="PARTIAL", deployable=False,
            fill_gross_proceeds=D(6), fill_exit_fee=D(".05"))
        self.owner.release_confirmed_partial("release:80", "sale:80", declared_redeploy=True)
        self.native.lots[native_before.lot_id] = replace(native_before,
            base_remaining=half.quantity, cost_basis_remaining_quote=half.gross_basis_release,
            entry_fees_remaining_quote=half.entry_fee_release)
        self.index += 1
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.confirm_disposal(sale, (native_before,), self.owner, self.native, self.index)
        self.assertEqual(self.book.checkpoint(), before)
        self.assertIsNone(self.reset_cross()[0])

    def test_profitable_sibling_cannot_subsidize_a_losing_episode(self):
        first, second = self.buy("80"), self.buy("65")
        before = (self.native.lots[f"lot:{first.order_id}"],
                  self.native.lots[f"lot:{second.order_id}"])
        owned = tuple(self.owner.lots[lot.lot_id] for lot in before)
        allocations = owned_cash.allocate_sale_proceeds(owned,
            gross_proceeds=D(32), exit_fee=D(".2"))
        sale = Fill((self.index + 1) * 300, 81, "AAA-USD", "PARTIAL_RECYCLE", 1,
            D(".2"), D(160), D(32), D(".2"), D(30), D("1.8"))
        self.owner.settle_sale("sale:81", allocations, sale_kind="PARTIAL", deployable=False,
            fill_gross_proceeds=D(32), fill_exit_fee=D(".2"))
        self.owner.release_confirmed_partial("release:81", "sale:81", declared_redeploy=True)
        for lot in before:
            self.native.lots[lot.lot_id] = replace(lot, base_remaining=D(0),
                cost_basis_remaining_quote=D(0), entry_fees_remaining_quote=D(0))
        self.index += 1
        self.book.confirm_disposal(sale, before, self.owner, self.native, self.index)
        self.assertGreater(self.owner.realized_pnl, D(0))
        self.assertTrue(self.book.parents[1]["episodes"][0][0]["eligible_disposal"])
        self.assertFalse(self.book.parents[1]["episodes"][1][0]["eligible_disposal"])
        self.reset_cross(above="95", below="65")
        self.assertEqual(self.book.parents[1]["offer"]["rung"], 0)

    def test_renewal_does_not_clear_or_mutate_profit_memory(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        memory = copy.deepcopy(vars(self.floor))
        renewal = self.buy()
        self.assertTrue(self.book.is_renewal(renewal.order_id))
        self.assertEqual(vars(self.floor), memory)
        self.assertEqual(self.native.lots["lot:1"], self.base)

    def test_rounding_safeguard_meets_nominal_exact_fraction_ceiling(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        for fee_rate in (D(".004"), D(".008"), D(".000123456789123456789")):
            with self.subTest(fee_rate=fee_rate), localcontext() as context:
                context.prec = 28
                budget = self.book.limit_renewal_budget(1, D(10), fee_rate)
                gross = budget / (D(1) + fee_rate)
                fee = gross * fee_rate
                self.assertLessEqual(Fraction(gross) + Fraction(fee), Fraction(10))
                self.assertGreater(budget, D(0))

    def test_all_in_lifetime_ceiling_is_exact_and_cannot_be_refilled_by_profit(self):
        extra = D("10.00000000000000000000000001")
        first = self.buy(amount=str(extra))
        self.dispose(first, profit="20")
        for _ in range(2):
            self.reset_cross()
            fill = self.buy()
            self.dispose(fill, profit="20")
        for _ in range(2):
            if self.book.parents[1]["consumed"] == 2:
                self.reset_cross(above="75", below="65")
            fill = self.buy("65")
            self.dispose(fill, profit="20")
        self.reset_cross(above="75", below="65")
        parent = self.book.parents[1]
        self.assertEqual(parent["turnover_cap"], Fraction(90))
        self.assertEqual(parent["turnover"], Fraction(extra) + Fraction(60))
        remaining = Fraction(90) - parent["turnover"]
        self.assertLess(remaining, Fraction(20))
        budget = self.book.limit_renewal_budget(1, D(20), D(".004"))
        with localcontext() as context:
            context.prec = 28
            gross = budget / D("1.004")
            fee = gross * D(".004")
        self.assertLessEqual(Fraction(gross) + Fraction(fee), remaining)
        self.assertGreater(self.owner.realized_pnl, D(90))
        self.assertEqual(parent["turnover"], Fraction(extra) + Fraction(60))

    def test_recovery_reservation_requires_original_base_inventory_still_alive(self):
        first, second = self.buy("80"), self.buy("65")
        self.dispose(first)
        base_owned = self.owner.lots["lot:1"]
        allocation = owned_cash.SaleAllocation("lot:1", base_owned.quantity,
            base_owned.gross_basis, base_owned.entry_fees, D("21.1"), D(".1"))
        self.owner.settle_sale("sale:base-only", (allocation,), sale_kind="WHOLE", deployable=True)
        self.native.lots["lot:1"] = replace(self.base, base_remaining=D(0),
            cost_basis_remaining_quote=D(0), entry_fees_remaining_quote=D(0))
        self.reset_cross()
        self.assertIn(f"lot:{second.order_id}", self.owner.lots)
        self.owner.reserve_buy("buy:90", kind="RECOVERY", parent_id="1", pair="AAA-USD",
            all_in_limit=D(10), equity_before=D(2000))
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.reserve_recovery(90, 1, "AAA-USD", D(10),
                owner_ledger=self.owner, index=self.index)
        self.assertEqual(self.book.checkpoint(), before)

    def test_full_exit_permanently_retires_old_lineage(self):
        recovery = self.buy()
        self.dispose(recovery)
        self.reset_cross()
        full = self.whole_exit()
        self.book.retire_parent(full, self.owner, self.native, self.index)
        self.assertEqual(self.book.parents[1]["retired"], "sale:99")
        self.assertEqual(self.offer("50"), (None, "PARENT_RETIRED"))
        with self.assertRaises(ValueError):
            self.book.confirmed_entry(1, "AAA-USD", D(100), D(20), fill=self.entry,
                native_lot=self.base, owner_ledger=self.owner)
        entry = Fill((self.index + 1) * 300, 2, "AAA-USD", "ENTRY", 2,
            D(".1"), D(111), D("11.1"), D(".1"))
        self._owner_buy(entry)
        native = self._native_lot(entry)
        self.native.lots[native.lot_id] = native
        self.book.confirmed_entry(2, "AAA-USD", D(111), D("11.2"), fill=entry,
            native_lot=native, owner_ledger=self.owner)
        self.assertEqual(self.book.parents[2]["consumed"], 0)
        self.assertEqual(self.book.parents[2]["turnover"], Fraction(0))
        self.assertEqual(self.book.parents[1]["seed"], D(20))
        self.assertEqual(self.book.parents[1]["anchor"], D(100))

    def test_retirement_needs_source_whole_exit_receipt(self):
        bogus = Fill(300, 99, "AAA-USD", "EXIT", 1, D(".199"), D(100),
            D("19.9"), D(".1"), D(20), D("-.2"))
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.retire_parent(bogus, self.owner, self.native, 1)
        self.assertEqual(self.book.checkpoint(), before)

    def test_checkpoint_rejects_episode_order_and_proposal_substitution(self):
        self.buy()
        for field, value in (("accepted_order_id", 999),
                             ("proposal_id", "buy:999"),
                             ("reservation_receipt_id", "buy:999")):
            with self.subTest(field=field):
                altered = self.mutate_checkpoint(lambda book:
                    book.parents[1]["episodes"][0][0].__setitem__(field, value))
                with self.assertRaises((ValueError, AssertionError)):
                    RecurringLadderBook.from_checkpoint(altered,
                        expected_sha256=altered["checkpoint_sha256"])

    def test_checkpoint_rejects_fictional_disposal_and_release_receipts(self):
        fill = self.buy()
        self.dispose(fill)
        self.reset_cross()
        def mutate(book):
            episode = book.parents[1]["episodes"][0][0]
            episode["sale_receipt_ids"] = ["sale:missing"]
            episode["release_proof_ids"] = ["release:missing"]
        altered = self.mutate_checkpoint(mutate)
        with self.assertRaises((ValueError, AssertionError)):
            RecurringLadderBook.from_checkpoint(altered,
                expected_sha256=altered["checkpoint_sha256"])

    def test_checkpoint_rejects_negative_remaining_inventory(self):
        self.buy()
        altered = self.mutate_checkpoint(lambda book:
            book.parents[1]["episodes"][0][0]["remaining"].__setitem__("lot:10",
                (D("-.1"), D("9.9"), D(".1"))))
        with self.assertRaises((ValueError, AssertionError)):
            RecurringLadderBook.from_checkpoint(altered,
                expected_sha256=altered["checkpoint_sha256"])

    def test_checkpoint_rejects_pending_reservation_rung_substitution(self):
        self.observe("80")
        order = self.reserve_only()
        altered = self.mutate_checkpoint(lambda book:
            book.reservations[order].__setitem__("rung", 999))
        with self.assertRaises((ValueError, AssertionError)):
            RecurringLadderBook.from_checkpoint(altered,
                expected_sha256=altered["checkpoint_sha256"])

    def test_first_pass_floor_behavior_is_preserved(self):
        self.observe("80")
        armed = SimpleNamespace(profit_floor_reached_at=300, peak_net_profit_bps=D(250))
        self.assertEqual(self.offer("80", floor=armed)[0], D(10))
        self.assertFalse(self.book.parents[1]["offer"]["renewal"])

    def test_actual_owner_reservation_is_required_before_recurrence_hold(self):
        self.observe("80")
        self.offer("80")
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.reserve_recovery(999, 1, "AAA-USD", D(10),
                owner_ledger=self.owner, index=self.index)
        self.assertEqual(self.book.checkpoint(), before)
        self.assertEqual(self.owner.held(), D(0))

    def test_reassigning_coin_profile_does_not_rebase_existing_parent(self):
        self.book.profiles["AAA-USD"] = RecoveryLadder("later-other-hypothesis", (
            RecoveryRung(D(".5"), D(9)),))
        self.observe("80")
        self.assertEqual(self.offer("80")[0], D(10))
        self.assertEqual(self.book.parents[1]["profile_name"], self.profile.name)
        self.assertEqual(self.book.parents[1]["turnover_cap"], Fraction(90))

    def test_entry_anchor_and_seed_must_match_source_fill_exactly(self):
        for anchor, seed in ((D(101), D(20)), (D(100), D("20.00000000000000000000000001"))):
            with self.subTest(anchor=anchor, seed=seed):
                book = RecurringLadderBook({"AAA-USD": self.profile})
                with self.assertRaises(ValueError):
                    book.confirmed_entry(1, "AAA-USD", anchor, seed, fill=self.entry,
                        native_lot=self.base, owner_ledger=self.owner)
                self.assertEqual(book.parents, {})

    def test_unknown_category_blocks_original_and_renewed_admission(self):
        book = RecurringLadderBook({})
        book.confirmed_entry(1, "AAA-USD", D(100), D(20), fill=self.entry,
            native_lot=self.base, owner_ledger=self.owner)
        book.observe_completed_close(1, "AAA-USD", 1, 300, D(50))
        self.assertEqual(book.requested_budget(1, "AAA-USD", D(50), index=1,
            profit_state=self.floor, pending_parent_known=True, pending_parent=()),
            (None, "UNKNOWN_LADDER_PROFILE"))

    def test_reset_must_be_strictly_above_original_threshold(self):
        fill = self.buy()
        self.dispose(fill)
        self.observe("90")
        self.observe("80")
        self.assertIsNone(self.offer("80")[0])
        self.assertEqual(self.reset_cross(above="90.0001", below="90")[0], D(10))

    def test_completed_timestamp_cannot_move_backwards(self):
        self.observe("95", index=1)
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.observe_completed_close(1, "AAA-USD", 2, 299, D(80))
        self.assertEqual(self.book.checkpoint(), before)

    def test_original_reservation_cannot_exceed_preserved_v2_rung_maximum(self):
        self.observe("80")
        self.offer("80")
        self.owner.reserve_buy("buy:90", kind="RECOVERY", parent_id="1", pair="AAA-USD",
            all_in_limit=D(11), equity_before=D(2000))
        before = self.book.checkpoint()
        with self.assertRaises(ValueError):
            self.book.reserve_recovery(90, 1, "AAA-USD", D(11),
                owner_ledger=self.owner, index=self.index)
        self.assertEqual(self.book.checkpoint(), before)

    def test_pending_checkpoint_preserves_one_hold_until_actual_zero_rejection(self):
        self.observe("80")
        order = self.reserve_only()
        checkpoint = self.book.checkpoint()
        restored = RecurringLadderBook.from_checkpoint(checkpoint,
            expected_sha256=checkpoint["checkpoint_sha256"])
        self.assertEqual(restored.checkpoint(), checkpoint)
        self.assertEqual(restored.parents[1]["pending"], {order: Fraction(10)})
        self.owner.confirm_terminal(f"terminal-rejected:{order}", f"buy:{order}",
            terminal_status="REJECTED", exact_zero_remaining=True)
        restored.rejected_order(order, owner_ledger=self.owner, index=self.index + 1)
        self.assertEqual(restored.parents[1]["pending"], {})
        self.assertEqual(restored.parents[1]["turnover"], Fraction(0))
        self.assertEqual(restored.parents[1]["consumed"], 0)

    def test_retired_checkpoint_cannot_restore_active_lineage(self):
        full = self.whole_exit()
        self.book.retire_parent(full, self.owner, self.native, self.index)
        checkpoint = self.book.checkpoint()
        restored = RecurringLadderBook.from_checkpoint(checkpoint,
            expected_sha256=checkpoint["checkpoint_sha256"])
        self.assertEqual(restored.checkpoint(), checkpoint)
        self.assertEqual(restored.requested_budget(1, "AAA-USD", D(50), index=self.index,
            profit_state=self.floor, pending_parent_known=True, pending_parent=()),
            (None, "PARENT_RETIRED"))

    def test_live_recovery_whole_exit_replay_cannot_double_profit_or_events(self):
        recovery = self.buy()
        episode = self.book.parents[1]["episodes"][0][0]
        lot_id = f"lot:{recovery.order_id}"
        self.assertEqual(episode["net_profit"][lot_id], Fraction(0))
        full = self.whole_exit()
        owner_before = self.owner.snapshot()
        self.book.retire_parent(full, self.owner, self.native, self.index)
        before, events = self.book.checkpoint(), len(self.events)
        self.assertNotEqual(episode["net_profit"][lot_id], Fraction(0))
        self.assertEqual(episode["remaining"][lot_id], (D(0), D(0), D(0)))
        self.book.retire_parent(full, self.owner, self.native, self.index)
        self.assertEqual(self.book.checkpoint(), before)
        self.assertEqual(len(self.events), events)
        self.assertEqual(self.owner.snapshot(), owner_before)

    def test_whole_exit_receipt_replay_rejects_conflicting_fill_members(self):
        self.buy()
        full = self.whole_exit()
        self.book.retire_parent(full, self.owner, self.native, self.index)
        before, events = self.book.checkpoint(), len(self.events)
        for changed in (
            replace(full, quantity=full.quantity + D(".01")),
            replace(full, basis_released=full.basis_released + D(1)),
            replace(full, kind="PARTIAL_RECYCLE"),
            replace(full, timestamp=full.timestamp + 300),
            replace(full, price=full.price + D(1)),
        ):
            with self.subTest(fill=changed), self.assertRaises(ValueError):
                self.book.retire_parent(changed, self.owner, self.native, self.index)
            self.assertEqual(self.book.checkpoint(), before)
            self.assertEqual(len(self.events), events)

    def test_retirement_replay_after_checkpoint_restore_is_idempotent(self):
        recovery = self.buy()
        full = self.whole_exit()
        self.book.retire_parent(full, self.owner, self.native, self.index)
        checkpoint = self.book.checkpoint()
        restored = RecurringLadderBook.from_checkpoint(checkpoint,
            expected_sha256=checkpoint["checkpoint_sha256"])
        events = []
        restored.emit = events.append
        recorded_profit = restored.parents[1]["episodes"][0][0]["net_profit"][f"lot:{recovery.order_id}"]
        restored.retire_parent(full, self.owner, self.native, self.index)
        self.assertEqual(restored.checkpoint(), checkpoint)
        self.assertEqual(events, [])
        with self.assertRaises(ValueError):
            restored.retire_parent(replace(full, basis_released=full.basis_released + D(1)),
                self.owner, self.native, self.index)
        self.assertEqual(restored.checkpoint(), checkpoint)
        self.assertEqual(events, [])
        self.assertEqual(restored.parents[1]["episodes"][0][0]["net_profit"][f"lot:{recovery.order_id}"],
            recorded_profit)

    def test_first_whole_retirement_requires_exact_native_quantity_and_basis(self):
        self.buy()
        full = self.whole_exit()
        before, events = self.book.checkpoint(), len(self.events)
        for changed in (
            replace(full, quantity=full.quantity + D(".01")),
            replace(full, basis_released=full.basis_released + D(1)),
        ):
            with self.subTest(fill=changed), self.assertRaises(ValueError):
                self.book.retire_parent(changed, self.owner, self.native, self.index)
            self.assertEqual(self.book.checkpoint(), before)
            self.assertEqual(len(self.events), events)
            self.assertIsNone(self.book.parents[1]["retired"])
        self.book.retire_parent(full, self.owner, self.native, self.index)
        self.assertEqual(self.book.parents[1]["retired"], "sale:99")

    def test_checkpoint_rejects_rehashed_native_shadow_quantity_and_basis(self):
        self.buy()
        for field in ("native_quantity", "native_basis"):
            with self.subTest(field=field):
                def mutate(book):
                    parent = book.parents[1]
                    parent[field] += D(1)
                altered = self.mutate_checkpoint(mutate)
                with self.assertRaises((ValueError, AssertionError)):
                    RecurringLadderBook.from_checkpoint(altered,
                        expected_sha256=altered["checkpoint_sha256"])


if __name__ == "__main__":
    unittest.main()
