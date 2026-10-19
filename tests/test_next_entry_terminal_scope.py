"""Exact durable closure membership when later authority shares its timestamp."""
from copy import deepcopy
import json
from shutil import copytree
import pytest
from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.model import ObservedOpen
from spectra_core.next_entry_profit_bridge import _json,_digest
from test_weekly_owned_adapter import config,seed
from test_weekly_compounding_adapter import profitable_sale,reentry_boundary,members,D
from test_psm_root_funding import mark,PAIR


@pytest.fixture
def closed_funding(tmp_path):
    root=tmp_path/'account';cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(root,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        order=a.request_reentry(PAIR+':seed:01',now=36000)
        mark(a,36300,'11');a.observe_open(order['id'],ObservedOpen(D('11'),36300,36300))
        a.refresh_accounting_all(now=36300);a.sync_profit_cash(now=36300)
        mark(a,36600,'11');a.sync_profit_cash(now=36600)
        with a.profit_bridge._paper() as db:
            row=db.execute('SELECT * FROM next_entry_terminal_health_scope').fetchone()
            assert row is not None
        entry=order['entry_id']
    return root,cfg,entry


def call_scope(a,entry_id):
    bridge=a.profit_bridge
    with bridge._paper() as db:
        entry,proof=bridge._funding_row(db,entry_id)
        context=bridge._context(db,entry,proof)
        return bridge._audit_terminal_authority(db,bridge._state(db),context,bridge._terminal_payload(entry))


def test_current_unbound_prepared_order_does_not_rewrite_old_scope(tmp_path,closed_funding):
    root,cfg,entry=closed_funding
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=36600,membership=members(cfg),enabled=True) as a:
        existing=a.state['orders'][next(o['id'] for o in a.state['orders'].values() if o.get('entry_id')==entry)]
        candidate=deepcopy(existing);candidate.update(id='later-candidate',proposal='later-proposal',executor='later-executor',
            status='ENTRY_PREPARED',terminal=None,created_at=36600,decision_at=36600)
        a.state['orders'][candidate['id']]=candidate;a._save()
        call_scope(a,entry)


@pytest.mark.parametrize('mutation',['missing','digest','rehashed_count','rehashed_boundary','original_audit','native_binding','native_fill','orphan'])
def test_frozen_closure_scope_fails_closed(tmp_path,closed_funding,mutation):
    root,cfg,entry=closed_funding
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=36600,membership=members(cfg),enabled=True) as a:
        with a.profit_bridge._paper() as db:
            row=dict(db.execute('SELECT * FROM next_entry_terminal_health_scope WHERE entry_id=?',(entry,)).fetchone())
            scope=json.loads(row['payload'])
            if mutation=='missing':db.execute('DELETE FROM next_entry_terminal_health_scope WHERE entry_id=?',(entry,))
            elif mutation=='digest':db.execute("UPDATE next_entry_terminal_health_scope SET payload_digest='bad' WHERE entry_id=?",(entry,))
            elif mutation in {'rehashed_count','rehashed_boundary'}:
                if mutation=='rehashed_count':scope['bindings'].pop()
                else:scope['audit_max_seq']-=1
                encoded=_json(scope);db.execute('UPDATE next_entry_terminal_health_scope SET payload=?,payload_digest=? WHERE entry_id=?',(encoded,_digest(encoded),entry))
            elif mutation=='original_audit':db.execute("UPDATE audit_events SET payload='{}' WHERE seq=?",(scope['audit_max_seq'],))
            elif mutation=='orphan':db.execute('INSERT INTO next_entry_terminal_health_scope VALUES (?,?,?)',('orphan',row['payload'],row['payload_digest']))
        if mutation in {'native_binding','native_fill'}:
            import sqlite3
            with sqlite3.connect(a.root/'native.sqlite') as native:
                if mutation=='native_binding':native.execute("UPDATE economic_fill_bindings SET account_name='foreign' WHERE order_id=?",(scope['bindings'][0]['order_id'],))
                else:native.execute("DELETE FROM economic_trade_fill_reconciliation WHERE trade_id=?",(scope['fills'][0]['trade_id'],))
        with pytest.raises(ValueError):a.profit_bridge.cash_projection()


@pytest.mark.parametrize('remove_anchor',[False,True])
def test_same_clock_real_native_sale_binding_needs_later_audit(tmp_path,closed_funding,remove_anchor):
    from dataclasses import asdict
    from research.weekly_reversal.owned_spot import native as N
    from research.weekly_reversal.owned_spot.adapter import serial
    root,cfg,entry=closed_funding
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=36600,membership=members(cfg),enabled=True) as a:
        cid=PAIR+':seed:01';cohort=a.state['cohorts'][cid]
        owners=N.economic_exit._group_owners(lots=a._cohort_lots(cohort),governor=a.governor,pair=PAIR)
        a.state['assets'][PAIR]['selected']=cid
        a.state['assets'][PAIR]['exit']={'id':'same-clock-native-sale','cohort':cid,'pair':PAIR,
            'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
        a._save();sale=a.continue_exit(PAIR,now=36600)
        assert sale is not None
        call_scope(a,entry)
        if remove_anchor:
            with a.profit_bridge._paper() as db:
                rows=[dict(row) for row in db.execute("SELECT * FROM audit_events WHERE kind='cohort_sell_intent'")
                      if json.loads(row['payload']).get('id')==sale['id']]
                assert len(rows)==1
                db.execute('DELETE FROM audit_events WHERE seq=?',(rows[0]['seq'],))
            with pytest.raises(ValueError,match='later causal admission audit'):call_scope(a,entry)
