"""Independent synthetic evidence checks. Never reads historical data or runtime."""
from copy import deepcopy
import pytest
from research.weekly_reversal.owned_spot.model import Blocked
from test_weekly_successor_health_refresh import (
    ETH, BTC, account, settled_fixture, rebuild, successor, economics,
    rows, assert_refresh_provenance_only, assert_successor,
)


def health_rows(a):
    with a.health._connect() as db:
        return {row['position_id']:dict(row) for row in db.execute('SELECT * FROM economic_accounting_health')}


def test_only_new_successor_health_row_is_added(account, monkeypatch):
    a=account
    actual=a.refresh_accounting
    observed=[]
    def inspect(pair, *, now):
        action=a.state['psm'][ETH].get('action')
        if action and action.get('sealed'):
            before=health_rows(a); before_all=rows(a,include_health=False); before_econ=economics(a)
            pid=successor(a)
            assert len(a.config.rules)==15
            assert pid not in before
            result=actual(pair,now=now)
            after=health_rows(a)
            assert set(after)-set(before)=={pid}
            assert all(after[k]==v for k,v in before.items())
            new=after[pid]
            assert new['recorded_state']=='HEALTHY'
            assert new['last_success_at']==new['last_attempt_at']==now
            assert new['proposal_count']==new['fully_reconciled_proposals']==5
            assert new['known_orders']==new['bound_orders']==5
            assert new['confirmed_fills']==new['ledgered_fills']==0
            assert new['unresolved_order_ids_json']==new['unresolved_trade_ids_json']=='[]'
            assert economics(a)==before_econ
            assert_refresh_provenance_only(before_all,rows(a,include_health=False),a,{ETH})
            observed.append(pid)
            return result
        return actual(pair,now=now)
    monkeypatch.setattr(a,'refresh_accounting',inspect)
    rebuild(a)
    assert observed==[successor(a)]
    assert_successor(a)
    a.selected_cohort_cycle(BTC,now=a.now)


def test_real_corrupt_child_binding_prevents_healthy_publication(account,monkeypatch):
    a=account
    actual=a.refresh_accounting
    corrupted=[]
    def inspect(pair, *, now):
        action=a.state['psm'][ETH].get('action')
        if action and action.get('sealed'):
            assert len(a.config.rules)==15
            child=action['children'][0]
            # Change the authoritative synthetic binding, not the synchronizer's return.
            with a.health._connect() as db:
                db.execute('UPDATE economic_fill_bindings SET executor_id=? WHERE order_id=?',
                           ('synthetic-wrong-executor',child))
            corrupted.append(child)
        return actual(pair,now=now)
    monkeypatch.setattr(a,'refresh_accounting',inspect)
    with pytest.raises((Blocked,ValueError,RuntimeError)):
        rebuild(a)
    assert len(corrupted)==1
    h=a.health.assess(position_id=successor(a),now=a.now)
    assert h.state.value=='CONFLICT' and not h.allows_live_mutation(now=a.now)
    assert h.last_success_at is None
    assert a.state['psm'][ETH]['action']['sealed']
    assert len(a._pending(ETH))==5 and not a._lots(ETH)
    assert all(not a._receipts(o['id']) for o in a._pending(ETH))
    with pytest.raises((Blocked,ValueError,RuntimeError)):
        a.selected_cohort_cycle(BTC,now=a.now)
