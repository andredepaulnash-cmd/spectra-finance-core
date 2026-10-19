"""Isolated root-cash extension of the preserved V2.1 partition verifier.

Pure, research-only bridge from proven native PSM profit to outward escrow.

This module never opens a database or issues an order. The native profit bridge
must independently verify receipt/disposal economics, then commit the returned
state WITH its balanced cash legs and immutable outbox in one paper transaction.
A proof identifies an immutable completed source episode, never mutable current
cash or a current journal digest. Caller booleans confer no ownership authority.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path

from . import psm_kernel as K

Blocked = K.Blocked
LEGACY_KERNEL_SHA256 = 'f5e9ffd8c456cd338bdcc361a74b138d2e9776992dc15eb5dfb60a35c85150c2'
KERNEL_SHA256 = '7e53ef0f2da3229a5fb5158655d090a14c2073b608ebb654f603b92acd7f0585'
ESCROW_POOL = 'next_entry_profit_escrow'
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED'}
PENDING = {'PREPARING', 'OPEN', 'PARTIAL', 'CANCEL_REQUESTED'}
LINEAGE_FIELDS = ('original_lineage', 'original_cohorts', 'initial_native_positions',
                  'initial_native_realized', 'original_basis', 'original_units')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False,
                      default=lambda x: str(x) if isinstance(x, Decimal) else _unsupported(x))


def _unsupported(value):
    raise Blocked('Unsupported proof value: ' + type(value).__name__)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _check(condition, reason):
    K.require(condition, reason)


def _id(value):
    _check(isinstance(value, str) and 0 < len(value) <= 512 and value == value.strip(),
           'Invalid partition proof identifier')
    return value


def _time(value):
    _check(not isinstance(value, bool) and isinstance(value, (int, float)) and
           value >= 0 and value == int(value), 'Exact nonnegative whole time required')
    return int(value)


def _money(value):
    _check(isinstance(value, (str, Decimal, int)) and not isinstance(value, bool),
           'Exact partition amount required')
    return K.dec(str(value))


def identity():
    """Pin these exact identities in the native bridge's persisted identity."""
    root = Path(__file__).resolve().parent
    kernel = hashlib.sha256((root / 'psm_kernel.py').read_bytes()).hexdigest()
    legacy = hashlib.sha256((root / 'psm_kernel_legacy.py').read_bytes()).hexdigest()
    _check(kernel == KERNEL_SHA256 and legacy == LEGACY_KERNEL_SHA256,
           'PSM partition kernel identity mismatch')
    return dict(schema=3, kernel_sha256=kernel, legacy_kernel_sha256=legacy,
                helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def _funding(order):
    explicit = order.get('funding_owner')
    inferred = ('repair' if order.get('kind') == 'REPAIR' or order.get('intent') == 'RECOVERY'
                else 'seed' if order.get('kind') == 'SEED' or order.get('intent') == 'ENTRY' else None)
    _check(explicit in (None, 'seed', 'repair'), 'Invalid immutable funding owner')
    _check(explicit or inferred, 'Missing immutable funding owner')
    _check(not explicit or not inferred or explicit == inferred, 'Conflicting immutable funding owner')
    return explicit or inferred


def _same_order(state, candidate):
    actual = state['orders'][candidate['id']]
    _check(all(actual.get(k) == v for k, v in candidate.items()), 'Order differs from persisted binding')
    return actual


def _episode(meta, position):
    archived = [x for x in meta['archived_episodes'] if x['position'] == position]
    _check(len(archived) <= 1, 'Duplicate archived PSM episode')
    if position == meta['position']:
        if archived:
            _check(archived[0]['journal'] == meta['journal'], 'Current/archive journal mismatch')
        return dict(episode=meta['episode'], position=meta['position'], journal=meta['journal'],
                    source_sha256=meta['source_sha256'], handoff_lineage=meta['episode_lineage'],
                    handoff_cohorts=meta['episode_cohorts'])
    _check(len(archived) == 1, 'Unknown PSM source episode')
    return archived[0]


def _sha256(value):
    _check(isinstance(value, str) and len(value) == 64 and
           all(c in '0123456789abcdef' for c in value), 'Invalid dedicated proof digest')
    return value


def _generation(value):
    _check(type(value) is int and value > 0, 'Invalid dedicated cohort generation')
    return value


def _root_source_owner(acquired, handoff, source):
    _check(isinstance(source,dict) and set(source)=={'schema','pair','root_cohort_id','funding_owner',
           'source_pool','latest_generation','ownership_proof_sha256'} and type(source['schema']) is int and source['schema']==1
           and source==acquired['source_owner'] and source['pair']==acquired['pair']
           and source['funding_owner']==acquired['funding_owner']=='seed'
           and source['source_pool']==acquired['source_pool']=='psm:'+acquired['pair']
           and source['root_cohort_id']==handoff.get('root_cohort_id',handoff['id'])
           and _generation(source['latest_generation'])+1==_generation(acquired['cohort_generation']),
           'Dedicated PSM source lost exact previous root/seed ownership')
    _sha256(source['ownership_proof_sha256'])


def _dedicated_acquisition(acquired, sale, handoff, funding, evidence, trade_id):
    """Bind V2's DB-verified evidence; this pure helper cannot verify those DBs."""
    fields = {'schema', 'kind', 'acquisition_order_id', 'entry_pool', 'pair', 'cohort',
              'cohort_generation', 'funding_owner', 'native_position', 'proposal_id',
              'executor_id', 'activation_sha256', 'lineage', 'lineage_sha256', 'proof_sha256'}
    _check(isinstance(evidence, dict) and set(evidence) == fields and
           type(evidence['schema']) is int and evidence['schema'] == 1 and
           evidence['kind'] == 'VERIFIED_DEDICATED_ACQUISITION',
           'Missing or malformed dedicated acquisition funding proof')
    unsigned = {k: v for k, v in evidence.items() if k != 'proof_sha256'}
    _check(_sha256(evidence['proof_sha256']) == _digest(unsigned),
           'Dedicated acquisition funding proof changed')
    bindings = {'acquisition_order_id': 'id', 'entry_pool': 'pool', 'pair': 'pair',
                'cohort': 'cohort', 'cohort_generation': 'cohort_generation',
                'funding_owner': 'funding_owner', 'native_position': 'position',
                'proposal_id': 'proposal', 'executor_id': 'executor'}
    _check(all(evidence[key] == acquired[field] for key, field in bindings.items()),
           'Dedicated acquisition funding ownership mismatch')
    generation = _generation(acquired['cohort_generation'])
    _check(_generation(evidence['cohort_generation']) == _generation(handoff['generation']) == generation
           and ('cohort_generation' not in sale or _generation(sale['cohort_generation']) == generation)
           and handoff['pair'] == acquired['pair'], 'Dedicated immutable handoff generation mismatch')
    _check((acquired['kind'], acquired['intent'], funding) in
           {('REENTRY', 'ENTRY', 'seed'), ('REPAIR', 'RECOVERY', 'repair')}
           and acquired['funding_owner'] == funding and sale['intent'] == acquired['intent'],
           'Dedicated acquisition kind/intent/funding mismatch')
    _check(acquired['status'] == acquired['terminal'] and acquired['terminal'] in {'FILLED', 'CANCELED'},
           'Dedicated acquisition is not terminal')
    activation = _sha256(acquired['activation']['proof_sha256'])
    _check(_sha256(evidence['activation_sha256']) == activation,
           'Dedicated acquisition activation changed')
    lineage = evidence['lineage']
    _check(isinstance(lineage, dict) and type(lineage['schema']) is int and lineage['schema'] == 2
           and _sha256(evidence['lineage_sha256']) == _digest(lineage),
           'Dedicated acquisition lineage changed')
    common = {'schema', 'kind', 'order_id', 'pair', 'cohort', 'cohort_generation', 'funding_owner', 'entry_pool'}
    _check(all(lineage[key] == acquired[field] for key, field in
               {'order_id': 'id', 'pair': 'pair', 'cohort': 'cohort',
                'cohort_generation': 'cohort_generation', 'funding_owner': 'funding_owner',
                'entry_pool': 'pool'}.items()) and _generation(lineage['cohort_generation']) == generation,
           'Dedicated acquisition lineage ownership mismatch')
    if lineage['kind'] == 'CLOSED_SAME_ROOT_ENTRY_POOL':
        root_source = lineage.get('psm_root_source')
        if root_source is not None:
            _root_source_owner(acquired, handoff, root_source)
        _check(set(lineage) == common | {'entry_id', 'source_pool', 'admission_sha256',
               'acquisition_source_sha256', 'terminal_sha256', 'cash_return_sha256'} |
               ({'psm_root_source'} if root_source is not None else set()),
               'Malformed closed dedicated acquisition lineage')
        entry_id = _id(lineage['entry_id'])
        _check(entry_id == acquired['entry_id'] and acquired['pool'] == 'next_entry:' + entry_id
               and acquired.get('base_entry_proof') is None,
               'Dedicated acquisition pool/root entry mismatch')
        source_pool = _id(lineage['source_pool'])
        _check(source_pool == acquired['source_pool'] and
               (source_pool == funding or funding == 'seed' and source_pool.startswith('next_entry:')
                and bool(source_pool[len('next_entry:'):])
                or root_source is not None and source_pool == root_source['source_pool']) and source_pool != acquired['pool'],
               'Dedicated acquisition source funding mismatch')
        _check(_sha256(lineage['admission_sha256']) == activation,
               'Dedicated admission does not match activation')
        for field in ('acquisition_source_sha256', 'terminal_sha256', 'cash_return_sha256'):
            _sha256(lineage[field])
    elif lineage['kind'] == 'RETAINED_ROOT_BASE_ENTRY':
        _check(set(lineage) == common | {'root_entry_id', 'base_entry_proof_sha256',
               'root_admission_sha256', 'predecessors'} and acquired['kind'] == 'REENTRY' and funding == 'seed',
               'Malformed retained dedicated acquisition lineage')
        root_id = _id(lineage['root_entry_id'])
        _check(acquired['pool'] == acquired['source_pool'] == 'next_entry:' + root_id
               and _id(acquired['entry_id']) != root_id,
               'Retained acquisition pool/root entry mismatch')
        base = acquired['base_entry_proof']
        _check(isinstance(base, dict) and _sha256(base['proof_sha256']) ==
               _digest({k: v for k, v in base.items() if k != 'proof_sha256'}) and
               _sha256(lineage['base_entry_proof_sha256']) == base['proof_sha256'] == activation,
               'Retained base admission does not match activation')
        _check(base['order_id'] == acquired['id'] and base['entry_id'] == acquired['entry_id'] and
               base['source_pool'] == base['entry_pool'] == acquired['pool'] and
               _money(base['base_entry']['approved_profit_increment']) == 0 and
               all(base['evidence']['order'][field] == acquired[field] for field in
                   ('id', 'entry_id', 'kind', 'side', 'intent', 'pair', 'cohort', 'cohort_generation',
                    'funding_owner', 'pool', 'source_pool', 'position', 'proposal', 'executor')),
               'Retained base admission order binding mismatch')
        _sha256(lineage['root_admission_sha256'])
        predecessors = lineage['predecessors']
        _check(isinstance(predecessors, list) and predecessors,
               'Missing retained acquisition predecessors')
        seen = set()
        for predecessor in predecessors:
            _check(isinstance(predecessor, dict) and set(predecessor) == {'trade_id', 'source_digest', 'funding'},
                   'Malformed retained acquisition predecessor')
            previous_trade = _id(predecessor['trade_id'])
            _check(previous_trade != trade_id and previous_trade not in seen,
                   'Replayed retained acquisition predecessor')
            seen.add(previous_trade)
            _sha256(predecessor['source_digest'])
            prior = predecessor['funding']
            _check(isinstance(prior, dict) and type(prior['schema']) is int and prior['schema'] == 2 and
                   prior['kind'] in {'CLOSED_SAME_ROOT_ENTRY_POOL', 'RETAINED_ROOT_BASE_ENTRY'} and
                   prior['entry_pool'] == acquired['pool'] and prior['pair'] == acquired['pair'] and
                   prior['cohort'] == acquired['cohort'] and prior['funding_owner'] == funding and
                   _generation(prior['cohort_generation']) ==
                   _generation(acquired['previous_cohort_generation']) < generation,
                   'Foreign retained acquisition predecessor')
    elif lineage['kind'] == 'RETAINED_PSM_ROOT_ENTRY':
        _check(set(lineage) == common | {'base_entry_proof_sha256','root_source_sha256','source_owner'}
               and acquired['kind']=='REENTRY' and funding=='seed'
               and acquired['pool']==acquired['source_pool']=='psm:'+acquired['pair'],
               'Malformed retained PSM root acquisition lineage')
        _root_source_owner(acquired, handoff, lineage['source_owner'])
        base=acquired['base_entry_proof']
        _check(isinstance(base,dict) and _sha256(base['proof_sha256'])==
               _digest({k:v for k,v in base.items() if k!='proof_sha256'})
               and lineage['base_entry_proof_sha256']==base['proof_sha256']==activation
               and base['source_owner']==lineage['source_owner']
               and base['evidence']['root_source']['proof_sha256']==lineage['root_source_sha256']
               and lineage['source_owner']['ownership_proof_sha256']==lineage['root_source_sha256']
               and base['order_id']==acquired['id'] and base['entry_id']==acquired['entry_id']
               and base['source_pool']==base['entry_pool']==acquired['pool']
               and _money(base['base_entry']['approved_profit_increment'])==0
               and all(base['evidence']['order'][key]==acquired[key] for key in
                 ('id','entry_id','kind','side','intent','pair','cohort','cohort_generation','funding_owner','pool',
                  'source_pool','source_owner','position','proposal','executor')),
               'Retained PSM root proof differs from original base activation')
    else:
        raise Blocked('Unsupported dedicated acquisition lineage')
    return deepcopy(evidence)


def _verify_transition(state, sale_order, acquired_order, trade_id, *, acquisition_funding=None):
    pinned = identity()
    sale = _same_order(state, sale_order)
    acquired = _same_order(state, acquired_order)
    pair, trade_id = sale['pair'], _id(trade_id)
    _check(sale.get('kind') == 'PSM_EXIT' and sale['side'] == 'SELL' and
           sale['pool'] == 'psm:' + pair, 'Only exact PSM_EXIT pool transition is permitted')
    dedicated = (isinstance(acquired['pool'], str) and acquired['pool'].startswith('next_entry:') or
                 acquired['pool']=='psm:'+pair and acquired['kind']=='REENTRY')
    _check(acquired['side'] == 'BUY' and acquired['pair'] == pair and
           (acquired['pool'] in {'seed', 'repair', 'psm:' + pair} or dedicated), 'Unsupported acquisition pool')
    _check(dedicated or acquisition_funding is None, 'Unexpected dedicated acquisition funding proof')
    _check(sale['status'] == sale['terminal'] and sale['terminal'] in {'FILLED', 'CANCELED'},
           'Native source sale is not terminal')
    for key in ('position', 'proposal', 'executor', 'cohort'):
        _check(sale[key] == acquired[key], 'PSM acquisition/sale ownership mismatch: ' + key)
    funding = _funding(acquired)
    _check(_funding(sale) == funding, 'PSM funding owner changed')
    if acquired['pool'] in {'seed', 'repair'}:
        _check(acquired['pool'] == funding, 'Acquisition pool/funding mismatch')
    meta = state['psm'][pair]
    config = state['identity']['config']
    campaign = f"{config['owner']}:{config['account']}:{pair}:psm"
    _check(meta['campaign'] == campaign, 'Foreign PSM campaign')
    _check(meta['source_sha256'] == pinned['kernel_sha256'], 'Unpinned PSM source')
    campaign_digest = _digest({key: meta[key] for key in LINEAGE_FIELDS})
    _check(campaign_digest == meta['campaign_lineage_sha256'], 'PSM campaign lineage changed')
    for archived in meta['archived_episodes']:
        _check(hashlib.sha256(archived['journal'].encode()).hexdigest() == archived['journal_sha256']
               and archived['source_sha256'] == pinned['kernel_sha256'], 'Archived PSM source changed')
    episode = _episode(meta, sale['psm_position'])
    _check(episode['position'] == campaign + ':episode:' + str(episode['episode']),
           'PSM episode identity mismatch')
    book = K.ResearchBook.loads(episode['journal'])
    position = book.positions[episode['position']]
    snapshots = [e for e in book.journal if e['kind'] == 'snapshot']
    _check(len(snapshots) == 1 and snapshots[0]['position'] == episode['position'] and
           position['account'] == config['account'] and position['owner'] == config['owner'] and
           position['asset'] == pair.replace('-', '/') and not position['quarantine'],
           'PSM snapshot account/asset/owner mismatch')
    actions = [x for x in meta['actions'] if x['order'] == sale['psm_order']]
    _check(len(actions) == 1, 'Missing unique completed logical PSM action')
    action = actions[0]
    _check(action['side'] == 'sell' and action['completed'] is True and action['sealed'] is True and
           action['episode'] == episode['episode'] and sale['id'] in action['children'] and
           len(set(action['children'])) == len(action['children']), 'PSM child/action binding mismatch')
    _check(action['terminal'] in {'FILLED', 'CANCELED'}, 'Logical PSM sale not terminal')
    owners = [x for x in action['owners'] if x['proposal_id'] == sale['proposal'] and
              x['executor_id'] == sale['executor']]
    _check(len(owners) == 1 and owners[0]['account_name'] == config['account'] and
           owners[0]['connector_name'] == config['connector'] and owners[0]['intent'] == sale['intent'] and
           set(sale['eligible_lot_ids']) <= set(owners[0]['lot_ids']),
           'Logical PSM action immutable native owner mismatch')
    for child_id in action['children']:
        child = state['orders'][child_id]
        _check(child['status'] == child['terminal'] and child['terminal'] in TERMINAL and
               child['kind'] == 'PSM_EXIT' and child['side'] == 'SELL' and child['pair'] == pair and
               child['pool'] == 'psm:' + pair and child['psm_order'] == action['order'] and
               child['psm_position'] == episode['position'], 'Logical PSM sibling is not terminal/bound')
    source_order = position['orders'][action['order']]
    _check(source_order['side'] == 'sell' and source_order['status'] == action['terminal'] and
           source_order['quantity'] == _money(action['quantity']), 'Logical sale journal mismatch')
    fills = [e for e in book.journal if e['kind'] == 'fill' and e['fill'] == trade_id]
    reconciles = [e for e in book.journal if e['kind'] == 'reconcile' and e['order'] == action['order']]
    _check(len(fills) == len(reconciles) == 1 and fills[0]['order'] == action['order'] and
           fills[0]['position'] == episode['position'] and reconciles[0]['status'] == action['terminal'] and
           reconciles[0]['complete_fill_history'] is True, 'Missing exact native fill/reconcile journal evidence')
    cohort = state['cohorts'][sale['cohort']]
    root = cohort.get('root_cohort_id', cohort['id'])
    roots = [c for c in meta['original_cohorts'] if c['id'] == root and c['pair'] == pair]
    handoffs = [c for c in episode['handoff_cohorts'] if c['id'] == sale['cohort']]
    _check(len(roots) == len(handoffs) == 1 and handoffs[0].get('root_cohort_id', sale['cohort']) == root,
           'Original root cohort ownership changed')
    if dedicated:
        acquisition_funding = _dedicated_acquisition(acquired, sale, handoffs[0], funding,
                                                     acquisition_funding, trade_id)
    allowed = sale['eligible_lot_ids']
    _check(isinstance(allowed, list) and len(set(allowed)) == len(allowed) and allowed,
           'Invalid immutable sale lot scope')
    lineage = [x for x in episode['handoff_lineage'] if x['acquisition_order'] == acquired['id']]
    _check(bool(lineage), 'Acquisition absent from immutable handoff lineage')
    matched = []
    for item in lineage:
        lot = item['lot']
        _check(item['source_pool'] == acquired['pool'] and item['cohort'] == sale['cohort'] and
               lot['proposal_id'] == sale['proposal'] and lot['executor_id'] == sale['executor'] and
               lot['position_id'] == sale['position'] and lot['pair'] == pair and
               lot['ownership'] == 'ACTIVE_TRADE', 'Immutable handoff lot/owner mismatch')
        if lot['lot_id'] in allowed:
            matched.append(deepcopy(item))
    _check(bool(matched), 'No permitted acquisition lot in sale scope')
    all_source_lots = {x['lot']['lot_id'] for x in episode['handoff_lineage']}
    _check(set(allowed) <= all_source_lots, 'Sale lot scope escapes source episode')
    proof = dict(schema=3, kernel_sha256=pinned['kernel_sha256'],
                 account=config['account'], connector=config['connector'], owner=config['owner'],
                 pair=pair, campaign=campaign, campaign_lineage_sha256=campaign_digest,
                 episode=episode['episode'], psm_position=episode['position'],
                 source_snapshot_id=snapshots[0]['id'], source_snapshot_sha256=_digest(snapshots[0]),
                 source_handoff_sha256=_digest({'lineage': episode['handoff_lineage'], 'cohorts': episode['handoff_cohorts']}),
                 original_source_pool=acquired['pool'], source_pool=sale['pool'], destination_pool=ESCROW_POOL,
                 root_cohort=root, funding_owner=funding, acquisition_order_id=acquired['id'],
                 sale_order_id=sale['id'], proposal_id=sale['proposal'], executor_id=sale['executor'],
                 native_position=sale['position'], trade_id=trade_id, native_realization_id='trade-fill:' + trade_id,
                 logical_action_id=action['order'], action_sha256=_digest(action),
                 eligible_lot_ids=sorted(allowed), acquisition_lineage=sorted(matched, key=lambda x: x['lot']['lot_id']),
                 fill_event=deepcopy(fills[0]), reconcile_event=deepcopy(reconciles[0]))
    if acquisition_funding is not None:
        proof['acquisition_funding'] = acquisition_funding
    proof['proof_sha256'] = _digest(proof)
    return proof


def verify_transition(state, sale_order, acquired_order, trade_id, *, acquisition_funding=None):
    """Return a detached immutable-source proof, or fail closed on malformed data.

    Mutable live balances, later source events and current episode are deliberately
    absent. The native bridge freezes these returned bytes in its source payload.
    """
    try:
        return _verify_transition(state, sale_order, acquired_order, trade_id,
                                  acquisition_funding=acquisition_funding)
    except Blocked:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Blocked('Malformed or missing PSM ownership proof: ' + str(exc)) from exc


def _stage(state, source, amount, at, allocation_event_id, cash_backing_id, snapshot_id):
    source = json.loads(source) if isinstance(source, str) else deepcopy(source)
    at = _time(at)
    _check(_time(state['now']) <= at, 'Distribution time predates paper state')
    amount = _money(amount)
    allocation_event_id, cash_backing_id, snapshot_id = map(_id, (allocation_event_id, cash_backing_id, snapshot_id))
    sale = _same_order(state, source['order'])
    raw = json.loads(source['receipt']['payload'])
    trade_id = _id(raw['trade_id'])
    _check(raw['order_id'] == sale['id'] and source['receipt']['id'] == trade_id and
           source['receipt']['order_id'] == sale['id'] and source['receipt']['applied'] == 1,
           'Unconfirmed native source receipt')
    proofs = []
    for acquisition in source['acquisitions']:
        acquired_order = _same_order(state, acquisition['order'])
        transition = acquisition.get('pool_transition')
        _check(transition is None or isinstance(transition, dict), 'Malformed acquisition pool transition')
        funding = None if transition is None else transition.get('acquisition_funding')
        proof = verify_transition(state, sale, acquired_order, trade_id, acquisition_funding=funding)
        if funding is not None:
            _check(transition == proof, 'Frozen dedicated acquisition transition mismatch')
        if proof not in proofs:
            proofs.append(proof)
    proofs.sort(key=lambda x: x['acquisition_order_id'])
    _check(proofs and source['psm_ownership'] == proofs, 'Frozen PSM ownership proof mismatch')
    first = proofs[0]
    _check(all((p['root_cohort'], p['funding_owner'], p['logical_action_id']) ==
               (first['root_cohort'], first['funding_owner'], first['logical_action_id']) for p in proofs),
           'A native receipt spans conflicting PSM cash owners')
    config = state['identity']['config']
    _check(raw['account_name'] == config['account'] and raw['connector_name'] == config['connector'] and
           raw['trading_pair'] == first['pair'] and raw['trade_type'] == 'SELL' and raw['fee_currency'] == 'USD',
           'Native receipt scope mismatch')
    fill, reconcile, event = first['fill_event'], first['reconcile_event'], source['event']
    units, price, exit_fees = (_money(raw[k]) for k in ('amount', 'price', 'fee_paid'))
    gross = units * price
    principal, entry_fees = (_money(event[k]) for k in ('allocated_cost_basis_quote', 'allocated_entry_fees_quote'))
    profit = gross - principal - entry_fees - exit_fees
    _check(units > 0 and price > 0 and min(principal, entry_fees, exit_fees) >= 0 and
           amount == profit and profit > 0, 'Only full positive native fee-net realized profit is distributable')
    _check(event['event_id'] == 'trade-fill:' + trade_id and event['position_id'] == sale['position'] and
           event['event_type'] in {'PARTIAL_EXIT', 'FINAL_EXIT'} and
           _money(event['base_amount']) == units and _money(event['quote_amount']) == gross and
           _money(event['fee_quote']) == exit_fees and _money(event['realized_pnl_quote']) == profit,
           'Native realization economics mismatch')
    _check(_money(fill['units']) == units and _money(fill['price']) == price and _money(fill['fee']) == exit_fees,
           'PSM/native receipt economics mismatch')
    allocations = source['allocations']
    _check(allocations and len({x['lot_id'] for x in allocations}) == len(allocations),
           'Missing or duplicate exact disposal allocations')
    lineage_lots = {x['lot']['lot_id']: x['lot'] for p in proofs for x in p['acquisition_lineage']}
    native_lots = {x['lot_id']: x for x in source['lots']}
    _check(len(native_lots) == len(source['lots']) and set(native_lots) == {x['lot_id'] for x in allocations},
           'Native original lot scope mismatch')
    for piece in allocations:
        lot = lineage_lots[piece['lot_id']]
        original = native_lots[piece['lot_id']]
        _check(all(original[k] == lot[k] for k in ('lot_id', 'position_id', 'pair', 'ownership', 'source',
                   'proposal_id', 'executor_id', 'created_at')) and
               all(_money(original[k]) == _money(lot[k]) for k in ('base_original',
                   'cost_basis_original_quote', 'entry_fees_original_quote')), 'Native original lot changed from handoff')
        _check(piece['event_id'] == event['event_id'] and piece['position_id'] == sale['position'] and
               piece['proposal_id'] == sale['proposal'] and piece['executor_id'] == sale['executor'] and
               piece['ownership'] == lot['ownership'] == 'ACTIVE_TRADE' and
               _money(piece['base_sold']) > 0 and _money(piece['allocated_cost_basis_quote']) >= 0 and
               _money(piece['allocated_entry_fees_quote']) >= 0, 'Foreign or invalid native disposal allocation')
    for field, total in (('base_sold', units), ('allocated_cost_basis_quote', principal),
                         ('allocated_entry_fees_quote', entry_fees)):
        _check(sum((_money(x[field]) for x in allocations), K.ZERO) == total,
               'Native disposal aggregation mismatch')
    native_settled_at = _time(source['terminal']['at'])
    _check(json.loads(source['terminal']['payload']) == {'order': sale['id'], 'status': sale['terminal']},
           'Native terminal receipt mismatch')
    _check(_time(event['occurred_at']) <= native_settled_at <= reconcile['at'] <= at,
           'Distribution settlement chronology mismatch')
    pair, position_id = first['pair'], first['psm_position']
    meta = state['psm'][pair]
    episode = _episode(meta, position_id)
    book = K.ResearchBook.loads(episode['journal'])
    position = book.positions[position_id]
    event_id = 'psm-profit:' + allocation_event_id
    immutable = dict(id=event_id, kind='REALIZED_PROFIT_DISTRIBUTION', position=position_id,
        owner=first['owner'], account=first['account'], asset=pair.replace('-', '/'),
        native_trade_id=trade_id, native_realization_id=event['event_id'],
        allocation_event_id=allocation_event_id, cash_backing_id=cash_backing_id, snapshot_id=snapshot_id,
        source_proof_sha256=_digest(source), order=first['logical_action_id'],
        fill_event_id=fill['id'], reconcile_event_id=reconcile['id'],
        source_pool=first['source_pool'], destination_pool=ESCROW_POOL,
        root_cohort=first['root_cohort'], funding_owner=first['funding_owner'],
        confirmed=True, reconciled=True, pending_orders=[], settled_at=native_settled_at,
        gross_realized_proceeds=str(gross), returned_principal=str(principal), entry_fees=str(entry_fees),
        exit_fees=str(exit_fees), fee_net_realized_profit=str(profit), amount=str(amount))
    matches = [e for e in book.journal if e['kind'] == 'REALIZED_PROFIT_DISTRIBUTION' and
               (e['native_trade_id'] == trade_id or e['id'] == event_id or
                e['cash_backing_id'] == cash_backing_id or e['allocation_event_id'] == allocation_event_id)]
    if matches:
        _check(len(matches) == 1 and all(matches[0].get(k) == v for k, v in immutable.items()),
               'Conflicting/reused PSM profit distribution proof')
        _check(at >= matches[0]['at'], 'Distribution replay predates settlement')
        return deepcopy(state)
    _check(position_id == meta['position'] and meta['mode'] == 'PSM' and
           not any(e['position'] == position_id for e in meta['archived_episodes']),
           'New distribution requires the current unarchived PSM episode')
    _check(meta['action'] is None and position['pending'] is None and
           not any(o['pair'] == pair and o['status'] in PENDING for o in state['orders'].values()),
           'Distribution requires no pending logical/native action')
    _check(not state.get('quarantine') and state.get('breaker') == 'NORMAL', 'Quarantined PSM cash')
    immutable.update(at=at, cash_before=str(position['cash']), cash_after=str(position['cash'] - amount))
    book.apply(immutable)
    result = deepcopy(state)
    result['psm'][pair]['journal'] = book.dumps()
    # Kept only in the source journal. Cash projection subtracts these same legs
    # once; never add cumulative distributions again to portfolio marked value.
    return result


def stage_distribution(state, source, amount, at, allocation_event_id, cash_backing_id, snapshot_id):
    """Stage a detached journal mutation; caller owns the atomic DB transaction."""
    try:
        with localcontext(K.ARITHMETIC):
            return _stage(state, source, amount, at, allocation_event_id, cash_backing_id, snapshot_id)
    except Blocked:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Blocked('Malformed or missing native PSM distribution proof: ' + str(exc)) from exc


def distribution_debits(state, pair):
    """Return lifetime root|funding debits, counting current/archive overlap once."""
    try:
        meta = state['psm'][pair]
        identity()
        _check(meta['source_sha256'] == KERNEL_SHA256, 'Unpinned distribution source')
        episodes = {}
        for archived in meta['archived_episodes']:
            _check(archived['position'] not in episodes and
                   hashlib.sha256(archived['journal'].encode()).hexdigest() == archived['journal_sha256'] and
                   archived['source_sha256'] == KERNEL_SHA256, 'Invalid archived distribution identity')
            episodes[archived['position']] = archived
        if meta['position'] in episodes:
            _check(episodes[meta['position']]['journal'] == meta['journal'], 'Current/archive distribution journal mismatch')
        episodes[meta['position']] = dict(position=meta['position'], journal=meta['journal'])
        result, seen = {}, set()
        for episode in episodes.values():
            for event in K.ResearchBook.loads(episode['journal']).journal:
                if event['kind'] != 'REALIZED_PROFIT_DISTRIBUTION':
                    continue
                _check(event['native_trade_id'] not in seen, 'Duplicate lifetime PSM distribution')
                seen.add(event['native_trade_id'])
                key = event['root_cohort'] + '|' + event['funding_owner']
                result[key] = result.get(key, K.ZERO) + _money(event['amount'])
        return result
    except Blocked:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked('Malformed distribution debit history') from exc
