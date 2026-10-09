"""Constructed-money mechanics; these tests make no profitability claim."""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal as D
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location("capital_quality_adapter_tested", HERE.parents[1] / "release_adapter.py")
quality = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = quality
spec.loader.exec_module(quality)
Z = D(0)


@dataclass(frozen=True)
class Lot:
    lot_id: str
    source: str
    base_remaining: D
    cost_basis_remaining_quote: D
    entry_fees_remaining_quote: D = Z


class Dataset:
    def __init__(self, parked=D(100)):
        self.ledger = SimpleNamespace(lots={})
        self.principal_cash = parked
        self.profit_cash = Z
        self.principal_reused = self.profit_reused = Z
        self.ledger.list_active_lots = lambda pid: list(self.ledger.lots.values())

    def parked_cash(self):
        return self.principal_cash + self.profit_cash

    def record_buy(self, fill, position, cash_before):
        total = fill.gross_quote + fill.fee_quote
        if total > cash_before - self.parked_cash() + D("1e-18"):
            raise ArithmeticError("Native source cash failed")
        lot_id = f"lot:{fill.order_id}"
        self.ledger.lots[lot_id] = Lot(lot_id, "BASE_ENTRY" if fill.kind == "ENTRY" else "VCAP_RECOVERY",
                                     fill.quantity, fill.gross_quote, fill.fee_quote)

    def record_full_exit(self, fill):
        self.ledger.lots.clear()

    def fill_partial(self, *args):
        raise NotImplementedError("Native partial owned-lot semantics tested separately")


class Gate:
    def __init__(self, allowed=False):
        self.allowed = allowed
        self.calls = []

    def eligible(self, pair, kind, timestamp):
        self.calls.append((pair, kind, timestamp))
        return self.allowed


class Fixture:
    def __init__(self, arm="SPLIT_RAW", cash="110", parked="100", gate=None):
        self.cash = D(cash)
        self.data = Dataset(D(parked))
        self.controller = quality.attach(self.data, arm, gate)
        self.cfg = SimpleNamespace(reserve_fraction=D(".25"))
        self.timestamp = 1782864300
        self.begin()

    def begin(self):
        self.controller.begin_planning(self.cash, D(20), self.cfg, {}, self.timestamp)

    def order(self, order_id, kind="ENTRY", budget="10", dependency=None):
        return SimpleNamespace(order_id=order_id, submitted_at=self.timestamp,
            pair="ALGO-USD", kind=kind, budget=D(budget), position_id=1 if kind == "RECOVERY" else None,
            depends_on_order_id=dependency)

    def buy(self, order):
        self.controller.claim_order(order)
        assert self.controller.may_fill(order, order.budget, self.cash, {}, 0)
        fill = SimpleNamespace(order_id=order.order_id, pair=order.pair, kind=order.kind,
            quantity=order.budget, gross_quote=order.budget / D("1.004"),
            fee_quote=order.budget - order.budget / D("1.004"))
        self.data.record_buy(fill, None, self.cash)
        self.cash -= order.budget


