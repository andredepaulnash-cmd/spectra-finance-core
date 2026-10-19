"""Synthetic only: actual adapter receipts and separate real native SQLite stores."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
import json
import sqlite3

import pytest

from research.weekly_reversal.entry_admission import NativeEntryAdmission, AdmissionBlocked, SIGNAL_LABEL
from research.weekly_reversal.owned_spot.adapter import OwnedSpotAdapter, serial
from research.weekly_reversal.owned_spot.model import AdapterConfig, TradingRule, CompletedBar, ObservedOpen
from research.weekly_reversal.owned_spot import native as N
from spectra_core.next_entry_allocator import Allocator, Member, MembershipSnapshot, Policy, EntryPermit
from spectra_core.next_entry_profit_bridge import NativeSettledProfitBridge

NOW = 36000
PAIR = "BTC-USD"


def setup_authority(root, *, repair=False, funding=False, base_only=False, commit=True):
    config = AdapterConfig(fee_rate=D('.008'), fee_quantum=D('.00001'),
        rules={PAIR: TradingRule(D('.00001'), D('.001'), D('.5'), D('.01'))},
        seed_count=2 if repair else 1, seed_ticket=D('50'), execution_delay_seconds=300,
        max_order_age_seconds=300)
    adapter = OwnedSpotAdapter(root, config, now=3600)
    adapter.observe(CompletedBar(PAIR, 3300, 3600, D('10'), D('1000000'), 3600), now=3600)
    orders = adapter.prepare_seed_asset(PAIR, now=3600)
    for order in orders:
        adapter.observe_open(order['id'], ObservedOpen(D('10'), 3900, 3900))
    adapter.refresh_accounting_all(now=3900)
    assert adapter.seed_complete()
    cid = orders[0]['cohort']
    sale_price=D('10') if base_only else D('12')
    adapter.observe(CompletedBar(PAIR, 6900, 7200, sale_price, D('1000000'), 7200), now=7200)
    adapter.refresh_accounting_all(now=7200)
    # Coherent exact sale fixture. We do not invent native fills or profit flags.
    owners = N.economic_exit._group_owners(lots=adapter._cohort_lots(adapter.state['cohorts'][cid]), governor=adapter.governor, pair=PAIR)
    adapter.state['assets'][PAIR]['selected'] = cid
    adapter.state['assets'][PAIR]['exit'] = {'id': 'fixture-exit', 'cohort': cid, 'pair': PAIR,
        'owners': [serial(asdict(owner)) for owner in owners], 'status': 'OPEN'}
    adapter._save()
    sale = adapter.continue_exit(PAIR, now=7200)
    trade = adapter.observe_open(sale['id'], ObservedOpen(sale_price, 7500, 7500))
    adapter.continue_exit(PAIR, now=7500)
    allocator = Allocator(root / 'allocator.sqlite', account_id=config.account, currency='USD',
        policy=Policy(D('.00000001'), D('.01'), 86400, 60))
    snapshot = MembershipSnapshot('weekly-members', config.account, 'USD', 1, 100000,
        (Member(PAIR, config.owner + ':' + PAIR, 'POSITION_ALLOCATION'),
         Member('VCAP', config.owner + ':VCAP', 'VCAP')))
    bridge_class, kwargs = NativeSettledProfitBridge, {}
    if funding:
        from spectra_core.next_entry_funding_bridge import NativeEntryFundingBridge
        bridge_class = NativeEntryFundingBridge
        helper = NativeEntryAdmission(adapter,allocator=allocator,snapshot_id=snapshot.snapshot_id)
        kwargs = dict(admission=helper,funding_enabled=True)
    bridge = bridge_class(**kwargs,paper_db=root/'paper.sqlite', native_db=root/'native.sqlite',
        allocator=allocator, account_id=config.account, connector=config.connector,
        owner_id=config.owner, enabled=True)
    bridge.stage(trade_id=trade, snapshot=snapshot, now=7500)
    bridge.dispatch(trade_id=trade, now=7500)
    price = D('9') if repair else D('11')
    adapter.observe(CompletedBar(PAIR, NOW-300, NOW, price, D('1000000'), NOW), now=NOW)
    adapter.refresh_accounting_all(now=NOW)
    adapter.state['next_entry_completed_hours'] = {PAIR: [
        dict(pair=PAIR, opened_at=28800, open=10., high=10., low=10., close=10., volume=100., trades=10),
        dict(pair=PAIR, opened_at=32400, open=10., high=11., low=10., close=11., volume=100., trades=10)]}
    allocation = 'VCAP' if repair else PAIR
    profit = D(next((row['available'] for row in allocator.audit()['balances'] if row['allocation_id']==allocation), '0'))
    if repair:
        candidate = adapter.repair_feasibility(PAIR, now=NOW, rung=1)
        assert candidate['eligible'], candidate
        cid = candidate['cohort_id']
        target = D(candidate['target'])
        base = target-profit
    else:
        helper = bridge.admission if funding else NativeEntryAdmission(adapter,allocator=allocator,snapshot_id=snapshot.snapshot_id,cash_bridge=bridge)
        base = D(helper.root_reentry_source(cid,now=NOW)['base_budget'])
        target = base+profit
    order = dict(id='candidate', entry_id='candidate', pair=PAIR,
        position=adapter._current_position(PAIR) if repair else config.owner+':'+PAIR+':native:2',
        cohort=cid, kind='REPAIR' if repair else 'REENTRY', side='BUY',
        pool='next_entry:candidate', source_pool='repair' if repair else 'seed',
        funding_owner='repair' if repair else 'seed', status='FUNDING_PREPARED',
        proposal='candidate:proposal', executor='candidate:executor', intent='RECOVERY' if repair else 'ENTRY',
        target=str(target), principal=str(target/(1+config.fee_rate)),
        quantity=str(config.quantity_for_budget(PAIR,target,price)), reference=str(price),
        decision_at=NOW, created_at=NOW, decision_bar=NOW, capacity='10000.00',
        terminal=None, execution_frozen=False, cohort_generation=1 if repair else 2,
        previous_cohort_generation=None if repair else 1, repair_rung=1 if repair else None,
        base_budget=str(base), profit_budget=str(profit), snapshot_id=snapshot.snapshot_id,
        allocation_id=allocation, allocation_owner_id=config.owner+':'+allocation,
        allocation_role='VCAP' if repair else 'POSITION_ALLOCATION')
    if base_only:
        order.update(status='ENTRY_PREPARED', pool='seed')
    order['decision_quantity'], order['decision_reference'] = order['quantity'], order['reference']
    adapter.state['orders'][order['id']] = order
    adapter._save()
    proposal = N.models.CapitalProposal(order['proposal'],PAIR,N.models.IntentKind(order['intent']),D(order['principal']))
    if commit:
        with N.paper_time(NOW):
            decision = adapter.governor.reserve(proposal, adapter._snapshot(PAIR,order))
            assert decision.approved
            assert adapter.governor.commit_if_matches(proposal)
    helper = bridge.admission if funding else NativeEntryAdmission(adapter,allocator=allocator,snapshot_id=snapshot.snapshot_id,cash_bridge=bridge)
    return adapter, allocator, helper, order


def mutate(adapter, fn):
    fn(adapter.state)
    adapter._save()


def test_exact_reentry_proof_is_pure_and_recomputable(tmp_path):
    a, allocator, helper, order = setup_authority(tmp_path)
    try:
        before = deepcopy(a.state)
        proof = helper.prove(order['id'],now=NOW)
        assert proof['evidence']['signal']['label'] == SIGNAL_LABEL
        assert proof['permit']['base_entry_budget']=='50'
        assert D(proof['permit']['full_fee_inclusive_budget']) > 50
        assert helper.validate(proof, order, now=NOW)==proof
        assert a.state == before
        assert allocator.audit()['reserved_entitlements']=='0.00000000'
        assert not sqlite3.connect(tmp_path/'native.sqlite').execute("SELECT * FROM execution_receipts WHERE proposal_id='candidate:proposal'").fetchall()
    finally:
        a.close()


def test_repair_keeps_exact_due_full_double_target(tmp_path):
    a, allocator, helper, order = setup_authority(tmp_path,repair=True)
    try:
        proof=helper.prove(order['id'],now=NOW)
        assert D(proof['permit']['full_fee_inclusive_budget']) == a.cohort_basis(order['cohort'])
        assert D(proof['permit']['base_entry_budget'])+D(proof['permit']['approved_profit_increment'])==D(order['target'])
        assert proof['permit']['role']=='VCAP'
        assert proof['evidence']['signal']['label']=='UNCHANGED_NATIVE_FULL_DOUBLE_REPAIR'
        assert a.state['assets'][PAIR]['selected'] is None
        assert a.state['cohorts'][order['cohort']]['rung'] is None
    finally:
        a.close()


@pytest.mark.parametrize('change,code',[
    (lambda s:s['orders']['candidate'].update(pool='seed'),'SOURCE_POOL'),
    (lambda s:s['orders']['candidate'].update(target='100'),'FULL_TARGET'),
    (lambda s:s['orders']['candidate'].update(allocation_id='VCAP'),'ALLOCATION_SCOPE'),
    (lambda s:s['orders']['candidate'].update(cohort_generation=9),'COHORT_GENERATION'),
    (lambda s:s['cohorts'][s['orders']['candidate']['cohort']].update(status='OWNED'),'HELD_POSITION_TOPUP'),
    (lambda s:s.update(next_entry_completed_hours={}), 'NEW_ENTRY_HOUR_MISSING'),
    (lambda s:s['next_entry_completed_hours'][PAIR][-1].update(close=10.,high=11.), 'NEW_ENTRY_HOUR_NOT_POSITIVE'),
    (lambda s:s['marks'][PAIR].update(opened_at=NOW-600),'FRESH_BAR'),
    (lambda s:s['psm'].update({PAIR:{'mode':'REPAIR','action':None,'episode':'fake','archived_episodes':[]}}),'PSM_HANDBACK'),
])
def test_independent_guards_do_not_follow_committed_governor(tmp_path,change,code):
    a, _, helper, order=setup_authority(tmp_path)
    try:
        mutate(a,change)
        assert a.governor.get_decision(order['proposal']).status.value=='COMMITTED'
        with pytest.raises(AdmissionBlocked,match=code):
            helper.prove(order['id'],now=NOW)
    finally:
        a.close()


def test_replay_requires_real_reservation_and_preserves_original_permit(tmp_path):
    a, allocator, helper, order=setup_authority(tmp_path)
    try:
        proof=helper.prove(order['id'],now=NOW)
        with pytest.raises(AdmissionBlocked,match='REPLAY_UNRESERVED'):
            helper.validate(proof,order,now=NOW,replay=True)
        allocator.reserve(operation_id='reserve',reservation_id='reservation',snapshot_id='weekly-members',permit=EntryPermit(**proof['permit']),now=NOW)
        assert helper.validate(proof,order,now=NOW,replay=True)==proof
        with pytest.raises(AdmissionBlocked,match='PROOF_EXPIRED'):
            helper.validate(proof,order,now=NOW+31,replay=True)
        changed=deepcopy(proof);changed['permit']['full_fee_inclusive_budget']='999'
        with pytest.raises(AdmissionBlocked,match='PROOF_IDENTITY'):
            helper.validate(changed,order,now=NOW,replay=True)
    finally:
        a.close()


def test_stale_adapter_object_cannot_override_durable_state(tmp_path):
    a,_,helper,order=setup_authority(tmp_path)
    try:
        stale=deepcopy(a.state)
        mutate(a,lambda s:s['orders']['candidate'].update(profit_budget='1'))
        with pytest.raises(AdmissionBlocked,match='AUTHORITY'):
            helper.prove(order['id'],now=NOW,state=stale)
    finally:
        a.close()


def test_initial_retry_keeps_frozen_approval_times(tmp_path):
    a,_,helper,order=setup_authority(tmp_path)
    try:
        proof=helper.prove(order['id'],now=NOW)
        a.advance(NOW+1); a._save()
        assert helper.validate(proof,order,now=NOW+1)==proof
        assert proof['permit']['approved_at']==NOW
    finally:
        a.close()


def test_decision_sizing_is_immutable_but_delayed_open_can_round_safely(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path)
    try:
        proof=helper.prove(order['id'],now=NOW)
        allocator.reserve(operation_id='reserve',reservation_id='reservation',snapshot_id='weekly-members',permit=EntryPermit(**proof['permit']),now=NOW)
        price=D('10.51')
        order.update(status='OPEN', execution_frozen=True, execution_price=str(price),
            execution_observed_at=NOW+300, reference=str(price),
            quantity=str(a.config.quantity_for_budget(PAIR,D(order['target']),price)))
        a._save()
        assert helper.validate(proof,order,now=NOW+300,replay=True)==proof
        order['decision_reference']='12';a._save()
        with pytest.raises(AdmissionBlocked,match='ORDER_BINDING'):
            helper.validate(proof,order,now=NOW+300,replay=True)
    finally:
        a.close()


def test_repair_not_due_cannot_be_made_eligible_by_profit(tmp_path):
    a,_,helper,order=setup_authority(tmp_path,repair=True)
    try:
        mutate(a,lambda s:s['marks'][PAIR].update(close='10'))
        with pytest.raises(AdmissionBlocked,match='REPAIR_GUARD'):
            helper.prove(order['id'],now=NOW)
    finally:
        a.close()


def test_changed_rule_identity_cannot_reuse_proof(tmp_path):
    a,_,helper,order=setup_authority(tmp_path)
    try:
        proof=helper.prove(order['id'],now=NOW)
        mutate(a,lambda s:s['identity']['config']['rules'][PAIR].update(min_quote='100'))
        with pytest.raises(AdmissionBlocked,match='CONFIG'):
            helper.validate(proof,now=NOW)
    finally:
        a.close()


def test_exact_native_cash_coverage_catches_receipt_tampering(tmp_path):
    a,_,helper,order=setup_authority(tmp_path)
    try:
        with a._db() as db:
            row=db.execute('SELECT id,payload FROM receipts LIMIT 1').fetchone()
            payload=json.loads(row[1]);payload['fee_paid']='0'
            db.execute('UPDATE receipts SET payload=? WHERE id=?',(json.dumps(payload),row[0]))
        with pytest.raises(ValueError,match='cash history amount mismatch'):
            helper.prove(order['id'],now=NOW)
    finally:
        a.close()


def test_v2_prepared_row_normalization_preserves_identical_guard_proof(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    try:
        before=helper.prove(order['id'],now=NOW)
        proof=helper.cash_bridge.prepare_entry(order_id=order['id'],now=NOW)
        assert proof==before
        assert helper.cash_bridge.cash_projection().pending_trade_ids==('candidate',)
        assert helper.validate(proof,order,now=NOW)==proof
        funded=helper.cash_bridge.fund_entry(entry_id=order['entry_id'],now=NOW)
        assert helper.validate(proof,order,now=NOW,replay=True)==proof
        assert helper.cash_bridge.cash_projection().cash_pools[order['pool']]==D(order['target'])
        assert a.state['orders'][order['id']]['status']=='FUNDING_PREPARED'
    finally:
        a.close()


def test_closed_original_position_cannot_skip_accounting_health(tmp_path):
    a,_,helper,order=setup_authority(tmp_path)
    try:
        with sqlite3.connect(tmp_path/'native.sqlite') as db:
            db.execute('DELETE FROM economic_accounting_health')
        with pytest.raises(AdmissionBlocked,match='ACCOUNTING_HEALTH'):
            helper.prove(order['id'],now=NOW)
    finally:
        a.close()


@pytest.mark.parametrize('repair',[False,True])
def test_funded_dedicated_cash_claim_counts_once_with_open_hold(tmp_path,repair):
    a,_,helper,order=setup_authority(tmp_path,funding=True,repair=repair)
    try:
        bridge=helper.cash_bridge
        bridge.prepare_entry(order_id=order['id'],now=NOW)
        bridge.fund_entry(entry_id=order['entry_id'],now=NOW)
        projection=bridge.cash_projection()
        view=helper._view(a.state,NOW,projection)
        view.state['orders'][order['id']]['status']='OPEN'
        candidate=dict(order,source_pool=order['pool'],target='20')
        with helper._paper() as db:
            claims=helper._capital_claims(db,view,projection,candidate,D('10'))
        cash=D(order['target'])
        assert claims['total']==a.deployed_basis()+cash
        assert claims['pairs'][PAIR]==a.deployed_basis(PAIR)+cash
        assert claims['recovery']==a.recovery_basis()+(cash if repair else D(0))
        assert claims['source_claim_transferred']==10
        assert claims['increment']==10
    finally:
        a.close()


def test_loss_sale_base_reentry_has_no_fabricated_allocator_permit(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,base_only=True)
    try:
        assert allocator.export_research_state()['snapshots']=={}
        proof=helper.prove_base_entry(order['id'],now=NOW)
        assert proof['permit'] is None
        assert proof['allocator_reservation_required'] is False
        assert D(proof['base_entry']['approved_profit_increment'])==0
        assert D(proof['base_entry']['full_fee_inclusive_budget'])==D(order['base_budget'])<50
        assert helper.validate_base_entry(proof,order,now=NOW)==proof
        assert allocator.export_research_state()['reservations']=={}
        assert not sqlite3.connect(tmp_path/'native.sqlite').execute("SELECT 1 FROM execution_receipts WHERE proposal_id='candidate:proposal'").fetchone()
    finally:
        a.close()


@pytest.mark.parametrize('base_only',[False,True])
def test_preview_explains_missing_commitment_without_authorizing(tmp_path,base_only):
    a,_,helper,order=setup_authority(tmp_path,base_only=base_only,commit=False)
    try:
        result=helper.preview(order['id'],now=NOW,base_only=base_only)
        assert result['eligible_except_native'] is True,result
        assert result['order_permission'] is False
        assert result['native_committed'] is False
        assert result['native_reason']=='NATIVE_RESERVATION_MISSING'
        assert 'permit' not in result
        prove=helper.prove_base_entry if base_only else helper.prove
        with pytest.raises(AdmissionBlocked,match='NATIVE_RESERVATION'):
            prove(order['id'],now=NOW)
        mutate(a,lambda s:s.update(next_entry_completed_hours={}))
        result=helper.preview(order['id'],now=NOW,base_only=base_only)
        assert result['eligible_except_native'] is False
        assert result['reason_code']=='NEW_ENTRY_HOUR_MISSING'
        assert a.governor.get_decision(order['proposal']) is None
    finally:
        a.close()


def test_expired_accepted_reserve_has_separate_non_authorizing_cleanup(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path)
    try:
        proof=helper.prove(order['id'],now=NOW)
        allocator.reserve(operation_id='reserve',reservation_id='reservation',snapshot_id='weekly-members',permit=EntryPermit(**proof['permit']),now=NOW)
        result=helper.validate(proof,order,now=NOW+60,replay=True,reconciliation_only=True)
        assert result['order_permission'] is False and result['reservation_permission'] is False
        assert 'permit' not in result
        with pytest.raises(AdmissionBlocked,match='PROOF_EXPIRED'):
            helper.validate(proof,order,now=NOW+60,replay=True)
    finally:
        a.close()


def test_unaccepted_expired_frozen_identity_is_reconciliation_only(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    try:
        proof=helper.cash_bridge.prepare_entry(order_id=order['id'],now=NOW)
        result=helper.validate_frozen(proof,order,now=NOW+60)
        assert result['order_permission'] is False and result['reservation_permission'] is False
        assert allocator.export_research_state()['reservations']=={}
        with pytest.raises(AdmissionBlocked,match='PROOF_EXPIRED'):
            helper.validate(proof,order,now=NOW+60)
    finally:
        a.close()


@pytest.mark.parametrize('field',['signal','cash_receipts','source_events'])
def test_rehashing_evidence_cannot_keep_external_original_proof_id(tmp_path,field):
    import hashlib
    a,allocator,helper,order=setup_authority(tmp_path)
    try:
        proof=helper.prove(order['id'],now=NOW)
        allocator.reserve(operation_id='reserve',reservation_id='reservation',snapshot_id='weekly-members',permit=EntryPermit(**proof['permit']),now=NOW)
        altered=deepcopy(proof)
        altered['evidence'][field]={} if field!='cash_receipts' else []
        altered.pop('proof_sha256')
        altered['proof_sha256']=hashlib.sha256(json.dumps(altered,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with pytest.raises(AdmissionBlocked,match='PROOF_IDENTITY'):
            helper.validate(altered,order,now=NOW,replay=True)
    finally:
        a.close()


def test_base_entry_replay_uses_native_execution_and_original_audit_anchor(tmp_path):
    a,_,helper,order=setup_authority(tmp_path,base_only=True)
    try:
        proof=helper.prove_base_entry(order['id'],now=NOW)
        order['base_entry_proof']=proof
        a._save('base_entry_admitted',{'order':order['id'],'proof':proof['proof_sha256']})
        a.ledger.open_position(position_id=order['position'],pair=PAIR,created_at=NOW)
        a.state['assets'][PAIR]['positions'].append(order['position'])
        a._authorize(order)
        a.observe_open(order['id'],ObservedOpen(D('10.51'),NOW+300,NOW+300))
        assert helper.validate_base_entry(proof,order,now=NOW+300,replay=True)==proof
        with a._db() as db:
            db.execute("DELETE FROM audit_events WHERE kind='base_entry_admitted'")
        with pytest.raises(AdmissionBlocked,match='BASE_REPLAY'):
            helper.validate_base_entry(proof,order,now=NOW+300,replay=True)
    finally:
        a.close()


def activate_fixture(a,helper,order):
    if order['status']=='ENTRY_PREPARED':
        proof=helper.prove_base_entry(order['id'],now=NOW)
        order['base_entry_proof']=proof
        a._save('base_entry_admitted',{'order':order['id'],'proof':proof['proof_sha256']})
    else:
        proof=helper.cash_bridge.prepare_entry(order_id=order['id'],now=NOW)
        helper.cash_bridge.fund_entry(entry_id=order['entry_id'],now=NOW)
    if order['position'] not in a.state['assets'][PAIR]['positions']:
        a.ledger.open_position(position_id=order['position'],pair=PAIR,created_at=NOW)
        a.state['assets'][PAIR]['positions'].append(order['position'])
    cohort=a.state['cohorts'][order['cohort']]
    if order['kind']=='REENTRY':
        fields=('generation','anchor','acquired_at','last_rung','rung','status','profit_state','lot_ids')
        cohort.setdefault('generation_history',[]).append({key:cohort.get(key) for key in fields})
        cohort.update(generation=order['cohort_generation'],anchor=None,acquired_at=None,
            last_rung=0,rung=None,status='OWNED',profit_state=None,lot_ids=[])
    else:
        a.state['assets'][PAIR].update(selected=cohort['id'],last_repair_bar=NOW,repair_block=None)
        cohort['rung']=dict(level=order['repair_rung'],requested_target=order['target'],
            start_basis=order['target'],actual_added='0',shortfall=order['target'],status='PENDING',
            decision_bar=NOW,quantity=order['decision_quantity'],reference=order['decision_reference'],orders=[order['id']])
    key=f'{PAIR}:{NOW}'
    a.state.setdefault('liquidity',{})[key]=str(D(proof['evidence']['prior_liquidity_used'])+D(order['target']))
    order.update(status='PREPARING',activation=dict(at=NOW,proof_sha256=proof['proof_sha256'],
        liquidity_key=key,liquidity_debit=order['target']))
    a._save('weekly_entry_activation',{'order':order['id'],'activation':order['activation']})
    return proof


@pytest.mark.parametrize('mode',['base','reentry','repair'])
def test_atomic_activation_verifies_original_state_without_redoing_sold_gate(tmp_path,mode):
    a,_,helper,order=setup_authority(tmp_path,funding=mode!='base',base_only=mode=='base',repair=mode=='repair')
    try:
        proof=activate_fixture(a,helper,order)
        assert not sqlite3.connect(tmp_path/'native.sqlite').execute("SELECT 1 FROM execution_receipts WHERE proposal_id='candidate:proposal'").fetchone()
        assert helper.validate_activation(proof,order,now=NOW)==proof
        a.advance(NOW+1);a._save()
        assert helper.validate_activation(proof,order,now=NOW+1)==proof
        a.advance(NOW+31);a._save()
        with pytest.raises(AdmissionBlocked,match='ACTIVATION'):
            helper.validate_activation(proof,order,now=NOW+31)
    finally:
        a.close()


@pytest.mark.parametrize('tamper',[
    lambda s:s['cohorts'][s['orders']['candidate']['cohort']].update(generation=9),
    lambda s:s['liquidity'].update({f'{PAIR}:{NOW}':'0'}),
    lambda s:s['orders']['candidate']['activation'].update(proof_sha256='changed'),
    lambda s:s['cohorts'][s['orders']['candidate']['cohort']]['generation_history'][-1].update(anchor='999'),
    lambda s:s['cohorts'][s['orders']['candidate']['cohort']].update(ordinal=2),
    lambda s:s['assets'][PAIR].update(last_native_decision=999),
])
def test_base_activation_rejects_changed_atomic_state(tmp_path,tamper):
    a,_,helper,order=setup_authority(tmp_path,base_only=True)
    try:
        proof=activate_fixture(a,helper,order)
        mutate(a,tamper)
        with pytest.raises(AdmissionBlocked,match='ACTIVATION'):
            helper.validate_activation(proof,order,now=NOW)
    finally:
        a.close()


@pytest.mark.parametrize('mode',['base','reentry','repair'])
def test_activation_rejects_released_native_authority(tmp_path,mode):
    a,_,helper,order=setup_authority(tmp_path,funding=mode!='base',base_only=mode=='base',repair=mode=='repair')
    try:
        proof=activate_fixture(a,helper,order)
        with sqlite3.connect(tmp_path/'native.sqlite') as native:
            native.execute("UPDATE capital_reservations SET status='RELEASED' WHERE proposal_id=?",(order['proposal'],))
        with pytest.raises(AdmissionBlocked,match='ACTIVATION'):
            helper.validate_activation(proof,order,now=NOW)
    finally:
        a.close()


def test_actual_lot_rounding_release_retains_subcent_for_future_allocation(tmp_path):
    from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
    from test_weekly_compounding_adapter import config, members, seed, profitable_sale, reentry_boundary
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        order=a.request_reentry('BTC-USD:seed:01',now=36000)
        a.observe(CompletedBar(PAIR,36000,36300,D('11'),D('1000000'),36300),now=36300)
        a.refresh_accounting_all(now=36300)
        a.observe_open(order['id'],ObservedOpen(D('11'),36300,36300))
        a.refresh_accounting_all(now=36300);a.sync_profit_cash(now=36300)
        a.observe(CompletedBar(PAIR,36300,36600,D('11'),D('1000000'),36600),now=36600)
        a.refresh_accounting_all(now=36600);a.sync_profit_cash(now=36600)
        raw=D(next(row['available'] for row in a.allocator.audit()['balances'] if row['allocation_id']==PAIR))
        assert 0<raw<D('.01')
        with a.entry_admission._paper() as db:
            _,_,lots,spendable,_=a.entry_admission._membership(a.state,order,36600,db=db,base_only=True)
        assert spendable==0
        assert D(a.allocator._amount(sum(lot['units'] for lot in lots)))==raw
        assert D(next(row['available'] for row in a.allocator.audit()['balances'] if row['allocation_id']==PAIR))==raw


@pytest.mark.parametrize('held',[False,True])
def test_original_slot_never_borrows_past_psm_root_successor(tmp_path,held):
    from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
    from test_weekly_compounding_adapter import config, members, seed, profitable_sale, reentry_boundary
    cfg=config(('BTC',));cid=PAIR+':seed:01'
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        # Real native lots exist; the adversarial mapping claims them as a PSM
        # successor of the sold root rather than pretending the root is empty.
        successor=deepcopy(a.state['cohorts'][PAIR+':seed:02'])
        successor.update(id='root-successor',root_cohort_id=cid,origin='PSM_REBUILD')
        if held:
            a.state['cohorts'][PAIR+':seed:02']['lot_ids']=[]
        else:
            successor.update(lot_ids=[],status='SOLD')
        a.state['cohorts'][successor['id']]=successor;a._save()
        expected='ROOT_SUCCESSOR_HELD' if held else 'ROOT_SUCCESSOR_LINEAGE_UNAVAILABLE'
        with pytest.raises(AdmissionBlocked,match=expected):a.request_reentry(cid,now=36000)
        assert a.state['cohorts'][cid]['status']=='SOLD'
        assert a.state['cohorts'][successor['id']]['origin']=='PSM_REBUILD'
