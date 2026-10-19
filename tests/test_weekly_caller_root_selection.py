"""Isolated caller control-flow checks; not economic or full-caller admission."""
from collections import Counter
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from research.weekly_reversal.replay import WeeklyReplay
from research.weekly_reversal.entry_admission import AdmissionBlocked

PAIR='BTC-USD'


def caller(*,blocked_root=None,failed_root=None,uncertain=False,initial_pending=False):
    r=object.__new__(WeeklyReplay);r.counts=Counter();r.signal_decisions={};r.psm_recovery_at={};r.repair_observation_reasons={}
    r._seed=lambda event:True;r.logs=[];r.log=lambda kind,**values:r.logs.append({'kind':kind,**values})
    roots=[f'{PAIR}:seed:{i:02d}' for i in (3,2,1)]
    state={'psm':{},'marks':{PAIR:{'completed_at':300}},'cohorts':{cid:{'id':cid,'pair':PAIR,'origin':'SEED','status':'SOLD','ordinal':int(cid[-2:])} for cid in roots}}
    a=SimpleNamespace(state=state);a.pending=initial_pending;a.diagnostics=[];a.attempts=[]
    a._pending=lambda pair: [{'id':'uncertain'}] if a.pending else []
    a._asset=lambda pair:{'selected':None}
    a.repair_feasibility=lambda pair,now:{'reason_code':'NO_REPAIR','reason':'Unit control-flow fixture','cohort_id':None,'guards':{'rung_due':False,'profit_clear':True}}
    a.available_profit=lambda pair:D('.40')
    def source(cid,now):
        a.diagnostics.append(cid)
        if cid.endswith(blocked_root or 'never'):
            raise AdmissionBlocked('ROOT_SUCCESSOR_HELD','Generated held-descendant diagnostic')
        return {'source_pool':'psm:'+PAIR,'available_base_cash':'2.50','base_budget':'2.50',
                'unavailable_to_this_entry':[{'source_pool':'seed','amount':'0.01'}]}
    def entry(cid,now):
        a.attempts.append(cid)
        if cid.endswith(failed_root or 'never'):
            a.pending=uncertain
            raise AdmissionBlocked('MINIMUM','Generated full-target minimum failure')
        return {'id':'real-api-return-shape','base_budget':'2.50','profit_budget':'.40'}
    a.entry_admission=SimpleNamespace(root_reentry_source=source);a.request_reentry=entry;r.a=a
    return r,SimpleNamespace(pairs=(PAIR,),at=300)


def test_held_first_root_does_not_hide_later_empty_root():
    r,event=caller(blocked_root='01');r._decisions(event)
    assert r.a.diagnostics==[f'{PAIR}:seed:01',f'{PAIR}:seed:02']
    assert r.a.attempts==[f'{PAIR}:seed:02']
    assert next(x for x in r.logs if x['kind']=='NEXT_ENTRY_ROOT_BLOCKED')['cohort']==f'{PAIR}:seed:01'


def test_minimum_failure_without_pending_authority_allows_next_root():
    r,event=caller(failed_root='01');r._decisions(event)
    assert r.a.attempts==[f'{PAIR}:seed:01',f'{PAIR}:seed:02']
    block=next(x for x in r.logs if x['kind']=='NEXT_ENTRY_BLOCKED')
    assert block['source_pool']=='psm:'+PAIR and block['available_base_cash']=='2.50'
    assert block['base_budget']=='2.50' and block['profit_budget']==D('.40') and block['full_target']==D('2.90')
    assert block['unavailable_to_this_entry']==[{'source_pool':'seed','amount':'0.01'}]


def test_failed_attempt_with_pending_authority_stops_other_roots():
    r,event=caller(failed_root='01',uncertain=True);r._decisions(event)
    assert r.a.diagnostics==r.a.attempts==[f'{PAIR}:seed:01']
    assert not any(x['kind']=='NEXT_ENTRY_ADMITTED' for x in r.logs)


def test_stable_ordinal_order_admits_at_most_one_root():
    r,event=caller();r._decisions(event)
    assert r.a.diagnostics==r.a.attempts==[f'{PAIR}:seed:01']
    assert len([x for x in r.logs if x['kind']=='NEXT_ENTRY_ADMITTED'])==1


def test_existing_pending_authority_blocks_entire_asset_loop():
    r,event=caller(initial_pending=True);r._decisions(event)
    assert r.a.diagnostics==r.a.attempts==[]
