"""Synthetic native successor-ordering regressions; no historical tape or replay.

The decision-loop harness supplies an already-qualified synthetic reversal input.
All positions, proposal evidence, fills, ownership, PSM actions, accounting health,
and selected-cohort decisions use the real pinned native implementation.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal as D
from io import StringIO
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from research.weekly_reversal.owned_spot.model import Blocked, CapitalBlocked, CompletedBar, ObservedOpen
from research.weekly_reversal.replay import WeeklyReplay
from test_weekly_compounding_adapter import members
from test_weekly_owned_adapter import config, seed

ETH = 'ETH-USD'
BTC = 'BTC-USD'
HEALTH_ERROR = 'Native whole-account accounting health is missing, stale or unresolved'


def mark(a, at, eth_price='1'):
    for pair in a.config.rules:
        price = D(eth_price if pair == ETH else '10')
        a.observe(CompletedBar(pair, at - 300, at, price, D('1000000'), at), now=at)
    a.refresh_accounting_all(now=at)


def make_settled(root, *, partial=False, assets=None):
    cfg = config() if assets is None else config(assets)
    if partial:
        # Exact terminating native basis for the retained-dust branch.
        cfg = replace(cfg, seed_ticket=D('17.136'), expected_slippage_bps=D(0))
    with WeeklyCompoundingAdapter(root, cfg, now=1000, membership=members(cfg), enabled=True) as a:
        seed(a)
        at = 1600
        repairs = []
        for rung in range(1, 8):
            mark(a, at)
            try:
                order = a.request_repair(ETH, rung=rung, now=at)
            except CapitalBlocked:
                break
            repairs.append(order)
            mark(a, at + 300)
            a.observe_open(order['id'], ObservedOpen(D('1'), at + 300, at + 300))
            a.refresh_accounting_all(now=at + 300)
            at += 600
        else:
            raise AssertionError('Expected an actual native capital block')
        assert len(repairs) == 4
        a.psm.start(ETH, decision_ref='synthetic-native-capital-block', falling_knife=True,
                    repair_eligible=False, policy_ref='synthetic-only', now=at)
        a.psm.request_sell(ETH, now=at)
        mark(a, at + 300)
        a.psm.observe_open(ETH, ObservedOpen(D('1'), at + 300, at + 300),
                           fill_fraction=D('.9999') if partial else D(1))
        if partial:
            for child in list(a._pending(ETH)):
                a.reconcile_terminal(child['id'], 'CANCELED', now=at + 300)
        a.refresh_accounting_all(now=at + 300)
        a.sync_profit_cash(now=at + 300)
        a.psm.close_exit(ETH, now=at + 300)
        at += 600
        mark(a, at, '.5')
        assert a.psm._position(ETH)['phase'] == 'REBUILD'
        assert not a._pending() and bool(a._lots(ETH)) == partial
        # Selection is setup only; the decision loop below still invokes the real
        # native lifecycle method and its whole-account health guard.
        a._asset(BTC)['selected'] = BTC + ':seed:01'
        a._save()
    return root, cfg, at


@pytest.fixture(scope='module')
def settled_fixture(tmp_path_factory):
    result = make_settled(tmp_path_factory.mktemp('native-successor-source') / 'account')
    root = result[0]
    shutil.copytree(root, root.parent / 'pristine')
    return result


@pytest.fixture
def account(settled_fixture, tmp_path):
    source, cfg, at = settled_fixture
    # The native funding bridge pins absolute database paths on restart.
    # Restore this generated fixture only at its original path, never rebase it.
    root = source
    shutil.rmtree(root)
    shutil.copytree(root.parent / 'pristine', root)
    with WeeklyCompoundingAdapter(root, cfg, now=at, membership=members(cfg), enabled=True) as a:
        yield a


def harness(a):
    r = WeeklyReplay.__new__(WeeklyReplay)
    r.a = a
    r.references = {ETH: SimpleNamespace(
        state=SimpleNamespace(cycle_id=a.state['psm'][ETH]['position']), closed_at=None,
        qualification=lambda *, now: {'eligible': True, 'intent_id': 'synthetic-qualified-rebuild',
                                      'source_hour': int(now)})}
    r.counts = Counter()
    r.events = StringIO()
    r.activation_at = None
    r.psm_recovery_at = {}
    r.signal_decisions = {ETH: {'rsi_priority': 2}, BTC: {'rsi_priority': 1}}
    r.repair_observation_reasons = {}
    return r, SimpleNamespace(at=int(a.now), pairs=tuple(a.config.rules), completed=())


def economics(a):
    return deepcopy({'cash': a.cash(), 'pools': a.cash_pools(), 'holds': a.holds(),
        'funding_cash': a.psm.funding_cash(ETH), 'basis': a.deployed_basis(),
        'lots': [asdict(lot) for lot in a._lots()], 'receipts': a._receipts(),
        'pending': a._pending(), 'allocator': a.allocator.export_research_state(),
        'state': a.state})


def rows(a, *, include_health=True):
    result = {}
    for name in ('paper.sqlite', 'native.sqlite', 'allocator.sqlite'):
        path = a.root / name
        if not path.exists():
            continue
        with sqlite3.connect(path) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for table in tables:
                if not include_health and table == 'economic_accounting_health':
                    continue
                result[name + ':' + table] = sorted(db.execute('SELECT * FROM "' + table + '"').fetchall(), key=repr)
    return result


def assert_refresh_provenance_only(before, after, a, pairs):
    """Identical native refreshes append provenance receipts, not economic deltas."""
    before, after = deepcopy(before), deepcopy(after)
    key = 'native.sqlite:executor_accounting_refresh_receipts'
    old, new = before.pop(key), after.pop(key)
    assert set(old) <= set(new)
    additions = set(new) - set(old)
    expected = {(o['proposal'], o['executor'], a.now) for o in a.state['orders'].values()
                if o['pair'] in pairs and o['status'] not in {'FUNDING_PREPARED', 'ENTRY_PREPARED', 'REJECTED'}}
    assert len(additions) == len(expected)
    assert {(row[2], row[3], row[4]) for row in additions} == expected
    for snapshot in (before, after):
        key = 'native.sqlite:sqlite_sequence'
        snapshot[key] = [row for row in snapshot[key] if row[0] != 'executor_accounting_refresh_receipts']
    assert before == after


def successor(a):
    return a._asset(ETH)['positions'][-1]


def rebuild(a):
    a.psm.rebuild_signal(ETH, decision_ref='synthetic-direct-rebuild', qualified=True,
                         expires_at=int(a.now) + 300, now=a.now)
    return a.psm.request_rebuild(ETH, min_unit_gain=D(0), now=a.now)


def assert_successor(a):
    action = a.state['psm'][ETH]['action']
    assert action['sealed'] and len(action['children']) == 5
    pid = successor(a)
    assert pid.endswith('ETH-USD:native:2') and pid in a.ledger.list_active_position_ids()
    children = [a.state['orders'][oid] for oid in action['children']]
    assert all(o['status'] == 'OPEN' and o['position'] == pid and not a._receipts(o['id']) for o in children)
    health = a.health.assess(position_id=pid, now=a.now)
    assert health.allows_live_mutation(now=a.now)
    assert health.last_success_at == a.now
    assert health.proposal_count == health.fully_reconciled_proposals == 5
    assert health.known_orders == health.bound_orders == 5
    assert health.confirmed_fills == health.ledgered_fills == 0


def test_same_bar_eth_successor_before_btc_selected_decision(account, monkeypatch):
    a = account
    r, event = harness(a)
    initial = economics(a)
    calls = []
    actual_refresh = a.refresh_accounting
    actual_selected = a.selected_cohort_cycle

    def refresh(pair, *, now):
        sealed = bool(a.state['psm'][ETH].get('action', {}) and a.state['psm'][ETH]['action'].get('sealed'))
        if sealed:
            calls.append(('sealed_refresh', pair))
            before, native_before = economics(a), rows(a, include_health=False)
        result = actual_refresh(pair, now=now)
        if sealed:
            assert economics(a) == before
            assert_refresh_provenance_only(native_before, rows(a, include_health=False), a, {ETH})
        return result

    def selected(pair, **kwargs):
        if pair == BTC:
            calls.append(('selected', pair))
        result = actual_selected(pair, **kwargs)
        if pair == BTC:
            assert_successor(a)
        return result

    monkeypatch.setattr(a, 'refresh_accounting', refresh)
    monkeypatch.setattr(a, 'selected_cohort_cycle', selected)
    r._decisions(event)
    assert calls == [('sealed_refresh', ETH), ('selected', BTC)]
    assert r.counts['PSM_REBUILD_ADMITTED'] == 1
    assert_successor(a)
    current = economics(a)
    for key in ('cash', 'pools', 'funding_cash', 'basis', 'lots', 'receipts', 'allocator'):
        assert current[key] == initial[key]
    assert len(a._pending()) == 5
    assert a.holds() == sum((D(o['target']) for o in a._pending()), D(0))
    assert all(a.state['marks'][p]['completed_at'] == a.now for p in a.config.rules)
    assert a.audit(require_marks=True)


def test_duplicate_refresh_has_no_economic_or_native_side_effects(account):
    a = account
    rebuild(a)
    assert_successor(a)
    before, tables = economics(a), rows(a)
    for _ in range(2):
        a.refresh_accounting(ETH, now=a.now)
        assert economics(a) == before
        assert_refresh_provenance_only(tables, rows(a), a, {ETH})
        tables = rows(a)
    with pytest.raises(Blocked, match='Pending native/PSM action'):
        a.psm.request_rebuild(ETH, min_unit_gain=D(0), now=a.now)
    assert economics(a) == before
    assert_refresh_provenance_only(tables, rows(a), a, set(a.config.rules))


@pytest.mark.parametrize('kind', ['missing', 'stale', 'conflict'])
@pytest.mark.parametrize('target', ['successor', 'unrelated'])
def test_missing_stale_conflicting_health_still_blocks_whole_account(account, kind, target):
    a = account
    rebuild(a)
    pid = successor(a) if target == 'successor' else a._current_position('SOL-USD')
    with a.health._connect() as db:
        if kind == 'missing':
            db.execute('DELETE FROM economic_accounting_health WHERE position_id=?', (pid,))
        elif kind == 'stale':
            db.execute('UPDATE economic_accounting_health SET last_success_at=? WHERE position_id=?',
                       (a.now - a.health.policy.max_age_seconds - 1, pid))
    if kind == 'conflict':
        a.health.record_conflict(position_id=pid, now=a.now, reason='synthetic unresolved native conflict')
    before = deepcopy(a.state), rows(a)
    with pytest.raises(ValueError, match='[Aa]ccounting'):
        a.selected_cohort_cycle(BTC, now=a.now)
    assert (deepcopy(a.state), rows(a)) == before


def test_real_native_sync_conflict_is_recorded_and_propagates(account, monkeypatch):
    a = account
    actual = a._sync_proposal

    def fail_successor(proposal, position):
        if position.endswith('ETH-USD:native:2') and a.state['psm'][ETH]['action'].get('sealed'):
            raise Blocked('synthetic exact-provenance mismatch')
        return actual(proposal, position)

    monkeypatch.setattr(a, '_sync_proposal', fail_successor)
    with pytest.raises(Blocked, match='synthetic exact-provenance mismatch'):
        rebuild(a)
    assert a.state['psm'][ETH]['action']['sealed']
    health = a.health.assess(position_id=successor(a), now=a.now)
    assert health.state.value == 'CONFLICT'
    assert not health.allows_live_mutation(now=a.now)
    with pytest.raises(ValueError, match='[Aa]ccounting'):
        a.selected_cohort_cycle(BTC, now=a.now)
    assert len(a._pending()) == 5 and not a._lots(ETH)


class SimulatedCrash(BaseException):
    pass


def test_partial_seal_never_publishes_successor_health(account, monkeypatch):
    a = account
    actual = a._new_buy
    calls = []
    actual_refresh = a.refresh_accounting

    def interrupt_after_first(*args, **kwargs):
        result = actual(*args, **kwargs)
        if args[2] == 'PSM_REBUILD':
            raise SimulatedCrash('after first authorized child')
        return result

    def refresh(pair, *, now):
        calls.append((pair, len(a._asset(ETH)['positions'])))
        return actual_refresh(pair, now=now)

    monkeypatch.setattr(a, '_new_buy', interrupt_after_first)
    monkeypatch.setattr(a, 'refresh_accounting', refresh)
    with pytest.raises(SimulatedCrash):
        rebuild(a)
    assert not a.state['psm'][ETH]['action']['sealed']
    assert len(a._pending(ETH)) == 1
    assert not any(count == 2 for _, count in calls)
    assert a.health.assess(position_id=successor(a), now=a.now).last_success_at is None
    with pytest.raises(Blocked, match=HEALTH_ERROR):
        a.selected_cohort_cycle(BTC, now=a.now)
    # Real native fill guard remains closed even if a caller later refreshes.
    a.refresh_accounting(ETH, now=a.now)
    child = a._pending(ETH)[0]
    with pytest.raises(Blocked, match='completely sealed parent batch'):
        a.psm.validate_fill(child['id'], quantity=D('1'), price=D('.5'), fee=D('.004'))


@pytest.mark.parametrize('sealed', [False, True])
def test_restart_between_seal_and_health_stays_fail_closed_until_native_refresh(account, monkeypatch, sealed):
    a = account
    actual_save = a._save
    point = 'psm_rebuild_batch_sealed' if sealed else 'psm_native_buy_binding'

    def crash_after_save(event=None, payload=None):
        result = actual_save(event, payload)
        if event == point:
            raise SimulatedCrash(point)
        return result

    monkeypatch.setattr(a, '_save', crash_after_save)
    with pytest.raises(SimulatedCrash):
        rebuild(a)
    assert bool(a.state['psm'][ETH]['action']['sealed']) == sealed
    assert a.health.assess(position_id=successor(a), now=a.now).last_success_at is None
    before = economics(a)
    root, cfg, now, membership = a.root, a.config, a.now, a.membership
    a.close()
    with WeeklyCompoundingAdapter(root, cfg, now=now, membership=membership, enabled=True) as reopened:
        assert economics(reopened) == before
        with pytest.raises(Blocked, match=HEALTH_ERROR):
            reopened.selected_cohort_cycle(BTC, now=now)
        reopened.refresh_accounting_all(now=now)
        if sealed:
            assert_successor(reopened)
            reopened.selected_cohort_cycle(BTC, now=now)
        else:
            child = reopened._pending(ETH)[0]
            with pytest.raises(Blocked, match='completely sealed parent batch'):
                reopened.psm.validate_fill(child['id'], quantity=D('1'), price=D('.5'), fee=D('.004'))
        for key in ('cash', 'pools', 'funding_cash', 'basis', 'lots', 'receipts', 'allocator', 'pending'):
            assert economics(reopened)[key] == before[key]


@pytest.mark.parametrize('fault, message', [
    ('missing_mark', 'No completed causal bar'),
    ('stale_mark', 'Missing, future or stale completed bar'),
    ('unapplied_receipt', 'Unapplied paper receipts require recovery'),
    ('duplicate_ownership', 'Native lot is claimed by multiple cohorts'),
])
def test_native_refresh_does_not_bypass_nonhealth_guards(account, fault, message):
    a = account
    rebuild(a)
    assert_successor(a)
    if fault == 'missing_mark':
        del a.state['marks'][BTC]
    elif fault == 'stale_mark':
        a.state['marks'][BTC]['completed_at'] -= a.config.max_mark_age_seconds + 1
        a.state['marks'][BTC]['opened_at'] -= a.config.max_mark_age_seconds + 1
    elif fault == 'unapplied_receipt':
        with a._db() as db:
            db.execute('UPDATE receipts SET applied=0 WHERE id=?', (a._receipts()[0]['trade_id'],))
    else:
        a.state['cohorts'][BTC + ':seed:02']['lot_ids'].append(a.state['cohorts'][BTC + ':seed:01']['lot_ids'][0])
    a._save()
    a.refresh_accounting(ETH, now=a.now)
    before = deepcopy(a.state), rows(a)
    with pytest.raises(Blocked, match=message):
        a.selected_cohort_cycle(BTC, now=a.now)
    assert (deepcopy(a.state), rows(a)) == before


def test_psm_existing_position_does_not_add_postseal_refresh(tmp_path, monkeypatch):
    root, cfg, now = make_settled(tmp_path / 'account', partial=True, assets=('BTC', 'ETH'))
    with WeeklyCompoundingAdapter(root, cfg, now=now, membership=members(cfg), enabled=True) as a:
        old_position = a._current_position(ETH)
        assert old_position.endswith('ETH-USD:native:1') and a._lots(ETH)
        calls = []
        actual = a.refresh_accounting

        def refresh(pair, *, now):
            action = a.state['psm'][ETH]['action']
            if action and action.get('sealed'):
                calls.append(pair)
            return actual(pair, now=now)

        monkeypatch.setattr(a, 'refresh_accounting', refresh)
        before = economics(a)
        rebuild(a)
        assert a._current_position(ETH) == old_position
        assert calls == []
        assert a.state['psm'][ETH]['action']['sealed']
        a.selected_cohort_cycle(BTC, now=now)
        after = economics(a)
        for key in ('cash', 'pools', 'funding_cash', 'basis', 'lots', 'receipts', 'allocator'):
            assert after[key] == before[key]
