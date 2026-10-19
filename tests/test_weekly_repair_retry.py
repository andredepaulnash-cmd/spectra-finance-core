"""Synthetic real native same-target repair retries, never historical input."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
import json
import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.model import CompletedBar, ObservedOpen, Blocked
from research.weekly_reversal.entry_admission import AdmissionBlocked
from test_weekly_compounding_adapter import config, members, seed, profitable_sale, reentry_boundary

PAIR='BTC-USD'


def bar(a,at,price=D('9')):
    a.observe(CompletedBar(PAIR,at-300,at,price,D('1000000'),at),now=at)
    a.refresh_accounting_all(now=at)


def prepare(root,funded,partial=False):
    cfg=config(('BTC',))
    a=WeeklyCompoundingAdapter(root,cfg,now=1000,membership=members(cfg),enabled=True)
    seed(a);assert a.seed_complete()
    if funded: profitable_sale(a);a.sync_profit_cash(now=7500)
    reentry_boundary(a,price=D('9'))
    initial=a.request_repair(PAIR,rung=1,now=36000)
    original=deepcopy(a.state['cohorts'][initial['cohort']]['rung'])
    bar(a,36300)
    if partial:
        a.observe_open(initial['id'],ObservedOpen(D('9'),36300,36300),fill_fraction=D('.5') if partial is True else D(partial))
    a.reconcile_terminal(initial['id'],'CANCELED',now=36300)
    a.sync_profit_cash(now=36300)
    bar(a,36600);a.sync_profit_cash(now=36600)
    return a,cfg,initial,original


@pytest.mark.parametrize('funded',[False,True])
@pytest.mark.parametrize('partial',[False,True])
def test_same_target_retry_exact_remaining_cash_fills_and_restarts(tmp_path,funded,partial):
    a,cfg,initial,original=prepare(tmp_path,funded,partial)
    try:
        cid=initial['cohort'];prior=deepcopy(a.state['cohorts'][cid]['rung'])
        state=deepcopy(a.state)
        with a.entry_admission._native() as db:
            native_before=list(db.iterdump())
        diagnostic=a.repair_retry_feasibility(PAIR,now=36600)
        assert diagnostic['eligible'],diagnostic
        assert diagnostic['order_permission'] is False
        assert a.state==state
        with a.entry_admission._native() as db:
            assert list(db.iterdump())==native_before
        actual=sum(a._totals(a.state['orders'][oid])[1]+a._totals(a.state['orders'][oid])[2] for oid in prior['orders'])
        remaining=D(original['requested_target'])-actual
        assert D(diagnostic['target'])==remaining
        retry=a.retry_repair(PAIR,now=36600)
        assert retry['status']=='OPEN' and retry['repair_retry']['prior_order_ids']==[initial['id']]
        assert D(retry['target'])==remaining
        assert D(retry['base_budget'])+D(retry['profit_budget'])==remaining
        rung=a.state['cohorts'][cid]['rung']
        assert rung['requested_target']==original['requested_target'] and rung['start_basis']==original['start_basis']
        assert rung['orders']==[initial['id'],retry['id']] and rung['actual_added']==prior['actual_added']
        assert rung['level']==original['level']
        if not funded:
            assert retry['pool']==retry['source_pool']=='repair'
            assert retry['base_entry_proof']['permit'] is None
            assert not a.allocator.export_research_state()['reservations']
        bar(a,36900)
        a.observe_open(retry['id'],ObservedOpen(D('9'),36900,36900))
        a.refresh_accounting_all(now=36900);a.sync_profit_cash(now=36900)
        bar(a,37200);a.sync_profit_cash(now=37200)
        rung=a.state['cohorts'][cid]['rung']
        assert rung['status'] in {'COMPLETE','ROUNDING_COMPLETED'}
        assert a.state['cohorts'][cid]['last_rung']==1
        assert 0<=D(rung['shortfall'])<D('.001')
        before=deepcopy(a.state['cohorts'][cid])
    finally:a.close()
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=37200,membership=members(cfg),enabled=True) as a:
        assert a.state['cohorts'][cid]==before
        evidence=a.repair_retry_feasibility(PAIR,now=37200)
        assert not evidence['eligible'] and evidence['reason_code']=='RETRY_NO_INCOMPLETE'


@pytest.mark.parametrize('tamper',[
    lambda a,c:a.state['cohorts'][c]['rung'].update(requested_target='999'),
    lambda a,c:a.state['cohorts'][c]['rung'].update(actual_added='0.01'),
    lambda a,c:a.state['cohorts'][c]['rung'].update(shortfall='0.01'),
    lambda a,c:a.state['cohorts'][c]['rung'].update(orders=[]),
])
def test_retry_cannot_replace_original_target_or_receipt_accounting(tmp_path,tamper):
    a,_,initial,_=prepare(tmp_path,False,partial=True)
    try:
        tamper(a,initial['cohort']);a._save()
        out=a.repair_retry_feasibility(PAIR,now=36600)
        assert not out['eligible'] and out['order_permission'] is False
        before=len(a.state['orders'])
        with pytest.raises(AdmissionBlocked):a.retry_repair(PAIR,now=36600)
        assert len(a.state['orders'])==before
    finally:a.close()


def test_native_profit_protection_still_blocks_same_target_retry(tmp_path):
    a,_,_,_=prepare(tmp_path,False)
    try:
        bar(a,36900,D('12'))
        out=a.repair_retry_feasibility(PAIR,now=36900)
        assert not out['eligible'] and out['reason_code']=='RETRY_PROFIT_PROTECTED'
    finally:a.close()


@pytest.mark.parametrize('funded,reason',[(False,'MINIMUM'),(True,'MINIMUM')])
def test_small_residual_stays_locked_without_clipping_or_penny_base(tmp_path,funded,reason):
    a,_,initial,_=prepare(tmp_path,funded,partial=D('.99'))
    try:
        prior=deepcopy(a.state['cohorts'][initial['cohort']]['rung'])
        before=len(a.state['orders'])
        out=a.repair_retry_feasibility(PAIR,now=36600)
        assert not out['eligible'] and out['reason_code']==reason,out
        with pytest.raises(AdmissionBlocked,match=reason):a.retry_repair(PAIR,now=36600)
        assert len(a.state['orders'])==before and a.state['cohorts'][initial['cohort']]['rung']==prior
    finally:a.close()


def test_new_available_vcap_cannot_replace_entire_retry_residual(tmp_path):
    from research.weekly_reversal.owned_spot import native as N
    from research.weekly_reversal.owned_spot.adapter import serial
    cfg=config(('BTC','ETH'))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);assert a.seed_complete()
        def actual_sale(pair,at):
            cid=pair+':seed:01'
            a.observe(CompletedBar(pair,at-300,at,D('12'),D('1000000'),at),now=at)
            a.refresh_accounting_all(now=at)
            owners=N.economic_exit._group_owners(lots=a._cohort_lots(a.state['cohorts'][cid]),governor=a.governor,pair=pair)
            a.state['assets'][pair]['selected']=cid
            a.state['assets'][pair]['exit']={'id':'new-vcap-sale:'+pair,'cohort':cid,'pair':pair,
                'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
            a._save();sale=a.continue_exit(pair,now=at)
            a.observe_open(sale['id'],ObservedOpen(D('12'),at+300,at+300))
            a.refresh_accounting_all(now=at+300)
            a.continue_exit(pair,now=at+300);a.sync_profit_cash(now=at+300)
        actual_sale(PAIR,7200);reentry_boundary(a,price=D('9'))
        first=a.request_repair(PAIR,rung=1,now=36000)
        bar(a,36300)
        a.observe_open(first['id'],ObservedOpen(D('9'),36300,36300),fill_fraction=D('.99'))
        a.reconcile_terminal(first['id'],'CANCELED',now=36300);a.sync_profit_cash(now=36300)
        bar(a,36600);a.sync_profit_cash(now=36600)
        actual_sale('ETH-USD',36900)
        bar(a,37500)
        residual=D(a.state['cohorts'][first['cohort']]['rung']['shortfall'])
        assert a.available_profit('VCAP')>=residual
        out=a.repair_retry_feasibility(PAIR,now=37500)
        assert not out['eligible'] and out['reason_code']=='PROFIT_EXCEEDS_REPAIR_TARGET',out


@pytest.mark.parametrize('funded',[False,True])
def test_retry_activation_crash_replays_only_original_residual_once(tmp_path,funded):
    a,cfg,initial,_=prepare(tmp_path,funded,partial=True)
    before=deepcopy(a.state['cohorts'][initial['cohort']]['rung'])
    def crash(_):raise RuntimeError('synthetic retry executor binding crash')
    a._authorize=crash
    with pytest.raises(RuntimeError,match='synthetic retry'):
        a.retry_repair(PAIR,now=36600)
    retry=next(o for o in a.state['orders'].values() if o.get('repair_retry'))
    assert retry['status']=='PREPARING'
    oid=retry['id'];a.close()
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=36601,membership=members(cfg),enabled=True) as a:
        assert a.state['orders'][oid]['status']=='OPEN'
        rung=a.state['cohorts'][initial['cohort']]['rung']
        assert rung['orders']==before['orders']+[oid]
        assert rung['requested_target']==before['requested_target']
        assert rung['actual_added']==before['actual_added']
        assert D(a.state['orders'][oid]['target'])==D(before['shortfall'])
