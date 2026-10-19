"""Synthetic cross-store saga tests: real paper receipts and native SQLite evidence."""
from copy import deepcopy
from decimal import Decimal as D
import json
import sqlite3

import pytest

from research.weekly_reversal.owned_spot.model import ObservedOpen
from research.weekly_reversal.entry_admission import NativeEntryAdmission
from spectra_core.next_entry_funding_bridge import NativeEntryFundingBridge
from spectra_core.next_entry_profit_bridge import ESCROW_POOL, ProfitBridgeBlocked, _json, _digest
from test_weekly_entry_admission import setup_authority, NOW, PAIR


def restart(a, allocator):
    helper = NativeEntryAdmission(a, allocator=allocator, snapshot_id='weekly-members')
    return NativeEntryFundingBridge(admission=helper, funding_enabled=True,
        paper_db=a.root/'paper.sqlite', native_db=a.root/'native.sqlite', allocator=allocator,
        account_id=a.config.account, connector=a.config.connector, owner_id=a.config.owner, enabled=True)


def fund(a, bridge, order):
    proof = bridge.prepare_entry(order_id=order['id'], now=NOW)
    ready = bridge.fund_entry(entry_id=order['entry_id'], now=NOW)
    assert ready['funding_ready'] and not ready['order_permission']
    return proof


def authorize(a, order):
    # Stand-in for coordinator's actual checked OPEN transition only. All
    # governance, execution provenance and subsequent fills are real native calls.
    if order['kind'] == 'REENTRY':
        a.ledger.open_position(position_id=order['position'], pair=PAIR, created_at=NOW)
        a.state['assets'][PAIR]['positions'].append(order['position'])
        cohort=a.state['cohorts'][order['cohort']]
        cohort.update(generation=order['cohort_generation'], status='OWNED', lot_ids=[], rung=None)
    else:
        a.state['cohorts'][order['cohort']]['rung'] = dict(level=1, orders=[order['id']],
            requested_target=order['target'], quantity=order['quantity'], reference=order['reference'], status='OPEN')
    a._authorize(order)


def open_fill(a, order, *, fraction='1'):
    trade = a.observe_open(order['id'], ObservedOpen(D(order['decision_reference']), NOW+300, NOW+300), fill_fraction=D(fraction))
    refresh_all_positions(a, NOW+300)
    return trade


def refresh_all_positions(a, now):
    a.advance(now)
    for asset in a.state['assets'].values():
        for position in asset['positions']:
            proposals=list(dict.fromkeys(o['proposal'] for o in a.state['orders'].values()
                if o['position']==position and o['status']!='REJECTED'))
            reports=[a._sync_proposal(p,position) for p in proposals]
            a.health.record_reports(position_id=position,reports=reports,now=now)
    a._save()


def refresh_after_commit(a):
    refresh_all_positions(a, NOW+301)


def rows(root, table):
    with sqlite3.connect(root/'paper.sqlite') as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM '+table)]


@pytest.mark.parametrize('repair',[False,True])
def test_balanced_fund_actual_fill_and_exact_unused_return(tmp_path,repair):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True,repair=repair)
    bridge=helper.cash_bridge
    try:
        original=bridge.cash_projection()
        fund(a,bridge,order)
        funded=bridge.cash_projection()
        assert funded.cash_pools[order['pool']]==D(order['target'])
        assert funded.cash_pools[order['source_pool']]==original.cash_pools[order['source_pool']]-D(order['base_budget'])
        assert funded.profit_escrow==original.profit_escrow-D(order['profit_budget'])
        assert funded.total_physical_cash==original.total_physical_cash
        assert not funded.new_spending_allowed
        authorize(a,order)
        trade=open_fill(a,order)
        result=bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
        assert result['status']=='AWAITING_NEWER_NATIVE_CLOSURE'
        assert not bridge.cash_projection().new_spending_allowed
        refresh_after_commit(a)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+301)['status']=='CLOSED'
        closed=bridge.before_new_spending()
        debit=sum(D(f['amount'])*D(f['price'])+D(f['fee_paid']) for f in a._receipts(order['id']))
        actual_profit=max(D(0),debit-D(order['base_budget']))
        assert closed.cash_pools[order['pool']]==0
        assert closed.profit_escrow==original.profit_escrow-actual_profit
        assert D(allocator.audit()['committed_profit_cash'])==actual_profit
        assert closed.cash_pools['protected']==500
        assert rows(tmp_path,'next_entry_observed_receipts')[0]['trade_id']==trade
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+301)['status']=='CLOSED'
    finally:
        a.close()


