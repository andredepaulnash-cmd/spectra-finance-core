"""Generated native cycles for latest-root ordinary next-buy admission only."""
from copy import deepcopy
from decimal import Decimal as D
import json
import sqlite3

import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.entry_admission import AdmissionBlocked
from research.weekly_reversal.owned_spot.model import ObservedOpen, Blocked
from test_psm_root_funding import rebuilt, sell_successor, mark, PAIR
from test_weekly_owned_adapter import ASSETS
from test_weekly_compounding_adapter import members

ROOT=PAIR+':seed:01'


def entry_hour(a,after,*,price='1.2',positive=True):
    at=((after//3600)+2)*3600
    mark(a,at,price)
    a.state['next_entry_completed_hours']={PAIR:[
        dict(pair=PAIR,opened_at=at-7200,open=1.,high=1.,low=1.,close=1.,volume=100.,trades=10),
        dict(pair=PAIR,opened_at=at-3600,open=1.,high=1.2,low=.9,
             close=1.2 if positive else .9,volume=100.,trades=10)]}
    a._save()
    return at


def eligible_root(path, *, empty_attempts=0):
    build=rebuilt if not empty_attempts else lambda path,**kwargs:rebuilt_after_empty_attempts(path,attempts=empty_attempts,**kwargs)
    a,at=build(path,sale_price='11.3',assets=ASSETS[:7])
    at,cid=sell_successor(a,ROOT,at)
    a.sync_profit_cash(now=at)
    return a,entry_hour(a,at),cid


@pytest.mark.parametrize('cancel_first',[False,True])
def test_actual_latest_root_funded_reentry_preserves_successors_and_advances_generation(tmp_path,cancel_first):
    a,at,cid=eligible_root(tmp_path/'account',empty_attempts=2 if cancel_first else 0)
    cfg=a.config
    try:
        prior=deepcopy(a.state['cohorts'])
        archive=deepcopy(a.state['psm'][PAIR]['archived_episodes'])
        cash=a.cash();claims=a.capital_commitments();profit=a.available_profit(PAIR)
        source=a.entry_admission.root_reentry_source(ROOT,now=at)
        assert source['order_permission'] is False
        assert source['source_pool']=='psm:'+PAIR and source['source_owner']['root_cohort_id']==ROOT
        assert source['latest_generation']==prior[cid]['generation']>prior[ROOT]['generation']
        assert D(source['available_base_cash'])>=cfg.seed_ticket
        order=a.request_reentry(ROOT,now=at)
        assert order['status']=='OPEN' and order['source_owner']==source['source_owner']
        assert order['previous_cohort_generation']==source['latest_generation']
        assert order['cohort_generation']==source['latest_generation']+1
        assert D(order['target'])==cfg.seed_ticket+profit
        assert a.cash()==cash and a.state['cohorts'][cid]==prior[cid]
        assert a.state['psm'][PAIR]['archived_episodes']==archive
        assert a.capital_commitments()['total']==claims['total']+profit
        if cancel_first:
            failed=order
            mark(a,at+300,'1.2');a.missing_open(order['id'],expected_at=at+300,now=at+300)
            a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300)
            assert a.state['cohorts'][ROOT]==prior[ROOT]
            assert a.state['orders'][failed['id']]['reentry_terminal_outcome']['outcome']=='EMPTY_RESTORED'
            at+=600;mark(a,at,'1.2');a.sync_profit_cash(now=at)
            order=a.request_reentry(ROOT,now=at)
            assert order['cohort_generation']==failed['cohort_generation']
            assert order['source_owner']==failed['source_owner']
            a.reconcile_terminal(failed['id'],'CANCELED',now=at)
            assert a.state['cohorts'][ROOT]['generation']==order['cohort_generation']
        mark(a,at+300,'1.2');a.observe_open(order['id'],ObservedOpen(D('1.2'),at+300,at+300))
        a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300)
        mark(a,at+600,'1.2');a.sync_profit_cash(now=at+600)
        assert a.state['cohorts'][ROOT]['generation']==source['latest_generation']+1
        assert a.state['cohorts'][cid]==prior[cid] and a.profit_bridge.before_new_spending().new_spending_allowed
        expected=deepcopy(a.state['cohorts'])
    finally:a.close()
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=at+600,membership=members(cfg),enabled=True) as a:
        assert a.state['cohorts']==expected
        a.sync_profit_cash(now=at+600)
        assert a.profit_bridge.before_new_spending().new_spending_allowed


