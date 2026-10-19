"""Repeated real native root entries, bounded principal and proof growth receipt."""
from copy import deepcopy
from decimal import Decimal as D
import json
from time import perf_counter
from research.weekly_reversal.owned_spot.model import ObservedOpen
from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from test_psm_root_funding import rebuilt,sell_successor,mark,PAIR
from test_psm_root_reentry import entry_hour
from test_weekly_compounding_adapter import members


def test_three_real_root_generations_conserve_cash_and_replay(tmp_path):
    root=PAIR+':seed:01';a,at=rebuilt(tmp_path/'account',sale_price='1')
    cfg=a.config;metrics=[]
    try:
        archive=deepcopy(a.state['psm'][PAIR]['archived_episodes'])
        for index in range(3):
            at,_=sell_successor(a,root,at,price='1.2');a.sync_profit_cash(now=at)
            at=entry_hour(a,at);source=a.entry_admission.root_reentry_source(root,now=at)
            assert D(source['base_budget'])==min(cfg.seed_ticket,D(source['available_base_cash']))>0
            if index==0:assert D(source['base_budget'])<cfg.seed_ticket
            started=perf_counter();order=a.request_reentry(root,now=at);admit_seconds=perf_counter()-started
            assert order['cohort_generation']==source['latest_generation']+1
            assert D(order['target'])==D(order['base_budget'])+D(order['profit_budget'])
            with a.profit_bridge._paper() as db:
                proof=order.get('base_entry_proof')
                if proof is None:_,proof=a.profit_bridge._funding_row(db,order['entry_id'])
                proof_bytes=len(json.dumps(proof,sort_keys=True,separators=(',',':')).encode())
            mark(a,at+300,'1.2');a.observe_open(order['id'],ObservedOpen(D('1.2'),at+300,at+300))
            a.refresh_accounting_all(now=at+300);a.sync_profit_cash(now=at+300)
            mark(a,at+600,'1.2');a.sync_profit_cash(now=at+600);at+=600
            started=perf_counter();assert a.profit_bridge.before_new_spending().new_spending_allowed
            metrics.append({'generation':order['cohort_generation'],'admission_bytes':proof_bytes,
                            'admit_seconds':admit_seconds,'replay_seconds':perf_counter()-started})
            assert a.state['psm'][PAIR]['archived_episodes']==archive
        state=deepcopy(a.state['cohorts']);cash=a.cash();audit=a.allocator.audit()
        print('ROOT_GENERATION_METRICS',json.dumps(metrics,sort_keys=True))
    finally:a.close()
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=at,membership=members(cfg),enabled=True) as a:
        a.sync_profit_cash(now=at)
        assert a.state['cohorts']==state and a.cash()==cash and a.allocator.audit()==audit
        assert a.profit_bridge.before_new_spending().new_spending_allowed