class Mechanics(unittest.TestCase):
    def test_strict_purpose_caps_cannot_borrow(self):
        f = Fixture(cash="200", parked="200")
        f.controller.claim_order(f.order(1, "RECOVERY", "50"))
        self.assertEqual(f.controller.authority("RECOVERY"), Z)
        self.assertEqual(f.controller.authority("ENTRY"), D(50))
        with self.assertRaises(ArithmeticError):
            f.controller.claim_order(f.order(2, "RECOVERY", ".01"))
        f.controller.claim_order(f.order(3, "ENTRY", "50"))
        self.assertEqual(f.controller.reserved_parked, D(100))
        self.assertEqual(f.controller.reserved_for_kind("ENTRY"), D(50))

    def test_shared_cap_keeps_recovery_first_competition_control(self):
        f = Fixture("SHARED_RAW", cash="200", parked="200")
        f.controller.claim_order(f.order(1, "RECOVERY", "100"))
        self.assertEqual(f.controller.authority("ENTRY"), Z)
        self.assertEqual(f.controller.reserved_for_kind("RECOVERY"), D(100))

    def test_gate_denial_preserves_ordinary_only_authority(self):
        for arm in ("SHARED_QUALITY", "SPLIT_QUALITY"):
            gate = Gate(False)
            f = Fixture(arm, gate=gate)
            self.assertEqual(f.controller.authority("ENTRY", "ALGO-USD", f.timestamp), D(10))
            f.controller.claim_order(f.order(1, budget="10"))
            self.assertEqual(f.controller.claims[1].parked, Z)
            with self.assertRaises(ArithmeticError):
                f.controller.claim_order(f.order(2, budget=".01"))
            self.assertEqual(gate.calls, [("ALGO-USD", "ENTRY", f.timestamp)])

    def test_gate_acceptance_keeps_ordinary_first(self):
        f = Fixture("SPLIT_QUALITY", gate=Gate(True))
        f.controller.claim_order(f.order(1, budget="30"))
        self.assertEqual(f.controller.claims[1].ordinary, D(10))
        self.assertEqual(f.controller.claims[1].parked, D(20))
        self.assertEqual(f.cash, D(110))
        self.assertEqual(f.data.parked_cash(), D(100))

    def test_native_lot_purpose_survives_whole_disposal_and_cap_reopens(self):
        f = Fixture(cash="100", parked="100")
        f.buy(f.order(1, "RECOVERY", "50"))
        f.begin()
        self.assertEqual(f.controller.owned_for_kind("RECOVERY"), D(50))
        self.assertEqual(f.controller.authority("RECOVERY"), Z)
        fill = SimpleNamespace(order_id=2, position_id=1, gross_quote=D("51.2"), fee_quote=D(".2"))
        f.data.record_full_exit(fill)
        f.cash += D(51)
        f.begin()
        self.assertEqual(f.controller.authority("RECOVERY"), D(50))
        self.assertEqual(f.controller.lot_purposes["lot:1"], "RECOVERY")
        self.assertEqual(f.controller.parked_retired_by_kind["RECOVERY"], D(50))
        self.assertEqual(f.data.parked_cash(), D(101))
        self.assertEqual(f.controller.terminal[1], "FILLED")
        f.buy(f.order(3, "RECOVERY", "50"))
        self.assertEqual(f.controller.parked_spent, D(100))
        self.assertEqual(f.controller.owned_parked_basis, D(50))
        self.assertEqual(f.controller.parked_spent_by_kind["RECOVERY"], D(100))
        f.controller.check(cash=f.cash)

    def test_conditional_replacement_cannot_use_future_gate(self):
        gate = Gate(False)
        f = Fixture("SPLIT_QUALITY", cash="100", parked="100", gate=gate)
        order = f.order(1, dependency=99)
        f.controller.claim_order(order)
        gate.allowed = True  # later execution condition is never consulted
        self.assertFalse(f.controller.may_fill(order, D(10), f.cash, {}, 1))
        self.assertEqual(gate.calls, [("ALGO-USD", "ENTRY", f.timestamp)])
        self.assertEqual(f.controller.reserved_parked, Z)
        self.assertEqual(f.cash, D(100))
        f.controller.reject(order.order_id, "source_denial")

    def test_conditional_gate_denial_can_still_use_settled_ordinary_receipt(self):
        f = Fixture("SPLIT_QUALITY", cash="100", parked="100", gate=Gate(False))
        order = f.order(1, dependency=99)
        f.controller.claim_order(order)
        self.assertTrue(f.controller.may_fill(order, D(10), D(110), {}, 1))
        self.assertEqual(f.controller.claims[1].ordinary, D(10))
        self.assertEqual(f.controller.claims[1].parked, Z)

    def test_reject_releases_purpose_claim_without_cash_change(self):
        f = Fixture(cash="100", parked="100")
        f.controller.claim_order(f.order(1, "RECOVERY", "50"))
        before = f.cash, f.data.parked_cash()
        self.assertTrue(f.controller.reject(1, "native_volume_fok"))
        self.assertFalse(f.controller.reject(1, "duplicate"))
        self.assertEqual(f.controller.authority("RECOVERY"), D(50))
        self.assertEqual((f.cash, f.data.parked_cash()), before)

    def test_unknown_native_purpose_or_missing_gate_fail_closed(self):
        data = Dataset()
        data.ledger.lots["bad"] = Lot("bad", "UNATTRIBUTED", D(1), D(1))
        with self.assertRaises(ArithmeticError):
            quality.attach(data, "SHARED_RAW")
        with self.assertRaises(ValueError):
            quality.attach(Dataset(), "SHARED_QUALITY")

    def test_snapshot_purpose_and_terminal_states_conserve(self):
        f = Fixture(cash="100", parked="100")
        f.buy(f.order(1, "ENTRY", "20"))
        f.controller.claim_order(f.order(2, "RECOVERY", "10"))
        snapshot = f.controller.finalize_window([f.order(2, "RECOVERY", "10")])
        self.assertEqual(snapshot["owned_parked_basis_by_kind"], {"ENTRY": D(20), "RECOVERY": Z})
        self.assertEqual(snapshot["terminal_order_states"], {1: "FILLED", 2: "EXPIRED"})
        self.assertEqual(snapshot["reserved_parked"], Z)
        self.assertEqual(snapshot["active_source_lots"][0]["purpose"], "ENTRY")
        self.assertFalse(snapshot["purpose_cross_borrowing"])


