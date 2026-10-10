"""Adversarial accounting/identity checks for the research arbiter, no markets."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
from itertools import permutations
import unittest

from research.danger_room.module_coordination import (
    Action, CashBudgets, ConfirmedRecycle, CoordinationState, Intent, Owner, ParentState,
    ParentStatus, PendingOrder, PORTFOLIO_HEDGE_PARENT, Reason, ShortfallReason,
    base_asset, confirm_recovery, decide_batch, slot_shortfall, unlock_confirmed_recycle,
)


def budgets(productive="100", vcap="50", hedge="25", park="0", **kwargs):
    return CashBudgets({Owner.PRODUCTIVE: D(productive), Owner.VCAP: D(vcap),
                        Owner.HEDGE: D(hedge), Owner.PARK: D(park)}, **kwargs)


def state(*parents, **kwargs):
    kwargs.setdefault("accounting_healthy", True)
    return CoordinationState(parents=parents, **kwargs)


def entry(name="entry", parent="new", pair="NEW-USD", cost="10", priority=0):
    return Intent(name, parent, pair, Action.ENTRY, Owner.PRODUCTIVE, D(cost), priority=priority)


def recovery(name="patch", parent="p", pair="COIN-USD", cost="10", rung=0):
    return Intent(name, parent, pair, Action.RECOVERY, Owner.VCAP, D(cost), rung=rung)


def exit_intent(name="exit", parent="p", pair="COIN-USD", priority=0):
    return Intent(name, parent, pair, Action.EXIT, Owner.PRODUCTIVE, priority=priority)


def hedge(name="hedge", parent=PORTFOLIO_HEDGE_PARENT, defensive=False, cost="10", pair="BTC-USD"):
    return Intent(name, parent, pair, Action.HEDGE, Owner.HEDGE, D(cost), blocks_spot_increase=defensive)


class ModuleCoordinationTests(unittest.TestCase):
    def test_exit_always_precedes_patch_and_one_intent_per_parent(self):
        s = state(ParentState("p", "COIN-USD"))
        for proposals in permutations((recovery(), exit_intent(), exit_intent("partial", priority=10))):
            batch = decide_batch(proposals, s, budgets())
            self.assertEqual([i.intent_id for i in batch.accepted], ["exit"])
            self.assertEqual({r.reason for r in batch.rejected}, {Reason.ONE_INTENT_PER_PARENT})
            self.assertEqual(dict(batch.remaining_cash), dict(budgets().cash_by_owner))

    def test_accounting_missing_or_unhealthy_rejects_all_new_decisions(self):
        proposals = (entry(),)
        for s in (CoordinationState(), state(accounting_healthy=False)):
            batch = decide_batch(proposals, s, budgets())
            self.assertEqual(batch.rejected[0].reason, Reason.ACCOUNTING_UNHEALTHY)
            self.assertEqual(dict(batch.remaining_cash), dict(budgets().cash_by_owner))

    def test_protected_profit_blocks_recovery_without_consuming_cash(self):
        batch = decide_batch((recovery(),), state(ParentState("p", "COIN-USD", protected_profit=True)), budgets())
        self.assertEqual(batch.rejected[0].reason, Reason.PROTECTED_PROFIT)
        self.assertEqual(batch.remaining_cash[Owner.VCAP], D("50"))

    def test_consumed_rung_not_reused(self):
        batch = decide_batch((recovery(rung=3),), state(ParentState("p", "COIN-USD", consumed_rungs=(3,))), budgets())
        self.assertEqual(batch.rejected[0].reason, Reason.RUNG_ALREADY_CONSUMED)

    def test_planning_only_reserves_cost_and_does_not_consume_a_rung(self):
        parent = ParentState("p", "COIN-USD")
        batch = decide_batch((recovery(),), state(parent), budgets())
        self.assertEqual(batch.remaining_cash[Owner.VCAP], D("40"))
        self.assertEqual(parent.consumed_rungs, ())
        self.assertEqual(batch.occupied_slots, 1)

    def test_recovery_missing_receipt_or_zero_fill_never_consumes_rung(self):
        parent = ParentState("p", "COIN-USD")
        for receipt, quantity, spent in ((False, "1", "10"), (True, "0", "0"), (True, "1", "0")):
            confirmed = confirm_recovery(parent, recovery(), confirmed_fill_base=D(quantity),
                                         confirmed_quote_spent=D(spent), exact_receipt=receipt)
            self.assertEqual(confirmed, parent)

    def test_positive_partial_fill_consumes_rung_once_with_no_cash_refund(self):
        parent = ParentState("p", "COIN-USD")
        confirmed = confirm_recovery(parent, recovery(rung=4), confirmed_fill_base=D("0.2"),
                                     confirmed_quote_spent=D("2"), exact_receipt=True)
        self.assertEqual(confirmed.consumed_rungs, (4,))
        repeated = confirm_recovery(confirmed, recovery(rung=4), confirmed_fill_base=D("0.2"),
                                    confirmed_quote_spent=D("2"), exact_receipt=True)
        self.assertEqual(repeated, confirmed)

    def test_recovery_confirmation_rejects_scope_or_overspend(self):
        parent = ParentState("p", "COIN-USD")
        for proposal, amount in ((recovery(parent="wrong"), "1"),
                                 (recovery(pair="OTHER-USD"), "1"), (recovery(), "10.01")):
            with self.assertRaises(ValueError):
                confirm_recovery(parent, proposal, confirmed_fill_base=D("1"),
                                 confirmed_quote_spent=D(amount), exact_receipt=True)

    def test_confirmed_rung_cannot_be_replayed_under_a_different_proposal(self):
        confirmed = confirm_recovery(ParentState("p", "COIN-USD"), recovery(),
                                     confirmed_fill_base=D("1"), confirmed_quote_spent=D("10"), exact_receipt=True)
        with self.assertRaises(ValueError):
            confirm_recovery(confirmed, recovery(name="other"), confirmed_fill_base=D("1"),
                             confirmed_quote_spent=D("10"), exact_receipt=True)

    def test_new_slot_cannot_use_requested_exit_or_future_proceeds(self):
        s = state(ParentState("p", "COIN-USD"), max_positions=1)
        batch = decide_batch((entry(), exit_intent()), s, budgets(productive="0"))
        self.assertEqual([i.action for i in batch.accepted], [Action.EXIT])
        self.assertEqual(batch.rejected[0].reason, Reason.MAXIMUM_SLOTS)
        self.assertEqual(batch.remaining_cash[Owner.PRODUCTIVE], D("0"))
        self.assertEqual(batch.occupied_slots, 1)

    def test_pending_entry_claim_counts_before_fill(self):
        s = state(pending=(PendingOrder("pending", "OLD-USD", Action.ENTRY),), max_positions=1)
        batch = decide_batch((entry(),), s, budgets())
        self.assertEqual(batch.rejected[0].reason, Reason.MAXIMUM_SLOTS)
        self.assertEqual(batch.occupied_slots, 1)

    def test_pending_asset_blocks_other_pair_for_same_coin(self):
        s = state(pending=(PendingOrder("pending", "COIN-USD", Action.ENTRY),))
        batch = decide_batch((entry(pair="COIN-USDT"),), s, budgets())
        self.assertEqual(batch.rejected[0].reason, Reason.PENDING_ASSET_IDENTITY)

    def test_active_asset_blocks_other_quote_pair(self):
        batch = decide_batch((entry(pair="COIN-USDT"),), state(ParentState("p", "COIN-USD")), budgets())
        self.assertEqual(batch.rejected[0].reason, Reason.ACTIVE_ASSET_IDENTITY)

    def test_cancel_request_does_not_release_cash_identity_or_slot(self):
        pending = PendingOrder("pending", "COIN-USD", Action.ENTRY)
        cancel = Intent("cancel", "pending", "COIN-USD", Action.CANCEL, Owner.PRODUCTIVE)
        b = budgets(productive="10", held_by_owner={Owner.PRODUCTIVE: D("10")})
        batch = decide_batch((cancel, entry(pair="COIN-USDT"), entry("other", "other", "OTHER-USD")),
                             state(pending=(pending,)), b)
        self.assertEqual([i.intent_id for i in batch.accepted], ["cancel"])
        self.assertEqual(batch.remaining_cash[Owner.PRODUCTIVE], D("10"))
        self.assertEqual({r.reason for r in batch.rejected}, {Reason.PENDING_ASSET_IDENTITY, Reason.OWNER_CASH})
        self.assertEqual(batch.occupied_slots, 1)

    def test_cancel_without_exact_pending_scope_rejected(self):
        cancel = Intent("cancel", "pending", "OTHER-USD", Action.CANCEL, Owner.PRODUCTIVE)
        batch = decide_batch((cancel,), state(pending=(PendingOrder("pending", "COIN-USD", Action.ENTRY),)), budgets())
        self.assertEqual(batch.rejected[0].reason, Reason.PARENT_SCOPE_MISMATCH)
        self.assertEqual(decide_batch((cancel,), state(), budgets()).rejected[0].reason, Reason.CANCEL_WITHOUT_PENDING)

    def test_pending_existing_parent_prevents_patch_and_exit(self):
        s = state(ParentState("p", "COIN-USD"), pending=(PendingOrder("p", "COIN-USD", Action.EXIT),))
        batch = decide_batch((recovery(), exit_intent()), s, budgets())
        self.assertEqual({r.reason for r in batch.rejected}, {Reason.PENDING_PARENT})

    def test_dust_classification_requires_terminal_confirmation(self):
        for status in (ParentStatus.RESIDUAL, ParentStatus.RETIRED, ParentStatus.FLAT_PENDING):
            batch = decide_batch((entry(pair="COIN-USD"),),
                                 state(ParentState("p", "COIN-USD", status=status)), budgets())
            self.assertEqual(batch.rejected[0].reason, Reason.ACTIVE_ASSET_IDENTITY)

    def test_proven_residual_retirement_allows_new_identity_without_erasing_old(self):
        old = ParentState("old", "COIN-USD", status=ParentStatus.RESIDUAL, terminal_confirmed=True)
        s = state(old, max_positions=1)
        batch = decide_batch((entry(pair="COIN-USD"),), s, budgets())
        self.assertEqual(len(batch.accepted), 1)
        self.assertEqual(batch.occupied_slots, 1)
        self.assertEqual(s.parents, (old,))
        reused = decide_batch((entry(parent="old", pair="COIN-USD"),), s, budgets())
        self.assertEqual(reused.rejected[0].reason, Reason.PARENT_ID_REUSE)

    def test_fill50_target_uses_distinct_reserved_slots_only(self):
        proposals = tuple(entry(f"e{i}", f"p{i}", f"C{i}-USD", "1", priority=i) for i in range(60))
        batch = decide_batch(proposals, state(), budgets(productive="100"))
        self.assertEqual(len(batch.accepted), 50)
        self.assertEqual(batch.occupied_slots, 50)
        self.assertEqual(batch.vacant_slots, 0)
        self.assertEqual({r.reason for r in batch.rejected}, {Reason.MAXIMUM_SLOTS})
        self.assertEqual(batch.remaining_cash[Owner.PRODUCTIVE], D("50"))

    def test_fundability_can_leave_slots_short_without_borrowing_from_vcap_or_park(self):
        b = budgets(productive="10", vcap="1000", hedge="1000", park="1000")
        batch = decide_batch((entry("first"), entry("second", "second", "OTHER-USD")), state(), b)
        self.assertEqual(len(batch.accepted), 1)
        self.assertEqual(batch.rejected[0].reason, Reason.OWNER_CASH)
        self.assertEqual(batch.remaining_cash[Owner.VCAP], D("1000"))
        self.assertEqual(batch.remaining_cash[Owner.PARK], D("1000"))
        self.assertEqual(batch.remaining_cash[Owner.HEDGE], D("1000"))

    def test_wrong_owner_cannot_fund_recovery_entry_or_hedge(self):
        s = state(ParentState("p", "COIN-USD"), hedge_admitted=True)
        for proposal in (replace(entry(), owner=Owner.VCAP), replace(recovery(), owner=Owner.PRODUCTIVE),
                         replace(hedge(), owner=Owner.PARK)):
            batch = decide_batch((proposal,), s, budgets())
            self.assertEqual(batch.rejected[0].reason, Reason.OWNER_MISMATCH)

    def test_disjoint_owner_cash_hold_and_protection_preserved(self):
        b = budgets(productive="100", vcap="50", held_by_owner={Owner.PRODUCTIVE: D("80")},
                    protected_by_owner={Owner.PRODUCTIVE: D("10"), Owner.VCAP: D("40")})
        batch = decide_batch((entry(cost="10"), recovery(cost="10")), state(ParentState("p", "COIN-USD")), b)
        self.assertEqual(len(batch.accepted), 2)
        self.assertEqual(batch.remaining_cash[Owner.PRODUCTIVE], D("90"))
        self.assertEqual(batch.remaining_cash[Owner.VCAP], D("40"))
        self.assertEqual(b.cash_by_owner[Owner.PRODUCTIVE], D("100"))

    def test_absolute_physical_floor_uses_total_cash_after_existing_holds(self):
        b = budgets(productive="30", vcap="10", hedge="0", held_by_owner={Owner.PRODUCTIVE: D("5")},
                    physical_reserve_floor=D("30"))
        batch = decide_batch((entry(cost="10"),), state(), b)
        self.assertEqual(batch.rejected[0].reason, Reason.RESERVE_FLOOR)
        self.assertEqual(batch.remaining_cash[Owner.PRODUCTIVE], D("30"))

    def test_tiny_reserve_is_not_rounded_away_in_wide_cash(self):
        b = budgets(productive="1E30", vcap="1E-30", hedge="0", physical_reserve_floor=D("2E-30"))
        with localcontext() as context:
            context.prec = 6
            batch = decide_batch((entry(cost="1E30"),), state(), b)
        self.assertEqual(batch.rejected[0].reason, Reason.RESERVE_FLOOR)

    def test_deterministic_reservations_ignore_input_order(self):
        proposals = (entry("a", "a", "AAA-USD", "60"), entry("b", "b", "BBB-USD", "60"))
        outputs = [decide_batch(p, state(), budgets()) for p in permutations(proposals)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual([i.intent_id for i in outputs[0].accepted], ["a"])
        self.assertEqual(outputs[0].remaining_cash[Owner.PRODUCTIVE], D("40"))

    def test_explicit_defensive_hedge_blocks_spot_increases_across_other_parents(self):
        s = state(ParentState("p", "COIN-USD"), hedge_admitted=True)
        batch = decide_batch((entry(), recovery(), hedge(defensive=True)), s, budgets())
        self.assertEqual([i.action for i in batch.accepted], [Action.HEDGE])
        self.assertEqual({r.reason for r in batch.rejected}, {Reason.DEFENSIVE_HEDGE_GATE})
        self.assertEqual(batch.occupied_slots, 1)

    def test_nondefensive_hedge_does_not_silently_rewrite_spot_policy(self):
        batch = decide_batch((entry(), recovery(), hedge()),
                             state(ParentState("p", "COIN-USD"), hedge_admitted=True), budgets())
        self.assertEqual(len(batch.accepted), 3)

    def test_hedge_not_admitted_cannot_pause_or_spend_spot(self):
        batch = decide_batch((entry(), hedge(defensive=True)), state(), budgets())
        self.assertEqual([i.action for i in batch.accepted], [Action.ENTRY])
        self.assertEqual(batch.rejected[0].reason, Reason.HEDGE_NOT_ADMITTED)
        self.assertEqual(batch.remaining_cash[Owner.HEDGE], D("25"))

    def test_portfolio_hedge_identity_cannot_be_split_to_bypass_one_intent(self):
        batch = decide_batch((hedge("a"), hedge("b"), hedge("wrong", parent="arbitrary")),
                             state(hedge_admitted=True), budgets())
        self.assertEqual([i.intent_id for i in batch.accepted], ["a"])
        self.assertEqual({r.reason for r in batch.rejected}, {Reason.ONE_INTENT_PER_PARENT, Reason.HEDGE_IDENTITY})
        self.assertEqual(batch.remaining_cash[Owner.HEDGE], D("15"))

    def test_pending_hedge_blocks_replacement_and_does_not_occupy_spot_slot(self):
        s = state(pending=(PendingOrder(PORTFOLIO_HEDGE_PARENT, "BTC-USD", Action.HEDGE),), hedge_admitted=True)
        batch = decide_batch((hedge(), entry()), s, budgets())
        self.assertEqual([i.action for i in batch.accepted], [Action.ENTRY])
        self.assertEqual(batch.rejected[0].reason, Reason.PENDING_PARENT)
        self.assertEqual(batch.occupied_slots, 1)

    def test_authoritative_risk_gate_blocks_all_spot_increases(self):
        batch = decide_batch((entry(), recovery(), exit_intent()),
                             state(ParentState("p", "COIN-USD"), risk_increase_allowed=False), budgets())
        self.assertEqual([i.action for i in batch.accepted], [Action.EXIT])
        self.assertEqual({r.reason for r in batch.rejected}, {Reason.SPOT_RISK_GATE, Reason.ONE_INTENT_PER_PARENT})

    def test_btc_eth_spot_exclusion_and_btc_alias(self):
        for pair in ("BTC-USD", "XBT-USD", "ETH-USD"):
            batch = decide_batch((entry(pair=pair),), state(), budgets())
            self.assertEqual(batch.rejected[0].reason, Reason.EXCLUDED_SPOT_ASSET)
        self.assertEqual(base_asset("BTC-USD"), base_asset("XBT-USDT"))
        with self.assertRaises(ValueError):
            state(ParentState("a", "BTC-USD"), ParentState("b", "XBT-USDT"))

    def test_bad_cash_values_and_inconsistent_physical_cash_fail(self):
        for bad in (D("NaN"), D("Infinity"), D("-1"), 1.0, True, "1"):
            with self.assertRaises(ValueError):
                CashBudgets({Owner.PRODUCTIVE: bad})
        with self.assertRaises(ValueError):
            CashBudgets({Owner.PRODUCTIVE: D("10")}, observed_physical_cash=D("11"))
        self.assertEqual(CashBudgets({Owner.PRODUCTIVE: D("10")}, observed_physical_cash=D("10")).physical_cash, D("10"))

    def test_overlapping_locks_unknown_owner_and_bad_intent_cost_fail(self):
        with self.assertRaises(ValueError):
            budgets(productive="10", held_by_owner={Owner.PRODUCTIVE: D("6")},
                    protected_by_owner={Owner.PRODUCTIVE: D("5")})
        with self.assertRaises(ValueError):
            CashBudgets({"UNKNOWN": D("1")})
        for bad in (D("NaN"), D("Infinity"), D("-1"), D("0"), 10.0):
            with self.assertRaises(ValueError):
                replace(entry(), requested_cost=bad)

    def test_invalid_state_or_pair_scope_is_rejected(self):
        for maximum in (0, 51, True, 1.0):
            with self.assertRaises(ValueError):
                state(max_positions=maximum)
        for pair in ("coin-USD", "COINUSD", "COIN/USD", " COIN-USD", "COIN-USD-USD"):
            with self.assertRaises(ValueError):
                entry(pair=pair)
        with self.assertRaises(ValueError):
            state(ParentState("same", "AAA-USD"), ParentState("same", "BBB-USD"))
        with self.assertRaises(ValueError):
            state(ParentState("p", "COIN-USD"), pending=(PendingOrder("other", "COIN-USDT", Action.ENTRY),))
        with self.assertRaises(ValueError):
            state(ParentState("p", "COIN-USD", status=ParentStatus.RETIRED, terminal_confirmed=True),
                  pending=(PendingOrder("p", "COIN-USD", Action.EXIT),))

    def test_duplicate_proposal_ids_fail_before_any_reservation(self):
        with self.assertRaises(ValueError):
            decide_batch((entry(), entry(parent="other", pair="OTHER-USD")), state(), budgets())

    def test_slot_shortfall_keeps_typed_reasons_and_rejected_ids(self):
        batch = decide_batch((entry(),), state(), budgets(productive="0"))
        shortfall = slot_shortfall(batch, insufficient_eligible=True, liquidity_blocked=True, stale_data=True)
        self.assertEqual(shortfall.vacant_slots, 50)
        self.assertEqual(shortfall.rejected_intents, ("entry",))
        self.assertEqual(set(shortfall.reasons), {ShortfallReason.CAPITAL, ShortfallReason.INSUFFICIENT_ELIGIBLE_COINS,
                                                 ShortfallReason.LIQUIDITY, ShortfallReason.STALE_MARKET_DATA})
        filled = decide_batch((entry(),), state(max_positions=1), budgets())
        self.assertEqual(slot_shortfall(filled).reasons, ())

    def test_control_does_not_unlock_confirmed_recycle(self):
        b = budgets(vcap="50", held_by_owner={Owner.VCAP: D("20")})
        receipt = ConfirmedRecycle("receipt", "p", Owner.VCAP, Owner.VCAP, D("10"), True)
        unchanged, journal = unlock_confirmed_recycle(b, receipt)
        self.assertIs(unchanged, b)
        self.assertEqual(journal, ())
        self.assertEqual(unchanged.spendable(Owner.VCAP), D("30"))

    def test_declared_recycle_unlocks_vcap_only_and_preserves_cash_protection(self):
        b = budgets(vcap="50", held_by_owner={Owner.VCAP: D("20")},
                    protected_by_owner={Owner.VCAP: D("10")}, physical_reserve_floor=D("40"))
        receipt = ConfirmedRecycle("receipt", "p", Owner.VCAP, Owner.VCAP, D("10"), True)
        updated, journal = unlock_confirmed_recycle(b, receipt, declared_redeploy=True)
        self.assertEqual(dict(updated.cash_by_owner), dict(b.cash_by_owner))
        self.assertEqual(dict(updated.protected_by_owner), dict(b.protected_by_owner))
        self.assertEqual(updated.physical_cash, b.physical_cash)
        self.assertEqual(updated.physical_reserve_floor, b.physical_reserve_floor)
        self.assertEqual(updated.spendable(Owner.VCAP), D("30"))
        self.assertEqual(updated.spendable(Owner.PRODUCTIVE), b.spendable(Owner.PRODUCTIVE))
        self.assertEqual(journal, (receipt,))

    def test_recycle_release_exact_replay_and_conflicting_identity(self):
        b = budgets(vcap="50", held_by_owner={Owner.VCAP: D("20")})
        receipt = ConfirmedRecycle("receipt", "p", Owner.VCAP, Owner.VCAP, D("10"), True)
        updated, journal = unlock_confirmed_recycle(b, receipt, declared_redeploy=True)
        repeated, repeated_journal = unlock_confirmed_recycle(updated, receipt, declared_redeploy=True,
                                                            previous_releases=journal)
        self.assertIs(repeated, updated)
        self.assertEqual(repeated_journal, journal)
        with self.assertRaises(ValueError):
            unlock_confirmed_recycle(updated, replace(receipt, amount=D("11")), declared_redeploy=True,
                                     previous_releases=journal)

    def test_recycle_unconfirmed_oversized_or_owner_transfer_refused(self):
        b = budgets(vcap="50", held_by_owner={Owner.VCAP: D("20")})
        receipt = ConfirmedRecycle("receipt", "p", Owner.VCAP, Owner.VCAP, D("10"), True)
        for invalid in (replace(receipt, exact_partial_settlement=False), replace(receipt, amount=D("21")),
                        replace(receipt, recorded_owner=Owner.PRODUCTIVE),
                        replace(receipt, original_owner=Owner.PARK, recorded_owner=Owner.PARK)):
            with self.assertRaises(ValueError):
                unlock_confirmed_recycle(b, invalid, declared_redeploy=True)

    def test_unlocked_recovery_proceeds_cannot_fund_new_productive_slot(self):
        b = budgets(productive="0", vcap="10", hedge="0", held_by_owner={Owner.VCAP: D("10")})
        receipt = ConfirmedRecycle("receipt", "p", Owner.VCAP, Owner.VCAP, D("10"), True)
        updated, _ = unlock_confirmed_recycle(b, receipt, declared_redeploy=True)
        batch = decide_batch((entry(), recovery()), state(ParentState("p", "COIN-USD")), updated)
        self.assertEqual([i.action for i in batch.accepted], [Action.RECOVERY])
        self.assertEqual(batch.rejected[0].reason, Reason.OWNER_CASH)
        self.assertEqual(batch.remaining_cash[Owner.PRODUCTIVE], D("0"))


if __name__ == "__main__":
    unittest.main()
