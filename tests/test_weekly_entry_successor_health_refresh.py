"""Synthetic ordinary-entry successor gap, separate from the observed PSM bug."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal as D
from io import StringIO
from types import SimpleNamespace

import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot import native as N
from research.weekly_reversal.owned_spot.adapter import serial
from research.weekly_reversal.owned_spot.model import Blocked, ObservedOpen
from research.weekly_reversal.replay import WeeklyReplay
from test_weekly_compounding_adapter import members
from test_weekly_owned_adapter import config, seed
from test_weekly_successor_health_refresh import BTC, ETH, HEALTH_ERROR, SimulatedCrash, mark, rows, assert_refresh_provenance_only


def economic_state(a):
    return deepcopy({'cash': a.cash(), 'pools': a.cash_pools(), 'holds': a.holds(),
        'basis': a.deployed_basis(), 'lots': [asdict(l) for l in a._lots()],
        'receipts': a._receipts(), 'pending': a._pending(), 'state': a.state,
        'allocator': a.allocator.export_research_state()})


def sold_account(root, *, remaining_cohorts=0, sale_price='12'):
    cfg = replace(config(), seed_count=1 + remaining_cohorts)
    a = WeeklyCompoundingAdapter(root, cfg, now=1000, membership=members(cfg), enabled=True)
    try:
        seed(a)
        assert a.seed_complete()
        cid = ETH + ':seed:01'
        mark(a, 7200, sale_price)
        owners = N.economic_exit._group_owners(lots=a._cohort_lots(a.state['cohorts'][cid]), governor=a.governor, pair=ETH)
        a._asset(ETH)['selected'] = cid
        a._asset(ETH)['exit'] = {'id': 'synthetic-ordinary-sale', 'cohort': cid, 'pair': ETH,
            'owners': [serial(asdict(owner)) for owner in owners], 'status': 'OPEN'}
        a._save()
        order = a.continue_exit(ETH, now=7200)
        mark(a, 7500, sale_price)
        a.observe_open(order['id'], ObservedOpen(D(sale_price), 7500, 7500))
        a.refresh_accounting_all(now=7500)
        a.continue_exit(ETH, now=7500)
        a.sync_profit_cash(now=7500)
        mark(a, 36000, '11')
        a.state['next_entry_completed_hours'] = {ETH: [
            dict(pair=ETH, opened_at=28800, open=10., high=10., low=10., close=10., volume=100., trades=10),
            dict(pair=ETH, opened_at=32400, open=10., high=11., low=9., close=11., volume=100., trades=10)]}
        a._asset(BTC)['selected'] = BTC + ':seed:01'
        a._save()
        assert (a._current_position(ETH) in a.ledger.list_active_position_ids()) == bool(remaining_cohorts)
        return a
    except BaseException:
        a.close()
        raise


def ordinary_harness(a):
    r = WeeklyReplay.__new__(WeeklyReplay)
    r.a = a
    r.references = {}
    r.counts = Counter()
    r.events = StringIO()
    r.activation_at = None
    r.psm_recovery_at = {}
    r.signal_decisions = {ETH: {'rsi_priority': 2}, BTC: {'rsi_priority': 1}}
    r.repair_observation_reasons = {}
    return r, SimpleNamespace(at=int(a.now), pairs=tuple(a.config.rules), completed=())


@pytest.mark.parametrize('sale_price', ['9', '12'])
def test_same_bar_ordinary_successor_before_btc_selected_decision(tmp_path, monkeypatch, sale_price):
    with sold_account(tmp_path / 'account', sale_price=sale_price) as a:
        r, event = ordinary_harness(a)
        before = economic_state(a)
        actual_refresh, actual_selected = a.refresh_accounting, a.selected_cohort_cycle
        calls = []

        def refresh(pair, *, now):
            calls.append(('refresh', pair))
            previous, tables = economic_state(a), rows(a, include_health=False)
            result = actual_refresh(pair, now=now)
            assert economic_state(a) == previous
            assert_refresh_provenance_only(tables, rows(a, include_health=False), a, {pair})
            return result

        def selected(pair, **kwargs):
            if pair == BTC:
                calls.append(('selected', pair))
            # Invoke the actual guard first so the original source fails at the
            # same whole-account guard, rather than a test-only health assertion.
            result = actual_selected(pair, **kwargs)
            if pair == BTC:
                pid = a._current_position(ETH)
                health = a.health.assess(position_id=pid, now=a.now)
                assert pid.endswith('ETH-USD:native:2')
                assert health.allows_live_mutation(now=a.now)
                assert health.proposal_count == health.known_orders == health.bound_orders == 1
                assert health.confirmed_fills == health.ledgered_fills == 0
            return result

        monkeypatch.setattr(a, 'refresh_accounting', refresh)
        monkeypatch.setattr(a, 'selected_cohort_cycle', selected)
        r._decisions(event)
        assert calls == [('refresh', ETH), ('selected', BTC)]
        assert r.counts['NEXT_ENTRY_ADMITTED'] == 1
        after = economic_state(a)
        for key in ('cash', 'basis', 'lots', 'receipts'):
            assert after[key] == before[key]
        pending = a._pending(ETH)
        assert len(pending) == 1 and pending[0]['status'] == 'OPEN'
        assert D(pending[0]['profit_budget']) > 0 if sale_price == '12' else D(pending[0]['profit_budget']) == 0
        assert a.audit(require_marks=True)
        previous, tables = economic_state(a), rows(a)
        actual_refresh(ETH, now=a.now)
        assert economic_state(a) == previous
        assert_refresh_provenance_only(tables, rows(a), a, {ETH})


def test_ordinary_existing_position_does_not_add_a_refresh(tmp_path, monkeypatch):
    with sold_account(tmp_path / 'account', remaining_cohorts=1) as a:
        calls = []
        actual_refresh = a.refresh_accounting

        def refresh(pair, *, now):
            calls.append(pair)
            return actual_refresh(pair, now=now)

        monkeypatch.setattr(a, 'refresh_accounting', refresh)
        order = a.request_reentry(ETH + ':seed:01', now=a.now)
        assert order['status'] == 'OPEN'
        assert a._current_position(ETH).endswith('ETH-USD:native:1')
        assert calls == []
        a.selected_cohort_cycle(BTC, now=a.now)


def test_ordinary_authorization_interruption_stays_fail_closed_on_restart(tmp_path, monkeypatch):
    root = tmp_path / 'account'
    with sold_account(root) as a:
        actual = a._authorize

        def crash(order):
            actual(order)
            raise SimulatedCrash('after ordinary successor authorization')

        monkeypatch.setattr(a, '_authorize', crash)
        with pytest.raises(SimulatedCrash):
            a.request_reentry(ETH + ':seed:01', now=a.now)
        before = economic_state(a)
        cfg, now, membership = a.config, a.now, a.membership
        assert a.health.assess(position_id=a._current_position(ETH), now=now).last_success_at is None
    with WeeklyCompoundingAdapter(root, cfg, now=now, membership=membership, enabled=True) as a:
        with pytest.raises(Blocked, match=HEALTH_ERROR):
            a.selected_cohort_cycle(BTC, now=now)
        a.refresh_accounting_all(now=now)
        a.selected_cohort_cycle(BTC, now=now)
        after = economic_state(a)
        for key in ('cash', 'pools', 'holds', 'basis', 'lots', 'receipts', 'pending', 'allocator'):
            assert after[key] == before[key]
