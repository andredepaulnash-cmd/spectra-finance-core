"""Adversarial checks for research boundaries, not manufactured profitability."""
from dataclasses import dataclass
from decimal import Decimal as D, localcontext
import hashlib
import importlib.util
import json
from pathlib import Path
from collections import Counter
from dataclasses import asdict, replace
from types import SimpleNamespace
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "research/danger_room/profitability_campaign.py"
spec = importlib.util.spec_from_file_location("profitability_campaign_under_test", PATH)
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.profile = module.RecoveryLadder("explicit-hypothesis", (
            module.RecoveryRung(D("0.1"), D("0.5")),
            module.RecoveryRung(D("0.3"), D("1"))))
        self.book = module.LadderBook({"A-USD": self.profile})
        self.book.confirmed_entry(1, "A-USD", D(100), D(20))

    def test_original_entry_anchor_survives_patch(self):
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(90))[0], D(10))
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=10)
        self.assertIsNone(self.book.requested_budget(1, "A-USD", D(75))[0])
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(70))[0], D(20))

    def test_rejected_or_unfilled_request_does_not_consume(self):
        for _ in range(3):
            self.assertEqual(self.book.requested_budget(1, "A-USD", D(80))[0], D(10))
        self.assertEqual(self.book.parents[1]["consumed"], 0)

    def test_gap_crosses_only_next_rung(self):
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(50))[0], D(10))
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=10)
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(50))[0], D(20))

    def test_unknown_profile_denies(self):
        self.assertEqual(self.book.requested_budget(1, "B-USD", D(50)), (None, "UNKNOWN_LADDER_PROFILE"))

    def test_parent_cannot_be_reused(self):
        with self.assertRaises(ValueError):
            self.book.confirmed_entry(1, "A-USD", D(100), D(20))

    def test_confirmation_requires_quantity_and_cash(self):
        for quantity, cash in [(D(0), D(10)), (D(1), D(0)), (D("NaN"), D(10))]:
            with self.assertRaises(ValueError):
                self.book.confirmed_recovery(1, "A-USD", quantity, cash, order_id=10)

    def test_exhausted_ladder_does_not_reset(self):
        for order_id in range(2):
            self.book.reserve_recovery(order_id, 1, "A-USD", D(10))
            self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=order_id)
        self.assertEqual(self.book.requested_budget(1, "A-USD", D(50)), (None, "LADDER_EXHAUSTED"))

    def test_invalid_order_and_claimed_original_ladder_rejected(self):
        with self.assertRaises(ValueError):
            module.RecoveryLadder("claimed-original", self.profile.rungs, hypothesis=False)
        with self.assertRaises(ValueError):
            module.RecoveryLadder("unordered", tuple(reversed(self.profile.rungs)))
        with self.assertRaises(ValueError):
            module.RecoveryRung(D(1), D(1))

    def test_confirmation_cannot_invent_or_overspend_reservation(self):
        with self.assertRaises(ValueError):
            self.book.confirmed_recovery(1, "A-USD", D(1), D(10), order_id=10)
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        with self.assertRaises(ValueError):
            self.book.confirmed_recovery(1, "A-USD", D(1), D(11), order_id=10)
        self.assertEqual(self.book.parents[1]["consumed"], 0)
        self.book.rejected_order(10)
        self.assertEqual(self.book.parents[1]["consumed"], 0)

    def test_pending_rung_cannot_be_reserved_twice(self):
        self.book.reserve_recovery(10, 1, "A-USD", D(10))
        with self.assertRaises(ValueError):
            self.book.reserve_recovery(11, 1, "A-USD", D(10))