def test_zero_fill_cancel_returns_exact_sources(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        original=bridge.cash_projection()
        fund(a,bridge,order);authorize(a,order)
        a.reconcile_terminal(order['id'],'CANCELED',now=NOW+1)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1)['status']=='CLOSED'
        closed=bridge.before_new_spending()
        assert closed.cash_pools==original.cash_pools | {order['pool']:D(0)}
        assert D(allocator.audit()['committed_profit_cash'])==0
    finally:a.close()


def test_base_only_partial_cancel_releases_all_reserved_profit(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        original=bridge.cash_projection();fund(a,bridge,order);authorize(a,order)
        open_fill(a,order,fraction='.5')
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)['status']=='FUNDED'
        assert not [r for r in rows(tmp_path,'next_entry_funding_outbox') if r['action']=='COMMIT']
        a.reconcile_terminal(order['id'],'CANCELED',now=NOW+300)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)['status']=='CLOSED'
        closed=bridge.before_new_spending()
        assert closed.profit_escrow==original.profit_escrow
        assert D(allocator.audit()['committed_profit_cash'])==0
    finally:a.close()


class Crash(RuntimeError):
    pass


def fail_once(bridge, point):
    def fault(where):
        if where==point:
            bridge.fault_hook=None
            raise Crash(point)
    bridge.fault_hook=fault


@pytest.mark.parametrize('point',[
    'before_entry_prepare_commit','after_entry_prepare_commit',
    'after_allocator_reserve_commit','before_reserve_ack_commit','after_reserve_ack_commit'])
def test_crash_at_prepare_reserve_and_ack_replays_same_ids(tmp_path,point):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fail_once(bridge,point)
        with pytest.raises(Crash):
            fund(a,bridge,order)
        before=rows(tmp_path,'next_entry_funding_outbox')
        bridge=restart(a,allocator)
        proof=bridge.prepare_entry(order_id=order['id'],now=NOW)
        assert bridge.fund_entry(entry_id=order['entry_id'],now=NOW)['funding_ready']
        after=rows(tmp_path,'next_entry_funding_outbox')
        assert len(after)==1 and len(rows(tmp_path,'next_entry_cash_transfers'))==1
        if before:
            assert before[0]['operation_id']==after[0]['operation_id']
            assert before[0]['payload']==after[0]['payload']
        assert len(allocator.export_research_state()['reservations'])==1
        assert bridge.cash_projection().cash_pools[order['pool']]==D(order['target'])
    finally:a.close()


@pytest.mark.parametrize('point',[
    'before_debit_stage_commit','after_debit_stage_commit',
    'after_allocator_commit_commit','before_commit_ack_commit','after_commit_ack_commit'])
def test_crash_at_actual_debit_and_commit_ack_replays_exact_payload(tmp_path,point):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order);open_fill(a,order)
        fail_once(bridge,point)
        with pytest.raises(Crash):bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
        old={r['operation_id']:r['payload'] for r in rows(tmp_path,'next_entry_funding_outbox')}
        bridge=restart(a,allocator)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)['status']=='AWAITING_NEWER_NATIVE_CLOSURE'
        refresh_after_commit(a)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+301)['status']=='CLOSED'
        after={r['operation_id']:r['payload'] for r in rows(tmp_path,'next_entry_funding_outbox')}
        assert all(after[k]==v for k,v in old.items())
        assert len(allocator.export_research_state()['commits'])==1
        assert bridge.before_new_spending().new_spending_allowed
    finally:a.close()


@pytest.mark.parametrize('point',[
    'before_return_stage_commit','after_return_stage_commit',
    'after_allocator_release_commit','before_release_ack_commit','after_release_ack_commit'])
def test_crash_at_terminal_return_release_and_ack_preserves_cash(tmp_path,point):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order);open_fill(a,order)
        bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
        refresh_after_commit(a)
        fail_once(bridge,point)
        with pytest.raises(Crash):bridge.sync_entry(entry_id=order['entry_id'],now=NOW+301)
        old={r['operation_id']:r['payload'] for r in rows(tmp_path,'next_entry_funding_outbox')}
        bridge=restart(a,allocator)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+301)['status']=='CLOSED'
        after={r['operation_id']:r['payload'] for r in rows(tmp_path,'next_entry_funding_outbox')}
        assert all(after[k]==v for k,v in old.items())
        assert len(rows(tmp_path,'next_entry_cash_transfers'))==2
        assert bridge.before_new_spending().cash_pools[order['pool']]==0
        assert D(allocator.audit()['reserved_entitlements'])==0
    finally:a.close()