def test_sold_root_cannot_spend_another_root_or_repair_cash(tmp_path):
    a,at=rebuilt(tmp_path/'account')
    try:
        at,cid=sell_successor(a,ROOT,at);a.sync_profit_cash(now=at)
        at,_=sell_successor(a,PAIR+':seed:02',at);a.sync_profit_cash(now=at)
        at=entry_hour(a,at)
        source=a.entry_admission.root_reentry_source(ROOT,now=at)
        assert D(source['available_base_cash'])<a.config.seed_ticket
        assert a.cash_pools()['psm:'+PAIR]>a.config.seed_ticket
        owners=a.psm.funding_cash(PAIR)
        order=a.request_reentry(ROOT,now=at)
        assert D(order['base_budget'])==D(source['available_base_cash'])<a.config.seed_ticket
        after=a.psm.funding_cash(PAIR)
        assert all(after[k]==v for k,v in owners.items() if k!=ROOT+'|seed')
        assert a.state['cohorts'][ROOT]['status']=='OWNED'
        assert a.state['cohorts'][cid]['status']=='SOLD'
    finally:a.close()


def rebuilt_after_empty_attempts(path,attempts=2,*,sale_price='1',assets=('BTC',)):
    from test_next_entry_psm_funding import funded_psm
    a,_,_,_,at=funded_psm(path,sale_price=sale_price,assets=assets)
    try:
        a.sync_profit_cash(now=at);a.psm.close_exit(PAIR,now=at)
        for attempt in range(attempts+1):
            at+=300;mark(a,at,'.5')
            a.psm.rebuild_signal(PAIR,decision_ref='root-retry:'+str(attempt),qualified=True,expires_at=at+600,now=at)
            a.psm.request_rebuild(PAIR,min_unit_gain=D(0),now=at)
            at+=300;mark(a,at,'.5')
            if attempt<attempts:
                if attempt%2==0:a.psm.missing_open(PAIR,expected_at=at,now=at)
                else:
                    for oid in list(a.state['psm'][PAIR]['action']['children']):
                        a.reconcile_terminal(oid,'REJECTED',now=at)
            else:a.psm.observe_open(PAIR,ObservedOpen(D('.5'),at,at))
            a.refresh_accounting_all(now=at);a.sync_profit_cash(now=at)
        a.psm.complete(PAIR,now=at)
        return a,at
    except BaseException:a.close();raise


def test_exact_empty_terminal_psm_shells_do_not_strand_latest_actual_root(tmp_path):
    a,at=rebuilt_after_empty_attempts(tmp_path/'account')
    try:
        at,cid=sell_successor(a,ROOT,at);a.sync_profit_cash(now=at);at=entry_hour(a,at)
        prior=deepcopy(a.state)
        source=a.entry_admission.root_reentry_source(ROOT,now=at)
        assert source['latest_generation']==a.state['cohorts'][cid]['generation']
        assert len(source['empty_attempts'])==2
        assert all(item['cohort']['status']=='PSM_PENDING' for item in source['empty_attempts'])
        assert {child['order']['terminal'] for item in source['empty_attempts'] for child in item['children']}=={'CANCELED','REJECTED'}
        assert a.state==prior
        empty=source['empty_attempts'][0]['children'][0]['order']
        with sqlite3.connect(a.root/'native.sqlite') as db:
            db.execute('DELETE FROM economic_fill_bindings WHERE order_id=?',(empty['id'],))
        with pytest.raises(ValueError):a.entry_admission.root_reentry_source(ROOT,now=at)
        binding=source['empty_attempts'][0]['children'][0]['native_binding']
        with sqlite3.connect(a.root/'native.sqlite') as db:
            db.execute('INSERT INTO economic_fill_bindings ('+','.join(binding)+') VALUES ('+','.join('?' for _ in binding)+')',tuple(binding.values()))
        order=a.request_reentry(ROOT,now=at)
        assert D(order['base_budget'])==D(source['base_budget'])<a.config.seed_ticket
        assert order['cohort_generation']==source['latest_generation']+1
    finally:a.close()


