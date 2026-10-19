"""Pure archived PSM acquisition bindings for post-handback ordinary cash.

No database, cash authority or mutation lives here. Native evidence is separately
reverified by NativePSMRootFundingBridge; archived reducer bytes never change.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from . import psm_profit_partition as P
from . import psm_kernel as K


def identity():
    return {'schema': 1, 'partition': P.identity(),
            'helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _check(condition, reason):
    P._check(condition, reason)


def archive(state, pair, position_id):
    pinned = identity()
    meta = state['psm'][pair]
    config = state['identity']['config']
    campaign = f"{config['owner']}:{config['account']}:{pair}:psm"
    _check(meta['campaign'] == campaign and meta['source_sha256'] == pinned['partition']['kernel_sha256']
           and meta['campaign_lineage_sha256'] == P._digest({k: meta[k] for k in P.LINEAGE_FIELDS}),
           'Archived root campaign ownership changed')
    records = [a for a in meta['archived_episodes'] if a['position'] == position_id]
    _check(len(records) == 1, 'Missing unique complete PSM handback archive')
    record = records[0]
    _check(record['source_sha256'] == pinned['partition']['kernel_sha256']
           and hashlib.sha256(record['journal'].encode()).hexdigest() == record['journal_sha256']
           and position_id == campaign + ':episode:' + str(record['episode']),
           'Archived root episode identity changed')
    if meta['position'] == position_id:
        _check(meta['journal'] == record['journal'], 'Archived/current root journal differs')
    book = K.ResearchBook.loads(record['journal'])
    position = book.positions[position_id]
    _check(position['phase'] == 'COMPLETE' and not position['quarantine']
           and position['pending'] is None and position['account'] == config['account']
           and position['owner'] == config['owner'] and position['asset'] == pair.replace('-', '/')
           and all(o['status'] != 'PENDING' for o in position['orders'].values()),
           'Archived PSM is not an exact completed owned episode')
    return deepcopy(record), book


def acquisition(state, acquired):
    """Prove one actual archived rebuild child's immutable root and generation."""
    actual = P._same_order(state, acquired)
    pair = actual['pair']
    funding = P._funding(actual)
    _check(actual['kind'] == 'PSM_REBUILD' and actual['side'] == 'BUY'
           and actual['pool'] == 'psm:' + pair and actual['funding_owner'] == funding
           and actual['intent'] == ('ENTRY' if funding == 'seed' else 'RECOVERY')
           and actual['status'] == actual['terminal'] and actual['terminal'] in {'FILLED', 'CANCELED'},
           'Only an actually terminal PSM rebuild acquisition qualifies')
    record, book = archive(state, pair, actual['psm_position'])
    meta = state['psm'][pair]
    actions = [a for a in meta['actions'] if a['order'] == actual['psm_order']]
    _check(len(actions) == 1, 'Missing unique archived rebuild action')
    action = actions[0]
    _check(action['episode'] == record['episode'] and action['side'] == 'buy'
           and action['completed'] is True and action['sealed'] is True
           and action['terminal'] in {'FILLED', 'CANCELED'}
           and actual['id'] in action['children']
           and len(action['children']) == len(set(action['children'])),
           'Archived rebuild action is not terminal or exact')
    allocations = [a for a in action['allocations'] if a['child'] == actual['id']]
    _check(len(allocations) == 1, 'Missing unique original root allocation')
    allocation = allocations[0]
    cohort = state['cohorts'][actual['cohort']]
    root = cohort.get('root_cohort_id', cohort['id'])
    original = state['cohorts'][root]
    _check(original['origin'] == 'SEED' and original['pair'] == pair
           and original['id'] == f"{pair}:seed:{original['ordinal']:02d}"
           and allocation['root'] == actual['psm_root_cohort'] == root
           and allocation['cohort'] == actual['cohort'] and allocation['funding_owner'] == funding
           and allocation['state'] == 'BOUND' and action['root_cohorts'][root] == actual['cohort']
           and P._money(allocation['quantity']) == P._money(actual['decision_quantity'])
           and P._money(allocation['target']) == P._money(actual['target']),
           'Archived rebuild root/funding allocation changed')
    for child_id in action['children']:
        child = state['orders'][child_id]
        _check(child['kind'] == 'PSM_REBUILD' and child['side'] == 'BUY' and child['pair'] == pair
               and child['pool'] == actual['pool'] and child['psm_order'] == action['order']
               and child['psm_position'] == record['position']
               and child['status'] == child['terminal'] and child['terminal'] in P.TERMINAL,
               'Archived rebuild sibling unresolved or changed')
    journal_order = book.positions[record['position']]['orders'][action['order']]
    reconciles = [e for e in book.journal if e['kind'] == 'reconcile' and e['order'] == action['order']]
    fills = [e for e in book.journal if e['kind'] == 'fill' and e['order'] == action['order']]
    _check(journal_order['side'] == 'buy' and journal_order['status'] == action['terminal']
           and journal_order['quantity'] == P._money(action['quantity'])
           and len(reconciles) == 1 and reconciles[0]['status'] == action['terminal']
           and reconciles[0]['complete_fill_history'] is True and fills
           and P._time(reconciles[0]['at']) <= P._time(record['completed_at']),
           'Archived rebuild native fill/reconciliation journal missing')
    anchors = [a for a in cohort['anchor_history'] if actual['id'] in a.get('receipt_orders', [])]
    _check(len(anchors) == 1 and type(anchors[0]['generation']) is int and anchors[0]['generation'] > 1
           and P._time(anchors[0]['acquired_at']) <= P._time(anchors[0]['at']) <= P._time(record['completed_at']),
           'Archived rebuild has no exact successor generation anchor')
    anchor = anchors[0]
    _check(actual['cohort_generation'] in {anchor['generation'], anchor['generation'] - 1},
           'Rebuild decision/successor generation mismatch')
    proof = {'schema': 1, 'kind': 'ARCHIVED_PSM_REBUILD_ACQUISITION', 'identity': identity(),
        'pair': pair, 'root_cohort_id': root, 'funding_owner': funding, 'source_pool': actual['pool'],
        'acquisition_order_id': actual['id'], 'cohort_id': actual['cohort'],
        'acquired_generation': anchor['generation'], 'proposal_id': actual['proposal'],
        'executor_id': actual['executor'], 'native_position': actual['position'],
        'archive': record, 'action': deepcopy(action), 'allocation': deepcopy(allocation),
        'generation_anchor': deepcopy(anchor), 'fill_events': deepcopy(fills),
        'reconcile_event': deepcopy(reconciles[0])}
    proof['proof_sha256'] = P._digest(proof)
    return proof


