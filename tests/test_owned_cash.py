"""Synthetic continuous-account owner tests; no historical price outcomes."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
import copy
import json
import unittest

from research.danger_room.module_coordination import Owner
from research.danger_room.owned_cash import OwnedCashLedger, OwnerFundingError, SaleAllocation, all_in_quote, allocate_sale_proceeds


def buy_entry(ledger, *, proposal="buy:1", lot="lot:1", parent="1", pair="COIN-USD",
              quantity="10", gross="99", fee="1", limit="100"):
    ledger.reserve_buy(proposal, kind="ENTRY", parent_id=f"pending-entry:{proposal}", pair=pair,
                       all_in_limit=D(limit), equity_before=D("2000"))
    ledger.confirm_buy(f"fill:{proposal}", proposal, lot_id=lot, quantity=D(quantity),
                       gross_quote=D(gross), entry_fee=D(fee), confirmed_parent_id=parent,
                       equity_before=D("2000"))
    ledger.confirm_terminal(f"terminal:{proposal}", proposal, terminal_status="FILLED", exact_zero_remaining=True)


def buy_recovery(ledger, *, proposal="buy:2", lot="lot:2", parent="1", pair="COIN-USD",
                 quantity="10", gross="99", fee="1", limit="100"):
    ledger.reserve_buy(proposal, kind="RECOVERY", parent_id=parent, pair=pair,
                       all_in_limit=D(limit), equity_before=D("2000"))
    ledger.confirm_buy(f"fill:{proposal}", proposal, lot_id=lot, quantity=D(quantity),
                       gross_quote=D(gross), entry_fee=D(fee), confirmed_parent_id=parent,
                       equity_before=D("2000"))
    ledger.confirm_terminal(f"terminal:{proposal}", proposal, terminal_status="FILLED", exact_zero_remaining=True)


def allocation(lot="lot:1", qty="10", basis="99", entry_fee="1", gross="111", exit_fee="1"):
    return SaleAllocation(lot, D(qty), D(basis), D(entry_fee), D(gross), D(exit_fee))


class OwnedCashTests(unittest.TestCase):
    def test_initial_capital_has_exact_finite_owners(self):
        ledger = OwnedCashLedger()
        self.assertEqual(ledger.cash_total, D("2000"))
        self.assertEqual(ledger.cash_by_owner[Owner.PRODUCTIVE], D("1500"))
        self.assertEqual(ledger.cash_by_owner[Owner.VCAP], D("500"))
        for bad in (D("NaN"), D("Infinity"), D("-1"), D("0"), 2000.0):
            with self.assertRaises(ValueError):
                OwnedCashLedger(bad)

    def test_reservation_locks_cash_without_spending_or_creating_a_lot(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        self.assertEqual(ledger.cash_total, D("2000"))
        self.assertEqual(ledger.available(Owner.PRODUCTIVE), D("1400"))
        self.assertEqual(ledger.held(), D("100"))
        self.assertEqual(len(ledger.lots), 0)

    def test_entry_and_recovery_draw_only_their_original_owner(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger)
        self.assertEqual(ledger.cash_by_owner[Owner.PRODUCTIVE], D("1400"))
        self.assertEqual(ledger.cash_by_owner[Owner.VCAP], D("400"))
        self.assertEqual(ledger.lots["lot:1"].owner, Owner.PRODUCTIVE)
        self.assertEqual(ledger.lots["lot:2"].owner, Owner.VCAP)
        self.assertEqual(ledger.fees_total, D("2"))
        self.assertEqual(ledger.snapshot()["remaining_basis"], "200")

    def test_reservation_replay_cannot_lock_twice_or_change_scope(self):
        ledger = OwnedCashLedger()
        args = dict(kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        first = ledger.reserve_buy("p", **args)
        self.assertEqual(ledger.reserve_buy("p", **args), first)
        self.assertEqual(ledger.held(), D("100"))
        with self.assertRaises(ValueError):
            ledger.reserve_buy("p", **dict(args, all_in_limit=D("101")))

    def test_buy_receipt_replay_and_conflicting_receipt_are_atomic(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        args = dict(lot_id="lot", quantity=D("10"), gross_quote=D("99"), entry_fee=D("1"), confirmed_parent_id="1", equity_before=D("2000"))
        ledger.confirm_buy("fill", "p", **args)
        before = ledger.snapshot()
        ledger.confirm_buy("fill", "p", **args)
        self.assertEqual(ledger.snapshot(), before)
        with self.assertRaises(ValueError):
            ledger.confirm_buy("fill", "p", **dict(args, entry_fee=D("2")))
        self.assertEqual(ledger.snapshot(), before)

    def test_pending_partial_fill_keeps_remaining_hold_until_confirmed_cancel(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        ledger.confirm_buy("fill", "p", lot_id="lot", quantity=D("4"), gross_quote=D("39"), entry_fee=D("1"), confirmed_parent_id="1", equity_before=D("2000"))
        self.assertEqual(ledger.held(), D("60"))
        self.assertEqual(ledger.available(Owner.PRODUCTIVE), D("1400"))
        self.assertEqual(ledger.cash_total, D("1960"))
        before = ledger.snapshot()
        with self.assertRaises(ValueError):
            ledger.confirm_terminal("cancel", "p", terminal_status="CANCELED", exact_zero_remaining=False)
        self.assertEqual(ledger.snapshot(), before)
        released = ledger.confirm_terminal("cancel", "p", terminal_status="CANCELED", exact_zero_remaining=True)
        self.assertEqual(released, D("60"))
        self.assertEqual(ledger.cash_total, D("1960"))
        self.assertEqual(ledger.available(Owner.PRODUCTIVE), D("1460"))
        self.assertEqual(ledger.confirm_terminal("cancel", "p", terminal_status="CANCELED", exact_zero_remaining=True), D("0"))

    def test_multiple_actual_partial_buy_fills_preserve_one_lot_and_fees_once(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        for i, quantity, gross, fee in ((1, "4", "39", "1"), (2, "6", "58.5", "1.5")):
            ledger.confirm_buy(f"f{i}", "p", lot_id="lot", quantity=D(quantity), gross_quote=D(gross), entry_fee=D(fee), confirmed_parent_id="1", equity_before=D("2000"))
        self.assertEqual(ledger.lots["lot"].quantity, D("10"))
        self.assertEqual(ledger.lots["lot"].basis, D("100"))
        self.assertEqual(ledger.entry_fees, D("2.5"))
        self.assertEqual(ledger.held(), D("0"))

    def test_whole_profit_returns_principal_and_splits_only_net_profit(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        added = ledger.settle_sale("sale", (allocation(),), sale_kind="WHOLE", deployable=True)
        self.assertEqual(added[Owner.PRODUCTIVE], D("107.5"))
        self.assertEqual(added[Owner.VCAP], D("2.5"))
        self.assertEqual(ledger.cash_by_owner[Owner.PRODUCTIVE], D("1507.5"))
        self.assertEqual(ledger.cash_by_owner[Owner.VCAP], D("502.5"))
        self.assertEqual(ledger.realized_pnl, D("10"))
        self.assertEqual(ledger.fees_total, D("2"))
        self.assertEqual(len(ledger.lots), 0)

    def test_loss_is_borne_by_exact_origin_lot_including_its_fees(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        ledger.settle_sale("loss", (allocation(gross="91"),), sale_kind="WHOLE", deployable=True)
        self.assertEqual(ledger.cash_by_owner[Owner.PRODUCTIVE], D("1490"))
        self.assertEqual(ledger.cash_by_owner[Owner.VCAP], D("500"))
        self.assertEqual(ledger.realized_pnl, D("-10"))

    def test_mixed_loss_and_profit_are_lot_specific_and_total_conserved(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger, gross="49", fee="1", limit="50")
        ledger.settle_sale("sale", (allocation(gross="81"), allocation(lot="lot:2", basis="49", gross="81")), sale_kind="WHOLE", deployable=True)
        self.assertEqual(ledger.realized_pnl, D("10"))
        self.assertEqual(ledger.cash_total, D("2010"))
        self.assertEqual(ledger.cash_by_owner[Owner.PRODUCTIVE], D("1502.5"))
        self.assertEqual(ledger.cash_by_owner[Owner.VCAP], D("507.5"))

    def test_control_partial_proceeds_are_owned_and_held_not_deployable(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger)
        ledger.settle_sale("partial", (allocation(lot="lot:2"),), sale_kind="PARTIAL", deployable=False)
        self.assertEqual(ledger.held(Owner.PRODUCTIVE), D("7.5"))
        self.assertEqual(ledger.held(Owner.VCAP), D("102.5"))
        self.assertEqual(ledger.available(Owner.PRODUCTIVE), D("1400"))
        self.assertEqual(ledger.available(Owner.VCAP), D("400"))
        before = ledger.snapshot()
        with self.assertRaises(ValueError):
            ledger.release_confirmed_partial("release", "partial", declared_redeploy=False)
        self.assertEqual(ledger.snapshot(), before)

    def test_confirmed_partial_release_is_idempotent_no_cash_creation_or_owner_transfer(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger)
        ledger.settle_sale("partial", (allocation(lot="lot:2"),), sale_kind="PARTIAL", deployable=False)
        cash_before = dict(ledger.cash_by_owner)
        released = ledger.release_confirmed_partial("release", "partial", declared_redeploy=True)
        self.assertEqual(released[Owner.VCAP], D("102.5"))
        self.assertEqual(released[Owner.PRODUCTIVE], D("7.5"))
        self.assertEqual(dict(ledger.cash_by_owner), cash_before)
        self.assertEqual(ledger.held(), D("0"))
        self.assertEqual(dict(ledger.release_confirmed_partial("release", "partial", declared_redeploy=True)), {Owner.PRODUCTIVE: D("0"), Owner.VCAP: D("0")})
        with self.assertRaises(ValueError):
            ledger.release_confirmed_partial("other", "partial", declared_redeploy=True)

    def test_sale_receipt_replay_cannot_credit_twice_or_dispose_another_lot(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        args = dict(sale_kind="WHOLE", deployable=True)
        ledger.settle_sale("sale", (allocation(),), **args)
        before = ledger.snapshot()
        ledger.settle_sale("sale", (allocation(),), **args)
        self.assertEqual(ledger.snapshot(), before)
        with self.assertRaises(ValueError):
            ledger.settle_sale("sale", (allocation(gross="112"),), **args)
        with self.assertRaises(ValueError):
            ledger.settle_sale("different", (allocation(),), **args)
        self.assertEqual(ledger.snapshot(), before)

    def test_sale_duplicate_lots_or_oversell_fail_before_state_changes(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        before = ledger.snapshot()
        for allocs in ((allocation(), allocation()), (allocation(qty="11"),), (allocation(basis="98"),)):
            with self.assertRaises(ValueError):
                ledger.settle_sale("sale", allocs, sale_kind="WHOLE", deployable=True)
            self.assertEqual(ledger.snapshot(), before)

    def test_partial_exit_cannot_take_productive_base_entry(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        with self.assertRaises(ValueError):
            ledger.settle_sale("partial", (allocation(),), sale_kind="PARTIAL", deployable=True)

    def test_fractional_disposal_retains_exact_remaining_units_basis_and_fees(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        ledger.settle_sale("sale", (allocation(qty="5", basis="49.5", entry_fee="0.5", gross="55.5", exit_fee="0.5"),), sale_kind="WHOLE", deployable=True)
        self.assertEqual(ledger.lots["lot:1"].quantity, D("5"))
        self.assertEqual(ledger.lots["lot:1"].gross_basis, D("49.5"))
        self.assertEqual(ledger.lots["lot:1"].entry_fees, D("0.5"))
        with self.assertRaises(ValueError):
            ledger.settle_sale("bad", (allocation(qty="1", basis="1", entry_fee="0", gross="10", exit_fee="0"),), sale_kind="WHOLE", deployable=True)

    def test_pending_holds_and_owner_caps_prevent_unfunded_orders(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("large", kind="ENTRY", parent_id="pending", pair="AAA-USD", all_in_limit=D("1450"), equity_before=D("2000"))
        self.assertEqual(ledger.can_reserve(kind="ENTRY", all_in_limit=D("51"), equity_before=D("2000")), (False, "OWNER_CASH"))
        self.assertEqual(ledger.can_reserve(kind="RECOVERY", all_in_limit=D("100"), equity_before=D("2000")), (False, "PHYSICAL_RESERVE"))
        self.assertEqual(ledger.cash_total, D("2000"))

    def test_entry_never_uses_other_owner_cash(self):
        ledger = OwnedCashLedger()
        self.assertEqual(ledger.can_reserve(kind="ENTRY", all_in_limit=D("1501"), equity_before=D("0")), (False, "OWNER_CASH"))
        self.assertEqual(ledger.available(Owner.PARK), D("0"))

    def test_strict_target_and_bootstrap_floor_prevent_native_bypass(self):
        ledger = OwnedCashLedger()
        self.assertEqual(ledger.available(Owner.PRODUCTIVE, D("2000")), D("1500"))
        self.assertEqual(ledger.can_reserve(kind="ENTRY", all_in_limit=D("100"), equity_before=D("7800")), (False, "PHYSICAL_RESERVE"))
        tiny = OwnedCashLedger(D("50"))
        self.assertEqual(tiny.available(Owner.PRODUCTIVE, D("50")), D("10"))

    def test_mark_change_rechecks_all_pending_holds_before_future_fill(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        allowed, reason = ledger.can_confirm_buy("p", gross_quote=D("49"), entry_fee=D("1"), equity_before=D("7800"))
        self.assertFalse(allowed)
        self.assertEqual(reason, "PHYSICAL_RESERVE")

    def test_alias_duplicate_pending_or_active_owner_is_blocked(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        with self.assertRaises(OwnerFundingError):
            ledger.reserve_buy("other", kind="ENTRY", parent_id="other", pair="COIN-USDT", all_in_limit=D("10"), equity_before=D("2000"))
        for pair in ("BTC-USD", "XBT-USD", "ETH-USD"):
            with self.assertRaises(OwnerFundingError):
                ledger.reserve_buy(pair, kind="ENTRY", parent_id="p", pair=pair, all_in_limit=D("10"), equity_before=D("2000"))

    def test_fifty_current_and_pending_claims_are_a_hard_maximum(self):
        ledger = OwnedCashLedger()
        for index in range(50):
            ledger.reserve_buy(f"p{index}", kind="ENTRY", parent_id=f"pending{index}", pair=f"C{index}-USD", all_in_limit=D("10"), equity_before=D("2000"))
        with self.assertRaises(OwnerFundingError) as error:
            ledger.reserve_buy("extra", kind="ENTRY", parent_id="extra", pair="EXTRA-USD", all_in_limit=D("10"), equity_before=D("2000"))
        self.assertEqual(error.exception.reason, "MAXIMUM_SLOTS")
        self.assertEqual(ledger.held(), D("500"))

    def test_recovery_cannot_create_or_change_a_parent(self):
        ledger = OwnedCashLedger()
        with self.assertRaises(OwnerFundingError):
            ledger.reserve_buy("recovery", kind="RECOVERY", parent_id="none", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        buy_entry(ledger)
        with self.assertRaises(OwnerFundingError):
            ledger.reserve_buy("recovery", kind="RECOVERY", parent_id="other", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))

    def test_pending_recovery_blocks_new_entry_after_old_lots_close(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        ledger.reserve_buy("recovery", kind="RECOVERY", parent_id="1", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        ledger.settle_sale("sale", (allocation(),), sale_kind="WHOLE", deployable=True)
        with self.assertRaises(OwnerFundingError):
            ledger.reserve_buy("replacement", kind="ENTRY", parent_id="new", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        self.assertEqual(ledger.can_confirm_buy("recovery", gross_quote=D("9"), entry_fee=D("1"), equity_before=D("2000")), (False, "RECOVERY_PARENT_SCOPE"))

    def test_no_late_fill_after_terminal_and_no_reuse_of_retired_lot_id(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        with self.assertRaises(ValueError):
            ledger.confirm_buy("late", "buy:1", lot_id="lot:1", quantity=D("1"), gross_quote=D("1"), entry_fee=D("0"), confirmed_parent_id="1", equity_before=D("2000"))
        ledger.settle_sale("sale", (allocation(),), sale_kind="WHOLE", deployable=True)
        ledger.reserve_buy("new", kind="ENTRY", parent_id="new", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        with self.assertRaises(ValueError):
            ledger.confirm_buy("newfill", "new", lot_id="lot:1", quantity=D("1"), gross_quote=D("9"), entry_fee=D("1"), confirmed_parent_id="new", equity_before=D("2000"))

    def test_canceled_pending_with_no_fills_can_release_without_profit(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        self.assertEqual(ledger.confirm_terminal("cancel", "p", terminal_status="REJECTED", exact_zero_remaining=True), D("100"))
        self.assertEqual(ledger.cash_total, D("2000"))
        self.assertEqual(ledger.realized_pnl, D("0"))
        self.assertEqual(ledger.fees_total, D("0"))

    def test_claimed_filled_terminal_without_inventory_cannot_release_pending_cash(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("p", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("100"), equity_before=D("2000"))
        before = ledger.snapshot()
        with self.assertRaises(ValueError):
            ledger.confirm_terminal("unproven", "p", terminal_status="FILLED", exact_zero_remaining=True)
        self.assertEqual(ledger.snapshot(), before)

    def test_decimal_ownership_survives_small_context_and_month_boundary(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        before = ledger.snapshot()
        with localcontext() as context:
            context.prec = 3
            ledger.assert_invariants()
            self.assertEqual(ledger.snapshot(), before)
        # No implicit month reset or reserve refresh exists in this ledger.
        buy_recovery(ledger)
        ledger.assert_invariants()
        self.assertEqual(ledger.snapshot()["remaining_basis"], "200")

    def test_multiple_selected_lots_sales_fail_atomically_when_last_lot_invalid(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger)
        before = ledger.snapshot()
        with self.assertRaises(ValueError):
            ledger.settle_sale("bad", (allocation(), allocation(lot="absent")), sale_kind="WHOLE", deployable=True)
        self.assertEqual(ledger.snapshot(), before)

    def test_recovery_reservation_cannot_overlap_active_unfilled_parent_buy(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        ledger.reserve_buy("pending", kind="RECOVERY", parent_id="1", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        before = ledger.snapshot()
        with self.assertRaises(OwnerFundingError) as error:
            ledger.reserve_buy("another", kind="RECOVERY", parent_id="1", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        self.assertEqual(error.exception.reason, "PENDING_PARENT")
        self.assertEqual(ledger.snapshot(), before)

    def test_sale_allocations_cannot_fabricate_different_lot_prices_or_fee_rates(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger)
        before = ledger.snapshot()
        for items in ((allocation(), allocation(lot="lot:2", gross="112")),
                      (allocation(), allocation(lot="lot:2", exit_fee="2"))):
            with self.assertRaises(ValueError):
                ledger.settle_sale("sale", items, sale_kind="WHOLE", deployable=True)
            self.assertEqual(ledger.snapshot(), before)

    def test_sale_sum_matches_exact_confirmed_fill_amounts(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        with self.assertRaises(ValueError):
            ledger.settle_sale("sale", (allocation(),), sale_kind="WHOLE", deployable=True,
                               fill_gross_proceeds=D("112"), fill_exit_fee=D("1"))
        ledger.settle_sale("sale", (allocation(),), sale_kind="WHOLE", deployable=True,
                           fill_gross_proceeds=D("111"), fill_exit_fee=D("1"))
        self.assertEqual(ledger.realized_pnl, D("10"))

    def test_whole_sale_cannot_combine_two_economic_parents(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_entry(ledger, proposal="buy:2", lot="lot:2", parent="2", pair="OTHER-USD")
        before = ledger.snapshot()
        with self.assertRaises(ValueError):
            ledger.settle_sale("sale", (allocation(), allocation(lot="lot:2")), sale_kind="WHOLE", deployable=True)
        self.assertEqual(ledger.snapshot(), before)

    def test_checkpoint_restores_pending_fills_proceeds_and_full_replay_journal(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        buy_recovery(ledger)
        ledger.settle_sale("partial", (allocation(lot="lot:2"),), sale_kind="PARTIAL", deployable=False)
        ledger.reserve_buy("pending", kind="ENTRY", parent_id="pending", pair="OTHER-USD", all_in_limit=D("25"), equity_before=D("2000"))
        checkpoint = json.loads(json.dumps(ledger.checkpoint()))
        restored = OwnedCashLedger.from_checkpoint(checkpoint, expected_sha256=checkpoint["sha256"])
        self.assertEqual(restored.snapshot(), ledger.snapshot())
        self.assertEqual(dict(restored.journal), dict(ledger.journal))
        self.assertEqual(restored.reservations, ledger.reservations)
        self.assertEqual(restored.parked_cash_total, D("110"))
        restored.release_confirmed_partial("release", "partial", declared_redeploy=True)
        checkpoint2 = restored.checkpoint()
        restored2 = OwnedCashLedger.from_checkpoint(checkpoint2, expected_sha256=checkpoint2["sha256"])
        before = restored2.snapshot()
        restored2.release_confirmed_partial("release", "partial", declared_redeploy=True)
        self.assertEqual(restored2.snapshot(), before)
        self.assertEqual(restored2.confirm_terminal("cancel", "pending", terminal_status="CANCELED", exact_zero_remaining=True), D("25"))

    def test_checkpoint_external_hash_rejects_corruption_without_mutating_source(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger)
        checkpoint = ledger.checkpoint()
        bad = copy.deepcopy(checkpoint)
        bad["state"]["cash"]["VCAP"] = "501"
        with self.assertRaises(ValueError):
            OwnedCashLedger.from_checkpoint(bad, expected_sha256=checkpoint["sha256"])
        with self.assertRaises(ValueError):
            OwnedCashLedger.from_checkpoint(checkpoint, expected_sha256="0" * 64)
        self.assertEqual(ledger.cash_by_owner[Owner.VCAP], D("500"))

    def test_checkpoint_identity_named_like_encoding_tag_is_not_ambiguous(self):
        ledger = OwnedCashLedger()
        ledger.reserve_buy("D", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=D("10"), equity_before=D("2000"))
        checkpoint = ledger.checkpoint()
        restored = OwnedCashLedger.from_checkpoint(checkpoint, expected_sha256=checkpoint["sha256"])
        self.assertEqual(restored.snapshot(), ledger.snapshot())

    def test_native_small_ulp_quote_components_reserve_without_rounding_fill(self):
        ledger = OwnedCashLedger()
        budget = D("2.415419587043000568289448759")
        gross = budget / D("1.004")
        fee = gross * D("0.004")
        limit = all_in_quote(gross, fee)
        ledger.reserve_buy("pending", kind="ENTRY", parent_id="pending", pair="COIN-USD", all_in_limit=limit, equity_before=D("2000"))
        ledger.confirm_buy("fill", "pending", lot_id="lot", quantity=D("1"), gross_quote=gross,
                           entry_fee=fee, confirmed_parent_id="1", equity_before=D("2000"))
        self.assertEqual(ledger.lots["lot"].gross_basis, gross)
        self.assertEqual(ledger.lots["lot"].entry_fees, fee)
        self.assertEqual(ledger.held(), D("0"))

    def test_whole_fill_allocates_final_decimal_residual_exactly(self):
        ledger = OwnedCashLedger()
        buy_entry(ledger, quantity="1", gross="9", fee="1", limit="10")
        buy_recovery(ledger, quantity="1", gross="9", fee="1", limit="10")
        buy_recovery(ledger, proposal="buy:3", lot="lot:3", quantity="1", gross="9", fee="1", limit="10")
        allocations = allocate_sale_proceeds(tuple(ledger.lots.values()), gross_proceeds=D("40"), exit_fee=D("1"))
        with localcontext() as context:
            context.prec = 60
            self.assertEqual(sum((item.gross_proceeds for item in allocations), D("0")), D("40"))
            self.assertEqual(sum((item.exit_fee for item in allocations), D("0")), D("1"))
        ledger.settle_sale("sale", allocations, sale_kind="WHOLE", deployable=True,
                           fill_gross_proceeds=D("40"), fill_exit_fee=D("1"))
        self.assertEqual(ledger.realized_pnl, D("9"))
        self.assertEqual(ledger.cash_total, D("2009"))

    def test_health_observation_reports_hard_floor_without_redistribution(self):
        ledger = OwnedCashLedger()
        before = dict(ledger.cash_by_owner)
        health = ledger.health(D("11000"))
        self.assertFalse(health["hard_floor_ok"])
        self.assertFalse(health["target_ok"])
        self.assertEqual(dict(ledger.cash_by_owner), before)


if __name__ == "__main__":
    unittest.main()