class SourceBoundaryTests(unittest.TestCase):
    def test_missing_and_duplicate_boundaries_rejected(self):
        for source in ["none", "mark mark"]:
            with self.assertRaises(ValueError):
                module.replace_once(source, "mark", "replacement")

    def test_passive_observer_has_one_declared_change(self):
        source = "        pending = exit_orders + partial_orders + recovery_orders + entry_orders"
        changed, receipt = module.build_campaign_body(source)
        self.assertEqual(len(receipt), 1)
        self.assertIn("dataset.observe_account(locals())", changed)

    def test_declaration_identity_and_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "protocol.json"
            data = {"research_only": True, "production_activation": False,
                    "holdout_access": False, "arms": [{"name": "control"}]}
            p.write_text(json.dumps(data))
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertEqual(module.validate_declaration(data, expected_sha256=digest, protocol_path=p, arm="control"), {"name": "control"})
            with self.assertRaises(ValueError):
                module.validate_declaration(data, expected_sha256="0" * 64, protocol_path=p, arm="control")
            for field, value in [("research_only", False), ("production_activation", True), ("holdout_access", True)]:
                bad = dict(data, **{field: value})
                with self.assertRaises(ValueError):
                    module.validate_declaration(bad, expected_sha256=digest, protocol_path=p, arm="control")

    def test_duplicate_and_missing_arm_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "protocol.json"
            data = {"research_only": True, "production_activation": False,
                    "holdout_access": False, "arms": [{"name": "control"}, {"name": "control"}]}
            p.write_text(json.dumps(data))
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                module.validate_declaration(data, expected_sha256=digest, protocol_path=p, arm="control")


class CausalGateTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.rows = np.ones((300, 1, 6), dtype="float64") * 10
        self.rows[288, 0, 3] = 11

    def test_directional_anchor_and_future_mutation(self):
        self.assertTrue(module.causal_anchor_allowed(self.rows, 0, 288))
        self.rows[289:] = 1e12
        self.assertTrue(module.causal_anchor_allowed(self.rows, 0, 288))
        self.rows[0, 0, 3] = 12
        self.assertFalse(module.causal_anchor_allowed(self.rows, 0, 288))

    def test_missing_anchor_and_insufficient_history_deny(self):
        self.rows[0, 0, 0] = self.np.nan
        self.assertFalse(module.causal_anchor_allowed(self.rows, 0, 288))
        self.assertFalse(module.causal_anchor_allowed(self.rows, 0, 287))

    def test_recovery_requires_all_actual_bars_and_rising_low(self):
        self.rows[3:6, 0, 2] = 11
        self.rows[5, 0, 3] = 12
        self.assertTrue(module.causal_recovery_allowed(self.rows, 0, 5))
        self.rows[2, 0, 0] = self.np.nan
        self.assertFalse(module.causal_recovery_allowed(self.rows, 0, 5))


@dataclass(frozen=True)
class ToyOrder:
    order_id: int
    kind: str = "ENTRY"
    pair: str = "AAA-USD"
    budget: D = D("10.25")
    position_id: int | None = None
    depends_on_order_id: int | None = None


@dataclass(frozen=True)
class ToyFill:
    order_id: int
    kind: str
    pair: str
    position_id: int
    quantity: D
    gross_quote: D
    fee_quote: D
    basis_released: D = D(0)