@pytest.mark.parametrize('table,column,mutate',[
    ('next_entry_cash_transfers','payload',lambda p:p.update(base='0')),
    ('next_entry_execution_contexts','payload',lambda p:p.update(reserved_at=NOW-100)),
    ('next_entry_observed_receipts','payload',lambda p:p.update(fee_inclusive_debit='0')),
    ('next_entry_funding_outbox','payload',lambda p:p['proof'].update(base_cash_used='0')),
])
def test_rehashed_native_journal_forgery_fails_closed(tmp_path,table,column,mutate):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order);open_fill(a,order)
        bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
        with sqlite3.connect(tmp_path/'paper.sqlite') as db:
            condition=" WHERE action='COMMIT'" if table=='next_entry_funding_outbox' else ''
            row=db.execute('SELECT rowid,'+column+' FROM '+table+condition+' LIMIT 1').fetchone()
            payload=json.loads(row[1]);mutate(payload);payload=_json(payload)
            db.execute('UPDATE '+table+' SET '+column+'=?,payload_digest=? WHERE rowid=?',(payload,_digest(payload),row[0]))
        with pytest.raises(ValueError):bridge.cash_projection()
    finally:a.close()


@pytest.fixture
def psm_source(tmp_path,monkeypatch,request):
    # Initialize a new V2 authority from genesis, never migrate a live V1 DB.
    import spectra_core.next_entry_profit_bridge as original
    from test_psm_profit_partition import native_source
    monkeypatch.setattr(original,'NativeSettledProfitBridge',NativeEntryFundingBridge)
    state,source,at=native_source.__wrapped__(tmp_path,monkeypatch,request)
    with sqlite3.connect(tmp_path/'paper.sqlite') as db:
        db.execute('UPDATE state SET value=? WHERE id=1',(_json(state),))
    from test_next_entry_profit_bridge import allocator,ACCOUNT,CONNECTOR,OWNER
    bridge=NativeEntryFundingBridge(paper_db=tmp_path/'paper.sqlite',native_db=tmp_path/'native.sqlite',
        allocator=allocator(tmp_path),account_id=ACCOUNT,connector=CONNECTOR,owner_id=OWNER,enabled=True)
    return bridge,state,source,at


def test_psm_profit_state_and_cash_outbox_commit_atomically(psm_source,tmp_path):
    from test_next_entry_profit_bridge import members
    from test_psm_profit_partition import position
    bridge,state,source,at=psm_source
    before=deepcopy(state)
    fail_once(bridge,'before_stage_commit')
    with pytest.raises(Crash):bridge.stage(trade_id='sell-fill',snapshot=members(),now=at)
    with bridge._paper() as db:assert bridge._state(db)==before
    assert rows(tmp_path,'next_profit_outbox')==[]
    assert bridge.allocator.audit()['event_count']==0
    staged=bridge.stage(trade_id='sell-fill',snapshot=members(),now=at)
    with bridge._paper() as db:updated=bridge._state(db)
    assert position(updated)['cash']==202
    assert position(updated)['cumulative_distributions']==36
    assert bridge.cash_projection().profit_escrow==36
    assert bridge.cash_projection().cash_pools['psm:BTC-USD']==202
    bridge.dispatch(trade_id='sell-fill',now=at)
    assert bridge.before_new_spending().new_spending_allowed
    assert bridge.stage(trade_id='sell-fill',snapshot=members(),now=at)['source_payload']==staged['source_payload']
    with bridge._paper() as db:assert bridge._state(db)==updated


@pytest.mark.parametrize('psm_source',['90'],indirect=True)
def test_psm_loss_stages_exact_native_source_without_distribution(psm_source,tmp_path):
    from test_next_entry_profit_bridge import members
    bridge,state,source,at=psm_source
    staged=bridge.stage(trade_id='sell-fill',snapshot=members(),now=at)
    assert staged['status']=='INELIGIBLE'
    with bridge._paper() as db:assert bridge._state(db)==state
    assert bridge.cash_projection().profit_escrow==0
    assert bridge.before_new_spending().new_spending_allowed