_NATIVE_FIXTURE = HERE.parents[4] / "independent_review" / "test_release_adapter.py"


@unittest.skipUnless(_NATIVE_FIXTURE.exists(), "Optional authentic native fixture archive unavailable; no native coverage claim")
class NativeMechanics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_spec = importlib.util.spec_from_file_location("quality_readonly_native_fixture", _NATIVE_FIXTURE)
        module = importlib.util.module_from_spec(fixture_spec)
        sys.modules[fixture_spec.name] = module
        fixture_spec.loader.exec_module(module)
        # Reuse the authentic native ownership fixtures without editing any
        # frozen fixture or strategy file; this module binding is test-local.
        module.release = quality
        cls.fixture_module = module
        cls.native = module.native

    def test_native_owned_lots_keep_exact_purpose_after_whole_sale(self):
        f = self.fixture_module.Fixture("SPLIT_RAW", seed=True)
        f.buy(f.order(2, "RECOVERY"))
        self.assertEqual(f.controller.lot_purposes, {"lot:1": "ENTRY", "lot:2": "RECOVERY"})
        self.assertEqual(f.controller.owned_for_kind("RECOVERY"), D(8))
        f.whole(3, "40")
        self.assertEqual(f.controller.owned_for_kind("RECOVERY"), Z)
        self.assertEqual(f.controller.parked_retired_by_kind["RECOVERY"], D(8))
        f.controller.check(cash=f.cash)

    def test_native_partial_protects_entry_and_reopens_only_recovery_cap(self):
        import numpy as np
        f = self.fixture_module.Fixture("SPLIT_RAW", seed=True)
        _, position = f.buy(f.order(2, "RECOVERY"))
        manifest = f.data.recycle_manifest(position, D("1.02"), f.t + 600, f.cfg,
            self.native.a.original.VCAPProfitExitAction.WAIT)
        self.assertIsNotNone(manifest)
        order = self.native.ResearchOrder(3, f.t + 600, "ALGO-USD", "PARTIAL_RECYCLE", Z, 1, None)
        f.data.note_partial(order, manifest, D("1.02"), self.native.a.original.VCAPProfitExitAction.WAIT)
        f.data.current_bars = {"ALGO-USD": self.native.a.RecordedBar(f.t + 600,
            np.asarray([1.02, 1.03, 1.01, 1.02, 1000, 10]), True)}
        result, reason = f.data.fill_partial(order, position, 0, f.t + 900, f.cfg, D(1000))
        self.assertIsNone(reason)
        fill, updated = result
        f.cash += fill.gross_quote - fill.fee_quote
        self.assertEqual(set(f.controller.lots), {"lot:1"})
        self.assertEqual(f.controller.lot_purposes["lot:2"], "RECOVERY")
        self.assertEqual(f.controller.parked_retired_by_kind["RECOVERY"], D(8))
        self.assertEqual(updated.basis, D(20))
        self.assertEqual(f.controller.owned_for_kind("ENTRY"), Z)
        f.controller.check(cash=f.cash)

    def test_source_anchors_compile_and_reverse_to_exact_native_source(self):
        source = self.native.GENERATED_SOURCE
        changed, patches = quality.build_source(source)
        self.assertEqual(len(patches), 9)
        compile(changed, "constructed_native_quality_overlay", "exec")
        restored = changed
        for patch in reversed(patches):
            self.assertEqual(restored.count(patch["after"]), 1)
            restored = restored.replace(patch["after"], patch["before"])
        self.assertEqual(restored, source)
        anchor = '            pair = candidates.pop(0)\n            entry_orders.append(\n                next_order(timestamp, pair, "ENTRY", budget=cfg.entry_budget)'
        with self.assertRaises(ArithmeticError):
            quality.build_source(source.replace(anchor, anchor.replace("pop(0)", "pop()")))

    def test_off_native_compilation_path_is_unchanged(self):
        f = self.fixture_module.Fixture("OFF", cash="120")
        f.data.hydration_generated_source = self.native.GENERATED_SOURCE
        simulator = self.native.Simulator(f.cfg)
        before_class, before_run = type(simulator), simulator.run.__func__
        returned = quality.compile_hydrated(self.native, f.data, simulator)
        self.assertIs(returned, simulator)
        self.assertIs(type(simulator), before_class)
        self.assertIs(simulator.run.__func__, before_run)
        self.assertEqual(f.data.release_source_changes, [])


if __name__ == "__main__":
    unittest.main()
