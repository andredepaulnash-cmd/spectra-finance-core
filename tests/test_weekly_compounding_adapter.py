from dataclasses import asdict
from decimal import Decimal as D
import json
import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.adapter import serial
from research.weekly_reversal.owned_spot.model import Blocked, CompletedBar, ObservedOpen
from research.weekly_reversal.owned_spot import native as N
from spectra_core.next_entry_allocator import MembershipSnapshot, Member
from test_weekly_owned_adapter import config, seed


def members(cfg):
    return MembershipSnapshot('test-members',cfg.account,'USD',1,1000000,
        tuple(Member(p,cfg.owner+':'+p,'POSITION_ALLOCATION') for p in sorted(cfg.rules))+
        (Member('VCAP',cfg.owner+':VCAP','VCAP'),))


def test_default_off(tmp_path):
    cfg=config(('BTC',))
    with pytest.raises(Blocked,match='default-off'):
        WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg))


def test_real_native_seed_and_restart(tmp_path):
    cfg=config(('BTC','ETH'))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a)
        assert a.audit(require_marks=False)['equity']<2000
        assert a.allocator.audit()['event_count']==0
        assert a.funding_cash()['protected']==500
        before=a.report()
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1300,membership=members(cfg),enabled=True) as a:
        assert a.report()==before


def profitable_sale(a):
    assert a.seed_complete()
    pair='BTC-USD';cid=pair+':seed:01'
    a.observe(CompletedBar(pair,6900,7200,D('12'),D('1000000'),7200),now=7200)
    a.refresh_accounting_all(now=7200)
    owners=N.economic_exit._group_owners(lots=a._cohort_lots(a.state['cohorts'][cid]),governor=a.governor,pair=pair)
    a.state['assets'][pair]['selected']=cid
    a.state['assets'][pair]['exit']={'id':'fixture-exit','cohort':cid,'pair':pair,
        'owners':[serial(asdict(owner)) for owner in owners],'status':'OPEN'}
    a._save()
    order=a.continue_exit(pair,now=7200)
    trade=a.observe_open(order['id'],ObservedOpen(D('12'),7500,7500))
    a.continue_exit(pair,now=7500)
    return trade


def test_real_sale_cash_is_reallocated_without_creation(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);trade=profitable_sale(a)
        cash=a.cash();basis=a.deployed_basis()
        assert not a.profit_bridge.cash_projection().new_spending_allowed
        out=a.sync_profit_cash(now=7500)
        assert out['allocator']['event_count']==1
        assert out['allocator']['conserved']
        assert a.cash()==cash and a.deployed_basis()==basis
        assert a.available_profit('BTC-USD')==a.available_profit('VCAP')>0
        assert a.profit_bridge.cash_projection().new_spending_allowed
        before=a.allocator.audit()
        a.sync_profit_cash(now=7500)
        assert a.allocator.audit()==before
        a.audit(require_marks=False)


def reentry_boundary(a,*,price=D('11'),positive=True):
    pair='BTC-USD';now=36000
    a.observe(CompletedBar(pair,now-300,now,price,D('1000000'),now),now=now)
    a.refresh_accounting_all(now=now)
    a.state['next_entry_completed_hours']={pair:[
        dict(pair=pair,opened_at=28800,open=10.,high=10.,low=10.,close=10.,volume=100.,trades=10),
        dict(pair=pair,opened_at=32400,open=10.,high=11.,low=9.,close=11. if positive else 9.,volume=100.,trades=10)]}
    a._save()


def test_real_profit_funds_next_buy_and_keeps_other_lots_unchanged(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500)
        reentry_boundary(a)
        old={l.lot_id:l.base_remaining for l in a._lots()}
        profit=a.available_profit('BTC-USD');cash=a.cash()
        order=a.request_reentry('BTC-USD:seed:01',now=36000)
        assert order['status']=='OPEN'
        assert D(order['target'])==cfg.seed_ticket+profit
        assert a.cash()==cash
        assert old=={l.lot_id:l.base_remaining for l in a._lots()}
        a.observe(CompletedBar('BTC-USD',36000,36300,D('11'),D('1000000'),36300),now=36300)
        a.refresh_accounting_all(now=36300)
        a.observe_open(order['id'],ObservedOpen(D('11'),36300,36300))
        a.refresh_accounting_all(now=36300)
        a.sync_profit_cash(now=36300)
        assert a.state['cohorts']['BTC-USD:seed:01']['generation']==2
        assert all(a.ledger.get_lot(k).base_remaining==v for k,v in old.items())
        assert D(a.allocator.audit()['committed_profit_cash'])>0
        assert a.state['cohorts']['BTC-USD:seed:01']['anchor'] is not None
        # Release of the lot-rounded remainder requires genuinely newer native health.
        a.observe(CompletedBar('BTC-USD',36300,36600,D('11'),D('1000000'),36600),now=36600)
        a.refresh_accounting_all(now=36600);a.sync_profit_cash(now=36600)
        assert a.profit_bridge.cash_projection().new_spending_allowed
        a.audit(require_marks=False)


def test_negative_hour_blocks_before_native_commit_and_keeps_profit(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500)
        reentry_boundary(a,positive=False)
        profit=a.available_profit('BTC-USD');cash=a.cash()
        with pytest.raises(Exception,match='positive'):
            a.request_reentry('BTC-USD:seed:01',now=36000)
        assert a.available_profit('BTC-USD')==profit and a.cash()==cash
        assert a.governor.protected_quote()==0
        assert not a._pending()
        assert a.state['cohorts']['BTC-USD:seed:01']['generation']==1


def test_vcap_profit_replaces_base_with_unchanged_full_double(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path,cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500)
        reentry_boundary(a,price=D('9'))
        evidence=a.repair_feasibility('BTC-USD',now=36000,rung=1)
        assert evidence['eligible'],evidence
        cid=evidence['cohort_id'];target=a.cohort_basis(cid);profit=a.available_profit('VCAP')
        old={c:a.cohort_basis(c) for c in a.state['cohorts'] if c!=cid}
        order=a.request_repair('BTC-USD',rung=1,now=36000)
        assert D(order['target'])==target
        assert D(order['base_budget'])+profit==target
        assert order['allocation_id']=='VCAP'
        a.observe(CompletedBar('BTC-USD',36000,36300,D('9'),D('1000000'),36300),now=36300)
        a.refresh_accounting_all(now=36300)
        a.observe_open(order['id'],ObservedOpen(D('9'),36300,36300));a.refresh_accounting_all(now=36300)
        a.sync_profit_cash(now=36300)
        assert all(a.cohort_basis(c)==v for c,v in old.items())
        assert a.state['cohorts'][cid]['rung']['status'] in {'COMPLETE','ROUNDING_COMPLETED'}
        assert D(a.allocator.audit()['committed_profit_cash'])>0
        a.audit(require_marks=False)
