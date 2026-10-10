"""Actual preserved native recurrence controller on explicitly synthetic inputs.

Only selector membership/category evidence is a deterministic fixture injection.
Directional and six-bar recovery gates, native funding/partial/controller, exact
owner settlement and recurring receipt validation execute their actual sources.
"""
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'research/danger_room/recurring_ladder_profitability_campaign_v4.py'
spec = importlib.util.spec_from_file_location('recurring_campaign_under_test', PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class SourceBoundaryTests(unittest.TestCase):
    def test_modified_helper_is_rejected_before_its_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            driver = Path(temporary) / PATH.name
            driver.write_bytes(PATH.read_bytes())
            driver.with_name("receipt_recurring_ladder.py").write_text(
                'raise AssertionError("MODIFIED_HELPER_EXECUTED")\n')
            result = subprocess.run([sys.executable, str(driver), "--help"],
                text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source differs before its first import", result.stderr)
            self.assertNotIn("MODIFIED_HELPER_EXECUTED", result.stderr)

    def test_disabled_body_keeps_identical_bytes(self):
        self.assertEqual(module.build_recurrence_body('preserved financial bytes'), ('preserved financial bytes', []))

    def test_active_missing_boundary_fails(self):
        with self.assertRaises(ValueError):
            module.build_recurrence_body('unknown financial bytes', recurrence=True)

    def test_registry_is_exact_frozen_research_and_sizing(self):
        protocol = PATH.with_name('profitability_protocol.json')
        args = SimpleNamespace(protocol=protocol,
            protocol_sha256='a10def9591d19bc8c775d136b5c845383d42778f274e824ad1d77af88552e374',
            owned_protocol=PATH.with_name('owned_cash_protocol.json'),
            sizing_protocol=PATH.with_name('productive_sizing_protocol.json'),
            recurrence_protocol=PATH.with_name('receipt_recurrence_protocol.json'),
            arm='receipt_recurrence_owned_full_sized')
        if not args.sizing_protocol.exists():
            args.sizing_protocol = PATH.with_name('sizing_protocol.json')
        arm, _ = module.resolve_arm(json.loads(protocol.read_text()), args)
        self.assertTrue(arm['productive_sizing'])
        self.assertEqual(arm['entry_budget_equity_fraction'], '0.01')
        args.arm = 'stale_inventory_rotation'
        with self.assertRaises(ValueError):
            module.resolve_arm(json.loads(protocol.read_text()), args)


class AdmissionBindingTests(unittest.TestCase):
    """Fabricated audit files exercise denial logic; these are not real audits."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policies = {name: module.sha256(PATH.with_name(name+'.py')) for name in
            ('owned_cash','module_coordination','productive_sizing','opportunity_selector','market_regime','receipt_recurring_ladder')}
        self.arm = next(item for item in json.loads(PATH.with_name('receipt_recurrence_protocol.json').read_text())['arms']
                        if item['name']=='receipt_recurrence_owned_full')
        self.source = module.sha256(PATH)
        self.output = self.root/'runs'/'new_candidate'
        self.output.mkdir(parents=True)
        self.gate = self.root/'synthetic_admission.json'
        self.gate.write_text(json.dumps({'status':'SYNTHETIC_RECURRENCE_INTEGRATION_PASS',
            'research_adapter_sha256':self.source,'policy_sources':self.policies,
            'recurrence_protocol_sha256':module.RECURRENCE_PROTOCOL_SHA256,
            'recurrence_specification_sha256':module.RECURRENCE_SPEC_SHA256,
            'neutral_v2_generated_body_equivalent':True,'fixture_only':True}))
        self.args = SimpleNamespace(arm=self.arm['name'],owner_adapter_admission=self.gate,
            owner_adapter_admission_sha256=module.sha256(self.gate))
        self.paths = {}
        for label,name,new in (('owned','owned_control_174',False),('v2','combined_full_supported',False),
                               ('recurrence','receipt_recurrence_control_full',True)):
            run = self.root/'runs'/name
            run.mkdir()
            result = {'arm':name,'research_adapter_sha256':self.source if new else module.PARENT_V2_SHA256,
                'policy_sources':self.policies if new else {k:v for k,v in self.policies.items() if k!='receipt_recurring_ladder'},
                'input_identity':{'array_sha256':module.REFERENCE_ARRAY_SHA256},
                'declared_arm':{**self.arm,'recurrence':False},'fixture_only':True}
            (run/'results.json').write_text(json.dumps(result))
            for filename in ('fills.csv','owner_account_5m.jsonl.gz','owner_transactions.jsonl.gz',
                             'owner_checkpoint.json','owner_terminal.json','terminal_lots.json'):
                (run/filename).write_text('explicit synthetic evidence placeholder\n')
            receipt = self.root/(label+'_audit.json')
            receipt.write_text(json.dumps({'passed':True,'full_cash_owner_adapter_verified':True,
                'strategy_imported':False,'arm':name,'input_array_sha256':module.REFERENCE_ARRAY_SHA256,
                'input_metadata_sha256':module.REFERENCE_METADATA_SHA256,
                'neutral_v2_financial_comparison_passed':True,'matched_v2_arm':'combined_full_supported','fixture_only':True}))
            self.paths[label] = (run,receipt)
            setattr(self.args,'matched_'+('owned_control' if label=='owned' else 'v2_comparator' if label=='v2' else 'recurrence_control')+'_receipt',receipt)
            self.rebind(label)

    def rebind(self,label):
        run,receipt = self.paths[label]
        artifacts=[{'file':item.name,'bytes':item.stat().st_size,'sha256':module.sha256(item)}
                   for item in sorted(run.iterdir()) if item.is_file() and item.name!='artifact_manifest.json']
        (run/'artifact_manifest.json').write_text(json.dumps(artifacts))
        proof=json.loads(receipt.read_text())
        proof.update(results_sha256=module.sha256(run/'results.json'),artifacts=artifacts)
        receipt.write_text(json.dumps(proof))
        attr='matched_'+('owned_control' if label=='owned' else 'v2_comparator' if label=='v2' else 'recurrence_control')+'_receipt_sha256'
        setattr(self.args,attr,module.sha256(receipt))

    def check(self):
        return module.verify_v4_gates(self.args,self.arm,self.output,self.source,self.policies)

    def test_exact_bound_fixture_is_accepted(self):
        self.assertEqual(set(self.check()),{'primary_owner_cost_control','v2_mechanism_comparator','v4_neutral_control'})

    def test_missing_new_neutral_receipt_is_rejected(self):
        self.args.matched_recurrence_control_receipt=None
        with self.assertRaises(ValueError): self.check()

    def test_admission_cannot_bind_different_helper_or_source(self):
        for field,value in (('research_adapter_sha256','0'*64),('policy_sources',{}),
                            ('recurrence_protocol_sha256','0'*64),('neutral_v2_generated_body_equivalent',False)):
            original=self.gate.read_text()
            proof=json.loads(original);proof[field]=value
            self.gate.write_text(json.dumps(proof));self.args.owner_adapter_admission_sha256=module.sha256(self.gate)
            with self.assertRaises(ValueError): self.check()
            self.gate.write_text(original);self.args.owner_adapter_admission_sha256=module.sha256(self.gate)

    def test_comparator_sizing_and_extra_mechanism_are_rejected(self):
        run,_=self.paths['v2']
        original=(run/'results.json').read_text()
        for key,value in (('productive_sizing',True),('native_dttp',True),('fee_bps','80')):
            result=json.loads(original);result['declared_arm'][key]=value
            (run/'results.json').write_text(json.dumps(result));self.rebind('v2')
            with self.assertRaises(ValueError): self.check()
            (run/'results.json').write_text(original);self.rebind('v2')

    def test_primary_cost_control_cannot_use_other_costs(self):
        run,_=self.paths['owned'];result=json.loads((run/'results.json').read_text())
        result['declared_arm']['slippage_bps']='15'
        (run/'results.json').write_text(json.dumps(result));self.rebind('owned')
        with self.assertRaises(ValueError): self.check()

    def test_changed_audited_artifact_is_rejected(self):
        run,_=self.paths['v2'];(run/'fills.csv').write_text('changed after audit\n')
        with self.assertRaises(ValueError): self.check()

    def test_same_source_neutral_requires_actual_equivalence_proof(self):
        _,receipt=self.paths['recurrence'];proof=json.loads(receipt.read_text())
        proof['neutral_v2_financial_comparison_passed']=False
        receipt.write_text(json.dumps(proof));self.rebind('recurrence')
        with self.assertRaises(ValueError): self.check()

    def test_parent_comparator_cannot_use_v4_source(self):
        run,_=self.paths['v2'];result=json.loads((run/'results.json').read_text())
        result['research_adapter_sha256']=self.source
        (run/'results.json').write_text(json.dumps(result));self.rebind('v2')
        with self.assertRaises(ValueError): self.check()


def run_native_fixture():
    import contextlib
    import csv
    import gzip
    import io
    import tempfile
    import numpy as np
    from research.danger_room import opportunity_selector
    from research.danger_room import owned_profitability_campaign_v2 as parent
    vendor = ROOT / 'research/holdout_replay/vendor'
    recovered = vendor / 'Spectra_2000_Recovery_Recycling_Evidence'
    core = recovered / 'recovered/original-plan/Spectra_Exit_Strategy_Evidence/Spectra_Exit_Strategy_Evidence/spectra-finance-core/spectra_core'
    manifest = PATH.with_name('native_prerequisites') / 'native_source_manifest.json'
    adapter = recovered / 'baseline-recovery-20261001/adapter/sparse_adapter.py'
    bridge = vendor / 'Spectra_One_Percent_Comparison_Evidence/one-percent-research-20261006/one_percent_test.py'
    inventory = module.verify_native(core, manifest, adapter, bridge)
    native = module.load_native(core, adapter, bridge, inventory)
    module.load_native = lambda *args: native  # confined to isolated synthetic interpreter
    parent_body, _ = parent.build_campaign_body(native.GENERATED_SOURCE,
        selection=True, directional=True, recovery_quality=True, ladder=True)
    parent_body, _ = parent.build_owned_body(parent_body, ladder=True)
    neutral_body, _ = module.build_campaign_body(native.GENERATED_SOURCE,
        selection=True, directional=True, recovery_quality=True, ladder=True)
    neutral_body, _ = module.build_owned_body(neutral_body, ladder=True)
    neutral_body, changes = module.build_recurrence_body(neutral_body, recurrence=False)
    assert neutral_body == parent_body and changes == []
    active_body, _ = module.build_recurrence_body(neutral_body, recurrence=True)
    compile(active_body, 'synthetic-recurring-native-controller', 'exec')
    assert active_body.count('profit_states.pop(order.pair, None)') == 2
    assert 'if not dataset.recurrence_order_is_renewal(order)' in active_body
    class FixtureSelector:
        def __init__(self, rows, pairs, start, config):
            self.pairs = pairs
        def rank_at_index(self, index):
            diagnostics = {pair: {'eligible': True, 'observed_completed_days': 28,
                'recent_coverage': 1, 'observed_close_drawdown_fraction': 0.05,
                'synthetic_selector_category_injection': True} for pair in self.pairs}
            return ['AAA-USD'], diagnostics
    original_selector = opportunity_selector.MatrixOpportunitySelector
    # v4 loads opportunity_selector under its explicit source module name.
    original_loader = module.importlib.util.module_from_spec
    def fixture_module(spec):
        obj = original_loader(spec)
        if spec.name == 'spectra_campaign_selector':
            original_exec = spec.loader.exec_module
            def injected(target):
                original_exec(target)
                target.MatrixOpportunitySelector = FixtureSelector
            spec.loader.exec_module = injected
        return obj
    module.importlib.util.module_from_spec = fixture_module
    completed = []
    for case in ('three_episodes', 'missing_renewal_bar', 'renewal_capacity', 'whole_retirement', 'neutral', 'current_cost', 'armed_floor', 'original_priority', 'sized', 'mixed_whole_retirement'):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); prepared = tmp/'prepared'; prepared.mkdir()
            prices = [95 + 5*i/288 for i in range(289)] + [100]
            pattern = [96,95,95.1,95.3,95.5,95.7,96,98.5,98.5,98.5]
            if case == 'current_cost':
                pattern = [99.6 if value == 98.5 else value for value in pattern]
            prices += pattern*4 + [pattern[-1]]*3
            if case == 'armed_floor':
                prices[299] = 103
            if case == 'original_priority':
                prices[300:330] = [92,90,90.1,90.3,90.5,90.7,91,94,94,94]*3
            if case == 'whole_retirement':
                prices[-3:] = [110,110,110]
            if case == 'mixed_whole_retirement':
                prices[296:] = [110]*(len(prices)-296)
            pairs = ['AAA-USD'] + [f'C{i:03}-USD' for i in range(51)]
            rows = np.zeros((len(prices),len(pairs),6), dtype='float64')
            rows[:,1:,:] = [100,100.25,99.75,100,1000000,1000]
            for index,price in enumerate(prices):
                rows[index,0,:] = [price,price+.25,price-.25,price,1000000,1000]
            # index304 is the first anticipated renewed execution; both failure
            # fixtures retain later complete bars to prove exact zero-fill retry.
            if case == 'missing_renewal_bar':
                rows[305,0,:] = np.nan
            if case == 'renewal_capacity':
                rows[305,0,4] = .001
                rows[307,0,:] = [95.7,98.75,95.45,98.5,1000000,1000]
            np.save(prepared/'ohlcv.npy',rows)
            (prepared/'metadata.json').write_text(json.dumps({'pairs':pairs,'start':1767225600,
                'end':1767225600+len(prices)*300,'interval_seconds':300}))
            module.verify_reference = lambda path: {'synthetic_fixture': True,
                'array_sha256': module.REFERENCE_ARRAY_SHA256,
                'metadata_sha256': module.REFERENCE_METADATA_SHA256, 'holdout_accessed': False}
            policy_names = ('owned_cash','module_coordination','productive_sizing','opportunity_selector','market_regime','receipt_recurring_ladder')
            policies = {name:module.sha256(PATH.with_name(name+'.py')) for name in policy_names}
            gate = tmp/'synthetic-admission.json'
            gate.write_text(json.dumps({'status':'SYNTHETIC_RECURRENCE_INTEGRATION_PASS',
                'research_adapter_sha256':module.sha256(PATH),'policy_sources':policies,
                'recurrence_protocol_sha256':module.RECURRENCE_PROTOCOL_SHA256,
                'recurrence_specification_sha256':module.RECURRENCE_SPEC_SHA256,
                'neutral_v2_generated_body_equivalent':True,'fixture_only':True}))
            cost = case == 'current_cost'
            arm_name = 'receipt_recurrence_control_full' if case == 'neutral' else (
                'current_cost_receipt_recurrence_owned_full' if cost else (
                    'receipt_recurrence_owned_full_sized' if case == 'sized' else 'receipt_recurrence_owned_full'))
            declaration = next(item for item in json.loads(PATH.with_name('receipt_recurrence_protocol.json').read_text())['arms'] if item['name']==arm_name)
            audit_paths = {}
            for label, name, new in (('primary','current_cost_owned_control_174' if cost else 'owned_control_174',False),
                    ('comparator','current_cost_owned_full' if cost else ('owned_full_sized' if case == 'sized' else 'combined_full_supported'),False),
                    ('neutral','receipt_recurrence_control_full',True)):
                run = tmp/'runs'/name; run.mkdir(parents=True,exist_ok=True)
                result = run/'results.json'
                result.write_text(json.dumps({'arm':name,'research_adapter_sha256':module.sha256(PATH) if new else module.PARENT_V2_SHA256,
                    'policy_sources':policies if new else {key:value for key,value in policies.items() if key!='receipt_recurring_ladder'},
                    'input_identity':{'array_sha256':module.REFERENCE_ARRAY_SHA256},
                    'declared_arm':{**declaration,'recurrence':False,'productive_sizing':case=='sized' and label=='comparator'},'fixture_only':True}))
                for artifact in ('fills.csv','owner_account_5m.jsonl.gz','owner_transactions.jsonl.gz',
                        'owner_checkpoint.json','owner_terminal.json','terminal_lots.json'):
                    (run/artifact).write_text('synthetic fixture placeholder\n')
                artifacts = [{'file':item.name,'bytes':item.stat().st_size,'sha256':module.sha256(item)}
                    for item in sorted(run.iterdir()) if item.is_file()]
                (run/'artifact_manifest.json').write_text(json.dumps(artifacts))
                receipt = tmp/(label+'-audit.json')
                receipt.write_text(json.dumps({'passed':True,'full_cash_owner_adapter_verified':True,
                    'strategy_imported':False,'arm':name,'input_array_sha256':module.REFERENCE_ARRAY_SHA256,
                    'input_metadata_sha256':module.REFERENCE_METADATA_SHA256,'results_sha256':module.sha256(result),
                    'neutral_v2_financial_comparison_passed':True,'matched_v2_arm':'combined_full_supported','artifacts':artifacts,'fixture_only':True}))
                audit_paths[label]=receipt
            args = SimpleNamespace(prepared=prepared,native_core=core,native_manifest=manifest,
                native_adapter=adapter,native_bridge=bridge,protocol=PATH.with_name('profitability_protocol.json'),
                protocol_sha256='a10def9591d19bc8c775d136b5c845383d42778f274e824ad1d77af88552e374',
                owned_protocol=PATH.with_name('owned_cash_protocol.json'),
                sizing_protocol=PATH.with_name('productive_sizing_protocol.json'), profit_protocol=None,
                recurrence_protocol=PATH.with_name('receipt_recurrence_protocol.json'),
                owner_adapter_admission=gate,owner_adapter_admission_sha256=module.sha256(gate),
                matched_owned_control_receipt=audit_paths['primary'],matched_owned_control_receipt_sha256=module.sha256(audit_paths['primary']),
                matched_v2_comparator_receipt=audit_paths['comparator'],matched_v2_comparator_receipt_sha256=module.sha256(audit_paths['comparator']),
                matched_recurrence_control_receipt=audit_paths['neutral'],matched_recurrence_control_receipt_sha256=module.sha256(audit_paths['neutral']),
                arm=arm_name,output=tmp/'runs'/('fixture_'+case))
            if not args.sizing_protocol.exists():
                args.sizing_protocol=PATH.with_name('sizing_protocol.json')
            with contextlib.redirect_stdout(io.StringIO()):
                payload=module.run_campaign(args)
            fills=list(csv.DictReader((args.output/'fills.csv').open()))
            buys=[fill for fill in fills if fill['kind']=='RECOVERY']
            if case=='neutral':
                assert len(buys)==1,(case,buys)
                assert not (args.output/'recurrence_checkpoint.json').exists()
            else:
                with gzip.open(args.output/'recurrence_events.jsonl.gz','rt') as stream:
                    events=[json.loads(line) for line in stream]
                episodes=[event['episode'] for event in events if event['event']=='RECURRENCE_BUY_CONFIRMED']
                expected = 1 if case in ('armed_floor','mixed_whole_retirement') else 3
                assert len(episodes)==expected,(case,episodes,payload['research_counts'])
                if case == 'original_priority':
                    assert [episode['rung'] for episode in episodes]==[0,1,0]
                    assert [episode['renewal'] for episode in episodes]==[False,False,True]
                elif case == 'mixed_whole_retirement':
                    assert not episodes[0]['renewal']
                    with gzip.open(args.output/'ownership_events.json.gz','rt') as stream:
                        ownership_events=json.load(stream)
                    sale=next(event for event in ownership_events if event['event']=='FULL_EXIT' and event['fill']['position_id']==1)
                    assert {lot['source'] for lot in sale['lots']}=={'BASE_ENTRY','VCAP_RECOVERY'}
                elif case == 'armed_floor':
                    assert payload['research_counts'].get('RENEWAL_ARMED_PROFIT_FLOOR',0)>0
                else:
                    assert [episode['renewal'] for episode in episodes]==[False,True,True]
                    assert [episode['ordinal'] for episode in episodes]==[1,2,3]
                    preserved=[event for event in events if event['event']=='RECURRENCE_PROFIT_STATE_PRESERVED']
                    assert len(preserved)==2 and all(event['before']==event['after'] for event in preserved)
                checkpoint=json.loads((args.output/'recurrence_checkpoint.json').read_text())
                book=module.RecurringLadderBook.from_checkpoint(checkpoint,expected_sha256=checkpoint['checkpoint_sha256'])
                parent_state=book.parents[1]
                assert parent_state['consumed']==(2 if case=='original_priority' else 1)
                assert len(parent_state['episodes'][0])==(1 if case in ('armed_floor','mixed_whole_retirement') else 2 if case=='original_priority' else 3)
                assert parent_state['turnover']<=parent_state['turnover_cap']
                if case in ('whole_retirement','mixed_whole_retirement'):
                    assert parent_state['retired'] is not None
                    assert parent_state['native_quantity']==D(0) and parent_state['native_basis']==D(0)
                else:
                    assert parent_state['retired'] is None
                if case in ('missing_renewal_bar','renewal_capacity'):
                    assert any(event['event']=='RECURRENCE_REJECTED' for event in events),(case,events)
                assert all(fill['dependency_order_id'] in {'','None'} for fill in fills)
            completed.append(case)
    opportunity_selector.MatrixOpportunitySelector=original_selector
    print(json.dumps({'native_cases':completed,'neutral_v2_body_identical':True,
        'neutral_body_sha256':hashlib.sha256(neutral_body.encode()).hexdigest(),
        'historical_outcomes_read':False,'holdout_accessed':False}))


class IsolatedNativeControllerTests(unittest.TestCase):
    def test_actual_native_recurring_and_neutral_paths(self):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--native-fixture'],
            cwd=ROOT,text=True,capture_output=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        receipt=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(receipt['native_cases']),10)
        self.assertTrue(receipt['neutral_v2_body_identical'])
        self.assertFalse(receipt['historical_outcomes_read'])


if __name__=='__main__':
    if '--native-fixture' in sys.argv:
        run_native_fixture()
    else:
        unittest.main()