def test_base_only_root_source_after_other_entry_spends_allocation_and_recursive_sale(tmp_path):
    a,at=rebuilt(tmp_path/'account',sale_price='11.3',assets=ASSETS[:7])
    second=PAIR+':seed:03'
    cfg=a.config
    try:
        at,_=sell_successor(a,ROOT,at);a.sync_profit_cash(now=at)
        at,cid=sell_successor(a,second,at);a.sync_profit_cash(now=at)
        at=entry_hour(a,at)
        first=a.request_reentry(ROOT,now=at)
        mark(a,at+300,'1.2');a.observe_open(first['id'],ObservedOpen(D('1.2'),at+300,at+300))
        a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300)
        mark(a,at+600,'1.2');a.sync_profit_cash(now=at+600);at+=600
        assert a.available_profit(PAIR)==0
        before=deepcopy(a.allocator.export_research_state()['reservations'])
        source=a.entry_admission.root_reentry_source(second,now=at)
        assert 0<D(source['base_budget'])<=cfg.seed_ticket
        order=a.request_reentry(second,now=at)
        assert D(order['base_budget'])==D(source['base_budget'])
        assert order['pool']==order['source_pool']=='psm:'+PAIR and D(order['profit_budget'])==0
        assert order['base_entry_proof']['permit'] is None
        assert order['source_owner']['root_cohort_id']==second
        assert a.allocator.export_research_state()['reservations']==before
        mark(a,at+300,'1.2');a.observe_open(order['id'],ObservedOpen(D('1.2'),at+300,at+300))
        a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300);at+=300
        at,_=sell_successor(a,second,at,price='1.2');a.sync_profit_cash(now=at)
        with a.profit_bridge._paper() as db:
            current=a.profit_bridge.root_source_proof(second,now=at,paper_connection=db)
        assert current['source_owner']['latest_generation']==order['cohort_generation']
        assert current['ownership_proof']['latest_cohort_ids']==[second]
        assert a.state['cohorts'][cid]['status']=='SOLD'
        state=deepcopy(a.state['cohorts'])
    finally:a.close()
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=at,membership=members(cfg),enabled=True) as a:
        assert a.state['cohorts']==state
        a.sync_profit_cash(now=at)
        assert a.profit_bridge.before_new_spending().new_spending_allowed


@pytest.fixture
def empty_root_snapshot(tmp_path):
    path=tmp_path/'account'
    a,at=rebuilt(path)
    at,cid=sell_successor(a,ROOT,at);a.sync_profit_cash(now=at);at=entry_hour(a,at)
    cfg=a.config;a.close()
    return path,cfg,at,cid


@pytest.mark.parametrize('change',['source_owner','generation','source_pool','old_hour','negative_hour'])
def test_root_scope_and_latest_sale_hour_fail_before_native_commit(empty_root_snapshot,change):
    path,cfg,at,cid=empty_root_snapshot
    with WeeklyCompoundingAdapter(path,cfg,now=at,membership=members(cfg),enabled=True) as a:
        native=a.governor.protected_quote()
        original_preview=a.entry_admission.preview
        def preview(oid,**kwargs):
            order=a.state['orders'][oid]
            if change=='source_owner':order['source_owner']['funding_owner']='repair'
            elif change=='generation':order['previous_cohort_generation']-=1
            elif change=='source_pool':order['source_pool']='seed'
            elif change=='old_hour':
                # Valid contiguous positive hours, but before the actual latest sale.
                for h in a.state['next_entry_completed_hours'][PAIR]:h['opened_at']-=3600
            elif change=='negative_hour':a.state['next_entry_completed_hours'][PAIR][-1]['close']=.9
            a._save()
            return original_preview(oid,**kwargs)
        a.entry_admission.preview=preview
        with pytest.raises(AdmissionBlocked) as error:a.request_reentry(ROOT,now=at)
        expected={'source_owner':'ROOT_SOURCE','generation':'COHORT_GENERATION','source_pool':'ROOT_SOURCE',
                  'old_hour':'NEW_ENTRY_HOUR_STALE','negative_hour':'NEW_ENTRY_HOUR_NOT_POSITIVE'}
        assert error.value.code==expected[change]
        assert a.governor.protected_quote()==native
        assert a.state['cohorts'][ROOT]['status']=='SOLD' and a.state['cohorts'][cid]['status']=='SOLD'


def test_root_inventory_and_latest_completed_sale_boundary_are_causal(tmp_path):
    a,at=rebuilt(tmp_path/'account')
    try:
        before=len(a.state['orders'])
        with pytest.raises(AdmissionBlocked,match='ROOT_SUCCESSOR_HELD'):
            a.entry_admission.root_reentry_source(ROOT,now=at)
        assert len(a.state['orders'])==before
        at,cid=sell_successor(a,ROOT,at);a.sync_profit_cash(now=at)
        sold_at=at
        at=((at//3600)+1)*3600
        mark(a,at,'1.2')
        a.state['next_entry_completed_hours']={PAIR:[
            dict(pair=PAIR,opened_at=at-7200,open=1.,high=1.,low=1.,close=1.,volume=100.,trades=10),
            dict(pair=PAIR,opened_at=at-3600,open=1.,high=1.2,low=1.,close=1.2,volume=100.,trades=10)]}
        a._save()
        assert at-3600<sold_at
        with pytest.raises(AdmissionBlocked,match='NEW_ENTRY_HOUR_PRE_SALE'):a.request_reentry(ROOT,now=at)
        assert a.governor.protected_quote()==0
        assert a.state['cohorts'][ROOT]['status']=='SOLD' and a.state['cohorts'][cid]['status']=='SOLD'
    finally:a.close()
