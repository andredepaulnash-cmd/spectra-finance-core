"""Synthetic integration of native funded/retained acquisition custody into PSM.

The independent failed reproducer remains unchanged outside this test module.
No historical data or external orders are used.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
import hashlib
import json
import sqlite3

import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.model import CompletedBar, ObservedOpen, CapitalBlocked
from research.weekly_reversal.owned_spot.adapter import serial
from research.weekly_reversal.owned_spot import native as N
from spectra_core.next_entry_profit_bridge import ProfitBridgeBlocked, _json, _digest
from test_weekly_owned_adapter import config, seed
from test_weekly_compounding_adapter import members, profitable_sale, reentry_boundary
from test_next_entry_funding_bridge import _weekly_bar, _weekly_sell

PAIR='BTC-USD'


def account_profitable_sale(a):
    """Refresh every actual account position at the terminal decision."""
    assert a.seed_complete()
    cid=PAIR+':seed:01'
    a.observe(CompletedBar(PAIR,6900,7200,D('12'),D('1000000'),7200),now=7200)
    a.refresh_accounting_all(now=7200)
    owners=N.economic_exit._group_owners(lots=a._cohort_lots(a.state['cohorts'][cid]),governor=a.governor,pair=PAIR)
    a.state['assets'][PAIR]['selected']=cid
    a.state['assets'][PAIR]['exit']={'id':'fixture-exit','cohort':cid,'pair':PAIR,
        'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
    a._save();order=a.continue_exit(PAIR,now=7200)
    a.observe_open(order['id'],ObservedOpen(D('12'),7500,7500))
    a.refresh_accounting_all(now=7500)
    a.continue_exit(PAIR,now=7500)


def funded_psm(root, *, sale_price='1.2', retained=False, assets=('BTC',)):
    cfg=config(assets)
    a=WeeklyCompoundingAdapter(root,cfg,now=1000,membership=members(cfg),enabled=True)
    def mark(at,price):
        # Other account assets get fresh real native health without inventing marks.
        a.observe(CompletedBar(PAIR,at-300,at,D(price),D('1000000'),at),now=at)
        a.refresh_accounting_all(now=at)
    try:
        seed(a);account_profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        entry=a.request_reentry(PAIR+':seed:01',now=36000)
        mark(36300,'11');a.observe_open(entry['id'],ObservedOpen(D('11'),36300,36300))
        a.refresh_accounting_all(now=36300);a.sync_profit_cash(now=36300)
        mark(36600,'11');a.sync_profit_cash(now=36600)
        acquired=entry
        at=36900
        if retained:
            _weekly_sell(a,PAIR+':seed:01',39600)
            assert a.available_profit(PAIR)==0
            mark(46800,'11')
            a.state['next_entry_completed_hours']={PAIR:[
                dict(pair=PAIR,opened_at=39600,open=10.,high=10.,low=10.,close=10.,volume=100.,trades=10),
                dict(pair=PAIR,opened_at=43200,open=10.,high=11.,low=10.,close=11.,volume=100.,trades=10)]}
            a._save();acquired=a.request_reentry(PAIR+':seed:01',now=46800)
            assert acquired['pool']==entry['pool'] and D(acquired['profit_budget'])==0
            mark(47100,'11');a.observe_open(acquired['id'],ObservedOpen(D('11'),47100,47100))
            a.refresh_accounting_all(now=47100);a.sync_profit_cash(now=47100)
            at=47400
        repairs=[];blocked=False
        for rung in range(1,8):
            mark(at,'1')
            try:order=a.request_repair(PAIR,rung=rung,now=at)
            except CapitalBlocked:
                blocked=True;break
            repairs.append(order)
            mark(at+300,'1');a.observe_open(order['id'],ObservedOpen(D('1'),at+300,at+300))
            a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300)
            mark(at+600,'1');a.sync_profit_cash(now=at+600)
            at+=900
        assert blocked and len(repairs)==4
        assert any(o['pool'].startswith('next_entry:') and o['funding_owner']=='repair' for o in repairs)
        a.psm.start(PAIR,decision_ref='synthetic-due-capital-block',falling_knife=True,
            repair_eligible=False,policy_ref='synthetic-only',now=at)
        a.psm.request_sell(PAIR,now=at)
        mark(at+300,sale_price)
        a.psm.observe_open(PAIR,ObservedOpen(D(sale_price),at+300,at+300))
        a.refresh_accounting_all(now=at+300)
        a._save()
        return a,entry,acquired,repairs,at+300
    except BaseException:
        a.close();raise


def psm_receipts(a):
    return [r for r in a._receipts() if a.state['orders'][r['order_id']]['kind']=='PSM_EXIT']


def dedicated_source(a, funding='seed'):
    for raw in psm_receipts(a):
        sale=a.state['orders'][raw['order_id']]
        acquired=next(o for o in a.state['orders'].values() if o['side']=='BUY' and o['proposal']==sale['proposal'])
        if acquired['pool'].startswith('next_entry:') and acquired['funding_owner']==funding:
            return raw,sale,acquired
    raise AssertionError('Missing actual dedicated acquisition')


def persisted_rows(a,table):
    with sqlite3.connect(a.db_path) as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM '+table)]


@pytest.mark.parametrize('retained,assets',[(False,('BTC',)),(True,('BTC',)),(False,('BTC','ETH'))])
def test_actual_psm_partition_from_funded_and_retained_acquisitions(tmp_path,retained,assets):
    a,entry,acquired,repairs,now=funded_psm(tmp_path/'account',retained=retained,assets=assets)
    cfg=a.config;snapshot=a.membership
    try:
        raw=psm_receipts(a)
        assert len(raw)==8
        before=a.profit_bridge.cash_projection();before_cash=a.cash()
        eth_before={l.lot_id:l.base_remaining for l in a._lots('ETH-USD')} if 'ETH' in assets else {}
        with a.profit_bridge._native() as db:
            profits={r['trade_id']:D(db.execute('SELECT realized_pnl_quote FROM economic_position_events WHERE event_id=?',
                ('trade-fill:'+r['trade_id'],)).fetchone()[0]) for r in raw}
        positive=sum(max(D(0),v) for v in profits.values())
        assert positive>0 and any(v<0 for v in profits.values())
        a.sync_profit_cash(now=now)
        after=a.profit_bridge.before_new_spending()
        assert a.cash()==before_cash
        assert after.cash_pools['psm:'+PAIR]==before.cash_pools['psm:'+PAIR]-positive
        assert after.profit_escrow==before.profit_escrow+positive
        assert after.cash_pools['protected']==500
        if eth_before:assert eth_before=={l.lot_id:l.base_remaining for l in a._lots('ETH-USD')}
        staged={row['trade_id']:row for row in persisted_rows(a,'next_profit_outbox')}
        for r in raw:
            row=staged[r['trade_id']]
            source=json.loads(row['source_payload'])
            assert D(source['event']['realized_pnl_quote'])==profits[r['trade_id']]
            assert (row['status']=='ACKNOWLEDGED')==(profits[r['trade_id']]>0)
        for owner in ('seed','repair'):
            r,sale,buy=dedicated_source(a,owner)
            source=json.loads(staged[r['trade_id']]['source_payload'])
            proof=source['psm_ownership'][0]['acquisition_funding']
            assert proof['funding_owner']==owner and proof['entry_pool']==buy['pool']
            assert proof['activation_sha256']==buy['activation']['proof_sha256']
            assert proof['lineage']['kind']==('RETAINED_ROOT_BASE_ENTRY' if retained and owner=='seed' else 'CLOSED_SAME_ROOT_ENTRY_POOL')
        original=deepcopy(a.state);outbox=deepcopy(staged);audit=a.allocator.audit()
        for r in raw:
            a.profit_bridge.stage(trade_id=r['trade_id'],snapshot=snapshot,now=now)
            a.profit_bridge.dispatch(trade_id=r['trade_id'],now=now)
        a._reload()
        assert a.state==original and a.allocator.audit()==audit
        assert {r['trade_id']:r for r in persisted_rows(a,'next_profit_outbox')}==outbox
    finally:a.close()
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=now,membership=snapshot,enabled=True) as reopened:
        reopened.sync_profit_cash(now=now)
        assert reopened.profit_bridge.before_new_spending().new_spending_allowed
        assert reopened.state==original and reopened.allocator.audit()==audit


def test_independent_funded_psm_loss_reproducer_now_reconciles(tmp_path):
    a,entry,_,_,now=funded_psm(tmp_path/'account',sale_price='1')
    try:
        before=a.cash();a.sync_profit_cash(now=now)
        assert a.cash()==before
        assert a.profit_bridge.before_new_spending().new_spending_allowed
        assert all(row['status']=='INELIGIBLE' for row in persisted_rows(a,'next_profit_outbox')
                   if a.state['orders'][json.loads(row['source_payload'])['order']['id']]['kind']=='PSM_EXIT')
    finally:a.close()


@pytest.mark.parametrize('mutation', ['missing_funding','not_closed','activation','foreign_owner','unknown_pool','missing_allocator_release'])
def test_missing_or_changed_dedicated_authority_fails_before_psm_distribution(tmp_path,mutation):
    a,_,_,_,now=funded_psm(tmp_path/'account')
    try:
        raw,sale,buy=dedicated_source(a,'repair')
        old_meta=deepcopy(a.state['psm']);old_audit=a.allocator.audit()
        with sqlite3.connect(a.db_path) as db:
            if mutation=='missing_funding':
                db.execute('DELETE FROM next_entry_funding WHERE entry_id=?',(buy['entry_id'],))
            elif mutation=='not_closed':
                db.execute("UPDATE next_entry_funding SET status='FUNDED' WHERE entry_id=?",(buy['entry_id'],))
            elif mutation=='activation':
                a.state['orders'][buy['id']]['activation']['proof_sha256']='0'*64
            elif mutation=='foreign_owner':
                a.state['orders'][buy['id']]['funding_owner']='seed'
            elif mutation=='unknown_pool':
                a.state['orders'][buy['id']]['pool']='next_entry:foreign'
            elif mutation=='missing_allocator_release':
                operation=db.execute("SELECT operation_id FROM next_entry_funding_outbox WHERE entry_id=? AND action='RELEASE'",(buy['entry_id'],)).fetchone()
                assert operation
                with sqlite3.connect(a.allocator.path) as allocator_db:
                    allocator_db.execute('DELETE FROM operations WHERE operation_id=?',(operation[0],))
        if mutation in {'activation','foreign_owner','unknown_pool'}:a._save()
        with pytest.raises(ValueError):a.profit_bridge.stage(trade_id=raw['trade_id'],snapshot=a.membership,now=now)
        with a.profit_bridge._paper() as db:
            state=a.profit_bridge._state(db)
            assert state['psm']==old_meta
            assert db.execute('SELECT 1 FROM next_profit_outbox WHERE trade_id=?',(raw['trade_id'],)).fetchone() is None
            assert db.execute('SELECT 1 FROM next_profit_discrepancies').fetchone() is not None
        assert a.allocator.audit()==old_audit
    finally:a.close()
