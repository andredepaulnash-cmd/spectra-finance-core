"""Generated fixtures only: terminal outcomes for native funded/base reentries."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
import json
import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.adapter import serial
from research.weekly_reversal.owned_spot.model import CompletedBar, ObservedOpen, Blocked
from research.weekly_reversal.owned_spot import native as N
from test_weekly_compounding_adapter import config, seed, members, profitable_sale, reentry_boundary

PAIR='BTC-USD'
CID=PAIR+':seed:01'


def sell(a,*,at=7200,price=D('9')):
    a.observe(CompletedBar(PAIR,at-300,at,price,D('1000000'),at),now=at)
    a.refresh_accounting_all(now=at)
    owners=N.economic_exit._group_owners(lots=a._cohort_lots(a.state['cohorts'][CID]),governor=a.governor,pair=PAIR)
    a.state['assets'][PAIR]['selected']=CID
    a.state['assets'][PAIR]['exit']={'id':'terminal-test-exit:'+str(at),'cohort':CID,'pair':PAIR,
        'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
    a._save()
    order=a.continue_exit(PAIR,now=at)
    a.observe_open(order['id'],ObservedOpen(price,at+300,at+300))
    a.continue_exit(PAIR,now=at+300)
    return order


def bar(a,at,price=D('11')):
    a.observe(CompletedBar(PAIR,at-300,at,price,D('1000000'),at),now=at)
    a.refresh_accounting_all(now=at)


def start(root,mode):
    cfg=config(('BTC',))
    a=WeeklyCompoundingAdapter(root,cfg,now=1000,membership=members(cfg),enabled=True)
    seed(a);assert a.seed_complete()
    if mode=='funded': profitable_sale(a)
    else: sell(a)
    a.sync_profit_cash(now=7500);reentry_boundary(a)
    prior=deepcopy(a.state['cohorts'][CID])
    order=a.request_reentry(CID,now=36000)
    assert (D(order['profit_budget'])>0)==(mode=='funded')
    return a,cfg,order,prior


def terminate(a,order,case):
    bar(a,36300)
    if case=='missing': a.missing_open(order['id'],expected_at=36300,now=36300)
    elif case=='expired': a.observe_open(order['id'],ObservedOpen(D('11'),observed_at=36301,opened_at=36300))
    elif case=='late': a.observe_open(order['id'],ObservedOpen(D('11'),36600,36600))
    elif case=='rejected': a.reconcile_terminal(order['id'],'REJECTED',now=36300)
    elif case=='partial':
        a.observe_open(order['id'],ObservedOpen(D('11'),36300,36300),fill_fraction=D('.5'))
        a.reconcile_terminal(order['id'],'CANCELED',now=36300)
    elif case=='filled': a.observe_open(order['id'],ObservedOpen(D('11'),36300,36300))
    else: raise AssertionError(case)


@pytest.mark.parametrize('mode',['funded','base'])
@pytest.mark.parametrize('case',['missing','expired','late','rejected','partial','filled'])
def test_exact_terminal_slot_outcomes_repeat_and_restart(tmp_path,mode,case):
    a,cfg,order,prior=start(tmp_path,mode)
    try:
        old_orders=deepcopy(a.state['orders'])
        terminate(a,order,case)
        current=a.state['orders'][order['id']]
        c=a.state['cohorts'][CID]
        if case in {'partial','filled'}:
            assert c['status']=='OWNED' and c['generation']==2 and a._cohort_lots(c)
            rows=a._receipts(order['id']);units=sum(D(r['amount']) for r in rows)
            assert D(c['anchor'])==sum(D(r['amount'])*D(r['price']) for r in rows)/units
            assert c['acquired_at']==36300
            assert len(c['anchor_history'])==len(prior['anchor_history'])+1
            assert current['reentry_terminal_outcome']['outcome']=='ACQUIRED'
        else:
            assert c==prior and c['status']=='SOLD' and c['generation']==1
            assert current['reentry_terminal_outcome']['outcome']=='EMPTY_RESTORED'
            assert not a._cohort_lots(c)
        assert not a._pending()
        before=deepcopy(c);outcome=deepcopy(current['reentry_terminal_outcome'])
        a.reconcile_terminal(order['id'],current['terminal'],now=a.now)
        assert a.state['cohorts'][CID]==before
        assert a.state['orders'][order['id']]['reentry_terminal_outcome']==outcome
        assert all(a.state['orders'][key]==value for key,value in old_orders.items() if key!=order['id'])
        with a._db() as db:
            assert sum(json.loads(row[0]).get('order')==order['id'] for row in db.execute("SELECT payload FROM audit_events WHERE kind='reentry_terminal_outcome'"))==1
        now=a.now
    finally: a.close()
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=now,membership=members(cfg),enabled=True) as a:
        assert a.state['cohorts'][CID]==before
        a.reconcile_terminal(order['id'],a.state['orders'][order['id']]['terminal'],now=now)
        assert a.state['cohorts'][CID]==before
        a.refresh_accounting_all(now=now);a.sync_profit_cash(now=now)
        if case not in {'partial','filled'}:
            assert a.profit_bridge.before_new_spending().new_spending_allowed
            assert a.available_profit(PAIR)==D(order['profit_budget'])


@pytest.mark.parametrize('mode',['funded','base'])
@pytest.mark.parametrize('case',['missing','partial','filled'])
def test_restart_finishes_crash_after_native_terminal_before_slot_transition(tmp_path,mode,case):
    a,cfg,order,prior=start(tmp_path,mode)
    def crash(_): raise RuntimeError('synthetic crash after native terminal')
    a._reconcile_reentry_outcome=crash
    with pytest.raises(RuntimeError,match='synthetic crash'):
        terminate(a,order,case)
    now=a.now
    assert a.state['orders'][order['id']]['terminal'] is not None
    assert 'reentry_terminal_outcome' not in a.state['orders'][order['id']]
    a.close()
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=now,membership=members(cfg),enabled=True) as a:
        outcome=a.state['orders'][order['id']]['reentry_terminal_outcome']
        assert outcome['outcome']==('EMPTY_RESTORED' if case=='missing' else 'ACQUIRED')
        before=deepcopy(a.state['cohorts'][CID])
        a.reconcile_terminal(order['id'],a.state['orders'][order['id']]['terminal'],now=now)
        assert a.state['cohorts'][CID]==before
        if case=='missing': assert before==prior
        else: assert before['anchor'] is not None and before['generation']==2


@pytest.mark.parametrize('mode',['funded','base'])
def test_partial_reentry_cannot_be_rejected_or_reseeded(tmp_path,mode):
    a,_,order,_=start(tmp_path,mode)
    try:
        bar(a,36300)
        a.observe_open(order['id'],ObservedOpen(D('11'),36300,36300),fill_fraction=D('.5'))
        with pytest.raises(Blocked,match='Rejected order cannot have fills'):
            a.reconcile_terminal(order['id'],'REJECTED',now=36300)
        assert a.state['orders'][order['id']]['status']=='PARTIAL'
        a.reconcile_terminal(order['id'],'CANCELED',now=36300)
        with pytest.raises(Blocked): a.request_reentry(CID,now=36300)
        assert a.state['cohorts'][CID]['generation']==2
        assert a.state['cohorts'][CID]['anchor'] is not None
    finally:a.close()


@pytest.mark.parametrize('mode',['funded','base'])
def test_zero_fill_retry_then_sale_and_later_generation_keeps_exact_source(tmp_path,mode):
    a,_,failed,prior=start(tmp_path,mode)
    try:
        terminate(a,failed,'missing');a.sync_profit_cash(now=36300)
        bar(a,36600);a.sync_profit_cash(now=36600)
        retried=a.request_reentry(CID,now=36600)
        assert retried['cohort_generation']==2 and retried['source_pool']=='seed'
        assert retried['id']!=failed['id'] and retried['proposal']!=failed['proposal']
        bar(a,36900)
        a.observe_open(retried['id'],ObservedOpen(D('11'),36900,36900))
        a.refresh_accounting_all(now=36900);a.sync_profit_cash(now=36900)
        bar(a,37200);a.sync_profit_cash(now=37200)
        sell(a,at=39600,price=D('10.5'));a.sync_profit_cash(now=39900)
        bar(a,46800)
        a.state['next_entry_completed_hours']={PAIR:[
            dict(pair=PAIR,opened_at=39600,open=10.,high=10.,low=10.,close=10.,volume=100.,trades=10),
            dict(pair=PAIR,opened_at=43200,open=10.,high=11.,low=10.,close=11.,volume=100.,trades=10)]}
        a._save()
        next_order=a.request_reentry(CID,now=46800)
        assert next_order['cohort_generation']==3
        assert next_order['source_pool']==retried['pool']
        before=deepcopy(a.state['cohorts'][CID])
        a.reconcile_terminal(failed['id'],'CANCELED',now=46800)
        a.reconcile_terminal(retried['id'],'FILLED',now=46800)
        assert a.state['cohorts'][CID]==before
    finally:a.close()
