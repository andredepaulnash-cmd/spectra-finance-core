"""Synthetic real-native evidence plus pure schema3 PSM partition regressions."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from research.weekly_reversal import psm_kernel as K
from research.weekly_reversal import psm_kernel_legacy as LEGACY
from research.weekly_reversal import psm_profit_partition as P


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@pytest.fixture
def native_source(tmp_path, monkeypatch, request):
    # Exercise actual native SQLite acquisition/disposal reconciliation, then
    # bind that verified synthetic sale into a canonical PSM episode.
    spec = importlib.util.spec_from_file_location('_profit_fixture', Path(__file__).with_name('test_next_entry_profit_bridge.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    sale_price = getattr(request, 'param', '120')
    sale_gross = str(D(sale_price)*2)
    bridge = fixture.seed(tmp_path, monkeypatch, sale_price=sale_price)
    with bridge._paper() as db:
        source = json.loads(bridge._verify_source(db, 'sell-fill', fixture.NOW)[0])
        state = bridge._state(db)
    at = fixture.NOW
    pair = 'BTC-USD'
    config = state['identity']['config']
    campaign = f"{config['owner']}:{config['account']}:{pair}:psm"
    position = campaign + ':episode:1'
    buy, sell = state['orders']['buy'], state['orders']['sell']
    buy.update(kind='SEED', intent='ENTRY', cohort='root')
    sell.update(kind='PSM_EXIT', intent='ENTRY', cohort='root', pool='psm:' + pair,
                psm_position=position, psm_order='logical-sell', funding_owner='seed')
    root = dict(id='root', pair=pair, generation=1, lot_ids=list(sell['eligible_lot_ids']))
    state['cohorts'] = {'root': root}
    lineage = []
    for original in source['lots']:
        lot = dict(original, base_remaining=original['base_original'],
                   cost_basis_remaining_quote=original['cost_basis_original_quote'],
                   entry_fees_remaining_quote=original['entry_fees_original_quote'], updated_at=at-50)
        lineage.append(dict(lot=lot, cohort='root', acquisition_order='buy', source_pool='seed'))
    book = K.ResearchBook()
    def apply(kind, **kw):
        event = dict(id=kind, kind=kind, at=at-45, position=position, owner=config['owner'])
        event.update(kw)
        book.apply(event)
    apply('snapshot', account=config['account'], asset='BTC/USD', provenance='decision',
          inventory_bucket='ACTIVE_TRADE', reconciled=True, pending_orders=[], units='2', basis='202')
    apply('handoff', vcap_eligible=False, falling_knife=True, reconciled=True,
          pending_orders=[], confirmed_units='2', decision_ref='decision')
    apply('intent', at=at-40, order='logical-sell', side='sell', price=sale_price, quote_at=at-40,
          max_age=300, step='0.001', min_units='0.001', min_quote='0.5', max_fee_rate='0.1')
    apply('fill', id='psm-fill:sell-fill', at=at-15, occurred_at=at-20, order='logical-sell',
          fill='sell-fill', units='2', price=sale_price, fee='2', fee_currency='quote')
    apply('reconcile', at=at-10, order='logical-sell', status='FILLED', complete_fill_history=True,
          filled_units='2', gross=sale_gross, fees='2')
    action = dict(order='logical-sell', side='sell', children=['sell'], quantity='2', price=sale_price,
                  fee_ceiling='0.1', at=at-40, completed=True, sealed=True, decision_bar=at-40,
                  episode=1, terminal='FILLED', owners=[dict(proposal_id='proposal', executor_id='executor',
                  intent='ENTRY', account_name=config['account'], connector_name=config['connector'],
                  lot_ids=list(sell['eligible_lot_ids']))])
    meta = dict(source_sha256=P.KERNEL_SHA256, position=position, campaign=campaign, episode=1,
                mode='PSM', archived_episodes=[], episode_cash_offsets={}, journal=book.dumps(),
                original_lineage=deepcopy(lineage), original_cohorts=[deepcopy(root)],
                initial_native_positions=['position'], initial_native_realized='0', original_basis='202',
                original_units='2', episode_lineage=deepcopy(lineage), episode_cohorts=[deepcopy(root)],
                action=None, actions=[action], successors=[])
    meta['campaign_lineage_sha256'] = digest({k:meta[k] for k in P.LINEAGE_FIELDS})
    state['psm'] = {pair:meta}
    source['order']['pool'] = sell['pool']
    source['psm_ownership'] = [P.verify_transition(state, sell, buy, 'sell-fill')]
    return state, source, at


def stage(fixture, **kw):
    state, source, at = fixture
    values = dict(amount='36', at=at, allocation_event_id='allocate-1', cash_backing_id='cash-1', snapshot_id='members-1')
    values.update(kw)
    return P.stage_distribution(state, source, **values)


def position(state):
    meta = state['psm']['BTC-USD']
    return K.ResearchBook.loads(meta['journal']).positions[meta['position']]


def test_legacy_kernel_preserved_exactly():
    assert hashlib.sha256(Path(LEGACY.__file__).read_bytes()).hexdigest() == P.LEGACY_KERNEL_SHA256
    assert P.identity()['kernel_sha256'] == P.KERNEL_SHA256


def test_real_native_sale_partitions_exact_profit_and_preserves_basis(native_source):
    state, source, at = native_source
    before = deepcopy(state)
    updated = stage(native_source)
    assert state == before
    old, new = position(state), position(updated)
    assert old['cash'] == 238 and new['cash'] == 202
    assert new['cumulative_distributions'] == 36
    for key in ('initial_economic_basis', 'initial_units', 'snapshot_basis', 'original_basis',
                'rebuilt_basis', 'released_basis', 'sell_fees', 'settled_sell_cash', 'cycle',
                'cycle_snapshot_ref', 'residual_class', 'phase', 'orders'):
        assert old[key] == new[key]
    assert P.distribution_debits(updated, 'BTC-USD') == {'root|seed': D(36)}
    book = K.ResearchBook.loads(updated['psm']['BTC-USD']['journal'])
    report = book.report(updated['psm']['BTC-USD']['position'], price='100', quote_at=at, now=at, max_age=0)
    assert report['marked_usd'] == report['current_wallet_marked_usd'] == 202
    assert report['source_value_including_distributions'] == 238
    assert report['total_economic_pnl'] == 36
    assert report['realized_sell_pnl'] == 36
    assert new['cash'] + new['cumulative_distributions'] == old['cash']


def test_restart_and_exact_replay_do_not_debit_twice(native_source):
    updated = stage(native_source)
    state, source, at = native_source
    restart = json.loads(json.dumps(updated))
    assert P.stage_distribution(restart, source, '36', at+10, 'allocate-1', 'cash-1', 'members-1') == updated
    assert P.verify_transition(updated, updated['orders']['sell'], updated['orders']['buy'], 'sell-fill') == source['psm_ownership'][0]
    journal = updated['psm']['BTC-USD']['journal']
    book = K.ResearchBook.loads(journal)
    event = book.journal[-1]
    book.apply(event)
    assert book.dumps() == journal


@pytest.mark.parametrize('override', [dict(amount='0'), dict(amount='-1'), dict(amount='35'), dict(amount='37'), dict(amount='202')])
def test_loss_partial_or_principal_refused(native_source, override):
    with pytest.raises(P.Blocked): stage(native_source, **override)


@pytest.mark.parametrize('change', [
    lambda s: s['orders']['sell'].update(pool='psm:ETH-USD'),
    lambda s: s['orders']['sell'].update(kind='PROFIT_EXIT'),
    lambda s: s['orders']['sell'].update(status='PARTIAL'),
    lambda s: s['orders']['sell'].update(proposal='foreign'),
    lambda s: s['orders']['sell'].update(executor='foreign'),
    lambda s: s['orders']['sell'].update(funding_owner='repair'),
    lambda s: s['orders']['sell'].update(eligible_lot_ids=['foreign']),
    lambda s: s['orders']['buy'].update(pool='repair'),
    lambda s: s['psm']['BTC-USD'].update(campaign='foreign'),
    lambda s: s['psm']['BTC-USD'].update(source_sha256='0'*64),
    lambda s: s['psm']['BTC-USD']['actions'][0].update(completed=False),
    lambda s: s['psm']['BTC-USD']['actions'][0].update(episode=2),
    lambda s: s['psm']['BTC-USD']['actions'][0].update(children=[]),
    lambda s: s['psm']['BTC-USD']['original_lineage'][0].update(source_pool='repair'),
    lambda s: s['cohorts']['root'].update(root_cohort_id='foreign'),
    lambda s: s['psm']['BTC-USD']['episode_lineage'][0]['lot'].update(executor_id='foreign'),
])
def test_ownership_tampering_fails_closed(native_source, change):
    state, source, at = native_source
    change(state)
    with pytest.raises(P.Blocked):
        P.verify_transition(state, state['orders']['sell'], state['orders']['buy'], 'sell-fill')


@pytest.mark.parametrize('change', [
    lambda x: x['event'].update(realized_pnl_quote='37'),
    lambda x: x['event'].update(allocated_cost_basis_quote='0'),
    lambda x: x['allocations'][0].update(allocated_cost_basis_quote='0'),
    lambda x: x['allocations'].append(deepcopy(x['allocations'][0])),
    lambda x: x['receipt'].update(applied=0),
    lambda x: x['psm_ownership'][0].update(root_cohort='foreign'),
    lambda x: x.update(psm_ownership=[]),
    lambda x: x.update(allocations=[]),
    lambda x: x['terminal'].update(at=0),
])
def test_native_proof_tampering_refused(native_source, change):
    change(native_source[1])
    with pytest.raises(P.Blocked): stage(native_source)


@pytest.mark.parametrize('field,value', [('allocation_event_id','new'), ('cash_backing_id','new'), ('snapshot_id','new')])
def test_duplicate_proof_under_new_identifier_refused(native_source, field, value):
    updated = stage(native_source)
    with pytest.raises(P.Blocked): stage((updated,native_source[1],native_source[2]), **{field:value})


def test_pending_logical_action_blocks_new_distribution(native_source):
    state, source, at = native_source
    state['psm']['BTC-USD']['action'] = {'order':'other'}
    with pytest.raises(P.Blocked, match='pending'): stage(native_source)


def test_pending_native_action_blocks_new_distribution(native_source):
    state, source, at = native_source
    state['orders']['pending'] = dict(id='pending', pair='BTC-USD', status='OPEN')
    with pytest.raises(P.Blocked, match='pending'): stage(native_source)


def test_later_pending_action_does_not_invalidate_existing_source(native_source):
    state = stage(native_source)
    state['psm']['BTC-USD']['action'] = {'order':'other'}
    state['orders']['pending'] = dict(id='pending', pair='BTC-USD', status='OPEN')
    source, at = native_source[1:]
    assert P.verify_transition(state,state['orders']['sell'],state['orders']['buy'],'sell-fill') == source['psm_ownership'][0]
    assert stage((state,source,at)) == state


def test_insufficient_settled_cash_and_monotonic_time(native_source):
    state, source, at = native_source
    with pytest.raises(P.Blocked): stage(native_source,at=at-11)
    book = K.ResearchBook.loads(state['psm']['BTC-USD']['journal'])
    book.apply(dict(id='close',kind='close_exit',at=at,position=state['psm']['BTC-USD']['position'],owner=state['identity']['config']['owner'],price='100',quote_at=at,max_age=0,step='.001',min_units='.001',min_quote='.5'))
    book.apply(dict(id='signal',kind='rebuild_signal',at=at,position=state['psm']['BTC-USD']['position'],owner=state['identity']['config']['owner'],qualified=True,decision_ref='signal',expires_at=at+1))
    psm_position = state['psm']['BTC-USD']['position']
    owner = state['identity']['config']['owner']
    book.apply(dict(id='buy-intent',kind='intent',at=at,position=psm_position,owner=owner,order='buy-psm',side='buy',price='50',quote_at=at,max_age=0,step='.001',min_units='.001',min_quote='.5',max_fee_rate='0',min_unit_gain='0'))
    book.apply(dict(id='buy-fill',kind='fill',at=at,position=psm_position,owner=owner,order='buy-psm',fill='buy-psm-fill',units='4.76',price='50',fee='0',fee_currency='quote'))
    book.apply(dict(id='buy-done',kind='reconcile',at=at,position=psm_position,owner=owner,order='buy-psm',status='FILLED',complete_fill_history=True,filled_units='4.76',gross='238',fees='0'))
    state['psm']['BTC-USD']['journal'] = book.dumps()
    with pytest.raises(P.Blocked,match='insufficient'): stage(native_source)


def test_schema3_required_for_distribution(native_source):
    result = stage(native_source)
    journal = json.loads(result['psm']['BTC-USD']['journal'])
    assert journal['schema'] == 3
    journal['schema'] = 2
    with pytest.raises(K.Blocked,match='schema 3'): K.ResearchBook.loads(json.dumps(journal))
    legacy = json.loads(native_source[0]['psm']['BTC-USD']['journal'])
    legacy['schema'] = 2
    assert K.ResearchBook.loads(json.dumps(legacy)).positions == position_book(native_source[0]).positions


def position_book(state):
    return K.ResearchBook.loads(state['psm']['BTC-USD']['journal'])


def test_new_episode_keeps_original_source_proof_stable(native_source):
    state = stage(native_source)
    source, at = native_source[1:]
    meta = state['psm']['BTC-USD']
    archived = dict(episode=1,position=meta['position'],journal=meta['journal'],
                    journal_sha256=hashlib.sha256(meta['journal'].encode()).hexdigest(),
                    source_sha256=meta['source_sha256'],handoff_lineage=deepcopy(meta['episode_lineage']),
                    handoff_cohorts=deepcopy(meta['episode_cohorts']))
    meta['archived_episodes'].append(archived)
    meta['episode'] = 2
    meta['position'] = meta['campaign'] + ':episode:2'
    book = K.ResearchBook()
    book.apply(dict(id='new-snapshot',kind='snapshot',at=at,position=meta['position'],owner=state['identity']['config']['owner'],account=state['identity']['config']['account'],asset='BTC/USD',provenance='new',inventory_bucket='ACTIVE_TRADE',reconciled=True,pending_orders=[],units='3',basis='202'))
    meta['journal'] = book.dumps()
    assert P.verify_transition(state,state['orders']['sell'],state['orders']['buy'],'sell-fill') == source['psm_ownership'][0]
    assert stage((state,source,at)) == state
    assert P.distribution_debits(state,'BTC-USD') == {'root|seed':D(36)}


def test_legacy_accounting_matches_without_distribution(native_source):
    raw = json.loads(native_source[0]['psm']['BTC-USD']['journal'])
    raw['schema'] = 2
    old, new = LEGACY.ResearchBook.loads(json.dumps(raw)), K.ResearchBook.loads(json.dumps(raw))
    position_id = native_source[0]['psm']['BTC-USD']['position']
    kwargs = dict(price='100',quote_at=native_source[2],now=native_source[2],max_age=0)
    original, updated = old.report(position_id,**kwargs), new.report(position_id,**kwargs)
    assert all(updated[k] == v for k,v in original.items())


def test_reducer_duplicate_native_proof_cannot_use_another_event_id(native_source):
    state = stage(native_source)
    book = position_book(state)
    event = dict(book.journal[-1], id='another-distribution', cash_before='202', cash_after='166')
    before = book.dumps()
    with pytest.raises(K.Blocked,match='duplicate'): book.apply(event)
    assert book.dumps() == before


def test_rebuild_unit_gain_rule_unchanged_and_no_incoming_cash_event(native_source):
    state = stage(native_source)
    meta, at = state['psm']['BTC-USD'], native_source[2]
    book = position_book(state)
    common = dict(position=meta['position'],owner=state['identity']['config']['owner'],at=at)
    quote = dict(price='115',quote_at=at,max_age=0,step='.001',min_units='.001',min_quote='.5')
    book.apply(dict(common,id='closed',kind='close_exit',**quote))
    book.apply(dict(common,id='signal',kind='rebuild_signal',qualified=True,decision_ref='signal',expires_at=at))
    before = book.dumps()
    with pytest.raises(K.Blocked,match='minimum unit gain'):
        book.apply(dict(common,id='buy',kind='intent',side='buy',order='buy',max_fee_rate='0',min_unit_gain='0',**quote))
    with pytest.raises(K.Blocked,match='unknown event'):
        book.apply(dict(common,id='inject',kind='cash_injection',amount='100'))
    assert book.dumps() == before


def test_same_pair_cash_is_reserved_from_other_pending_actions(native_source):
    state, source, at = native_source
    book = position_book(state)
    meta=state['psm']['BTC-USD']
    common=dict(position=meta['position'],owner=state['identity']['config']['owner'],at=at)
    quote=dict(price='50',quote_at=at,max_age=0,step='.001',min_units='.001',min_quote='.5')
    book.apply(dict(common,id='closed',kind='close_exit',**quote))
    book.apply(dict(common,id='signal',kind='rebuild_signal',qualified=True,decision_ref='signal',expires_at=at))
    book.apply(dict(common,id='buy',kind='intent',side='buy',order='buy',max_fee_rate='0',min_unit_gain='0',**quote))
    meta['journal']=book.dumps()
    with pytest.raises(P.Blocked,match='pending'): stage(native_source)


def test_missing_action_owner_refused(native_source):
    state,source,at=native_source
    state['psm']['BTC-USD']['actions'][0]['owners']=[]
    with pytest.raises(P.Blocked,match='owner'): stage(native_source)


def test_native_lifetime_original_basis_cannot_be_reset(native_source):
    native_source[1]['lots'][0]['cost_basis_original_quote']='0'
    with pytest.raises(P.Blocked,match='original lot changed'): stage(native_source)


def test_source_proof_unchanged_by_json_restart(native_source):
    state,source,at=native_source
    restored=json.loads(json.dumps(state))
    assert P.verify_transition(restored,restored['orders']['sell'],restored['orders']['buy'],'sell-fill') == source['psm_ownership'][0]


def test_current_archived_overlap_counts_distribution_once(native_source):
    state=stage(native_source)
    meta=state['psm']['BTC-USD']
    meta['archived_episodes']=[dict(position=meta['position'],episode=1,journal=meta['journal'],journal_sha256=hashlib.sha256(meta['journal'].encode()).hexdigest(),source_sha256=meta['source_sha256'],handoff_lineage=deepcopy(meta['episode_lineage']),handoff_cohorts=deepcopy(meta['episode_cohorts']))]
    meta['mode']='REPAIR'
    assert P.distribution_debits(state,'BTC-USD') == {'root|seed':D(36)}
    assert stage((state,native_source[1],native_source[2])) == state


def test_source_acquisition_order_tampering_is_rejected(native_source):
    native_source[1]['acquisitions'][0]['order']['quantity']='999'
    with pytest.raises(P.Blocked,match='persisted binding'): stage(native_source)


def test_debit_projection_rejects_bad_archive_identity(native_source):
    state=stage(native_source)
    meta=state['psm']['BTC-USD']
    meta['archived_episodes']=[dict(position='old',journal=meta['journal'],journal_sha256='0'*64,source_sha256=P.KERNEL_SHA256)]
    with pytest.raises(P.Blocked,match='archived'): P.distribution_debits(state,'BTC-USD')


@pytest.mark.parametrize('native_source',['80','102'],indirect=True)
def test_actual_native_loss_or_break_even_has_no_distributable_profit(native_source):
    source=native_source[1]
    assert D(source['event']['realized_pnl_quote']) <= 0
    before=deepcopy(native_source[0])
    with pytest.raises(P.Blocked,match='positive native'): stage(native_source)
    assert native_source[0] == before


def test_distribution_preserves_repeated_cycle_lifetime_basis_and_claim(native_source):
    state=stage(native_source)
    meta,at=state['psm']['BTC-USD'],native_source[2]
    book=position_book(state)
    common=dict(position=meta['position'],owner=state['identity']['config']['owner'],at=at)
    quote=dict(price='50',quote_at=at,max_age=0,step='.001',min_units='.001',min_quote='.5')
    book.apply(dict(common,id='closed',kind='close_exit',**quote))
    book.apply(dict(common,id='signal',kind='rebuild_signal',qualified=True,decision_ref='signal',expires_at=at))
    book.apply(dict(common,id='buy',kind='intent',side='buy',order='buy',max_fee_rate='0',min_unit_gain='0',**quote))
    book.apply(dict(common,id='bought',kind='fill',order='buy',fill='rebuild',units='4.04',price='50',fee='0',fee_currency='quote'))
    book.apply(dict(common,id='settled',kind='reconcile',order='buy',status='FILLED',complete_fill_history=True,filled_units='4.04',gross='202',fees='0'))
    book.apply(dict(common,id='complete',kind='complete'))
    p=book.positions[meta['position']]
    assert p['cycle_history'][0]['accounting_delta']['cumulative_distributions'] == 36
    book.apply(dict(common,id='cycle2',kind='next_cycle',reconciled=True,pending_orders=[],inventory_bucket='ACTIVE_TRADE',confirmed_units='4.04',confirmed_cash='0',decision_ref='new-cycle'))
    p=book.positions[meta['position']]
    assert p['cycle']==2 and p['initial_units']==2 and p['initial_economic_basis']==202
    assert p['snapshot_units']==D('4.04') and p['snapshot_basis']==202 and p['cumulative_distributions']==36
    assert book._claims[(state['identity']['config']['account'],'BTC/USD')] == meta['position']
    assert K.ResearchBook.loads(book.dumps()).positions == book.positions


def dedicated_source(native_source, *, retained=False, repair=False):
    """Pure binding fixture only; V2 tests own real dedicated-funding DB evidence."""
    state, source, at = deepcopy(native_source)
    acquired, sale = state['orders']['buy'], state['orders']['sell']
    pool = 'next_entry:predecessor' if retained else 'next_entry:buy'
    owner, kind, intent = ('repair', 'REPAIR', 'RECOVERY') if repair else ('seed', 'REENTRY', 'ENTRY')
    acquired.update(pool=pool, source_pool=pool if retained else owner, entry_id='buy',
                    kind=kind, intent=intent, funding_owner=owner, cohort_generation=2,
                    previous_cohort_generation=1 if not repair else None,
                    activation=dict(proof_sha256='a'*64))
    sale.update(intent=intent, funding_owner=owner)
    meta = state['psm']['BTC-USD']
    meta['actions'][0]['owners'][0]['intent'] = intent
    state['cohorts']['root']['generation'] = 2
    for records in (meta['original_cohorts'], meta['episode_cohorts']):
        records[0]['generation'] = 2
    for records in (meta['original_lineage'], meta['episode_lineage']):
        for item in records:
            item['source_pool'] = pool
    meta['campaign_lineage_sha256'] = digest({k: meta[k] for k in P.LINEAGE_FIELDS})
    common = dict(schema=2, order_id='buy', pair=acquired['pair'], cohort=acquired['cohort'],
                  cohort_generation=2, funding_owner=owner, entry_pool=pool)
    if retained:
        base = dict(schema=1, order_id='buy', entry_id='buy', source_pool=pool, entry_pool=pool,
                    base_entry=dict(approved_profit_increment='0'), evidence=dict(order=deepcopy(acquired)))
        base['proof_sha256'] = digest(base)
        acquired['base_entry_proof'] = base
        acquired['activation']['proof_sha256'] = base['proof_sha256']
        prior = dict(common, kind='CLOSED_SAME_ROOT_ENTRY_POOL', order_id='predecessor',
                     entry_id='predecessor', cohort_generation=1, source_pool='seed',
                     admission_sha256='b'*64, acquisition_source_sha256='c'*64,
                     terminal_sha256='d'*64, cash_return_sha256='e'*64)
        lineage = dict(common, kind='RETAINED_ROOT_BASE_ENTRY', root_entry_id='predecessor',
                       base_entry_proof_sha256=base['proof_sha256'], root_admission_sha256='b'*64,
                       predecessors=[dict(trade_id='prior-sale', source_digest='f'*64, funding=prior)])
    else:
        lineage = dict(common, kind='CLOSED_SAME_ROOT_ENTRY_POOL', entry_id='buy', source_pool=owner,
                       admission_sha256='a'*64, acquisition_source_sha256='c'*64,
                       terminal_sha256='d'*64, cash_return_sha256='e'*64)
    evidence = dict(schema=1, kind='VERIFIED_DEDICATED_ACQUISITION', acquisition_order_id='buy',
                    entry_pool=pool, pair=acquired['pair'], cohort=acquired['cohort'], cohort_generation=2,
                    funding_owner=owner, native_position=acquired['position'], proposal_id=acquired['proposal'],
                    executor_id=acquired['executor'], activation_sha256=acquired['activation']['proof_sha256'],
                    lineage=lineage)
    rehash_funding(evidence)
    proof = P.verify_transition(state, sale, acquired, 'sell-fill', acquisition_funding=evidence)
    source['order'] = deepcopy(sale)
    source['psm_ownership'] = [proof]
    for acquisition in source['acquisitions']:
        acquisition['order'] = deepcopy(acquired)
        acquisition['pool_transition'] = deepcopy(proof)
    return (state, source, at), evidence


def rehash_funding(evidence):
    evidence['lineage_sha256'] = digest(evidence['lineage'])
    evidence['proof_sha256'] = digest({k: v for k, v in evidence.items() if k != 'proof_sha256'})


def verify_funding(fixture, evidence):
    state = fixture[0]
    return P.verify_transition(state, state['orders']['sell'], state['orders']['buy'],
                               'sell-fill', acquisition_funding=evidence)


@pytest.mark.parametrize('variant', [{}, {'repair': True}, {'retained': True}])
def test_verified_dedicated_lineage_partitions_and_replays(native_source, variant):
    fixture, evidence = dedicated_source(native_source, **variant)
    state, source, at = fixture
    proof = verify_funding(fixture, evidence)
    assert proof['acquisition_funding'] == evidence
    assert proof['original_source_pool'] == evidence['entry_pool']
    before = deepcopy(fixture)
    result = stage(fixture)
    assert fixture == before
    assert position(result)['cash'] == 202
    assert P.distribution_debits(result, 'BTC-USD') == {'root|' + evidence['funding_owner']: D(36)}
    restarted = json.loads(json.dumps(result))
    assert stage((restarted, source, at + 1)) == result
    assert verify_funding((restarted, source, at), evidence) == proof
    evidence['lineage']['pair'] = 'ETH-USD'
    assert proof['acquisition_funding']['lineage']['pair'] == 'BTC-USD'


@pytest.mark.parametrize('evidence', [None, True, False, {}, [], 'verified'])
def test_dedicated_pool_requires_explicit_structured_evidence(native_source, evidence):
    fixture, _ = dedicated_source(native_source)
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('pool', ['seed', 'repair', 'psm:BTC-USD'])
def test_legacy_pools_reject_unexpected_dedicated_evidence(native_source, pool):
    state = native_source[0]
    state['orders']['buy']['pool'] = pool
    with pytest.raises(P.Blocked, match='Unexpected dedicated'):
        verify_funding(native_source, {'verified': True})


@pytest.mark.parametrize('field,value', [
    ('schema', True), ('schema', 2), ('kind', 'TRUSTED'),
    ('acquisition_order_id', 'foreign'), ('entry_pool', 'next_entry:foreign'),
    ('pair', 'ETH-USD'), ('cohort', 'foreign'), ('cohort_generation', 3),
    ('cohort_generation', True), ('funding_owner', 'repair'),
    ('native_position', 'foreign'), ('proposal_id', 'foreign'), ('executor_id', 'foreign'),
    ('activation_sha256', 'b'*64),
])
def test_resigned_foreign_dedicated_envelope_rejected(native_source, field, value):
    fixture, evidence = dedicated_source(native_source)
    evidence[field] = value
    rehash_funding(evidence)
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('field,value', [
    ('schema', True), ('kind', 'CALLER_VERIFIED'), ('entry_id', 'foreign'), ('order_id', 'foreign'),
    ('pair', 'ETH-USD'), ('cohort', 'foreign'), ('cohort_generation', 3),
    ('funding_owner', 'repair'), ('entry_pool', 'next_entry:foreign'), ('source_pool', 'repair'),
    ('admission_sha256', 'b'*64), ('acquisition_source_sha256', ''),
    ('terminal_sha256', None), ('cash_return_sha256', True),
])
def test_resigned_foreign_direct_lineage_rejected(native_source, field, value):
    fixture, evidence = dedicated_source(native_source)
    evidence['lineage'][field] = value
    rehash_funding(evidence)
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('change', [
    lambda x: x.pop('lineage'),
    lambda x: x.pop('activation_sha256'),
    lambda x: x.update(verified=True),
    lambda x: x.update(proof_sha256='0'*64),
    lambda x: x.update(lineage_sha256='0'*64),
    lambda x: x['lineage'].update(cash_return_sha256='0'*64),
])
def test_malformed_or_changed_funding_evidence_rejected(native_source, change):
    fixture, evidence = dedicated_source(native_source)
    change(evidence)
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('change', [
    lambda s: s['orders']['buy'].update(kind='SEED'),
    lambda s: s['orders']['buy'].update(intent='RECOVERY'),
    lambda s: s['orders']['buy'].update(status='OPEN'),
    lambda s: s['orders']['buy'].update(entry_id='foreign'),
    lambda s: s['orders']['buy'].update(source_pool='repair'),
    lambda s: s['orders']['buy'].pop('activation'),
    lambda s: s['orders']['buy']['activation'].update(proof_sha256='f'*64),
    lambda s: s['orders']['sell'].update(cohort_generation=3),
    lambda s: s['psm']['BTC-USD']['episode_cohorts'][0].update(generation=3),
    lambda s: s['psm']['BTC-USD']['episode_cohorts'][0].update(pair='ETH-USD'),
    lambda s: s['psm']['BTC-USD']['episode_lineage'][0].update(source_pool='next_entry:foreign'),
])
def test_dedicated_persisted_order_and_handoff_binding_rejected(native_source, change):
    fixture, evidence = dedicated_source(native_source)
    change(fixture[0])
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('field,value', [
    ('root_entry_id', 'foreign'), ('base_entry_proof_sha256', 'a'*64),
    ('root_admission_sha256', True), ('predecessors', []),
    ('predecessors', [True]),
])
def test_retained_lineage_rejects_missing_or_foreign_root(native_source, field, value):
    fixture, evidence = dedicated_source(native_source, retained=True)
    evidence['lineage'][field] = value
    rehash_funding(evidence)
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('change', [
    lambda p: p.update(trade_id='sell-fill'),
    lambda p: p.update(source_digest='bad'),
    lambda p: p['funding'].update(pair='ETH-USD'),
    lambda p: p['funding'].update(entry_pool='next_entry:foreign'),
    lambda p: p['funding'].update(cohort='foreign'),
    lambda p: p['funding'].update(funding_owner='repair'),
    lambda p: p['funding'].update(cohort_generation=2),
])
def test_retained_predecessors_cannot_be_foreign_or_self_replayed(native_source, change):
    fixture, evidence = dedicated_source(native_source, retained=True)
    change(evidence['lineage']['predecessors'][0])
    rehash_funding(evidence)
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


def test_retained_duplicate_predecessor_refused(native_source):
    fixture, evidence = dedicated_source(native_source, retained=True)
    evidence['lineage']['predecessors'] *= 2
    rehash_funding(evidence)
    with pytest.raises(P.Blocked, match='Replayed'):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('change', [
    lambda b: b.update(proof_sha256='0'*64),
    lambda b: b.update(source_pool='seed'),
    lambda b: b['base_entry'].update(approved_profit_increment='1'),
    lambda b: b['evidence']['order'].update(executor='foreign'),
])
def test_retained_actual_base_proof_cannot_change(native_source, change):
    fixture, evidence = dedicated_source(native_source, retained=True)
    change(fixture[0]['orders']['buy']['base_entry_proof'])
    with pytest.raises(P.Blocked):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('change', [
    lambda a: a.pop('pool_transition'),
    lambda a: a.update(pool_transition=True),
    lambda a: a['pool_transition'].pop('acquisition_funding'),
    lambda a: a['pool_transition'].update(root_cohort='foreign'),
    lambda a: a['pool_transition']['acquisition_funding'].update(proof_sha256='0'*64),
])
def test_staging_rederives_exact_dedicated_transition(native_source, change):
    fixture, _ = dedicated_source(native_source)
    change(fixture[1]['acquisitions'][0])
    with pytest.raises(P.Blocked):
        stage(fixture)


def test_staging_rejects_dedicated_proof_substitution_after_distribution(native_source):
    fixture, _ = dedicated_source(native_source)
    updated = stage(fixture)
    source = fixture[1]
    # A consistent new envelope cannot replace the bytes frozen by a distribution.
    for acquisition in source['acquisitions']:
        evidence = acquisition['pool_transition']['acquisition_funding']
        evidence['lineage']['cash_return_sha256'] = 'f'*64
        rehash_funding(evidence)
        proof = verify_funding((updated, source, fixture[2]), evidence)
        acquisition['pool_transition'] = proof
    source['psm_ownership'] = [proof]
    with pytest.raises(P.Blocked, match='Conflicting/reused'):
        stage((updated, source, fixture[2]))


def test_legacy_proof_has_no_dedicated_envelope(native_source):
    assert 'acquisition_funding' not in native_source[1]['psm_ownership'][0]


def test_arbitrary_dedicated_pool_cannot_reuse_a_valid_envelope(native_source):
    fixture, evidence = dedicated_source(native_source)
    fixture[0]['orders']['buy']['pool'] = 'next_entry:arbitrary'
    evidence['entry_pool'] = evidence['lineage']['entry_pool'] = 'next_entry:arbitrary'
    rehash_funding(evidence)
    with pytest.raises(P.Blocked, match='pool/root entry'):
        verify_funding(fixture, evidence)


@pytest.mark.parametrize('variant', [{}, {'repair': True}, {'retained': True}])
def test_dedicated_proof_survives_later_generation_and_episode(native_source, variant):
    fixture, evidence = dedicated_source(native_source, **variant)
    state = stage(fixture)
    source, at = fixture[1:]
    meta = state['psm']['BTC-USD']
    meta['archived_episodes'].append(dict(episode=1, position=meta['position'], journal=meta['journal'],
        journal_sha256=hashlib.sha256(meta['journal'].encode()).hexdigest(), source_sha256=P.KERNEL_SHA256,
        handoff_lineage=deepcopy(meta['episode_lineage']), handoff_cohorts=deepcopy(meta['episode_cohorts'])))
    state['cohorts']['root']['generation'] = 3
    meta['episode'], meta['position'] = 2, meta['campaign'] + ':episode:2'
    meta['episode_cohorts'][0]['generation'] = 3
    book = K.ResearchBook()
    book.apply(dict(id='later-snapshot', kind='snapshot', at=at, position=meta['position'],
        owner=state['identity']['config']['owner'], account=state['identity']['config']['account'],
        asset='BTC/USD', provenance='later', inventory_bucket='ACTIVE_TRADE', reconciled=True,
        pending_orders=[], units='3', basis='202'))
    meta['journal'] = book.dumps()
    assert verify_funding((state, source, at), evidence) == source['psm_ownership'][0]
    assert stage((state, source, at)) == state