class OwnerHookTests(unittest.TestCase):
    def setUp(self):
        from research.danger_room import owned_cash
        self.owned = owned_cash
        self.events = []
        self.hooks = module.OwnedCashHooks(owned_cash, emit=self.events.append)
        self.cfg = SimpleNamespace(fee_bps=D(40), pair_cap_fraction=D("0.05"),
            simulated_min_order_quote=D(5), minimum_position_basis=D(10))

    def buy(self, order, equity=D(2000)):
        self.assertTrue(self.hooks.authorize(order, equity=equity, cfg=self.cfg))
        gross = order.budget / D("1.004")
        fee = gross * D("0.004")
        self.assertTrue(self.hooks.preflight(order, gross, fee, equity, self.cfg)[0])
        fill = ToyFill(order.order_id, order.kind, order.pair, order.position_id or 1,
                       gross / D(100), gross, fee)
        self.hooks.buy(fill, equity=equity)
        return fill

    def test_receipt_bound_entry_and_exact_source_fee_sum(self):
        order = ToyOrder(1, budget=D("10.00000000000000000000000001"))
        fill = self.buy(order)
        lot = self.hooks.ledger.lots["lot:1"]
        self.assertEqual(lot.basis, self.owned.all_in_quote(fill.gross_quote, fill.fee_quote))
        self.assertEqual(lot.owner, self.owned.Owner.PRODUCTIVE)
        self.assertEqual(self.hooks.ledger.held(), D(0))

    def test_confirmation_after_new_reserve_breach_rejects_before_native_mutation(self):
        order = ToyOrder(1, budget=D(90))
        self.assertTrue(self.hooks.authorize(order, equity=D(2000), cfg=self.cfg))
        gross = order.budget / D("1.004")
        self.assertEqual(self.hooks.preflight(order, gross, gross * D("0.004"), D(10000), self.cfg),
                         (False, "PHYSICAL_RESERVE"))
        self.assertEqual(len(self.hooks.ledger.lots), 0)
        self.hooks.reject(order, "PHYSICAL_RESERVE")
        self.assertEqual(self.hooks.ledger.held(), D(0))
        self.assertEqual(self.hooks.ledger.cash_total, D(2000))

    def test_entry_pair_cap_checked_at_proposal_and_execution(self):
        self.assertFalse(self.hooks.authorize(ToyOrder(1), equity=D(200), cfg=self.cfg))
        order = ToyOrder(2)
        self.assertTrue(self.hooks.authorize(order, equity=D(2000), cfg=self.cfg))
        gross = order.budget / D("1.004")
        self.assertEqual(self.hooks.preflight(order, gross, gross * D("0.004"), D(200), self.cfg),
                         (False, "ENTRY_PAIR_CAP"))

    def test_no_expected_exit_cash_or_unreleased_slot_admission(self):
        with self.assertRaises(ValueError):
            self.hooks.authorize(ToyOrder(1, depends_on_order_id=10), equity=D(2000), cfg=self.cfg)
        for i in range(50):
            self.assertTrue(self.hooks.authorize(ToyOrder(i + 2, pair=f"C{i:03}-USD"), equity=D(2000), cfg=self.cfg))
        self.assertFalse(self.hooks.authorize(ToyOrder(99, pair="OTHER-USD"), equity=D(2000), cfg=self.cfg))
        self.assertEqual(self.hooks.counts["MAXIMUM_SLOTS"], 1)

    def test_missing_next_bar_proof_releases_only_matching_reservation(self):
        one, two = ToyOrder(1), ToyOrder(2, pair="BBB-USD")
        self.hooks.authorize(one, equity=D(2000), cfg=self.cfg)
        self.hooks.authorize(two, equity=D(2000), cfg=self.cfg)
        held_two = self.hooks.ledger.reservations["buy:2"].remaining
        self.hooks.reject(one, "absent_immediate_next_candle")
        self.assertEqual(self.hooks.ledger.held(), held_two)
        self.assertEqual(len(self.hooks.ledger.lots), 0)

    def test_pending_locks_remain_at_terminal_boundary(self):
        order = ToyOrder(1)
        self.hooks.authorize(order, equity=D(2000), cfg=self.cfg)
        state = {"cash": D(2000), "realized": D(0), "fees": D(0), "committed": D(0), "pending": [order], "positions": {}}
        self.hooks.observe(state)
        self.assertGreater(self.hooks.ledger.held(), D(10))
        with self.assertRaises(ArithmeticError):
            self.hooks.observe(dict(state, pending=[]))

    def test_confirmed_partial_redeploy_keeps_vcap_principal_and_profit_split(self):
        entry = self.buy(ToyOrder(1))
        recovery = self.buy(ToyOrder(2, kind="RECOVERY", position_id=1, budget=D(20)))
        native_lot = SimpleNamespace(lot_id="lot:2", base_remaining=recovery.quantity,
            cost_basis_remaining_quote=recovery.gross_quote, entry_fees_remaining_quote=recovery.fee_quote)
        fill = ToyFill(3, "PARTIAL_RECYCLE", "AAA-USD", 1, recovery.quantity, D(25), D(1),
                       self.owned.all_in_quote(recovery.gross_quote, recovery.fee_quote))
        before = self.hooks.ledger.cash_total
        self.hooks.sale(fill, [native_lot], partial=True)
        with localcontext() as ctx:
            ctx.prec = 60
            self.assertEqual(self.hooks.ledger.cash_total, before + D(24))
        self.assertEqual(self.hooks.ledger.parked_cash_total, D(24))
        parked = {owner: self.hooks.ledger.parked(owner) for owner in self.hooks.ledger.cash_by_owner}
        self.assertGreater(parked[self.owned.Owner.VCAP], D(20))
        self.assertLess(parked[self.owned.Owner.PRODUCTIVE], D(4))
        released = self.hooks.ledger.release_confirmed_partial("explicit-release:3", "sale:3", declared_redeploy=True)
        self.assertEqual(dict(released), parked)
        self.assertEqual(self.hooks.ledger.parked_cash_total, D(0))
        self.assertEqual(self.hooks.ledger.lots["lot:1"].quantity, entry.quantity)

    def test_sale_wrong_quantity_or_basis_cannot_dispose_owned_lot(self):
        recovery = self.buy(ToyOrder(1))
        native_lot = SimpleNamespace(lot_id="lot:1", base_remaining=recovery.quantity,
            cost_basis_remaining_quote=recovery.gross_quote, entry_fees_remaining_quote=recovery.fee_quote)
        basis = self.owned.all_in_quote(recovery.gross_quote, recovery.fee_quote)
        fill = ToyFill(2, "EXIT", "AAA-USD", 1, recovery.quantity, D(12), D("0.05"), basis)
        for changed in (replace(fill, quantity=fill.quantity + D(1)), replace(fill, basis_released=basis + D(1))):
            with self.assertRaises(ArithmeticError):
                self.hooks.sale(changed, [native_lot], partial=False)
            self.assertIn("lot:1", self.hooks.ledger.lots)

    def test_equal_aggregate_cash_cannot_mask_wrong_parent_quantity(self):
        fill = self.buy(ToyOrder(1))
        book = self.hooks.ledger
        basis = self.owned.all_in_quote(fill.gross_quote, fill.fee_quote)
        position = SimpleNamespace(position_id=1, quantity=fill.quantity, basis=basis)
        state = {"cash": book.cash_total, "realized": book.realized_pnl, "fees": book.fees_total,
                 "committed": basis, "pending": [], "positions": {fill.pair: position}}
        self.hooks.observe(state)
        wrong = SimpleNamespace(position_id=1, quantity=fill.quantity + D(1), basis=basis)
        with self.assertRaises(ArithmeticError):
            self.hooks.observe(dict(state, positions={fill.pair: wrong}))

    def test_exact_frozen_supplements_bind_owner_sizing_and_actual_dttp(self):
        root = PATH.parent
        original = root / "profitability_protocol.json"
        protocol = json.loads(original.read_text())
        args = SimpleNamespace(protocol=original, protocol_sha256=module.sha256(original),
            owned_protocol=root / "owned_cash_protocol.json", sizing_protocol=root / "productive_sizing_protocol.json",
            profit_protocol=root / "native_profit_policy_protocol.json")
        for name in ("causal_category_ladders", "confirmed_principal_redeployment", "owned_full_sized",
                     "current_cost_owned_full_sized", "native_dttp_174_park", "owned_full_dttp"):
            args.arm = name
            arm, supplements = module.resolve_arm(protocol, args)
            self.assertEqual(arm["name"], name)
            self.assertEqual(len(supplements), 3)
            if "sized" in name:
                self.assertTrue(arm["productive_sizing"])
            if "dttp" in name:
                self.assertTrue(arm["native_dttp"])


