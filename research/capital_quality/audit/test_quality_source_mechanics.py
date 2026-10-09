"""Independent source-purpose mechanics on real native immutable economic lots."""
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

HERE=Path(__file__).resolve().parent
RESEARCH=HERE.parent.parent
if not os.environ.get("SPECTRA_NATIVE_WRAPPER"):
    raise unittest.SkipTest("Native archive required: set SPECTRA_NATIVE_WRAPPER to the frozen continuation_wrapper.py")


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


OLD=load(RESEARCH/"parked_cash/tests/test_release_adapter_native.py","prior_native_money_fixture")
FEATURE_ADAPTER=HERE.parent/"release_adapter.py"
QUALITY=load(FEATURE_ADAPTER,"independent_quality_source_adapter")
native=OLD.native
Z=D(0)


class Gate:
    def __init__(self,good=True): self.good=good;self.calls=[]
    def eligible(self,pair,kind,timestamp):
        self.calls.append((pair,kind,timestamp))
        return self.good


def fixture(arm="SPLIT_RAW",gate=None,**kwargs):
    gate=gate or Gate()
    OLD.release=SimpleNamespace(attach=lambda dataset,chosen:QUALITY.attach(dataset,chosen,gate))
    return OLD.Fixture(arm,**kwargs)


class SourcePurposeMechanics(unittest.TestCase):
    def close(self,a,b): self.assertLessEqual(abs(a-D(b)),D("1e-18"))

    def test_strict_fifty_each_counts_shared_pending_cash_once(self):
        f=fixture(cash="502",principal="500",profit="0")
        before=(f.cash,f.data.parked_cash())
        f.controller.claim_order(f.order(2,"ENTRY","52"))
        f.controller.claim_order(f.order(3,"RECOVERY","50"))
        self.assertEqual(before,(f.cash,f.data.parked_cash()))
        self.assertEqual(f.controller.reserved_for_kind("ENTRY"),D(50))
        self.assertEqual(f.controller.reserved_for_kind("RECOVERY"),D(50))
        self.assertEqual(f.controller.reserved_parked,D(100))
        self.assertEqual(f.controller.authority("ENTRY"),Z)
        self.assertEqual(f.controller.authority("RECOVERY"),Z)
        f.controller.check(cash=f.cash)

    def test_no_cross_borrow_even_when_other_sleeve_empty(self):
        f=fixture(cash="202",principal="200",profit="0")
        f.controller.claim_order(f.order(2,"RECOVERY","52"))
        self.assertEqual(f.controller.reserved_for_kind("RECOVERY"),D(50))
        self.assertEqual(f.controller.reserved_for_kind("ENTRY"),Z)
        self.assertEqual(f.controller.authority("RECOVERY"),Z)
        self.assertEqual(f.controller.authority("ENTRY"),D(50))
        with self.assertRaises(ArithmeticError):f.controller.claim_order(f.order(3,"RECOVERY","1"))
        self.assertEqual(f.controller.reserved_parked,D(50))

    def test_shared_raw_allows_the_same_unused_global_permission(self):
        f=fixture("SHARED_RAW",cash="202",principal="200",profit="0")
        f.controller.claim_order(f.order(2,"RECOVERY","82"))
        self.assertEqual(f.controller.reserved_for_kind("RECOVERY"),D(80))
        self.assertEqual(f.controller.authority("RECOVERY"),D(20))
        f.controller.check(cash=f.cash)

    def test_rejection_restores_same_purpose_capacity_without_cash_movement(self):
        f=fixture(cash="202",principal="200",profit="0")
        before=(f.cash,f.data.parked_cash())
        f.controller.claim_order(f.order(2,"ENTRY","52"))
        self.assertEqual(f.controller.authority("ENTRY"),Z)
        self.assertTrue(f.controller.reject(2,"native_capacity"))
        self.assertFalse(f.controller.reject(2,"duplicate"))
        self.assertEqual(f.controller.authority("ENTRY"),D(52))
        self.assertEqual(before,(f.cash,f.data.parked_cash()))

    def test_failed_quality_gate_retains_ordinary_authority_and_buy(self):
        gate=Gate(False)
        f=fixture("SPLIT_QUALITY",gate=gate)
        self.assertEqual(f.controller.authority("RECOVERY","ALGO-USD",f.t),D(2))
        self.assertEqual(f.controller.authority("ENTRY","ALGO-USD",f.t),D(2))
        before=f.data.parked_cash()
        f.buy(f.order(2,"ENTRY","2"))
        self.assertEqual(f.data.parked_cash(),before)
        self.assertEqual(f.controller.owned_for_kind("ENTRY"),Z)
        self.assertEqual(f.controller.events[-1]["parked_used"],Z)

    def test_denied_parked_shortfall_does_not_create_claim_or_debit(self):
        f=fixture("SPLIT_QUALITY",gate=Gate(False))
        before=(f.cash,f.data.parked_cash())
        with self.assertRaises(ArithmeticError):f.controller.claim_order(f.order(2,"ENTRY","10"))
        self.assertFalse(f.controller.claims)
        self.assertEqual(before,(f.cash,f.data.parked_cash()))

    def test_parent_recovery_does_not_reclassify_entry_source_lot(self):
        f=fixture()
        f.buy(f.order(1,"ENTRY"))
        f.begin()
        f.buy(f.order(2,"RECOVERY"))
        self.assertEqual(f.controller.lot_purposes["lot:1"],"ENTRY")
        self.assertEqual(f.controller.lot_purposes["lot:2"],"RECOVERY")
        self.assertEqual(f.controller.owned_for_kind("ENTRY"),D(8))
        self.assertEqual(f.controller.owned_for_kind("RECOVERY"),D(10))
        self.assertEqual(f.controller.kind_remaining("ENTRY"),D(42))
        self.assertEqual(f.controller.kind_remaining("RECOVERY"),D(40))
        f.controller.check(cash=f.cash)

    def test_mixed_whole_exit_retires_each_original_purpose_once(self):
        f=fixture()
        f.buy(f.order(1,"ENTRY"));f.begin()
        f.buy(f.order(2,"RECOVERY"))
        fill=f.whole(3,"40")
        self.assertEqual(f.controller.owned_for_kind("ENTRY"),Z)
        self.assertEqual(f.controller.owned_for_kind("RECOVERY"),Z)
        self.assertEqual(f.controller.parked_retired_by_kind,{"ENTRY":D(8),"RECOVERY":D(10)})
        self.close(f.data.parked_cash(),"118")
        self.close(f.controller.productive_exit_proceeds(fill),"4")
        self.assertEqual(f.controller.lot_purposes,{"lot:1":"ENTRY","lot:2":"RECOVERY"})
        before=f.data.parked_cash()
        with self.assertRaises(ArithmeticError):f.data.record_full_exit(fill)
        self.assertEqual(f.data.parked_cash(),before)

    def test_actual_native_partial_frees_recovery_keeps_entry_budget(self):
        f=fixture()
        f.buy(f.order(1,"ENTRY"));f.begin()
        _,position=f.buy(f.order(2,"RECOVERY"))
        manifest=f.data.recycle_manifest(position,D("1.02"),f.t+600,f.cfg,native.a.original.VCAPProfitExitAction.WAIT)
        self.assertIsNotNone(manifest)
        order=native.ResearchOrder(3,f.t+600,"ALGO-USD","PARTIAL_RECYCLE",Z,1,None)
        f.data.note_partial(order,manifest,D("1.02"),native.a.original.VCAPProfitExitAction.WAIT)
        f.data.current_bars={"ALGO-USD":native.a.RecordedBar(f.t+600,np.asarray([1.02,1.03,1.01,1.02,1000,10]),True)}
        before=f.data.parked_cash()
        result,reason=f.data.fill_partial(order,position,0,f.t+900,f.cfg,D(1000))
        self.assertIsNone(reason)
        fill,updated=result
        net=fill.gross_quote-fill.fee_quote
        f.cash+=net
        self.close(f.data.parked_cash()-before,net)
        self.assertEqual(f.controller.owned_for_kind("ENTRY"),D(8))
        self.assertEqual(f.controller.owned_for_kind("RECOVERY"),Z)
        self.assertEqual(f.controller.parked_retired_by_kind,{"ENTRY":Z,"RECOVERY":D(10)})
        self.assertEqual(f.controller.lots["lot:1"].parked_basis,D(8))
        f.controller.check(cash=f.cash)
        parked_after=f.data.parked_cash()
        duplicate,reason=f.data.fill_partial(order,updated,0,f.t+1200,f.cfg,D(1000))
        self.assertIsNone(duplicate)
        self.assertEqual(f.data.parked_cash(),parked_after)

    def test_purpose_identity_corruption_fails_closed(self):
        f=fixture();f.buy(f.order(2,"ENTRY"))
        f.controller.lot_purposes["lot:2"]="RECOVERY"
        with self.assertRaises(ArithmeticError):f.controller.check(cash=f.cash)

    def test_contingent_eligibility_frozen_before_execution(self):
        gate=Gate(True)
        f=fixture("SPLIT_QUALITY",gate=gate)
        order=f.order(2,"ENTRY","10",dependency=99)
        f.controller.claim_order(order)
        self.assertEqual(gate.calls,[("ALGO-USD","ENTRY",f.t)])
        self.assertFalse(f.controller.claims)
        gate.good=False
        self.assertTrue(f.controller.may_fill(order,D(10),f.cash,{},1))
        self.assertEqual(gate.calls,[("ALGO-USD","ENTRY",f.t)])
        self.assertEqual(f.controller.claims[2].parked,D(8))
        self.assertEqual(f.controller.events[-1]["decision_timestamp"],f.t)
        f.controller.reject(2,"later_native_guard")

    def test_ineligible_contingent_cannot_requalify_from_future_data(self):
        gate=Gate(False)
        f=fixture("SPLIT_QUALITY",gate=gate)
        order=f.order(2,"ENTRY","10",dependency=99)
        f.controller.claim_order(order)
        gate.good=True
        self.assertFalse(f.controller.may_fill(order,D(10),f.cash,{},1))
        self.assertEqual(gate.calls,[("ALGO-USD","ENTRY",f.t)])
        self.assertFalse(f.controller.claims)
        self.assertTrue(f.controller.reject(2,"denied"))


if __name__=="__main__":
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SourcePurposeMechanics))
    receipt=dict(passed=result.wasSuccessful(),tests=result.testsRun,
        adapter_sha256=hashlib.sha256(FEATURE_ADAPTER.read_bytes()).hexdigest(),
        base_adapter_sha256=hashlib.sha256((RESEARCH/"parked_cash/release_adapter.py").read_bytes()).hexdigest(),
        failures=[dict(test=str(test),trace=trace) for test,trace in result.failures],
        errors=[dict(test=str(test),trace=trace) for test,trace in result.errors],
        scope="Independent constructed money checks through authentic native ownership hooks; no strategy replay or profitability claim.")
    (HERE/"Ported_Purpose_Quality_Mechanics_Receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
