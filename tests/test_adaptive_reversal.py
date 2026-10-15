"""Synthetic mechanics only. No historical data, profitability or live execution."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D, localcontext
import json
import random
import unittest
from spectra_core.adaptive_reversal.policy import *
from spectra_core.adaptive_reversal.atr import WilderATR, update_atr, snapshot_atr, restore_atr
from spectra_core.adaptive_reversal.research_adapter import CausalFlag, count_evidence

C = PROVISIONAL_RESEARCH_CONFIG


def opened(config=C, quantity="1", at=0):
    state = start(config, "cycle-1", "SYNTH/USD", at)
    state = reduce(config, state, EntryFill("cycle-1", "entry-1", at, at, D(quantity), D("100"))).state
    return reduce(config, state, SealEntry(at)).state


def evidence(n, *, low="90", high="91", close="90.5", atr="2", score="0", count=0,
             reversal=False, delay=0, arm=None, evidence_id=None):
    return Evidence(evidence_id or f"e-{n}", n*3600+delay,
                    CompletedBar((n-1)*3600, n*3600, D(high), D(low), D(close)),
                    n*3600, True, D(atr), D(score), count, reversal,
                    None if arm is None else D(arm), None if arm is None else n*3600)


def covering(config=C):
    state = opened(config)
    for n in (1, 2):
        state = reduce(config, state, evidence(n, score="2", count=2, reversal=True)).state
    assert state.phase == Phase.COVER
    return state


def flat(config=C):
    state = covering(config)
    at = state.last_event_at+1
    state = reduce(config, state, Dispatch(state.intent.intent_id, 1, at)).state
    return reduce(config, state, CoverFill(state.cycle_id, state.intent.intent_id, 1,
                                          "cover-1", at, at, D("1"), D("91"))).state


class PolicyTests(unittest.TestCase):
    def test_actual_weighted_entry_and_quantity(self):
        state = start(C, "cycle-1", "SYNTH/USD", 0)
        state = reduce(C, state, EntryFill("cycle-1", "a", 0, 0, D("2"), D("100"))).state
        state = reduce(C, state, EntryFill("cycle-1", "b", 1, 1, D("1"), D("103"))).state
        self.assertEqual(state.actual_entry_price, D("101"))
        self.assertEqual(state.open_quantity, D("3"))
        self.assertIsNone(state.lowest_completed_low)

    def test_no_unconfirmed_entry_or_pre_entry_bar(self):
        state = start(C, "cycle-1", "SYNTH/USD", 0)
        self.assertIsNotNone(reduce(C, state, evidence(1)).state.halted_reason)
        state = opened(at=600)
        self.assertIn("predates", reduce(C, state, evidence(1)).state.halted_reason)
        self.assertIsNone(reduce(C, state, evidence(2)).state.halted_reason)

    def test_ride_follows_new_lows_without_fixed_profit_exit(self):
        state = opened()
        for n in range(1, 20):
            low = D("100")-n*3
            state = reduce(C, state, evidence(n, low=str(low), high=str(low+1), close=str(low+D(".5")))).state
            self.assertEqual(state.phase, Phase.SHORT_RIDE)
            self.assertEqual(state.lowest_completed_low, low)
            self.assertEqual(state.active_stop, low+6)
            self.assertIsNone(state.intent)

    def test_progressive_tightening_is_monotonic(self):
        state = opened()
        expected = [("0", 0, Phase.SHORT_RIDE, D("96")),
                    ("1", 1, Phase.REVERSAL_WATCH, D("94")),
                    ("2", 2, Phase.REVERSAL_LIKELY, D("92"))]
        for n, (score, count, phase, stop) in enumerate(expected, 1):
            state = reduce(C, state, evidence(n, score=score, count=count)).state
            self.assertEqual((state.phase, state.active_stop), (phase, stop))
        state = reduce(C, state, evidence(4, atr="20")).state
        self.assertEqual((state.phase, state.active_stop), (Phase.REVERSAL_LIKELY, D("92")))

    def test_tightened_stop_not_applied_to_same_bar_high(self):
        state = reduce(C, opened(), evidence(1)).state
        result = reduce(C, state, evidence(2, high="95", close="91", score="2", count=2))
        self.assertEqual(result.state.active_stop, D("92"))
        self.assertIsNone(result.state.intent)
        result = reduce(C, result.state, evidence(3, high="92", close="91"))
        self.assertEqual(result.state.intent.reason, "prior_active_stop_crossed")
        self.assertEqual(len(result.new_intents), 1)

    def test_delayed_stop_does_not_retroactively_use_high(self):
        state = reduce(C, opened(), evidence(1, delay=300, score="2", count=2)).state
        self.assertEqual(state.stop_effective_after, 3900)
        result = reduce(C, state, evidence(2, high="95", close="91"))
        self.assertIsNone(result.state.intent)  # High could have been before 3900.
        result = reduce(C, result.state, evidence(3, high="95", close="91"))
        self.assertIsNotNone(result.state.intent)

    def test_delayed_stop_close_is_causal_and_old_boundary_remains_usable(self):
        state = reduce(C, opened(), evidence(1)).state  # 96 at 3600
        state = reduce(C, state, evidence(2, score="2", count=2, delay=300)).state  # 92 at 7500
        self.assertIsNone(reduce(C, state, evidence(3, high="95", close="91")).state.intent)
        self.assertIsNotNone(reduce(C, state, evidence(3, high="97", close="91")).state.intent)
        self.assertIsNotNone(reduce(C, state, evidence(3, high="95", close="93")).state.intent)

    def test_confirmation_requires_two_consecutive_completed_bars(self):
        state = opened()
        for n, flag in enumerate((True, False, True, True), 1):
            result = reduce(C, state, evidence(n, score="2", count=2, reversal=flag))
            state = result.state
            self.assertEqual(state.phase == Phase.COVER, n == 4)
        self.assertEqual(state.intent.reason, "confirmed_reversal")

    def test_confirmation_requires_feature_family_count_and_score(self):
        for score, count in (("9", 1), ("1", 9), ("0", 0)):
            state = opened()
            for n in range(1, 5):
                state = reduce(C, state, evidence(n, score=score, count=count, reversal=True)).state
            self.assertIsNone(state.intent)

    def test_optional_arm_accounting_is_not_fixed_take_profit(self):
        config = replace(C, arm_threshold=D(".0444"))
        state = opened(config)
        state = reduce(config, state, evidence(1, arm=".0443", score="2", count=2, reversal=True)).state
        self.assertFalse(state.armed)
        self.assertEqual(state.phase, Phase.SHORT_RIDE)
        state = reduce(config, state, evidence(2, arm=".0444")).state
        self.assertTrue(state.armed)
        self.assertIsNone(state.intent)
        state = reduce(config, state, evidence(3, arm="-.1")).state
        self.assertTrue(state.armed)

    def test_unarmed_emergency_stop_still_works(self):
        config = replace(C, arm_threshold=D(".9"))
        state = reduce(config, opened(config), evidence(1)).state
        state = reduce(config, state, evidence(2, high="100", close="99")).state
        self.assertEqual(state.phase, Phase.COVER)
        self.assertFalse(state.armed)

    def test_stale_gap_future_and_uncertified_fail_closed(self):
        for ev in (evidence(1, delay=301), replace(evidence(1), observed_at=3599),
                   replace(evidence(1), feature_asof=3601), replace(evidence(1), causal_confirmed=False),
                   evidence(2), replace(evidence(1, arm=".1"), accounting_asof=3599), evidence(1, atr="0")):
            result = reduce(C, opened(), ev)
            self.assertIsNotNone(result.state.halted_reason)
            self.assertIsNone(result.state.intent)
            self.assertEqual(result.new_intents, ())

    def test_gap_preserves_stop_and_requires_reconcile(self):
        state = reduce(C, opened(), evidence(1)).state
        state = reduce(C, state, evidence(3)).state
        self.assertEqual(state.active_stop, D("96"))
        state = reduce(C, state, evidence(4)).state
        self.assertEqual(state.last_bar_close_at, 3600)
        state = reduce(C, state, Reconcile(14400, 14400, "Completed causal source repaired and position checked")).state
        self.assertIsNone(state.halted_reason)
        self.assertEqual(reduce(C, state, evidence(5)).state.last_bar_close_at, 18000)

    def test_duplicate_evidence_idempotent_conflict_and_same_bar_rejected(self):
        ev = evidence(1, score="2", count=2, reversal=True)
        state = reduce(C, opened(), ev).state
        self.assertEqual(reduce(C, state, replace(ev, observed_at=3700)).state, state)
        self.assertIsNotNone(reduce(C, state, replace(ev, evidence_score=D("3"))).state.halted_reason)
        self.assertIsNotNone(reduce(C, state, replace(ev, evidence_id="other")).state.halted_reason)
        self.assertEqual(state.confirmations, 1)

    def test_clock_waits_for_first_wholly_post_entry_completed_bar(self):
        state = opened(at=600)
        self.assertIsNone(reduce(C, state, Clock(7500)).state.halted_reason)
        self.assertIsNotNone(reduce(C, state, Clock(7501)).state.halted_reason)

    def test_clock_staleness_blocks_dispatch(self):
        state = covering()
        self.assertFalse(execution_eligible(C, state, state.last_event_at))
        self.assertTrue(execution_eligible(C, state, state.last_event_at+1))
        state = reduce(C, state, Clock(11101)).state
        self.assertIsNotNone(state.halted_reason)
        self.assertFalse(execution_eligible(C, state, 11102))

    def test_only_intents_never_same_bar_fill(self):
        state = covering()
        result = reduce(C, state, Dispatch(state.intent.intent_id, 1, 7200))
        self.assertIsNone(result.state.intent.attempts[0].dispatched_at)
        bad = CoverFill(state.cycle_id, state.intent.intent_id, 1, "bad", 7200, 7200, D("1"), D("90"))
        self.assertIsNotNone(reduce(C, state, bad).state.halted_reason)
        self.assertEqual(state.open_quantity, D("1"))

    def test_pending_does_not_duplicate_intents_and_tracks_lows(self):
        state = covering()
        for n in (3, 4, 5):
            result = reduce(C, state, evidence(n, low="80", high="81", close="80.5", score="2", count=2, reversal=True))
            self.assertEqual(result.new_intents, ())
            state = result.state
        self.assertEqual(state.lowest_completed_low, D("80"))
        self.assertEqual(len(state.intent.attempts), 1)

    def test_fill_duplicates_conflicts_overfill_and_partial(self):
        state = covering()
        state = reduce(C, state, Dispatch(state.intent.intent_id, 1, 7201)).state
        fill = CoverFill(state.cycle_id, state.intent.intent_id, 1, "part", 7201, 7201, D(".4"), D("90"))
        state = reduce(C, state, fill).state
        self.assertEqual(state.open_quantity, D(".6"))
        self.assertEqual(reduce(C, state, replace(fill, received_at=7210)).state, state)
        self.assertIsNotNone(reduce(C, state, replace(fill, quantity=D(".5"))).state.halted_reason)
        self.assertIsNotNone(reduce(C, state, replace(fill, fill_id="over", quantity=D(".7"))).state.halted_reason)
        state = reduce(C, state, replace(fill, fill_id="rest", quantity=D(".6"))).state
        self.assertEqual(state.phase, Phase.COOLDOWN)
        self.assertEqual(state.open_quantity, 0)

    def test_entry_fill_duplicates_after_seal_and_reset_are_idempotent(self):
        state = opened()
        original = state.fills[0]
        self.assertEqual(reduce(C, state, original).state, state)
        self.assertIsNotNone(reduce(C, state, replace(original, fill_id="late")).state.halted_reason)

    def test_retry_waits_for_cancel_and_fill_acknowledgement(self):
        state = covering()
        intent_id = state.intent.intent_id
        state = reduce(C, state, Dispatch(intent_id, 1, 7201)).state
        state = reduce(C, state, Acknowledge(intent_id, 1, 7202)).state
        self.assertEqual(len(reduce(C, state, Retry(intent_id, 7203)).state.intent.attempts), 1)
        state = reduce(C, state, RequestCancel(intent_id, 1, 7203)).state
        self.assertEqual(len(reduce(C, state, Retry(intent_id, 7204)).state.intent.attempts), 1)
        state = reduce(C, state, CancelAcknowledged(intent_id, 1, 7204)).state
        self.assertEqual(len(reduce(C, state, Retry(intent_id, 7205)).state.intent.attempts), 1)
        state = reduce(C, state, CancelAcknowledged(intent_id, 1, 7205, True, D("0"))).state
        result = reduce(C, state, Retry(intent_id, 7206))
        self.assertEqual(result.new_intents, ())
        self.assertEqual(result.state.intent.intent_id, intent_id)
        self.assertEqual(len(result.state.intent.attempts), 2)
        self.assertFalse(execution_eligible(C, result.state, 7206))
        self.assertTrue(execution_eligible(C, result.state, 7207))

    def test_retry_uses_remaining_quantity(self):
        state = covering()
        iid = state.intent.intent_id
        for ev in (Dispatch(iid, 1, 7201), CoverFill(state.cycle_id, iid, 1, "part", 7202, 7202, D(".4"), D("90")),
                   RequestCancel(iid, 1, 7203), CancelAcknowledged(iid, 1, 7204, True, D(".4")), Retry(iid, 7205)):
            state = reduce(C, state, ev).state
        self.assertEqual(state.intent.attempts[-1].quantity, D(".6"))
        self.assertTrue(execution_eligible(C, state, 7206))
        self.assertEqual(restore(snapshot(C, state)), (C, state))

    def test_late_fill_before_cancel_records_after_cancel(self):
        state = covering()
        iid = state.intent.intent_id
        for ev in (Dispatch(iid, 1, 7201), RequestCancel(iid, 1, 7203), CancelAcknowledged(iid, 1, 7204)):
            state = reduce(C, state, ev).state
        state = reduce(C, state, CoverFill(state.cycle_id, iid, 1, "late", 7202, 7205, D(".4"), D("90"))).state
        self.assertEqual(state.open_quantity, D(".6"))
        self.assertIsNone(state.halted_reason)

    def test_late_fill_after_reported_reconciliation_halts_and_blocks_retry(self):
        state = covering()
        iid = state.intent.intent_id
        for ev in (Dispatch(iid, 1, 7201), RequestCancel(iid, 1, 7203),
                   CancelAcknowledged(iid, 1, 7204, True, D("0")), Retry(iid, 7205)):
            state = reduce(C, state, ev).state
        state = reduce(C, state, CoverFill(state.cycle_id, iid, 1, "late", 7202, 7206, D(".4"), D("90"))).state
        self.assertEqual(state.open_quantity, D(".6"))
        self.assertIsNotNone(state.halted_reason)
        self.assertFalse(execution_eligible(C, state, 7207))

    def test_aggregate_flat_never_hides_working_retry_residual(self):
        config = replace(C, cooldown_bars=0)
        state = covering(config)
        iid = state.intent.intent_id
        for ev in (Dispatch(iid, 1, 7201), RequestCancel(iid, 1, 7203),
                   CancelAcknowledged(iid, 1, 7204, True, D("0")), Retry(iid, 7205),
                   Dispatch(iid, 2, 7206),
                   CoverFill(state.cycle_id, iid, 1, "late", 7202, 7207, D(".4"), D("90")),
                   CoverFill(state.cycle_id, iid, 2, "retry-part", 7208, 7208, D(".6"), D("90"))):
            state = reduce(config, state, ev).state
            self.assertEqual(restore(snapshot(config, state)), (config, state))
        self.assertEqual(state.open_quantity, 0)
        self.assertEqual(state.phase, Phase.COOLDOWN)
        self.assertEqual(state.intent.attempts[1].status, AttemptStatus.DISPATCHED)
        self.assertTrue(state.execution_reconciliation_required)
        # Data reconciliation cannot clear an execution discrepancy.
        state = reduce(config, state, Reconcile(7209, 7200, "market data repaired")).state
        self.assertIsNotNone(state.halted_reason)
        state = reduce(config, state, Reconcile(7210, 7200, "position checked", True, D("0"))).state
        self.assertIsNotNone(state.halted_reason)  # Still has a working residual.
        self.assertEqual(reduce(config, state, Reset("cycle-2", 7211, True)).state.cycle_id, "cycle-1")
        for ev in (CancelAcknowledged(iid, 1, 7211, True, D(".4")), RequestCancel(iid, 2, 7212),
                   CancelAcknowledged(iid, 2, 7213, True, D(".6")),
                   Reconcile(7214, 7200, "both attempts canceled, fills and flat position reconciled", True, D("0"))):
            state = reduce(config, state, ev).state
        self.assertIsNone(state.halted_reason)
        self.assertEqual(restore(snapshot(config, state)), (config, state))
        state = reduce(config, state, Reset("cycle-2", 7215, True)).state
        self.assertEqual(state.cycle_id, "cycle-2")

    def test_reconcile_cannot_unblock_oversized_working_retry(self):
        state = covering()
        iid = state.intent.intent_id
        for ev in (Dispatch(iid, 1, 7201), RequestCancel(iid, 1, 7203),
                   CancelAcknowledged(iid, 1, 7204, True, D("0")), Retry(iid, 7205), Dispatch(iid, 2, 7206),
                   CoverFill(state.cycle_id, iid, 1, "late", 7202, 7207, D(".4"), D("90")),
                   CancelAcknowledged(iid, 1, 7208, True, D(".4")),
                   Reconcile(7209, 7200, "position checked", True, D(".6"))):
            state = reduce(C, state, ev).state
            self.assertEqual(restore(snapshot(C, state)), (C, state))
        self.assertIsNotNone(state.halted_reason)
        self.assertTrue(state.execution_reconciliation_required)
        for ev in (RequestCancel(iid, 2, 7210), CancelAcknowledged(iid, 2, 7211, True, D("0")),
                   Reconcile(7212, 7200, "oversized retry canceled; position checked", True, D(".6"))):
            state = reduce(C, state, ev).state
        self.assertIsNone(state.halted_reason)
        state = reduce(C, state, Retry(iid, 7213)).state
        self.assertEqual(state.intent.attempts[-1].quantity, D(".6"))
        self.assertTrue(execution_eligible(C, state, 7214))

    def test_cancel_reconciliation_mismatch_and_post_cancel_fill(self):
        state = covering()
        iid = state.intent.intent_id
        for ev in (Dispatch(iid, 1, 7201), RequestCancel(iid, 1, 7203)):
            state = reduce(C, state, ev).state
        self.assertIsNotNone(reduce(C, state, CancelAcknowledged(iid, 1, 7204, True, D(".4"))).state.halted_reason)
        state = reduce(C, state, CancelAcknowledged(iid, 1, 7204)).state
        fill = CoverFill(state.cycle_id, iid, 1, "bad", 7205, 7205, D("1"), D("90"))
        self.assertIsNotNone(reduce(C, state, fill).state.halted_reason)

    def test_exact_ack_redelivery_after_later_clock_is_idempotent(self):
        state = covering()
        iid = state.intent.intent_id
        state = reduce(C, state, Dispatch(iid, 1, 7201)).state
        ack = Acknowledge(iid, 1, 7202)
        state = reduce(C, state, ack).state
        state = reduce(C, state, Clock(7203)).state
        self.assertEqual(reduce(C, state, ack).state, state)

    def test_halted_accepts_real_fill_receipts_and_cancellation(self):
        state = covering()
        iid = state.intent.intent_id
        state = reduce(C, state, Dispatch(iid, 1, 7201)).state
        state = reduce(C, state, Clock(11101)).state
        state = reduce(C, state, RequestCancel(iid, 1, 11102)).state
        self.assertEqual(state.intent.attempts[-1].status, AttemptStatus.CANCEL_PENDING)
        state = reduce(C, state, CoverFill(state.cycle_id, iid, 1, "delayed", 7202, 11103, D("1"), D("90"))).state
        self.assertEqual(state.open_quantity, 0)
        self.assertIsNotNone(state.halted_reason)

    def test_cooldown_reset_requires_flat_bars_and_explicit_reset(self):
        state = flat()
        self.assertEqual(state.cooldown_remaining, 2)
        self.assertEqual(reduce(C, state, Reset("cycle-2", 7202, True)).state.cycle_id, "cycle-1")
        state = reduce(C, state, evidence(3)).state
        state = reduce(C, state, evidence(4)).state
        self.assertEqual(state.cooldown_remaining, 0)
        self.assertEqual(reduce(C, state, Reset("cycle-2", 14400, False)).state.cycle_id, "cycle-1")
        self.assertEqual(reduce(C, state, Reset("cycle-1", 14400, True)).state.cycle_id, "cycle-1")
        state = reduce(C, state, Reset("cycle-2", 14400, True)).state
        self.assertEqual(state.phase, Phase.SHORT_RIDE)
        self.assertEqual(state.entry_quantity, 0)
        self.assertIsNone(state.active_stop)
        self.assertIsNone(state.intent)
        self.assertEqual(restore(snapshot(C, state)), (C, state))

    def test_owned_spot_reconstruction_is_separate_and_no_one_percent_gate(self):
        state = covering()
        ledger = ReconstructionAccounting(state.cycle_id, state.instrument, D("1"), D("1.000001"),
                                          7201, 7300, True, "separate owned-spot ledger")
        self.assertFalse(reconstruction_eligible(state, ledger, 7201))
        state = flat()
        self.assertTrue(reconstruction_eligible(state, ledger, 7201))
        for bad in (replace(ledger, affordable_units_after_all_costs=D("1")),
                    replace(ledger, accounting_confirmed=False), replace(ledger, instrument="OTHER"),
                    replace(ledger, cycle_id="other"), replace(ledger, asof=7200)):
            self.assertFalse(reconstruction_eligible(state, bad, 7201))
        self.assertFalse(reconstruction_eligible(state, ledger, 7301))
        self.assertFalse(hasattr(state, "net_profit"))

    def test_immutable_config_evidence_state(self):
        for obj, name, value in ((C, "paper_only", False), (opened(), "armed", True),
                                 (evidence(1), "confirmed_reversal", True)):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, name, value)
        with self.assertRaises(ValueError):
            replace(C, paper_only=False)

    def test_snapshot_every_prefix_restart_deterministic(self):
        events = [evidence(1), evidence(2, score="1", count=1),
                  evidence(3, score="2", count=2, reversal=True),
                  evidence(4, score="2", count=2, reversal=True),
                  Dispatch("cycle-1:cover", 1, 14401), Acknowledge("cycle-1:cover", 1, 14402),
                  CoverFill("cycle-1", "cycle-1:cover", 1, "c", 14403, 14403, D("1"), D("91")),
                  evidence(5), evidence(6), Reset("cycle-2", 21600, True)]
        direct, restarted = opened(), opened()
        for ev in events:
            direct = reduce(C, direct, ev).state
            restored_config, restarted = restore(snapshot(C, restarted))
            restarted = reduce(restored_config, restarted, ev).state
            self.assertEqual(snapshot(C, direct), snapshot(C, restarted))
            validate_state(C, direct)

    def test_config_drift_schema_and_corrupted_attempt_rejected(self):
        state = covering()
        with self.assertRaises(ValueError):
            reduce(replace(C, confirmations_required=3), state, Clock(7201))
        payload = json.loads(snapshot(C, state))
        payload["schema_version"] = 2
        with self.assertRaises(ValueError):
            restore(json.dumps(payload))
        for mutate in (lambda p: p["state"]["intent"].update(reduce_only=False),
                       lambda p: p["state"]["intent"]["attempts"]["$tuple"][0].update(quantity={"$decimal":"-123"}),
                       lambda p: p["state"].update(entry_quantity={"$decimal":"2"}),
                       lambda p: p["state"].update(phase="COVER")):
            payload = json.loads(snapshot(C, state)); mutate(payload)
            with self.assertRaises(ValueError):
                restore(json.dumps(payload))

    def test_ambient_decimal_context_does_not_change_results(self):
        with localcontext() as ctx:
            ctx.prec = 4
            low_precision = snapshot(C, covering())
        with localcontext() as ctx:
            ctx.prec = 60
            high_precision = snapshot(C, covering())
        self.assertEqual(low_precision, high_precision)

    def test_randomized_declines_keep_stop_monotone_and_never_fill(self):
        rng = random.Random(20261015)
        for trial in range(20):
            state = opened()
            low = D("100")
            previous = None
            for n in range(1, 30):
                low -= D(str(rng.randint(1, 3)))
                result = reduce(C, state, evidence(n, low=str(low), high=str(low+D(".1")),
                                close=str(low+D(".05")), atr=str(rng.randint(1, 8)),
                                score=str(rng.randint(0, 2)), count=rng.randint(0, 3)))
                state = result.state
                if previous is not None:
                    self.assertLessEqual(state.active_stop, previous)
                previous = state.active_stop
                self.assertEqual(state.covered_quantity, 0)
                self.assertEqual(restore(snapshot(C, state)), (C, state))


class AdapterTests(unittest.TestCase):
    def test_counts_feature_families_without_inventing_confirmation(self):
        flags = (CausalFlag("a", "group1", True, 3500, 3600),
                 CausalFlag("b", "group1", True, 3500, 3600),
                 CausalFlag("c", "group2", True, 3600, 3900))
        ev = count_evidence(evidence_id="x", observed_at=3600, bar=evidence(1).bar, atr=D("2"),
                            flags=flags, confirmed_reversal=False)
        self.assertEqual((ev.evidence_score, ev.evidence_count, ev.confirmed_reversal), (D("2"), 2, False))

    def test_duplicate_future_and_expired_flags_rejected(self):
        good = CausalFlag("a", "group", True, 3500, 3600)
        for flags in ((good, good), (replace(good, asof=3601, valid_until=3900),),
                      (replace(good, valid_until=3599),)):
            with self.assertRaises(ValueError):
                count_evidence(evidence_id="x", observed_at=3600, bar=evidence(1).bar, atr=D("2"),
                               flags=flags, confirmed_reversal=True)


class ATRTests(unittest.TestCase):
    def bar(self, n, high="101", low="99", close="100"):
        return CompletedBar((n-1)*3600, n*3600, D(high), D(low), D(close))

    def test_exact_wilder_seed_and_recurrence(self):
        state = WilderATR()
        for n in range(1, 15):
            state = update_atr(state, self.bar(n), n*3600)
            self.assertEqual(state.value, None if n < 14 else D("2"))
        state = update_atr(state, self.bar(15, high="104", low="100", close="102"), 54000)
        with localcontext(MATH_CONTEXT):
            self.assertEqual(state.value, D("30")/14)

    def test_gap_true_range_uses_previous_close(self):
        state = update_atr(WilderATR(period=2), self.bar(1), 3600)
        state = update_atr(state, self.bar(2, high="110", low="108", close="109"), 7200)
        self.assertEqual(state.value, D("6"))

    def test_duplicate_gap_future_invalid_and_conflicting_bar(self):
        state = update_atr(WilderATR(), self.bar(1), 3600)
        self.assertEqual(update_atr(state, self.bar(1), 3700), state)
        for bar, now in ((self.bar(3),10800), (self.bar(2),7199),
                         (self.bar(1, high="102"),3600)):
            with self.assertRaises(ValueError):
                update_atr(state, bar, now)

    def test_flat_market_zero_atr_is_explicitly_blocked_by_policy(self):
        state = WilderATR()
        for n in range(1, 15):
            state = update_atr(state, self.bar(n, high="100", low="100", close="100"), n*3600)
        self.assertEqual(state.value, 0)
        self.assertEqual(reduce(C, opened(), evidence(1, atr="0")).state.halted_reason, "zero_atr_unusable")

    def test_atr_every_prefix_restart_and_corruption(self):
        state = WilderATR()
        replayed = WilderATR()
        for n in range(1, 30):
            state = update_atr(state, self.bar(n, high=str(100+n), low=str(98+n), close=str(99+n)), n*3600)
            replayed = restore_atr(snapshot_atr(replayed))
            replayed = update_atr(replayed, self.bar(n, high=str(100+n), low=str(98+n), close=str(99+n)), n*3600)
            self.assertEqual(state, replayed)
        payload = json.loads(snapshot_atr(state)); payload["value"] = None
        with self.assertRaises(ValueError):
            restore_atr(json.dumps(payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