def test_expired_accepted_reserve_reconciles_cash_but_never_execution(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fail_once(bridge,'after_allocator_reserve_commit')
        with pytest.raises(Crash):fund(a,bridge,order)
        original=rows(tmp_path,'next_entry_funding_outbox')[0]['payload']
        refresh_all_positions(a,NOW+31)
        bridge=restart(a,allocator)
        with pytest.raises(ProfitBridgeBlocked,match='expired'):
            bridge.fund_entry(entry_id=order['entry_id'],now=NOW+31)
        assert rows(tmp_path,'next_entry_funding')[0]['status']=='FUNDED'
        assert rows(tmp_path,'next_entry_funding_outbox')[0]['payload']==original
        assert not bridge.cash_projection().new_spending_allowed
        assert a.state['orders'][order['id']]['status']=='FUNDING_PREPARED'
        assert D(allocator.audit()['reserved_entitlements'])==D(order['profit_budget'])
        with pytest.raises(ProfitBridgeBlocked,match='outbox'):
            bridge.before_new_spending()
    finally:a.close()


@pytest.mark.parametrize('action',['commit','release'])
def test_accepted_commit_or_release_replay_after_original_proof_expiry(tmp_path,action):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order);open_fill(a,order)
        if action=='release':
            bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
            refresh_after_commit(a)
        fail_once(bridge,'after_allocator_'+action+'_commit')
        with pytest.raises(Crash):bridge.sync_entry(entry_id=order['entry_id'],now=NOW+(301 if action=='release' else 300))
        frozen={r['operation_id']:r['payload'] for r in rows(tmp_path,'next_entry_funding_outbox')}
        refresh_all_positions(a,NOW+1000)
        bridge=restart(a,allocator)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1000)['status']=='CLOSED'
        current={r['operation_id']:r['payload'] for r in rows(tmp_path,'next_entry_funding_outbox')}
        assert all(current[key]==value for key,value in frozen.items())
        assert bridge.before_new_spending().new_spending_allowed
    finally:a.close()


def test_unaccepted_stale_commit_cannot_refresh_attestation(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order);open_fill(a,order)
        fail_once(bridge,'after_debit_stage_commit')
        with pytest.raises(Crash):bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
        frozen=rows(tmp_path,'next_entry_funding_outbox')
        refresh_all_positions(a,NOW+1000)
        with pytest.raises(ValueError,match='[Ss]tale|[Ee]xpired'):
            bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1000)
        assert rows(tmp_path,'next_entry_funding_outbox')==frozen
        assert allocator.export_research_state()['commits']=={}
        assert not bridge.cash_projection().new_spending_allowed
    finally:a.close()


def test_forged_release_ack_cannot_hide_unaccepted_allocator_outcome(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order)
        a.reconcile_terminal(order['id'],'CANCELED',now=NOW+1)
        fail_once(bridge,'after_return_stage_commit')
        with pytest.raises(Crash):bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1)
        with sqlite3.connect(tmp_path/'paper.sqlite') as db:
            db.execute("UPDATE next_entry_funding SET status='CLOSED'")
            db.execute("UPDATE next_entry_funding_outbox SET status='ACKNOWLEDGED',acknowledgement='{}' WHERE action='RELEASE'")
        with pytest.raises(ProfitBridgeBlocked,match='missing in allocator'):
            bridge.before_new_spending()
    finally:a.close()


def test_rehashed_admission_cannot_change_allocator_anchored_evidence(tmp_path):
    from research.weekly_reversal.entry_admission import _hash
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order)
        with sqlite3.connect(tmp_path/'paper.sqlite') as db:
            proof=json.loads(db.execute('SELECT proof FROM next_entry_funding').fetchone()[0])
            proof['evidence']['signal']={'label':'FORGED'}
            proof['evidence']['cash_receipts']=[];proof['evidence']['source_events']={}
            del proof['proof_sha256'];proof['proof_sha256']=_hash(proof)
            encoded=_json(proof)
            db.execute('UPDATE next_entry_funding SET proof=?,proof_digest=?',(encoded,_digest(encoded)))
            row=db.execute('SELECT transfer_id,payload FROM next_entry_cash_transfers').fetchone()
            payload=json.loads(row[1]);payload['admission_sha256']=proof['proof_sha256'];payload=_json(payload)
            db.execute('UPDATE next_entry_cash_transfers SET payload=?,payload_digest=? WHERE transfer_id=?',(payload,_digest(payload),row[0]))
        with pytest.raises(ValueError,match='PROOF_IDENTITY'):
            bridge.before_new_spending()
    finally:a.close()


