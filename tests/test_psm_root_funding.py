"""Isolated actual native post-handback root cash integration; synthetic only."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
import json
import sqlite3
import pytest
from research.weekly_reversal.owned_spot.model import CompletedBar, ObservedOpen
from research.weekly_reversal.owned_spot.adapter import serial
from research.weekly_reversal.owned_spot import native as N
from research.weekly_reversal import psm_root_cash
from test_next_entry_psm_funding import funded_psm, PAIR


def mark(a,at,price):
    a.observe(CompletedBar(PAIR,at-300,at,D(price),D('1000000'),at),now=at)
    a.refresh_accounting_all(now=at)


def rebuilt(root, *, sale_price='1', assets=('BTC',)):
    a,_,_,_,at=funded_psm(root,sale_price=sale_price,assets=assets)
    try:
        a.sync_profit_cash(now=at)
        a.psm.close_exit(PAIR,now=at)
        at+=300;mark(a,at,'.5')
        a.psm.rebuild_signal(PAIR,decision_ref='synthetic-independent-rebuild-signal',qualified=True,expires_at=at+600,now=at)
        a.psm.request_rebuild(PAIR,min_unit_gain=D(0),now=at)
        at+=300;mark(a,at,'.5');a.psm.observe_open(PAIR,ObservedOpen(D('.5'),at,at))
        a.refresh_accounting_all(now=at);a.sync_profit_cash(now=at)
        a.psm.complete(PAIR,now=at)
        assert a.state['psm'][PAIR]['mode']=='REPAIR'
        return a,at
    except BaseException:a.close();raise


def sell_successor(a,root,at,price='1.2'):
    candidates=[c for c in a.state['cohorts'].values() if c.get('root_cohort_id',c['id'])==root and a._cohort_lots(c)]
    assert len(candidates)==1
    cohort=candidates[0];cid=cohort['id'];at+=300;mark(a,at,price)
    owners=N.economic_exit._group_owners(lots=a._cohort_lots(cohort),governor=a.governor,pair=PAIR)
    a.state['assets'][PAIR]['selected']=cid
    a.state['assets'][PAIR]['exit']={'id':'root-fixture-exit:'+str(at),'cohort':cid,'pair':PAIR,
        'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
    a._save();sale=a.continue_exit(PAIR,now=at)
    while sale:
        at+=300;mark(a,at,price)
        a.observe_open(sale['id'],ObservedOpen(D(price),at,at))
        a.refresh_accounting_all(now=at)
        sale=a.continue_exit(PAIR,now=at)
    a.refresh_accounting_all(now=at)
    return at,cid


def test_actual_archived_successor_profit_has_exact_root_cash(tmp_path):
    a,now=rebuilt(tmp_path/'account')
    try:
        root=PAIR+':seed:01'
        archive=deepcopy(a.state['psm'][PAIR]['archived_episodes'])
        now,cid=sell_successor(a,root,now)
        physical=a.cash();before=a.psm.funding_cash(PAIR)
        a.sync_profit_cash(now=now)
        after=a.psm.funding_cash(PAIR)
        assert a.cash()==physical
        assert a.state['psm'][PAIR]['archived_episodes']==archive
        assert after[root+'|seed']<before[root+'|seed']
        assert {k:v for k,v in after.items() if k!=root+'|seed'}=={k:v for k,v in before.items() if k!=root+'|seed'}
        with a.profit_bridge._paper() as db:
            result=a.profit_bridge.root_source_proof(root,now=now,paper_connection=db)
            print('ROOT_SOURCE',result['source_owner'],result['available_base_cash'])
            assert result['available_base_cash']==after[root+'|seed']
            assert result['ownership_proof']['latest_cohort_ids']==[cid]
            assert a.profit_bridge.validate_root_source_proof(result['ownership_proof'],now=now,paper_connection=db)==result['ownership_proof']
        assert a.profit_bridge.before_new_spending().new_spending_allowed
    finally:a.close()


@pytest.mark.parametrize('mutation',['journal_root','journal_owner','missing_journal','native_fee','foreign_native_owner','source_pool'])
def test_root_profit_projection_rejects_changed_actual_authority(tmp_path,mutation):
    a,now=rebuilt(tmp_path/'account')
    try:
        root=PAIR+':seed:01';now,_=sell_successor(a,root,now);a.sync_profit_cash(now=now)
        with a.profit_bridge._paper() as db:
            journal=dict(db.execute('SELECT * FROM next_entry_psm_root_cash LIMIT 1').fetchone())
            outbox=dict(db.execute('SELECT * FROM next_profit_outbox WHERE trade_id=?',(journal['trade_id'],)).fetchone())
        source=json.loads(outbox['source_payload']);trade=journal['trade_id']
        with sqlite3.connect(a.db_path) as db:
            if mutation in {'journal_root','journal_owner'}:
                payload=json.loads(journal['payload'])
                payload['root_cohort_id' if mutation=='journal_root' else 'funding_owner']=PAIR+':seed:02' if mutation=='journal_root' else 'repair'
                from spectra_core.next_entry_profit_bridge import _json,_digest
                encoded=_json(payload)
                db.execute('UPDATE next_entry_psm_root_cash SET payload=?,payload_digest=? WHERE trade_id=?',(encoded,_digest(encoded),trade))
            elif mutation=='missing_journal':db.execute('DELETE FROM next_entry_psm_root_cash WHERE trade_id=?',(trade,))
            elif mutation=='source_pool':
                a.state['orders'][source['order']['id']]['pool']='seed';a._save()
        if mutation in {'native_fee','foreign_native_owner'}:
            with sqlite3.connect(a.root/'native.sqlite') as db:
                if mutation=='native_fee':db.execute("UPDATE economic_trade_fill_reconciliation SET fee_paid='0' WHERE trade_id=?",(trade,))
                else:db.execute("UPDATE economic_fill_bindings SET account_name='foreign' WHERE order_id=?",(source['order']['id'],))
        with pytest.raises(ValueError):a.profit_bridge.cash_projection()
        with pytest.raises(ValueError):a.profit_bridge.psm_funding_cash_adjustments(PAIR)
    finally:a.close()


class SimulatedRootCrash(BaseException):pass


@pytest.mark.parametrize('point',['before_stage_commit','after_stage_commit','after_allocator_commit','before_ack_commit','after_ack_commit'])
def test_post_handback_cash_stage_crash_and_exact_restart(tmp_path,point):
    a,now=rebuilt(tmp_path/'account')
    root=PAIR+':seed:01';now,_=sell_successor(a,root,now)
    cfg=a.config;membership=a.membership;physical=a.cash();archive=deepcopy(a.state['psm'][PAIR]['archived_episodes'])
    try:
        raw=next(r for r in a._receipts() if a.state['orders'][r['order_id']]['kind']=='COHORT_EXIT'
                 and a.state['orders'][r['order_id']]['pool']=='psm:'+PAIR)
        def crash(name):
            if name==point:raise SimulatedRootCrash(name)
        a.profit_bridge.fault_hook=crash
        with pytest.raises(SimulatedRootCrash):
            a.profit_bridge.stage(trade_id=raw['trade_id'],snapshot=membership,now=now)
            a.profit_bridge.dispatch(trade_id=raw['trade_id'],now=now)
        with a.profit_bridge._paper() as db:
            durable=db.execute('SELECT * FROM next_profit_outbox WHERE trade_id=?',(raw['trade_id'],)).fetchone()
            journal=db.execute('SELECT * FROM next_entry_psm_root_cash WHERE trade_id=?',(raw['trade_id'],)).fetchone()
            cash=db.execute('SELECT * FROM next_profit_cash WHERE trade_id=?',(raw['trade_id'],)).fetchone()
            assert (durable is not None)==(journal is not None)==(cash is not None)==(point!='before_stage_commit')
            frozen=dict(durable) if durable else None
    finally:a.close()
    from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=now,membership=membership,enabled=True) as a:
        a.sync_profit_cash(now=now)
        assert a.cash()==physical and a.state['psm'][PAIR]['archived_episodes']==archive
        assert a.profit_bridge.before_new_spending().new_spending_allowed
        audit=a.allocator.audit();rows=[]
        with a.profit_bridge._paper() as db:
            current=dict(db.execute('SELECT * FROM next_profit_outbox WHERE trade_id=?',(raw['trade_id'],)).fetchone())
            if frozen:assert all(current[k]==frozen[k] for k in ('operation_id','source_payload','source_digest','snapshot_payload','event_payload','payload_digest','staged_at'))
            rows=[dict(r) for r in db.execute('SELECT * FROM next_entry_psm_root_cash')]
        a.sync_profit_cash(now=now)
        with a.profit_bridge._paper() as db:assert rows==[dict(r) for r in db.execute('SELECT * FROM next_entry_psm_root_cash')]
        assert a.allocator.audit()==audit


def test_mixed_seed_repair_ordinary_successor_cash_never_nets(tmp_path):
    a,now=rebuilt(tmp_path/'account')
    try:
        root=next(key.rsplit('|',1)[0] for key in a.psm.funding_cash(PAIR) if key.endswith('|repair'))
        now,_=sell_successor(a,root,now)
        before=a.psm.funding_cash(PAIR);physical=a.cash();a.sync_profit_cash(now=now)
        after=a.psm.funding_cash(PAIR)
        assert after[root+'|seed']<before[root+'|seed'] and after[root+'|repair']<before[root+'|repair']
        assert a.cash()==physical
        with a.profit_bridge._paper() as db:
            result=a.profit_bridge.root_source_proof(root,now=now,paper_connection=db)
            assert result['available_base_cash']==after[root+'|seed']
            assert result['available_base_cash']<after[root+'|seed']+after[root+'|repair']
            assert result['available_base_cash']<a.config.seed_ticket
    finally:a.close()