def verify_sale(state, sale_order, acquired_order, trade_id):
    try:
        sale = P._same_order(state, sale_order)
        acquired = P._same_order(state, acquired_order)
        proof = acquisition(state, acquired)
        _check(sale['kind'] == 'COHORT_EXIT' and sale['side'] == 'SELL'
               and sale['status'] == sale['terminal'] and sale['terminal'] in {'FILLED', 'CANCELED'}
               and sale['pool'] == acquired['pool'] and sale['funding_owner'] == proof['funding_owner']
               and sale['intent'] == acquired['intent'] and sale['cohort_generation'] == proof['acquired_generation']
               and P._time(sale['decision_at']) >= P._time(proof['archive']['completed_at']),
               'Ordinary successor sale is not a terminal post-handback disposal')
        for key in ('pair', 'position', 'proposal', 'executor', 'cohort'):
            _check(sale[key] == acquired[key], 'Ordinary successor source owner changed: ' + key)
        _check(sale['eligible_lot_ids'] and len(set(sale['eligible_lot_ids'])) == len(sale['eligible_lot_ids']),
               'Ordinary successor sale lot scope missing or duplicated')
        result = {'schema': 1, 'kind': 'ARCHIVED_PSM_ROOT_SALE', 'acquisition': proof,
                  'sale_order_id': sale['id'], 'trade_id': P._id(trade_id),
                  'native_realization_id': 'trade-fill:' + trade_id,
                  'root_cohort_id': proof['root_cohort_id'], 'funding_owner': proof['funding_owner'],
                  'pair': proof['pair'], 'source_pool': proof['source_pool'],
                  'eligible_lot_ids': sorted(sale['eligible_lot_ids'])}
        result['proof_sha256'] = P._digest(result)
        return result
    except K.Blocked:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise K.Blocked('Malformed archived successor ownership: ' + str(exc)) from exc


def verify_acquisition_receipt(proof, acquisition_source):
    """Cross-check the actual native BUY receipt against immutable reducer fill."""
    import json
    from datetime import datetime
    raw = json.loads(acquisition_source['receipt']['payload'])
    acquired = proof['acquisition'] if proof['kind'] == 'ARCHIVED_PSM_ROOT_SALE' else proof
    fills = [e for e in acquired['fill_events'] if e['fill'] == raw['trade_id']]
    _check(len(fills) == 1 and raw['order_id'] == acquired['acquisition_order_id'],
           'Native acquisition absent from archived rebuild fill history')
    fill = fills[0]
    _check(P._money(fill['units']) == P._money(raw['amount'])
           and P._money(fill['price']) == P._money(raw['price'])
           and P._money(fill['fee']) == P._money(raw['fee_paid'])
           and fill['fee_currency'] == 'quote'
           and P._time(fill.get('occurred_at', fill['at'])) == int(datetime.fromisoformat(raw['timestamp']).timestamp()),
           'Actual native acquisition economics differ from archived rebuild fill')