def _weekly_bar(a, now, price='11'):
    from research.weekly_reversal.owned_spot.model import CompletedBar
    a.observe(CompletedBar(PAIR,now-300,now,D(price),D('1000000'),now),now=now)
    a.refresh_accounting_all(now=now)


def _weekly_sell(a,cid,now,price='11'):
    from dataclasses import asdict
    from research.weekly_reversal.owned_spot.adapter import serial
    from research.weekly_reversal.owned_spot import native as N
    _weekly_bar(a,now,price)
    cohort=a.state['cohorts'][cid]
    owners=N.economic_exit._group_owners(lots=a._cohort_lots(cohort),governor=a.governor,pair=PAIR)
    a.state['assets'][PAIR]['selected']=cid
    a.state['assets'][PAIR]['exit']={'id':'fixture-exit:'+str(now),'cohort':cid,'pair':PAIR,
        'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
    a._save()
    sale=a.continue_exit(PAIR,now=now)
    _weekly_bar(a,now+300,price)
    trade=a.observe_open(sale['id'],ObservedOpen(D(price),now+300,now+300))
    a.continue_exit(PAIR,now=now+300)
    a.refresh_accounting_all(now=now+300)
    a.sync_profit_cash(now=now+300)
    return sale,trade


def test_later_loss_and_base_only_reentry_retain_exact_root_pool(tmp_path):
    from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
    from test_weekly_owned_adapter import config,seed
    from test_weekly_compounding_adapter import members,profitable_sale,reentry_boundary
    cfg=config(('BTC',));cid=PAIR+':seed:01'
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        first=a.request_reentry(cid,now=NOW)
        _weekly_bar(a,NOW+300)
        a.observe_open(first['id'],ObservedOpen(D('11'),NOW+300,NOW+300))
        a.refresh_accounting_all(now=NOW+300);a.sync_profit_cash(now=NOW+300)
        _weekly_bar(a,NOW+600);a.sync_profit_cash(now=NOW+600)
        sale,trade=_weekly_sell(a,cid,39600)
        assert sale['pool']==first['pool']
        assert a.available_profit(PAIR)==0
        assert a.available_profit(PAIR,raw=True)>0  # exact subquantum entitlement retained
        before_reservations=len(a.allocator.export_research_state()['reservations'])
        _weekly_bar(a,46800)
        a.state['next_entry_completed_hours']={PAIR:[
            dict(pair=PAIR,opened_at=39600,open=10.,high=10.,low=10.,close=10.,volume=100.,trades=10),
            dict(pair=PAIR,opened_at=43200,open=10.,high=11.,low=10.,close=11.,volume=100.,trades=10)]}
        a._save()
        base=a.request_reentry(cid,now=46800)
        assert base['pool']==base['source_pool']==first['pool']
        assert D(base['profit_budget'])==0 and D(base['target'])==cfg.seed_ticket
        assert len(a.allocator.export_research_state()['reservations'])==before_reservations
        _weekly_bar(a,47100)
        a.observe_open(base['id'],ObservedOpen(D('11'),47100,47100))
        a.refresh_accounting_all(now=47100)
        sale,trade=_weekly_sell(a,cid,50400)
        with a.profit_bridge._paper() as db:
            source=json.loads(db.execute('SELECT source_payload FROM next_profit_outbox WHERE trade_id=?',(trade,)).fetchone()[0])
        transition=source['acquisitions'][0]['pool_transition']
        assert transition['kind']=='RETAINED_ROOT_BASE_ENTRY'
        assert transition['root_entry_id']==first['entry_id']
        assert transition['funding_owner']=='seed' and transition['pair']==PAIR
        assert a.profit_bridge.before_new_spending().new_spending_allowed


def test_two_partial_native_fills_commit_incremental_exact_base_first(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order)
        first=open_fill(a,order,fraction='.95')
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)['status']=='FUNDED'
        initial=D(allocator.audit()['committed_profit_cash'])
        assert 0<initial<D(order['profit_budget'])
        second=open_fill(a,order)
        assert second!=first
        bridge.sync_entry(entry_id=order['entry_id'],now=NOW+300)
        commits=[json.loads(r['payload']) for r in rows(tmp_path,'next_entry_funding_outbox') if r['action']=='COMMIT']
        assert len(commits)==2
        assert sum(D(c['proof']['base_cash_used']) for c in commits)==D(order['base_budget'])
        assert set(commits[0]['proof']['settlement_ids']).isdisjoint(commits[1]['proof']['settlement_ids'])
        refresh_after_commit(a)
        assert bridge.sync_entry(entry_id=order['entry_id'],now=NOW+301)['status']=='CLOSED'
        assert bridge.before_new_spending().cash_pools[order['pool']]==0
    finally:a.close()