class CompiledNativeIntegrationTests(unittest.TestCase):
    """Actual frozen controller on synthetic OHLC; no historical outcomes."""
    @classmethod
    def setUpClass(cls):
        root = PATH.parents[2]
        vendor = root / "research/holdout_replay/vendor"
        recovered = vendor / "Spectra_2000_Recovery_Recycling_Evidence"
        core = recovered / "recovered/original-plan/Spectra_Exit_Strategy_Evidence/Spectra_Exit_Strategy_Evidence/spectra-finance-core/spectra_core"
        manifest = root / "research/danger_room/native_prerequisites/native_source_manifest.json"
        adapter = recovered / "baseline-recovery-20261001/adapter/sparse_adapter.py"
        bridge = vendor / "Spectra_One_Percent_Comparison_Evidence/one-percent-research-20261006/one_percent_test.py"
        inventory = module.verify_native(core, manifest, adapter, bridge)
        cls.native = module.load_native(core, adapter, bridge, inventory)

    def test_all_predeclared_source_combinations_compile_without_editing_native(self):
        native_source = self.native.GENERATED_SOURCE
        for ladder in (False, True):
            body, changes = module.build_campaign_body(native_source, selection=True, directional=True,
                recovery_quality=False, ladder=ladder, entry_gate=True)
            owned, owner_changes = module.build_owned_body(body, ladder=ladder)
            dttp, profit_changes = module.build_profit_body(owned, native_dttp=True)
            compile(dttp, "synthetic-complete-controller", "exec")
            self.assertNotIn("recovery_quality_allowed", dttp)
            self.assertNotIn("for exit_order in exit_orders", dttp)
            self.assertNotIn("reserve_breached and not recycled_replacement", dttp)
            self.assertIn("owner_preflight", dttp)
            self.assertEqual(len(profit_changes), 1)
            self.assertGreater(len(owner_changes), 5)
        unchanged, changes = module.build_profit_body(native_source, native_dttp=False)
        self.assertEqual(unchanged, native_source)
        self.assertEqual(changes, [])

    def run_toy(self, *, dttp=False, ladder=False, sized=False, redeploy=False,
                conflict=False, protect_dttp=True):
        import numpy as np
        from research.danger_room import owned_cash
        native = self.native
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp)
            pairs = [f"C{i:03}-USD" for i in range(51)]
            prices = [100] * 8 + [105] * 8 + [95] * 8 + [99] * 8 + [106] * 8
            rows = np.zeros((len(prices), len(pairs), 6), dtype="float64")
            for index, price in enumerate(prices):
                rows[index, :, :] = [price, price * 1.005, price * .995, price, 100000, 1000]
            if conflict:
                rows[:, :, :] = [100, 100.5, 99.5, 100, 100000, 1000]
                for index in range(8, len(prices)):
                    price = 30 if index < 11 else 72
                    rows[index, 0, :] = [price, price * 1.005, price * .995, price, 100000, 1000]
            np.save(prepared / "ohlcv.npy", rows)
            (prepared / "metadata.json").write_text(json.dumps({"pairs": pairs,
                "start": 1767225600, "end": 1767225600 + len(prices) * 300, "interval_seconds": 300}))
            original_init = native.a.SparseDataset.__init__
            class ToyDataset(native.Dataset):
                def __init__(self):
                    native.a.SparseDataset.__init__ = lambda instance, ignored, **kwargs: original_init(instance, prepared, **kwargs)
                    try:
                        super().__init__(case="legacy_174_park" if dttp else "hard_174_park", enabled=True)
                    finally:
                        native.a.SparseDataset.__init__ = original_init
                    self.owner = module.OwnedCashHooks(owned_cash, redeploy=redeploy)
                    rungs = ((module.RecoveryRung(D("0.04"), D("0.5")),
                              module.RecoveryRung(D("0.10"), D("0.75")),
                              module.RecoveryRung(D("0.18"), D(1)),
                              module.RecoveryRung(D("0.25"), D("1.25")),
                              module.RecoveryRung(D("0.30"), D("1.5"))) if conflict else (
                              module.RecoveryRung(D("0.03"), D(1)), module.RecoveryRung(D("0.06"), D(1))))
                    self.ladders = module.LadderBook({pair: module.RecoveryLadder("synthetic-only", rungs) for pair in pairs})
                    self.research_counts = Counter()
                    self.observed = []
                    self.eligible_protected = []
                def parked_cash(self):
                    return self.owner.ledger.parked_cash_total if hasattr(self, "owner") else super().parked_cash()
                def owner_authorize(self, order, equity, cfg):
                    return self.owner.authorize(order, equity=equity, cfg=cfg)
                def owner_preflight(self, order, gross, fee, equity, cfg):
                    return self.owner.preflight(order, gross, fee, equity, cfg)
                def owner_recovery_available(self, equity):
                    return self.owner.ledger.available(owned_cash.Owner.VCAP, equity_before=equity)
                def owner_entry_ticket(self, equity, cfg):
                    if sized:
                        from research.danger_room.productive_sizing import productive_entry_budget
                        return productive_entry_budget(equity, self.owner.ledger.available(owned_cash.Owner.PRODUCTIVE),
                            equity * cfg.pair_cap_fraction,
                            self.owner.ledger.available(owned_cash.Owner.PRODUCTIVE, equity_before=equity))[0]
                    return cfg.entry_budget if self.owner.ledger.available(owned_cash.Owner.PRODUCTIVE, equity_before=equity) >= cfg.entry_budget else D(0)
                def note_rejection(self, order, reason):
                    self.ladders.rejected_order(order.order_id)
                    self.owner.reject(order, reason)
                def ladder_budget(self, position, close):
                    return self.ladders.requested_budget(position.position_id, position.pair, close)[0]
                def note_recovery_order(self, order, position):
                    self.ladders.reserve_recovery(order.order_id, position.position_id, position.pair, order.budget)
                def record_buy(self, fill, position, cash_before, *, equity_before):
                    self.owner.buy(fill, equity=equity_before)
                    if ladder and fill.kind == "ENTRY":
                        self.ladders.confirmed_entry(fill.position_id, fill.pair, fill.price,
                            owned_cash.all_in_quote(fill.gross_quote, fill.fee_quote))
                    elif ladder:
                        self.ladders.confirmed_recovery(fill.position_id, fill.pair, fill.quantity,
                            owned_cash.all_in_quote(fill.gross_quote, fill.fee_quote), order_id=fill.order_id)
                    return super().record_buy(fill, position, cash_before)
                def record_full_exit(self, fill):
                    self.owner.sale(fill, self.ledger.list_active_lots(str(fill.position_id)), partial=False)
                    return super().record_full_exit(fill)
                def fill_partial(self, order, position, index, timestamp, cfg, capacity):
                    selected = [self.ledger.lots[key] for key in self.orders[order.order_id].lot_ids]
                    outcome, reason = super().fill_partial(order, position, index, timestamp, cfg, capacity)
                    if outcome is not None:
                        self.owner.sale(outcome[0], selected, partial=True)
                    return outcome, reason
                def observe_account(self, state):
                    self.observed.append(self.owner.observe(state))
                    if conflict:
                        for pair, position in state["positions"].items():
                            profit = state["profit_states"].get(pair)
                            if profit is not None and profit.profit_floor_reached_at is not None:
                                budget, _ = self.ladders.requested_budget(position.position_id, pair, self.marks[pair])
                                if budget is not None:
                                    queued = [order for order in state["pending"]
                                              if order.kind == "RECOVERY" and order.position_id == position.position_id]
                                    self.eligible_protected.append((state["index"], position.position_id, bool(queued)))
            dataset = ToyDataset()
            body, _ = module.build_campaign_body(native.GENERATED_SOURCE, ladder=ladder)
            body, _ = module.build_owned_body(body, ladder=ladder)
            body, _ = module.build_profit_body(body, native_dttp=dttp and protect_dttp)
            namespace = dict(native.a.original.__dict__)
            namespace["FleetOrder"] = native.ResearchOrder
            exec(compile(body, "synthetic-owned-controller", "exec"), namespace)
            class Simulator(native.a.original.RotatingFleetSimulator):
                run = namespace["run"]
            simulator = Simulator(exit_policy=replace(native.a.VCAPProfitExitPolicy(), target_net_profit_bps=D(174)),
                exit_strategy=replace(native.a.original.FleetExitStrategy(),
                    mode=native.a.original.FleetExitMode.DTTP if dttp else native.a.original.FleetExitMode.HARD_TARGET))
            result = simulator.run(dataset)
            self.assertEqual(len(dataset.observed), len(prices))
            self.assertTrue(any(fill.kind == "ENTRY" for fill in result.fills))
            if not conflict:
                self.assertTrue(any(fill.kind == "EXIT" for fill in result.fills))
            self.assertTrue(all(sample.active_positions <= 50 for sample in result.samples))
            self.assertTrue(all(fill.dependency_order_id is None for fill in result.fills))
            self.assertEqual(set(dataset.owner.ledger.lots), {lot.lot_id for position in result.positions
                for lot in dataset.ledger.list_active_lots(str(position.position_id))})
            return dataset, result

    def test_native_owned_control_actual_entry_exit_and_locked_terminal_orders(self):
        dataset, result = self.run_toy()
        self.assertGreater(dataset.research_counts["deferred_until_confirmed_slot_closure"], 0)
        self.assertEqual(dataset.hard_target, True)
        self.assertEqual(dataset.execution_floor_bps, D(174))
        self.assertTrue(any(fill.kind == "RECOVERY" for fill in result.fills))
        self.assertTrue(any(fill.kind == "PARTIAL_RECYCLE" for fill in result.fills))
        self.assertGreater(dataset.owner.ledger.parked_cash_total, 0)

    def test_actual_dttp_mode_armed_floor_prevents_recovery(self):
        dataset, result = self.run_toy(dttp=True)
        self.assertFalse(dataset.hard_target)
        self.assertIsNone(dataset.execution_floor_bps)
        self.assertGreater(dataset.research_counts["armed_profit_floor_blocks_recovery"], 0)

    def test_armed_dttp_floor_blocks_an_independently_eligible_deep_rung(self):
        guarded, result = self.run_toy(dttp=True, ladder=True, redeploy=True, conflict=True)
        self.assertGreater(len(guarded.eligible_protected), 0)
        self.assertFalse(any(queued for _, _, queued in guarded.eligible_protected))
        unguarded, _ = self.run_toy(dttp=True, ladder=True, redeploy=True, conflict=True, protect_dttp=False)
        self.assertTrue(any(queued for _, _, queued in unguarded.eligible_protected))

    def test_compiled_ladder_rung_only_commits_confirmed_owner_fills(self):
        dataset, result = self.run_toy(ladder=True, redeploy=True)
        recovery_fills = [fill for fill in result.fills if fill.kind == "RECOVERY"]
        self.assertGreater(len(recovery_fills), 0)
        self.assertEqual(sum(parent["consumed"] for parent in dataset.ladders.parents.values()), len(recovery_fills))
        self.assertTrue(any(fill.kind == "PARTIAL_RECYCLE" for fill in result.fills))
        self.assertEqual(dataset.owner.ledger.parked_cash_total, D(0))

    def test_compiled_productive_sizing_frozen_at_proposal_and_no_held_resize(self):
        dataset, result = self.run_toy(ladder=True, sized=True, redeploy=True)
        entries = [fill for fill in result.fills if fill.kind == "ENTRY"]
        self.assertEqual(entries[0].gross_quote + entries[0].fee_quote, D(20))
        seeds = {fill.position_id: owned_cash_basis for fill in entries
                 for owned_cash_basis in [dataset.ladders.parents[fill.position_id]["seed"]]}
        self.assertTrue(all(seeds[fill.position_id] == dataset.ladders.parents[fill.position_id]["seed"]
                            for fill in result.fills if fill.kind == "RECOVERY"))


if __name__ == "__main__":
    unittest.main()
