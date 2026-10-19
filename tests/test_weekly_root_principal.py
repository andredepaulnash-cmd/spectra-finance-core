"""Synthetic actual-source principal caps; no shared-pool loss replenishment."""
from copy import deepcopy
from dataclasses import replace, asdict
from decimal import Decimal as D
import json
import pytest
from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.entry_admission import AdmissionBlocked
from research.weekly_reversal.owned_spot.model import CompletedBar, ObservedOpen, TradingRule
from research.weekly_reversal.owned_spot.adapter import serial
from research.weekly_reversal.owned_spot import native as N
from test_weekly_owned_adapter import config, seed
from test_weekly_compounding_adapter import members

PAIR='BTC-USD';ROOT=PAIR+':seed:01'


def sell_root(a,price,at=7200,root=ROOT):
    price=D(price)
    a.observe(CompletedBar(PAIR,at-300,at,price,D('1000000'),at),now=at)
    a.refresh_accounting_all(now=at)
    owners=N.economic_exit._group_owners(lots=a._cohort_lots(a.state['cohorts'][root]),governor=a.governor,pair=PAIR)
    a.state['assets'][PAIR]['selected']=root
    a.state['assets'][PAIR]['exit']={'id':'root-loss-sale:'+str(at),'cohort':root,'pair':PAIR,
        'owners':[serial(asdict(o)) for o in owners],'status':'OPEN'}
    a._save();order=a.continue_exit(PAIR,now=at)
    a.observe_open(order['id'],ObservedOpen(price,at+300,at+300))
    a.refresh_accounting_all(now=at+300);a.continue_exit(PAIR,now=at+300)
    a.sync_profit_cash(now=at+300)
    return order


def entry_boundary(a,price='11'):
    price=D(price);at=36000
    a.observe(CompletedBar(PAIR,at-300,at,price,D('1000000'),at),now=at)
    a.refresh_accounting_all(now=at)
    a.state['next_entry_completed_hours']={PAIR:[
        dict(pair=PAIR,opened_at=28800,open=float(price-1),high=float(price-1),low=float(price-1),close=float(price-1),volume=100.,trades=10),
        dict(pair=PAIR,opened_at=32400,open=float(price-1),high=float(price),low=float(price-1),close=float(price),volume=100.,trades=10)]}
    a._save();return at


def test_loss_reentry_uses_only_its_remaining_seed_principal_and_replays(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);assert a.seed_complete()
        initial=[o for o in a.state['orders'].values() if o['cohort']==ROOT and o['kind']=='SEED'][0]
        buy=a._totals(initial);unused=cfg.seed_ticket-buy[1]-buy[2]
        sale=sell_root(a,'9');totals=a._totals(sale);expected=unused+totals[1]-totals[2]
        at=entry_boundary(a)
        source=a.entry_admission.root_reentry_source(ROOT,now=at)
        assert 0<D(source['available_base_cash'])==expected<cfg.seed_ticket<a.available_pool('seed')
        other=deepcopy(a.state['cohorts'][PAIR+':seed:02'])
        order=a.request_reentry(ROOT,now=at)
        assert D(order['base_budget'])==D(order['target'])==expected and D(order['profit_budget'])==0
        proof=order['base_entry_proof']
        assert D(proof['evidence']['root_principal']['available_base_cash'])==expected
        assert a.state['cohorts'][PAIR+':seed:02']==other
        assert not a.allocator.export_research_state()['reservations']
        a.entry_admission.validate_base_entry(proof,now=at,replay=True)
        mark=at+300;a.observe(CompletedBar(PAIR,at,mark,D('11'),D('1000000'),mark),now=mark)
        a.refresh_accounting_all(now=mark);a.observe_open(order['id'],ObservedOpen(D('11'),mark,mark))
        assert a.state['cohorts'][ROOT]['generation']==2


def test_root_full_order_below_minimum_reports_all_available_amounts(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);assert a.seed_complete();sell_root(a,'.4');at=entry_boundary(a,'1000')
        source=a.entry_admission.root_reentry_source(ROOT,now=at)
        assert 0<D(source['base_budget'])<1
        with pytest.raises(AdmissionBlocked,match='MINIMUM'):a.request_reentry(ROOT,now=at)
        order=next(reversed(a.state['orders'].values()))
        diagnostic=order['admission_rejection']
        assert D(diagnostic['base_budget'])==D(diagnostic['full_target'])==D(source['base_budget'])
        assert D(diagnostic['profit_budget'])==0
        assert a.governor.protected_quote()==0 and a.state['cohorts'][ROOT]['status']=='SOLD'


def test_zero_root_principal_is_explicit_and_never_subsidized(tmp_path):
    cfg=replace(config(('BTC',)),seed_count=2,seed_ticket=D('10.08'),expected_slippage_bps=D(0),
                rules={PAIR:TradingRule(D('.00001'),D('.001'),D('.00001'),D('.00001'))})
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);assert a.seed_complete();sell_root(a,'.00001')
        sell_root(a,'12',at=10800,root=PAIR+':seed:02');at=entry_boundary(a)
        assert a.available_profit(PAIR)>0
        source=a.entry_admission.root_reentry_source(ROOT,now=at)
        assert D(source['base_budget'])==D(source['available_base_cash'])==0
        assert a.available_pool('seed')>900
        with pytest.raises(AdmissionBlocked,match='BASE_PRINCIPAL_EMPTY'):a.request_reentry(ROOT,now=at)
        assert a.governor.protected_quote()==0 and not a.allocator.export_research_state()['reservations']


def test_frozen_principal_recomputes_row_totals_even_after_internal_rehash(tmp_path):
    from research.weekly_reversal.entry_admission import _hash
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);assert a.seed_complete();sell_root(a,'9');at=entry_boundary(a)
        order=a.request_reentry(ROOT,now=at)
        proof=deepcopy(order['base_entry_proof'])
        principal=proof['evidence']['root_principal']
        principal['available_base_cash']=str(D(principal['available_base_cash'])+D('1'))
        principal['balances']['seed']=principal['available_base_cash']
        principal['proof_sha256']=_hash({k:v for k,v in principal.items() if k!='proof_sha256'})
        with a.entry_admission._paper() as db:
            with pytest.raises(AdmissionBlocked,match='ROOT_PRINCIPAL'):
                a.entry_admission._replay_root_principal(proof,db,a.state)