def test_zero_fill_closed_native_binding_change_preserves_forensic_bytes(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order)
        a.reconcile_terminal(order['id'],'CANCELED',now=NOW+1)
        bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1)
        frozen=rows(tmp_path,'next_entry_funding')
        with sqlite3.connect(tmp_path/'native.sqlite') as db:
            db.execute('UPDATE economic_fill_bindings SET created_at=? WHERE order_id=?',(NOW+999,order['id']))
        with pytest.raises(ValueError):bridge.before_new_spending()
        assert rows(tmp_path,'next_entry_funding')==frozen
    finally:a.close()


def test_rehashed_terminal_health_cannot_authorize_first_release(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    bridge=helper.cash_bridge
    try:
        fund(a,bridge,order);authorize(a,order)
        a.reconcile_terminal(order['id'],'CANCELED',now=NOW+1)
        fail_once(bridge,'after_return_stage_commit')
        with pytest.raises(Crash):bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1)
        with sqlite3.connect(tmp_path/'paper.sqlite') as db:
            terminal=json.loads(db.execute('SELECT terminal_payload FROM next_entry_funding').fetchone()[0])
            closure=json.loads(terminal['closure_source_payload']);closure['native_health']['recorded_state']='CONFLICT'
            terminal['closure_source_payload']=_json(closure);terminal['closure_source_digest']=_digest(_json(closure))
            db.execute('UPDATE next_entry_funding SET terminal_payload=?',(_json(terminal),))
            row=db.execute("SELECT transfer_id,payload FROM next_entry_cash_transfers WHERE payload LIKE '%RETURN%'").fetchone()
            transfer=json.loads(row[1]);transfer['source_evidence']=terminal;payload=_json(transfer)
            db.execute('UPDATE next_entry_cash_transfers SET payload=?,payload_digest=? WHERE transfer_id=?',(payload,_digest(payload),row[0]))
            row=db.execute("SELECT operation_id,payload FROM next_entry_funding_outbox WHERE action='RELEASE'").fetchone()
            release=json.loads(row[1]);release.update(terminal)
            release['proof']['proof_id']=bridge._key('entry-release-proof',order['entry_id'],terminal['closure_source_digest'])
            payload=_json(release)
            db.execute('UPDATE next_entry_funding_outbox SET payload=?,payload_digest=? WHERE operation_id=?',(payload,_digest(payload),row[0]))
        with pytest.raises(ValueError,match='native health'):
            bridge.sync_entry(entry_id=order['entry_id'],now=NOW+1)
        assert next(iter(allocator.export_research_state()['reservations'].values()))['status']=='RESERVED'
        assert rows(tmp_path,'next_entry_funding')[0]['status']=='CLOSING'
    finally:a.close()


def test_disabled_v2_does_not_create_paper_db(tmp_path):
    from test_next_entry_profit_bridge import allocator,ACCOUNT,CONNECTOR,OWNER
    bridge=NativeEntryFundingBridge(paper_db=tmp_path/'paper.sqlite',native_db=tmp_path/'native.sqlite',
        allocator=allocator(tmp_path),account_id=ACCOUNT,connector=CONNECTOR,owner_id=OWNER)
    assert not (tmp_path/'paper.sqlite').exists()
    with pytest.raises(ProfitBridgeBlocked,match='disabled'):
        bridge.prepare_entry(order_id='anything',now=NOW)


def test_arbitrary_admission_subclass_is_not_authority(tmp_path):
    a,allocator,helper,order=setup_authority(tmp_path,funding=True)
    class Unreviewed(NativeEntryAdmission):
        def prove(self,*args,**kwargs):return {'verified':True}
    try:
        invalid=Unreviewed(a,allocator=allocator,snapshot_id='weekly-members')
        with pytest.raises(ProfitBridgeBlocked,match='Pinned native admission'):
            NativeEntryFundingBridge(admission=invalid,funding_enabled=True,paper_db=a.db_path,
                native_db=a.root/'native.sqlite',allocator=allocator,account_id=a.config.account,
                connector=a.config.connector,owner_id=a.config.owner,enabled=True)
    finally:a.close()
