"""Independent mechanics tests on genuine immutable native lots and methods.

Constructed monetary cases verify authority/accounting, not historical alpha.
No tape loading, hydration parsing, exchange access, or production mutation.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import os

HERE = Path(__file__).resolve().parent
WORK = HERE.parent.parent

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

native_wrapper_path = os.environ.get('SPECTRA_NATIVE_WRAPPER')
if not native_wrapper_path:
    raise unittest.SkipTest(
        'Native archive required: set SPECTRA_NATIVE_WRAPPER to the frozen continuation_wrapper.py'
    )
import numpy as np

wrapper = load(Path(native_wrapper_path).expanduser().resolve(), 'independent_native_wrapper')
native = wrapper.load_bridge()
release = load(HERE.parent/'release_adapter.py', 'independent_release_adapter')
Z = D(0)

class Fixture:
    """Real native ownership hooks on a deliberately tiny constructed wallet."""
    def __init__(self, arm='BOTH', cash='102', principal='90', profit='10', seed=False):
        self.cash = D(cash)
        self.data = native.Dataset.__new__(native.Dataset)
        data = self.data
        data.ledger = native.MemoryLedger()
        data.governor = native.MemoryGovernor()
        data.store = native.MemoryStore(data)
        data.events = []
        data.orders = {}
        data.counts = Counter()
        data.plan_context = {}
        data.principal_cash = D(principal)
        data.profit_cash = D(profit)
        data.principal_reused = data.profit_reused = Z
        data.principal_released = data.gross_basis_released = Z
        data.partial_profit = data.principal_returned = Z
        data.park_proceeds = True
        data.enabled = True
        data.hard_target = True
        data.whole_target_bps = data.partial_target_bps = data.execution_floor_bps = D(100)
        data.trace_writer = None
        self.positions = {}
        self.cfg = native.a.original.FleetConfig()
        self.t = 1782864000
        if seed:
            self.seed_entry()
        self.controller = release.attach(data, arm)
        self.begin()

    def seed_entry(self):
        position = native.a.original.Position(1, 'ALGO-USD', D(10), D(20), self.t)
        fill = native.a.original.FleetFill(self.t, 1, 'ALGO-USD', 'ENTRY', 1, D(10), D('1.992'), D('19.92'), D('.08'), Z, Z, None)
        self.data.record_buy(fill, position, D(1000))
        self.positions['ALGO-USD'] = position

    def begin(self):
        self.controller.begin_planning(self.cash, D(20), self.cfg, self.positions, self.t)

    def order(self, order_id, kind='ENTRY', budget='10', pair='ALGO-USD', dependency=None):
        return native.ResearchOrder(order_id, self.t, pair, kind, D(budget),
                                    1 if kind == 'RECOVERY' else None, dependency)

    def buy(self, order):
        self.controller.claim_order(order)
        assert self.controller.may_fill(order, order.budget, self.cash, self.positions, 0)
        old = self.positions.get(order.pair)
        quantity = order.budget
        gross = order.budget / D('1.004')
        fee = order.budget - gross
        pid = 1 if old else order.order_id
        position = native.a.original.Position(pid, order.pair,
            quantity+(old.quantity if old else Z), order.budget+(old.basis if old else Z), self.t,
            1 if old else 0)
        fill = native.a.original.FleetFill(self.t+300, order.order_id, order.pair, order.kind,
            pid, quantity, gross/quantity, gross, fee, Z, Z, order.depends_on_order_id)
        before = self.cash
        self.cash -= order.budget
        self.positions[order.pair] = position
        self.data.record_buy(fill, position, before)
        return fill, position

    def whole(self, order_id, net):
        position = self.positions['ALGO-USD']
        net = D(net)
        gross = net / D('.996')
        fee = gross-net
        fill = native.a.original.FleetFill(self.t+600, order_id, position.pair, 'EXIT',
            position.position_id, position.quantity, gross/position.quantity,
            gross, fee, position.basis, net-position.basis, None)
        self.cash += net
        self.data.record_full_exit(fill)
        del self.positions['ALGO-USD']
        self.controller.check(cash=self.cash)
        return fill

class Mechanics(unittest.TestCase):
    def close(self, observed, expected):
        self.assertLessEqual(abs(observed-D(expected)), D('1e-18'))

    def test_claims_use_ordinary_first_and_cash_never_changes(self):
        f = Fixture()
        before = f.cash, f.data.parked_cash()
        a=f.order(2); b=f.order(3, pair='XTZ-USD')
        f.controller.claim_order(a); f.controller.claim_order(b)
        self.assertEqual(before, (f.cash, f.data.parked_cash()))
        self.assertEqual((f.controller.claims[2].ordinary, f.controller.claims[2].parked), (D(2),D(8)))
        self.assertEqual((f.controller.claims[3].ordinary, f.controller.claims[3].parked), (Z,D(10)))
        self.assertEqual(f.controller.reserved_total, D(20))
        f.controller.check(cash=f.cash)

    def test_shared_hundred_cap_and_physical_reserve(self):
        f=Fixture(cash='110', principal='98', profit='10')
        f.controller.claim_order(f.order(2,budget='60'))
        f.controller.claim_order(f.order(3,'RECOVERY','42'))
        self.assertEqual(f.controller.reserved_parked,D(100))
        self.assertEqual(f.controller.authority('ENTRY'),Z)
        with self.assertRaises(ArithmeticError):f.controller.claim_order(f.order(4,budget='1'))
        physical=Fixture(cash='110',principal='98',profit='10')
        physical.controller.begin_planning(physical.cash,D(400),physical.cfg,{},physical.t)
        self.assertEqual(physical.controller.authority('ENTRY'),D(10))
        with self.assertRaises(ArithmeticError):physical.controller.claim_order(physical.order(5,budget='10.01'))

    def test_tighter_cap_is_fifty(self):
        f=Fixture('BOTH_TIGHT',cash='110',principal='98',profit='10')
        f.controller.claim_order(f.order(2,budget='52'))
        self.assertEqual(f.controller.reserved_parked,D(50))
        with self.assertRaises(ArithmeticError):f.controller.claim_order(f.order(3,budget='1'))

    def test_restricted_modes_leave_ordinary_path_available(self):
        for arm,denied in [('ENTRY_ONLY','RECOVERY'),('RECOVERY_ONLY','ENTRY')]:
            f=Fixture(arm)
            self.assertEqual(f.controller.authority(denied),D(2))
            with self.assertRaises(ArithmeticError):f.controller.claim_order(f.order(2,denied,'10'))
            f.controller.claim_order(f.order(3,denied,'2'))
            self.assertEqual(f.controller.claims[3].parked,Z)

    def test_reject_expire_duplicate_and_next_frame_cleanup(self):
        f=Fixture(); a=f.order(2);before=(f.cash,f.data.parked_cash())
        f.controller.claim_order(a)
        with self.assertRaises(ArithmeticError):f.controller.claim_order(a)
        with self.assertRaises(ArithmeticError):f.begin()
        self.assertTrue(f.controller.reject(2,'capacity'))
        self.assertFalse(f.controller.reject(2,'repeat'))
        self.assertFalse(f.controller.expire(2))
        self.assertEqual(before,(f.cash,f.data.parked_cash()))
        f.begin();b=f.order(3);f.controller.claim_order(b)
        snapshot=f.controller.finalize_window([b])
        self.assertEqual(snapshot['reserved_total'],Z)
        self.assertEqual(f.controller.terminal[3],'EXPIRED')
        self.assertEqual(before,(f.cash,f.data.parked_cash()))
        with self.assertRaises(ArithmeticError):f.controller.claim_order(b)

    def test_window_cannot_discard_absent_claim_silently(self):
        f=Fixture();f.controller.claim_order(f.order(2))
        with self.assertRaises(ArithmeticError):f.controller.finalize_window([])

    def test_actual_fill_retires_claim_and_creates_exact_native_source_lot(self):
        f=Fixture();fill,position=f.buy(f.order(2))
        self.assertEqual(f.cash,D(92));self.close(f.data.parked_cash(),'92')
        self.assertEqual(f.controller.owned_parked_basis,D(8))
        self.assertFalse(f.controller.claims)
        self.assertEqual(f.controller.terminal[2],'FILLED')
        source=f.controller.lots['lot:2'];owned=f.data.ledger.lots['lot:2']
        self.assertEqual(source.quantity,owned.base_remaining)
        self.assertEqual(source.total_basis,owned.cost_basis_remaining_quote+owned.entry_fees_remaining_quote)
        self.assertEqual(source.parked_basis,D(8))
        before=(f.cash,f.data.parked_cash(),len(f.data.ledger.lots))
        with self.assertRaises(ArithmeticError):f.data.record_buy(fill,position,D(102))
        self.assertEqual(before,(f.cash,f.data.parked_cash(),len(f.data.ledger.lots)))

    def test_amount_or_identity_mismatch_denied_without_financial_mutation(self):
        f=Fixture();order=f.order(2);f.controller.claim_order(order)
        before=(f.cash,f.data.parked_cash())
        with self.assertRaises(ArithmeticError):f.controller.may_fill(order,D('10.01'),f.cash,f.positions,0)
        with self.assertRaises(ArithmeticError):f.controller.may_fill(replace(order,pair='XTZ-USD'),D(10),f.cash,f.positions,0)
        self.assertEqual(before,(f.cash,f.data.parked_cash()))

    def test_mixed_whole_profit_returns_by_owned_quantity_not_parent_basis(self):
        f=Fixture(seed=True);f.buy(f.order(2,'RECOVERY'))
        fill=f.whole(3,'40')
        # ENTRY owns 10 units/basis20; RECOVERY owns 10/basis10, of which8 was P.
        # Each gets20 net; P gets20*(8/10)=16, not40*(8/30).
        self.close(f.data.parked_cash(),'108')
        self.close(f.controller.parked_whole_returned,'16')
        self.close(f.controller.productive_exit_proceeds(fill),'24')
        self.assertEqual(f.controller.parked_basis_retired,D(8))
        self.assertEqual(f.controller.owned_parked_basis,Z)
        self.assertFalse(f.controller.lots)
        with self.assertRaises(ArithmeticError):f.data.record_full_exit(fill)
        self.close(f.data.parked_cash(),'108')

    def test_mixed_whole_loss_retires_basis_without_manufacturing_recovery(self):
        # Deliberate source-accounting stress; native hard-floor strategy would
        # not authorize this losing whole sale in a historical replay.
        f=Fixture(seed=True);f.buy(f.order(2,'RECOVERY'))
        fill=f.whole(3,'15')
        self.close(f.data.parked_cash(),'98')
        self.close(f.controller.parked_whole_returned,'6')
        self.close(f.controller.productive_exit_proceeds(fill),'9')
        self.assertEqual(f.controller.parked_basis_retired,D(8))
        self.assertEqual(f.controller.owned_parked_basis,Z)
        self.assertEqual(f.cash,D(107))

    def test_native_partial_reparks_once_protects_entry_and_retires_source_basis(self):
        f=Fixture(seed=True);_,position=f.buy(f.order(2,'RECOVERY'))
        manifest=f.data.recycle_manifest(position,D('1.02'),f.t+600,f.cfg,native.a.original.VCAPProfitExitAction.WAIT)
        self.assertIsNotNone(manifest)
        order=native.ResearchOrder(3,f.t+600,'ALGO-USD','PARTIAL_RECYCLE',Z,1,None)
        f.data.note_partial(order,manifest,D('1.02'),native.a.original.VCAPProfitExitAction.WAIT)
        f.data.current_bars={'ALGO-USD':native.a.RecordedBar(f.t+600,np.asarray([1.02,1.03,1.01,1.02,1000,10]),True)}
        before=f.data.parked_cash()
        result,reason=f.data.fill_partial(order,position,0,f.t+900,f.cfg,D(1000))
        self.assertIsNone(reason);self.assertIsNotNone(result)
        fill,updated=result;net=fill.gross_quote-fill.fee_quote
        self.close(f.data.parked_cash()-before,net)
        f.cash+=net
        self.close(f.controller.parked_partial_returned,net*D('.8'))
        self.assertEqual(f.controller.parked_basis_retired,D(8))
        self.assertEqual(set(f.controller.lots),{'lot:1'})
        self.assertEqual(f.data.ledger.lots['lot:1'].base_remaining,D(10))
        self.assertEqual(updated.basis,D(20))
        f.controller.check(cash=f.cash)
        after=f.data.parked_cash()
        repeat,reason=f.data.fill_partial(order,updated,0,f.t+1200,f.cfg,D(1000))
        self.assertIsNone(repeat);self.assertEqual(reason,'stale_recovery_manifest')
        self.assertEqual(f.data.parked_cash(),after)

    def test_conditional_replacement_claims_only_settled_cash(self):
        f=Fixture();order=f.order(2,dependency=99)
        before=(f.cash,f.data.parked_cash())
        f.controller.claim_order(order)
        self.assertFalse(f.controller.claims);self.assertIn(2,f.controller.contingent)
        self.assertEqual(before,(f.cash,f.data.parked_cash()))
        # Native dependency guard precedes this call; modeled settled ordinary
        # receipt becomes available only at the later fill-authority boundary.
        self.assertTrue(f.controller.may_fill(order,D(10),D(122),{},0))
        self.assertEqual(f.controller.claims[2].parked,Z)
        self.assertEqual(f.controller.claims[2].ordinary,D(10))
        f.controller.reject(2,'native_reserve_or_other_later_guard')
        self.assertFalse(f.controller.claims);self.assertFalse(f.controller.contingent)

    def test_source_ownership_corruption_is_detected(self):
        f=Fixture();f.buy(f.order(2))
        f.controller.lots['lot:2']=replace(f.controller.lots['lot:2'],quantity=D(11))
        with self.assertRaises(ArithmeticError):f.controller.check(cash=f.cash)

    def test_off_leaves_compiled_kernel_and_native_cash_colors_unchanged(self):
        f=Fixture('OFF',cash='120')
        source=native.GENERATED_SOURCE
        f.data.hydration_generated_source=source
        simulator=native.Simulator(f.cfg);class_before=type(simulator);run_before=simulator.run.__func__
        result=release.compile_hydrated(native,f.data,simulator)
        self.assertIs(result,simulator);self.assertIs(type(result),class_before)
        self.assertIs(result.run.__func__,run_before)
        self.assertEqual(f.data.release_generated_source,source)
        self.assertEqual(f.data.release_source_changes,[])
        before=f.data.parked_cash()
        f.buy(f.order(2))
        self.assertEqual(f.data.parked_cash(),before)
        self.assertEqual(f.controller.parked_spent,Z)

    def test_exact_native_anchors_fail_closed_on_drift_or_duplicate(self):
        source=native.GENERATED_SOURCE
        changed,patches=release.build_source(source)
        self.assertEqual(len(patches),7)
        compile(changed,'independent_generated_release_body.py','exec')
        target='        rejection_counts[reason] += 1'
        with self.assertRaises(ArithmeticError):release.build_source(source.replace(target,'        rejection_counts[reason] += 2'))
        with self.assertRaises(ArithmeticError):release.build_source(source+'\n'+target)

    def test_unknown_arm_and_duplicate_attach_fail_closed(self):
        f=Fixture()
        with self.assertRaises(ValueError):release.ReleaseController(f.data,'UNLIMITED')
        with self.assertRaises(ArithmeticError):release.attach(f.data,'BOTH')

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Mechanics)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    payload={'passed':result.wasSuccessful(),'test_count':result.testsRun,
        'failures':[{'test':str(t),'trace':trace} for t,trace in result.failures],
        'errors':[{'test':str(t),'trace':trace} for t,trace in result.errors],
        'source_sha256':hashlib.sha256((HERE.parent/'release_adapter.py').read_bytes()).hexdigest(),
        'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope':'Independent constructed-money mechanics on authentic native immutable economic lots, native buy/full-exit/partial methods; no historical strategy performance claim',
        'historical_profitability_proven':False,'full_off_historical_equivalence_proven':False}
    (HERE/'Ported_Allocator_Mechanics_Receipt.json').write_text(json.dumps(payload,indent=2)+'\n')
    sys.exit(0 if result.wasSuccessful() else 1)
