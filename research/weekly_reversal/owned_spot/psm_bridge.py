"""New, offline native/PSM coordinator with a pinned schema3 profit partition.

Native inventory, receipt cash and native realized PnL are authoritative. The
reducer retains its own documented pro-rata cost attribution as a diagnostic.
This bridge never constructs an exchange client or reads market-data files.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from fractions import Fraction
import hashlib
import importlib.util
import json
from pathlib import Path

from . import native as N
from .model import Blocked, D, ZERO, ONE, BPS, decimal, floor_step


SOURCE_SHA256 = '7e53ef0f2da3229a5fb5158655d090a14c2073b608ebb654f603b92acd7f0585'
SOURCE_PATH = Path(__file__).resolve().parents[1] / 'psm_kernel.py'


def load_reducer():
    if hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise Blocked('PSM immutable source hash mismatch')
    spec = importlib.util.spec_from_file_location('_owned_spot_pinned_psm', SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PSM = load_reducer()
LINEAGE_FIELDS = ('original_lineage', 'original_cohorts', 'initial_native_positions',
                  'initial_native_realized', 'original_basis', 'original_units')


def _serial(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serial(v) for v in value]
    return value


def _int_time(value):
    if isinstance(value, bool) or int(value) != value or value < 0:
        raise Blocked('PSM requires exact integer event times')
    return int(value)


def _lineage_digest(meta):
    value = {key: meta[key] for key in LINEAGE_FIELDS}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class PSMBridge:
    """One persistent claim per adapter account/asset, including after COMPLETE.

    Hooks: validate_fill before journaling; sync after applied receipts, terminal
    reconciliation and restart. Every public mutation also catches up receipts.
    A single reducer SELL intent owns a sequence of exact native-owner children.
    BUY intent quantity is frozen by the real reducer, never resized at open.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self._syncing = False
        # Verify at creation/restart, rather than trusting an earlier import.
        if hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise Blocked('PSM immutable source hash mismatch')

    def _meta(self, pair):
        try:
            meta = self.adapter.state['psm'][pair]
        except KeyError as exc:
            raise Blocked('PSM has no account/asset claim') from exc
        if meta['source_sha256'] != SOURCE_SHA256:
            raise Blocked('PSM persisted source identity mismatch')
        if meta.get('campaign_lineage_sha256') != _lineage_digest(meta):
            raise Blocked('PSM immutable campaign lineage identity mismatch')
        for episode in meta.get('archived_episodes', []):
            if hashlib.sha256(episode['journal'].encode()).hexdigest() != episode['journal_sha256']:
                raise Blocked('Archived PSM episode journal identity mismatch')
        return meta

    def _book(self, pair):
        return PSM.ResearchBook.loads(self._meta(pair)['journal'])

    def _position(self, pair):
        meta = self._meta(pair)
        return self._book(pair).positions[meta['position']]

    def _event(self, pair, kind, **fields):
        a = self.adapter
        meta = self._meta(pair)
        return dict(id=a._id('psm-' + kind), kind=kind,
                    at=_int_time(a.now), position=meta['position'],
                    owner=a.config.owner, **fields)

    def _apply(self, pair, event):
        book = self._book(pair)
        try:
            book.apply(event)
        except PSM.Blocked as exc:
            raise Blocked('Pinned PSM reducer: ' + str(exc)) from exc
        self._meta(pair)['journal'] = book.dumps()
        self.adapter._save('psm_' + event['kind'], {'pair': pair, 'event': event['id']})
        return book.positions[self._meta(pair)['position']]

    def _quote(self, pair, price=None):
        a = self.adapter
        bar = a._bar(pair)
        rule = a.config.rules[pair]
        return dict(price=str(bar.close if price is None else price),
                    quote_at=_int_time(bar.completed_at), max_age=a.config.max_mark_age_seconds,
                    step=str(rule.base_step), min_units=str(rule.min_base),
                    min_quote=str(rule.min_quote))

    def _fee_ceiling(self, pair):
        a = self.adapter
        minimum = a.config.rules[pair].min_quote
        if minimum <= 0:
            raise Blocked('PSM needs a positive minimum fill quote for rounded-fee bounds')
        # The reducer accepts at most 18 fractional input digits. Round UP so
        # representing the ceiling never understates a rounding quantum.
        ceiling = (a.config.fee_rate + a.config.fee_quantum / minimum).quantize(D('1e-18'), rounding=ROUND_UP)
        if ceiling >= ONE:
            raise Blocked('PSM rounded-fee ceiling must be below one')
        return ceiling

    def _prepare(self, pair, now, *, inventory_health=True):
        a = self.adapter
        a.advance(now)
        _int_time(a.now)
        self.sync()
        a._assert_clean(pair, health=False)
        if inventory_health:
            a.refresh_accounting_all(now=a.now)
            a._assert_clean(pair)
        a._bar(pair)

    def _lineage(self, pair):
        a = self.adapter
        lots = tuple(a._lots(pair))
        if not lots:
            raise Blocked('PSM snapshot requires actually owned native lots')
        owners = N.economic_exit._group_owners(lots=lots, governor=a.governor, pair=pair)
        cohort_by_lot = {}
        for cohort in a.state['cohorts'].values():
            if cohort['pair'] == pair:
                for lid in cohort['lot_ids']:
                    if lid in cohort_by_lot:
                        raise Blocked('PSM cannot accept duplicate native lot/cohort claims')
                    cohort_by_lot[lid] = cohort['id']
        if any(l.lot_id not in cohort_by_lot for l in lots):
            raise Blocked('PSM cannot accept unclaimed native inventory')
        lineage = []
        for lot in lots:
            acquisition = [o for o in a.state['orders'].values()
                           if o['side'] == 'BUY' and o['proposal'] == lot.proposal_id
                           and o['executor'] == lot.executor_id]
            if len(acquisition) != 1:
                raise Blocked('PSM requires a unique native acquisition owner')
            lineage.append(dict(lot=_serial(asdict(lot)), cohort=cohort_by_lot[lot.lot_id],
                                acquisition_order=acquisition[0]['id'], source_pool=acquisition[0]['pool']))
        return lineage, [_serial(asdict(owner)) for owner in owners]

    def start(self, pair, *, decision_ref, falling_knife, repair_eligible,
              policy_ref, now):
        a = self.adapter
        self._prepare(pair, now)
        if hasattr(a, 'seed_complete') and not a.seed_complete():
            raise Blocked('All assets must cross seed-completion barrier before PSM')
        previous = a.state['psm'].get(pair)
        if previous and previous.get('mode') != 'REPAIR':
            raise Blocked('PSM persistent account/asset claim already exists')
        if a._pending(pair) or a._asset(pair).get('exit'):
            raise Blocked('Reconcile all native actions before PSM handoff')
        if falling_knife is not True or repair_eligible is not False:
            raise Blocked('PSM requires explicit falling knife and frozen-policy repair ineligibility')
        if not isinstance(decision_ref, str) or not decision_ref or not isinstance(policy_ref, str) or not policy_ref:
            raise Blocked('PSM requires causal decision and frozen-policy references')
        evidence = a._asset(pair).get('repair_block')
        if not evidence or not evidence.get('capital_blocked'):
            raise Blocked('PSM requires an actual due-rung full-target capital block')
        fresh = a.repair_feasibility(pair, rung=evidence['requested_rung'], now=a.now)
        for key in ('capital_blocked', 'cohort_id', 'generation', 'next_rung', 'target', 'completed_bar', 'reason_code'):
            if fresh.get(key) != evidence.get(key):
                raise Blocked('PSM repair-block evidence changed or became ineligible')
        if not fresh['capital_blocked'] or not all(fresh['guards'].values()):
            raise Blocked('PSM fallback is allowed only for a due healthy capital-blocked repair')
        a.selected_cohort_cycle(pair, cohort_id=fresh['cohort_id'], now=a.now)
        native = a._asset(pair)['last_native_decision']
        if not native or not 0 <= a.now - native['at'] <= a.config.max_mark_age_seconds:
            raise Blocked('PSM requires a fresh real native lifecycle decision')
        if native['action'] in {'EXIT', 'PROTECT_PROFIT'}:
            raise Blocked('Native whole-position profit protection outranks PSM')
        lineage, owners = self._lineage(pair)
        cash_offsets = self.funding_cash(pair) if previous else {}
        units = sum((l.base_remaining for l in a._lots(pair)), ZERO)
        basis = a.deployed_basis(pair)
        campaign = f'{a.config.owner}:{a.config.account}:{pair}:psm'
        episode = 1 if previous is None else previous['episode'] + 1
        position = f'{campaign}:episode:{episode}'
        snapshot = dict(id=a._id('psm-snapshot'), kind='snapshot', at=_int_time(a.now),
                        position=position, owner=a.config.owner, account=a.config.account,
                        asset=pair.replace('-', '/'), provenance=decision_ref,
                        inventory_bucket='ACTIVE_TRADE', reconciled=True, pending_orders=[],
                        units=str(units), basis=str(basis))
        # Apply to a temporary real reducer first. Precision is checked exactly;
        # unsupported snapshots are never quantized into artificial agreement.
        book = PSM.ResearchBook()
        try:
            book.apply(snapshot)
            book.apply(dict(id=a._id('psm-handoff'), kind='handoff', at=_int_time(a.now),
                            position=position, owner=a.config.owner, vcap_eligible=False,
                            falling_knife=True, reconciled=True, pending_orders=[],
                            confirmed_units=str(units), decision_ref=decision_ref))
        except PSM.Blocked as exc:
            raise Blocked('Pinned PSM reducer: ' + str(exc)) from exc
        a.state['psm'][pair] = dict(source_sha256=SOURCE_SHA256, position=position,
                                   campaign=campaign, episode=episode, mode='PSM', archived_episodes=[],
                                   episode_cash_offsets={k: str(v) for k, v in cash_offsets.items()},
                                   journal=book.dumps(), original_lineage=lineage,
                                   original_cohorts=deepcopy([c for c in a.state['cohorts'].values() if c['pair'] == pair]),
                                   initial_native_positions=list(a._asset(pair)['positions']),
                                   native_positions=list(a._asset(pair)['positions']),
                                   selected_at_handoff=a._asset(pair)['selected'],
                                   initial_native_realized=str(self._native_realized(pair)),
                                   original_basis=str(basis), original_units=str(units),
                                   policy_ref=policy_ref, handoff_decision_ref=decision_ref,
                                   repair_block_evidence=deepcopy(fresh),
                                   native_handoff_decision=deepcopy(native), owners=owners,
                                   episode_lineage=lineage, episode_cohorts=deepcopy([c for c in a.state['cohorts'].values() if c['pair'] == pair and a._cohort_lots(c)]),
                                   action=None, actions=[], successors=[])
        if previous:
            current = a.state['psm'][pair]
            for key in ('original_lineage', 'original_cohorts', 'initial_native_positions',
                        'initial_native_realized', 'original_basis', 'original_units',
                        'successors', 'actions', 'archived_episodes'):
                current[key] = deepcopy(previous[key])
            if previous['policy_ref'] != policy_ref:
                a.state['psm'][pair] = previous
                raise Blocked('A later PSM episode cannot change the frozen policy')
        a.state['psm'][pair]['campaign_lineage_sha256'] = _lineage_digest(a.state['psm'][pair])
        a._save('psm_handoff', {'pair': pair, 'position': position, 'decision_ref': decision_ref})
        return deepcopy(a.state['psm'][pair])

    def request_sell(self, pair, *, now):
        a = self.adapter
        self._prepare(pair, now)
        meta = self._meta(pair)
        if meta['mode'] != 'PSM' or meta['action'] is not None or a._pending(pair):
            raise Blocked('PSM already has a pending action or native repair authority')
        _, owners = self._lineage(pair)
        bar, rule = a._bar(pair), a.config.rules[pair]
        prior = [action for action in meta['actions'] if action['side'] == 'sell' and action.get('episode', meta['episode']) == meta['episode']]
        if prior and bar.completed_at <= prior[-1]['decision_bar']:
            raise Blocked('PSM sell retry requires a fresh completed decision bar')
        price = (bar.close * (ONE - a.config.expected_slippage_bps / BPS) / rule.price_tick).to_integral_value(rounding=ROUND_DOWN) * rule.price_tick
        event = self._event(pair, 'intent', order=a._id('psm-sell'), side='sell',
                            max_fee_rate=str(self._fee_ceiling(pair)), **self._quote(pair, price))
        book = self._book(pair)
        try:
            book.apply(event)
        except PSM.Blocked as exc:
            raise Blocked('Pinned PSM reducer: ' + str(exc)) from exc
        p = book.positions[meta['position']]
        plan = []
        for owner in owners:
            eligible = [lid for lid in owner['lot_ids'] if a.ledger.get_lot(lid).base_remaining > 0]
            live = a.ledger.active_lot_ids_for_provenance(position_id=a._current_position(pair),
                         proposal_id=owner['proposal_id'], executor_id=owner['executor_id'])
            if set(live) != set(eligible):
                raise Blocked('PSM exact native source-owner scope changed')
            quantity = floor_step(sum((a.ledger.get_lot(lid).base_remaining for lid in eligible), ZERO), rule.base_step)
            if quantity <= 0 or quantity < rule.min_base or quantity * price < rule.min_quote:
                continue
            original = next(o for o in a.state['orders'].values() if o['side'] == 'BUY' and o['proposal'] == owner['proposal_id'])
            plan.append((owner, eligible, quantity, original))
        if not plan:
            raise Blocked('No executable native source-owner PSM children')
        capacity = sum((q * bar.close for _, _, q, _ in plan), ZERO)
        a._admit_liquidity(pair, capacity, bar)
        action = dict(order=event['order'], side='sell', children=[], owners=owners,
                      quantity=str(p['orders'][event['order']]['quantity']), price=str(price),
                      fee_ceiling=event['max_fee_rate'], at=_int_time(a.now), completed=False,
                      sealed=True, decision_bar=bar.completed_at, capacity=str(capacity), observed_open=None,
                      episode=meta['episode'])
        for owner, eligible, quantity, original in plan:
            oid = a._id('psm-native-sell')
            order = dict(id=oid, pair=pair, position=a._current_position(pair), cohort=original['cohort'],
                         kind='PSM_EXIT', side='SELL', pool='psm:' + pair, status='OPEN',
                         proposal=owner['proposal_id'], executor=owner['executor_id'], intent=original['intent'],
                         target='0', principal='0', quantity=str(quantity), reference=str(price),
                         decision_at=a.now, created_at=a.now, capacity=str(quantity * bar.close),
                         decision_bar=bar.completed_at, terminal=None, eligible_lot_ids=eligible,
                         psm_order=action['order'], psm_position=meta['position'], fixed_quantity=True,
                         psm_fee_ceiling=action['fee_ceiling'], funding_owner=self._funding_owner(original))
            a.state['orders'][oid] = order
            action['children'].append(oid)
        meta['journal'], meta['action'] = book.dumps(), action
        a._save('psm_sell_batch_sealed', {'pair': pair, 'order': action['order'], 'children': action['children']})
        for oid in action['children']:
            order = a.state['orders'][oid]
            a._sync_proposal(order['proposal'], order['position'])
        return deepcopy(a.state['orders'][action['children'][0]])

    def _pending_child(self, pair, side, now):
        a = self.adapter
        a.advance(now)
        self.sync()
        action = self._meta(pair)['action']
        if action is None:
            return None
        if action['side'] != side or not action.get('sealed'):
            raise Blocked('No sealed PSM batch of the requested side')
        for oid in action['children']:
            if a.state['orders'][oid]['status'] in {'OPEN', 'PARTIAL', 'CANCEL_REQUESTED'}:
                return deepcopy(a.state['orders'][oid])
        self._finish_action(pair)
        return None

    def continue_sell(self, pair, *, now):
        """Return an already-sealed child; never create a new market-time action."""
        return self._pending_child(pair, 'sell', now)

    def validate_open(self, order_id, opening):
        """Bind all siblings to one exact parent delayed observed-open event."""
        a = self.adapter
        order = a.state['orders'][order_id]
        if order['kind'] not in {'PSM_EXIT', 'PSM_REBUILD'}:
            return
        action = self._meta(order['pair'])['action']
        if not action or not action.get('sealed') or order_id not in action['children']:
            raise Blocked('PSM order is not a member of a sealed batch')
        expected = action['at'] + a.config.execution_delay_seconds
        if opening.opened_at != expected or opening.observed_at != expected:
            raise Blocked('PSM batch requires the exact shared delayed-open observation')
        evidence = dict(price=str(opening.price), opened_at=opening.opened_at, observed_at=opening.observed_at)
        if action['observed_open'] is not None and action['observed_open'] != evidence:
            raise Blocked('PSM batch children must share identical observed-open evidence')
        if action['observed_open'] is None:
            action['observed_open'] = evidence
            a._save('psm_batch_open_bound', {'pair': order['pair'], 'order': action['order'], **evidence})

    def observe_open(self, pair, opening, *, fill_fraction=ONE):
        a = self.adapter
        action = self._meta(pair)['action']
        if not action or not action.get('sealed'):
            raise Blocked('No sealed PSM batch')
        ids = list(action['children'])
        for oid in ids:
            if a.state['orders'][oid]['status'] not in {'OPEN', 'PARTIAL'}:
                continue
            self.validate_open(oid, opening)
            a.observe_open(oid, opening, fill_fraction=fill_fraction)
        self.sync()
        return ids

    def missing_open(self, pair, *, expected_at, now):
        a = self.adapter
        action = self._meta(pair)['action']
        if not action or expected_at != action['at'] + a.config.execution_delay_seconds:
            raise Blocked('Missing PSM open does not match the sealed batch boundary')
        for oid in list(action['children']):
            if a.state['orders'][oid]['status'] in {'OPEN', 'PARTIAL', 'CANCEL_REQUESTED'}:
                a.missing_open(oid, expected_at=expected_at, now=now)
        self.sync()

    def cancel_unsealed_batch(self, pair, *, now):
        """Recover a crash while creating native buy children without dispatch.

        No missing child is created at a later market time. Existing reservations
        are terminally canceled and the source receives exact observed totals.
        """
        a = self.adapter
        a.advance(now)
        meta = self._meta(pair)
        action = meta['action']
        if not action or action.get('sealed'):
            raise Blocked('No interrupted unsealed PSM batch')
        self._bind_rebuild_child(pair)
        for allocation in action.get('allocations', []):
            if allocation['state'] == 'PLANNED':
                allocation['state'] = 'UNSUBMITTED_CANCELED'
        action['sealed'] = True
        action['preparation_interrupted'] = True
        a._save('psm_interrupted_batch_cancel', {'pair': pair, 'order': action['order']})
        for oid in list(action['children']):
            if a.state['orders'][oid]['status'] in {'OPEN', 'PARTIAL', 'CANCEL_REQUESTED'}:
                a.reconcile_terminal(oid, 'CANCELED', now=a.now)
        self.sync()
        if meta['action'] is not None:
            self._finish_action(pair)

    def _finish_action(self, pair):
        a = self.adapter
        meta = self._meta(pair)
        action = meta['action']
        if action is None:
            return
        if action['side'] == 'buy' and any(p['state'] == 'PLANNED' for p in action['allocations']):
            raise Blocked('PSM rebuild partitions remain unsubmitted')
        if any(a.state['orders'][oid]['status'] not in {'FILLED', 'CANCELED', 'REJECTED'} for oid in action['children']):
            raise Blocked('PSM child terminal fill history is incomplete')
        p = self._position(pair)
        order = p['orders'][action['order']]
        status = 'FILLED' if order['filled'] == order['quantity'] else 'CANCELED'
        if action['side'] == 'buy' and action['children'] and all(a.state['orders'][oid]['status'] == 'REJECTED' for oid in action['children']):
            status = 'REJECTED'
        if order['status'] == 'PENDING':
            self._apply(pair, self._event(pair, 'reconcile', order=action['order'], status=status,
                                         complete_fill_history=True, filled_units=str(order['filled']),
                                         gross=str(order['gross']), fees=str(order['fees'])))
        else:
            # Crash recovery after the durable reducer terminal event but before
            # its bridge action index was cleared. Do not emit a second terminal.
            status = order['status']
        action['completed'] = True
        action['terminal'] = status
        meta['actions'].append(action)
        meta['action'] = None
        a._save('psm_action_terminal', {'pair': pair, 'order': action['order'], 'status': status})

    def finish_sell(self, pair, *, now):
        """Cancel the unfilled aggregate remainder only after all children terminal."""
        a = self.adapter
        self._prepare(pair, now)
        if self._meta(pair)['action'] is None or self._meta(pair)['action']['side'] != 'sell':
            raise Blocked('No pending PSM sell intent')
        if a._pending(pair):
            raise Blocked('Native sell child still pending')
        self._finish_action(pair)

    def close_exit(self, pair, *, now):
        self._prepare(pair, now)
        if self._meta(pair)['action'] is not None or self.adapter._pending(pair):
            raise Blocked('Native exit action remains pending')
        return self._apply(pair, self._event(pair, 'close_exit', **self._quote(pair)))

    def rebuild_signal(self, pair, *, decision_ref, qualified, expires_at, now):
        self._prepare(pair, now)
        event = self._event(pair, 'rebuild_signal', qualified=qualified,
                            decision_ref=decision_ref, expires_at=_int_time(expires_at))
        result = self._apply(pair, event)
        self._meta(pair)['rebuild_signal_event'] = event['id']
        self.adapter._save('psm_signal_bound', {'pair': pair, 'event': event['id']})
        return result

    def _planned_native_position(self, pair):
        a = self.adapter
        current = a._current_position(pair)
        if a.ledger.snapshot(current).status == N.economic_position.EconomicPositionStatus.CLOSED:
            return f'{a.config.owner}:{pair}:native:{len(a._asset(pair)["positions"]) + 1}'
        return current

    def ensure_order_position(self, order):
        """Called by core _authorize AFTER a durable PREPARING order exists."""
        if order['kind'] != 'PSM_REBUILD':
            return
        a = self.adapter
        if order['id'] not in a.state['orders']:
            raise Blocked('Cannot open native successor without a durable buy intent')
        pair, target = order['pair'], order['position']
        meta = self._meta(pair)
        old = a._current_position(pair)
        if target == old:
            return
        expected = f'{a.config.owner}:{pair}:native:{len(a._asset(pair)["positions"]) + 1}'
        if target != expected or a.ledger.snapshot(old).status != N.economic_position.EconomicPositionStatus.CLOSED:
            raise Blocked('PSM successor intent has invalid predecessor or identity')
        a.ledger.open_position(position_id=target, pair=pair, created_at=order['decision_at'])
        a._asset(pair)['positions'].append(target)
        meta['native_positions'].append(target)
        meta['successors'].append(dict(parent=meta['campaign'], predecessor=old, successor=target,
            cycle=self._position(pair)['cycle'], episode=meta['episode'], at=_int_time(order['decision_at'])))
        a._save('psm_native_successor', {'pair': pair, 'predecessor': old, 'successor': target,
                                       'native_intent': order['id']})

    def _root_cohort(self, cid):
        cohort = self.adapter.state['cohorts'][cid]
        return cohort.get('root_cohort_id', cid)

    def _funding_owner(self, order):
        owner = order.get('funding_owner')
        if owner in {'seed', 'repair'}:
            return owner
        if order['kind'] == 'REPAIR' or order.get('intent') == 'RECOVERY':
            return 'repair'
        if order['kind'] == 'SEED' or order.get('intent') == 'ENTRY':
            return 'seed'
        raise Blocked('Native order has no immutable seed/repair funding owner')

    def funding_cash(self, pair):
        """Exact root AND funding-owner subcash; never net away recovery ownership."""
        a = self.adapter
        result = {}
        for raw in a._receipts():
            order = a.state['orders'][raw['order_id']]
            if order['pool'] != 'psm:' + pair:
                continue
            root = self._root_cohort(order['cohort'])
            key = root + '|' + self._funding_owner(order)
            result.setdefault(key, ZERO)
            gross, fee = D(raw['amount']) * D(raw['price']), D(raw['fee_paid'])
            result[key] += gross - fee if order['side'] == 'SELL' else -(gross + fee)
        # Schema3 transfers only independently verified realized profit. The
        # physical cash projection applies the paired escrow credit separately;
        # this view removes the exact root/funding-owner debit once.
        from ..psm_profit_partition import distribution_debits
        for key, debit in distribution_debits(a.state, pair).items():
            if key not in result or result[key] < debit:
                raise Blocked('PSM distribution exceeds its original root/funding cash')
            result[key] -= debit
        bridge = getattr(a, 'profit_bridge', None)
        if bridge is not None and getattr(bridge, 'root_cash_enabled', False):
            for key, delta in bridge.psm_funding_cash_adjustments(pair).items():
                result[key] = result.get(key, ZERO) + delta
            if any(cash < ZERO for cash in result.values()):
                raise Blocked('PSM post-handback adjustment exceeds exact root/funding cash')
        if sum(result.values(), ZERO) != a.cash_pools().get('psm:' + pair, ZERO):
            raise Blocked('PSM funding-subowner cash does not conserve physical owner pool')
        return result

    def cohort_cash(self, pair):
        result = {c['id']: ZERO for c in self._meta(pair)['original_cohorts']}
        for key, cash in self.funding_cash(pair).items():
            root, _ = key.rsplit('|', 1)
            result[root] = result.get(root, ZERO) + cash
        return result

    def recovery_cash(self):
        return sum((cash for pair in self.adapter.state['psm'] for key, cash in self.funding_cash(pair).items()
                    if key.endswith('|repair')), ZERO)

    def request_rebuild(self, pair, *, min_unit_gain, now):
        a = self.adapter
        self._prepare(pair, now)
        meta = self._meta(pair)
        if meta['mode'] != 'PSM' or meta['action'] is not None or a._pending(pair):
            raise Blocked('Pending native/PSM action or repair authority blocks rebuild')
        gain = decimal(min_unit_gain)
        bar = a._bar(pair)
        prior = [action for action in meta['actions'] if action['side'] == 'buy' and action.get('episode', meta['episode']) == meta['episode']]
        if prior and (bar.completed_at <= prior[-1]['decision_bar'] or meta.get('rebuild_signal_event') == prior[-1].get('signal_event')):
            raise Blocked('PSM rebuild retry requires a fresh completed bar and new qualified reversal signal')
        price = (bar.close * (ONE + a.config.expected_slippage_bps / BPS) / a.config.rules[pair].price_tick).to_integral_value(rounding=ROUND_UP) * a.config.rules[pair].price_tick
        event = self._event(pair, 'intent', order=a._id('psm-buy'), side='buy',
                            max_fee_rate=str(self._fee_ceiling(pair)), min_unit_gain=str(gain),
                            **self._quote(pair, price))
        book = self._book(pair)
        try:
            book.apply(event)
        except PSM.Blocked as exc:
            raise Blocked('Pinned PSM reducer: ' + str(exc)) from exc
        position = book.positions[meta['position']]
        intent = position['orders'][event['order']]
        step = a.config.rules[pair].base_step
        allocations = []
        for key, cash in sorted(self.funding_cash(pair).items()):
            root, funding_owner = key.rsplit('|', 1)
            cash -= D(meta.get('episode_cash_offsets', {}).get(key, '0'))
            quantity = D(Fraction(cash) // (Fraction(price) * (1 + Fraction(intent['fee_rate'])) * Fraction(step))) * step
            if quantity <= 0:
                continue
            rule = a.config.rules[pair]
            if quantity < rule.min_base or quantity * price < rule.min_quote:
                # Owner dust stays with this exact owner. Never subsidize another.
                continue
            target = quantity * price * (ONE + intent['fee_rate'])
            allocations.append(dict(root=root, funding_owner=funding_owner, quantity=str(quantity), target=str(target),
                                    child=None, cohort=None, state='PLANNED'))
        allocated_units = sum((D(p['quantity']) for p in allocations), ZERO)
        resulting_units = position['original_units'] + position['rebuilt_units'] + allocated_units
        if allocated_units > intent['quantity'] or resulting_units <= position['snapshot_units'] or resulting_units < position['rebuild_target']:
            raise Blocked('Own-cash cohort partitions cannot meet the canonical rebuild target')
        total_target = sum((D(p['target']) for p in allocations), ZERO)
        a._budget_admission(pair, total_target, 'psm:' + pair)
        meta['journal'] = book.dumps()
        meta['action'] = dict(order=event['order'], side='buy', children=[], allocations=allocations,
                              quantity=str(intent['quantity']), price=str(price), fee_ceiling=event['max_fee_rate'],
                              at=_int_time(a.now), completed=False, root_cohorts={},
                              episode=meta['episode'], signal_event=meta.get('rebuild_signal_event'),
                              decision_bar=bar.completed_at)
        a._save('psm_rebuild_intent', {'pair': pair, 'order': event['order'], 'partitions': len(allocations)})
        return self._seal_rebuild(pair)

    def _seal_rebuild(self, pair):
        a = self.adapter
        meta, bar = self._meta(pair), a._bar(pair)
        action = meta['action']
        action.update(sealed=False, observed_open=None, decision_bar=bar.completed_at)
        total_target = sum((D(p['target']) for p in action['allocations']), ZERO)
        a._admit_liquidity(pair, total_target, bar)
        action['capacity'] = str(total_target)
        a._save('psm_rebuild_batch_preparing', {'pair': pair, 'order': action['order']})
        native_position = self._planned_native_position(pair)
        new_position = native_position not in a._asset(pair)['positions']
        price = D(action['price'])
        for allocation in action['allocations']:
            root = allocation['root']
            quantity, target = D(allocation['quantity']), D(allocation['target'])
            key = root + '|' + allocation['funding_owner']
            if self.funding_cash(pair).get(key, ZERO) - D(meta.get('episode_cash_offsets', {}).get(key, '0')) < target:
                raise Blocked('A rebuild partition may spend only its own actual sale cash')
            cid = action['root_cohorts'].get(root)
            if cid is None:
                members = [c for c in a.state['cohorts'].values() if c['pair'] == pair and self._root_cohort(c['id']) == root]
                live = [c for c in members if a._cohort_lots(c)]
                if len(live) > 1:
                    raise Blocked('Root cohort has multiple active successor partitions')
                generation = max(c.get('generation', 1) for c in members) + 1
                cid = live[0]['id'] if live else f'{root}:generation:{generation}'
                if live:
                    cohort = live[0]
                    cohort['pending_psm_generation'] = dict(generation=generation, episode=meta['episode'])
                else:
                    original = a.state['cohorts'][root]
                    a.state['cohorts'][cid] = dict(id=cid, pair=pair, ordinal=original['ordinal'], origin='PSM_REBUILD',
                        generation=generation, anchor=None, anchor_history=[], lot_ids=[], last_rung=0,
                        rung=None, status='PSM_PENDING', profit_state=None,
                        pending_psm_generation=dict(generation=generation, episode=meta['episode']))
                cohort = a.state['cohorts'][cid]
                cohort.update(root_cohort_id=root, psm_parent=meta['campaign'],
                              original_cohort_ids=[root], psm_episode=meta['episode'])
                action['root_cohorts'][root] = cid
            allocation['cohort'] = cid
            a._save('psm_successor_cohort', {'pair': pair, 'cohort': cid, 'root': root})
            before = set(a.state['orders'])
            try:
                a._new_buy(pair, cid, 'PSM_REBUILD', 'psm:' + pair, target, price, quantity,
                           decision_at=action['at'], capacity=total_target, funding_owner=allocation['funding_owner'], position_id=native_position)
            finally:
                candidates = [o for oid, o in a.state['orders'].items() if oid not in before and o['cohort'] == cid]
                if candidates:
                    allocation['child'], allocation['state'] = candidates[0]['id'], 'BOUND'
                self._bind_rebuild_child(pair)
        action['sealed'] = True
        a._save('psm_rebuild_batch_sealed', {'pair': pair, 'order': action['order'], 'children': action['children']})
        # Child authorization can open a native successor after this boundary's
        # account-wide refresh. Publish real, complete native proposal health
        # only once the whole batch is sealed, before another asset is decided.
        if new_position:
            a.refresh_accounting(pair, now=a.now)
        return deepcopy(a.state['orders'][action['children'][0]])

    def continue_rebuild(self, pair, *, now):
        return self._pending_child(pair, 'buy', now)

    def _bind_rebuild_child(self, pair):
        a = self.adapter
        meta = self._meta(pair)
        action = meta['action']
        if not action or action['side'] != 'buy':
            return
        changed = False
        for allocation in action['allocations']:
            cid = allocation.get('cohort')
            if not cid:
                continue
            candidates = [o for o in a.state['orders'].values() if o['kind'] == 'PSM_REBUILD'
                          and o['cohort'] == cid and o['decision_at'] >= action['at']
                          and self._funding_owner(o) == allocation['funding_owner']
                          and (o.get('psm_order') in (None, action['order']))]
            if len(candidates) > 1:
                raise Blocked('Multiple native orders claimed one PSM rebuild partition')
            for order in candidates:
                order.update(psm_order=action['order'], psm_position=meta['position'],
                             fixed_quantity=True, psm_fee_ceiling=action['fee_ceiling'],
                             psm_root_cohort=allocation['root'], funding_owner=allocation['funding_owner'])
                allocation['child'], allocation['state'] = order['id'], 'BOUND'
                if order['id'] not in action['children']:
                    action['children'].append(order['id'])
                    changed = True
        if changed:
            a._save('psm_native_buy_binding', {'pair': pair, 'order': action['order']})

    def validate_fill(self, order_id, *, quantity, price, fee):
        a = self.adapter
        order = a.state['orders'][order_id]
        if order['kind'] not in {'PSM_EXIT', 'PSM_REBUILD'}:
            return
        pair = order['pair']
        action = self._meta(pair)['action']
        if not action or not action.get('sealed'):
            raise Blocked('PSM synthetic fill requires a completely sealed parent batch')
        if order.get('psm_position') != self._meta(pair)['position'] or not order.get('psm_order'):
            raise Blocked('PSM native order has no exact reducer binding')
        p = self._position(pair)
        if p['pending'] != order['psm_order']:
            raise Blocked('PSM native fill has no pending reducer intent')
        intent = p['orders'][order['psm_order']]
        quantity, price, fee = decimal(quantity, positive=True), decimal(price, positive=True), decimal(fee)
        if quantity * price < a.config.rules[pair].min_quote:
            raise Blocked('PSM fill fragment below declared minimum rounded-fee notional')
        if fee > quantity * price * intent['fee_rate']:
            raise Blocked('PSM fee exceeds conservative reducer ceiling')
        if order['side'] == 'SELL' and price < intent['limit']:
            raise Blocked('PSM sell below authorized reducer bound')
        if order['side'] == 'BUY' and price > intent['limit']:
            raise Blocked('PSM buy above authorized reducer bound')
        if order['side'] == 'BUY':
            key = self._root_cohort(order['cohort']) + '|' + self._funding_owner(order)
            cash = self.funding_cash(pair).get(key, ZERO) - D(self._meta(pair).get('episode_cash_offsets', {}).get(key, '0'))
            if quantity * price + fee > cash:
                raise Blocked('PSM fill would consume another root/funding owner cash')
        if intent['filled'] + quantity > intent['quantity']:
            raise Blocked('PSM fill exceeds frozen reducer quantity')

    def sync(self):
        """Idempotently mirror only actual receipts and terminal actions.

        No periodic valuation event is journaled. Receipt IDs are deterministic;
        replay repairs a crash between native reconciliation and reducer storage.
        """
        if self._syncing:
            return
        self._syncing = True
        a = self.adapter
        try:
            for pair in list(a.state['psm']):
                meta = self._meta(pair)
                self.cohort_cash(pair)
                if meta.get('mode') == 'REPAIR':
                    # Completed source episodes are immutable historical books.
                    # New native repair receipts belong to the external campaign
                    # and enter a fresh exact snapshot only at the next handoff.
                    continue
                self._bind_rebuild_child(pair)
                book = self._book(pair)
                changed = False
                seen = {event['id'] for event in book.journal}
                for raw in a._receipts():
                    order = a.state['orders'][raw['order_id']]
                    if order['pair'] != pair or order.get('psm_position') != meta['position']:
                        continue
                    event_id = 'psm-fill:' + raw['trade_id']
                    if event_id in seen:
                        continue
                    occurred_at = _int_time(datetime.fromisoformat(raw['timestamp']).timestamp())
                    # Reducer time is event observation order. A genuinely late
                    # receipt must still be retained after newer terminal events;
                    # its original occurrence time remains explicit evidence.
                    at = _int_time(a.now)
                    event = dict(id=event_id, kind='fill', at=at, occurred_at=occurred_at, position=meta['position'],
                                 owner=a.config.owner, order=order['psm_order'], fill=raw['trade_id'],
                                 units=raw['amount'], price=raw['price'], fee=raw['fee_paid'], fee_currency='quote')
                    try:
                        book.apply(event)
                    except PSM.Blocked as exc:
                        raise Blocked('PSM receipt replay failed: ' + str(exc)) from exc
                    seen.add(event_id)
                    changed = True
                if changed:
                    meta['journal'] = book.dumps()
                    a._save('psm_receipts_reconciled', {'pair': pair})
                if book.positions[meta['position']]['quarantine']:
                    raise Blocked('PSM reducer quarantined actual native receipts')
                action = meta['action']
                if action and book.positions[meta['position']]['orders'][action['order']]['status'] != 'PENDING':
                    self._finish_action(pair)
                    action = meta['action']
                if action and action['side'] == 'buy':
                    self._refresh_rebuild_anchors(pair)
                if action and action.get('sealed') and action['children'] and (action['side'] == 'sell' or all(p['state'] != 'PLANNED' for p in action['allocations'])) and all(
                        a.state['orders'][oid]['status'] in {'FILLED', 'CANCELED', 'REJECTED'} for oid in action['children']):
                    self._finish_action(pair)
                self.assert_reconciled(pair)
                if self._position(pair)['phase'] == 'COMPLETE':
                    self._archive_complete(pair)
        finally:
            self._syncing = False

    def assert_reconciled(self, pair):
        a = self.adapter
        if self._meta(pair).get('mode') == 'REPAIR':
            self.cohort_cash(pair)
            return True
        p = self._position(pair)
        native_units = sum((l.base_remaining for l in a._lots(pair)), ZERO)
        if native_units != p['original_units'] + p['rebuilt_units']:
            raise Blocked('PSM/native inventory reconciliation mismatch')
        receivable = p['sell_gross'] - p['sell_fees'] - p['settled_sell_cash']
        cash_offset = sum((D(v) for v in self._meta(pair).get('episode_cash_offsets', {}).values()), ZERO)
        if a.cash_pools().get('psm:' + pair, ZERO) != p['cash'] + receivable + cash_offset:
            raise Blocked('PSM owner cash reconciliation mismatch')
        if any(o['pool'] != 'psm:' + pair for o in a.state['orders'].values()
               if o['pair'] == pair and o['kind'] in {'PSM_EXIT', 'PSM_REBUILD'}):
            raise Blocked('PSM owner cash escaped its same-asset pool')
        return True

    def complete(self, pair, *, now):
        self._prepare(pair, now)
        a = self.adapter
        if a._pending(pair) or self._meta(pair)['action'] is not None:
            raise Blocked('Every native PSM child must be terminal before handback')
        result = self._apply(pair, self._event(pair, 'complete'))
        self._archive_complete(pair)
        return result

    def _refresh_rebuild_anchors(self, pair):
        a = self.adapter
        action = self._meta(pair)['action']
        changed = False
        grouped = {}
        for allocation in action['allocations']:
            if allocation.get('child'):
                grouped.setdefault(allocation['cohort'], []).append(allocation['child'])
        for cid, ids in grouped.items():
            ids = sorted({*ids, *(o['id'] for o in a.state['orders'].values()
                                 if o['kind'] == 'PSM_REBUILD' and o['cohort'] == cid
                                 and o.get('psm_position') == self._meta(pair)['position'])})
            if any(a.state['orders'][oid]['status'] not in {'FILLED', 'CANCELED', 'REJECTED'} for oid in ids):
                continue
            cohort = a.state['cohorts'][cid]
            acquired = [raw for oid in ids for raw in a._receipts(oid)]
            if not acquired:
                # A canceled unfilled rebuild cannot manufacture a fresh
                # generation/anchor from the original retained dust alone.
                continue
            lots = a._cohort_lots(cohort)
            units = sum((lot.base_remaining for lot in lots), ZERO)
            if units and cohort.get('pending_psm_anchor', {}).get('receipt_orders') != sorted(ids):
                anchor = sum((lot.cost_basis_remaining_quote for lot in lots), ZERO) / units
                acquired_at = max(_int_time(datetime.fromisoformat(raw['timestamp']).timestamp()) for raw in acquired)
                cohort['pending_psm_anchor'] = dict(price=str(anchor), receipt_orders=sorted(ids), at=a.now,
                                                    acquired_at=acquired_at)
                changed = True
        if changed:
            a._save('psm_successor_anchors', {'pair': pair})

    def _archive_complete(self, pair):
        a = self.adapter
        meta = self._meta(pair)
        if meta.get('mode') == 'REPAIR':
            return
        if self._position(pair)['phase'] != 'COMPLETE' or meta['action'] is not None or a._pending(pair):
            raise Blocked('Canonical episode cannot hand back while incomplete')
        self.assert_reconciled(pair)
        if not any(e['episode'] == meta['episode'] for e in meta['archived_episodes']):
            journal = meta['journal']
            meta['archived_episodes'].append(dict(episode=meta['episode'], position=meta['position'],
                journal=journal, journal_sha256=hashlib.sha256(journal.encode()).hexdigest(),
                source_sha256=SOURCE_SHA256, handoff_lineage=deepcopy(meta['episode_lineage']),
                handoff_cohorts=deepcopy(meta['episode_cohorts']),
                native_position_chain=list(a._asset(pair)['positions']), completed_at=_int_time(a.now)))
        for cohort in a.state['cohorts'].values():
            pending = cohort.get('pending_psm_generation')
            if cohort['pair'] != pair or not pending or pending['episode'] != meta['episode']:
                continue
            anchor = cohort.get('pending_psm_anchor')
            if not anchor or not a._cohort_lots(cohort):
                continue
            cohort.setdefault('generation_history', []).append(dict(generation=cohort.get('generation', 1),
                anchor=cohort['anchor'], last_rung=cohort['last_rung'], rung=deepcopy(cohort['rung'])))
            cohort.update(generation=pending['generation'], anchor=anchor['price'],
                          last_rung=0, rung=None, profit_state=None, status='OWNED',
                          acquired_at=anchor['acquired_at'])
            cohort.setdefault('anchor_history', []).append(dict(generation=pending['generation'], **anchor))
            cohort['anchor_receipt_orders'] = anchor['receipt_orders']
            del cohort['pending_psm_generation']
            del cohort['pending_psm_anchor']
        meta['mode'] = 'REPAIR'
        a._asset(pair)['last_native_decision'] = None
        a._asset(pair)['last_repair_bar'] = None
        a._save('psm_episode_handback', {'pair': pair, 'episode': meta['episode']})

    def next_cycle(self, pair, *, decision_ref, falling_knife, repair_eligible, policy_ref, now):
        if self._meta(pair).get('mode') == 'REPAIR':
            raise Blocked('Native repair handback requires a fresh reducer episode via start, never same-book next_cycle')
        self._prepare(pair, now)
        a = self.adapter
        meta = self._meta(pair)
        if falling_knife is not True or repair_eligible is not False or not decision_ref or policy_ref != meta['policy_ref']:
            raise Blocked('Repeated PSM cycle requires the same frozen policy and explicit fallback decision')
        if a._pending(pair) or meta['action'] is not None:
            raise Blocked('Prior PSM/native action incomplete')
        native = a._asset(pair)['last_native_decision']
        if not native or not 0 <= a.now - native['at'] <= a.config.max_mark_age_seconds:
            raise Blocked('Fresh native lifecycle decision required for next PSM cycle')
        if native['action'] in {'EXIT', 'PROTECT_PROFIT'}:
            raise Blocked('Native whole-position profit protection outranks PSM')
        p = self._position(pair)
        book = self._book(pair)
        event = self._event(pair, 'next_cycle', reconciled=True, pending_orders=[],
                            inventory_bucket='ACTIVE_TRADE', confirmed_units=str(p['original_units'] + p['rebuilt_units']),
                            confirmed_cash=str(p['cash']), decision_ref=decision_ref)
        try:
            book.apply(event)
            book.apply(self._event(pair, 'handoff', vcap_eligible=False, falling_knife=True,
                                   reconciled=True, pending_orders=[],
                                   confirmed_units=str(p['original_units'] + p['rebuilt_units']), decision_ref=decision_ref))
        except PSM.Blocked as exc:
            raise Blocked('Pinned PSM reducer: ' + str(exc)) from exc
        meta['journal'] = book.dumps()
        a._save('psm_next_cycle', {'pair': pair, 'decision_ref': decision_ref})
        return self._position(pair)

    def _native_realized(self, pair):
        a = self.adapter
        return sum((a.ledger.snapshot(pid).realized_pnl_quote for pid in a._asset(pair)['positions']), ZERO)

    def report(self, pair, *, now):
        a = self.adapter
        a.advance(now)
        reconciliation_error = None
        try:
            self.sync()
        except Blocked as exc:
            # Actual contradictory evidence is retained. Diagnostics remain
            # readable while all mutation entry points continue to fail closed.
            reconciliation_error = str(exc)
        raw_mark = a.state['marks'].get(pair)
        if raw_mark is None:
            raise Blocked('PSM diagnostic has no last-observed mark')
        mark_at = _int_time(raw_mark['completed_at'])
        age = _int_time(a.now) - mark_at
        if age < 0:
            raise Blocked('PSM diagnostic cannot use a future mark')
        meta = self._meta(pair)
        # Valuation-only widening is explicit in the returned freshness fields;
        # order and transition admission still uses the unchanged strict _bar.
        report = self._book(pair).report(meta['position'], price=str(raw_mark['close']),
                                         quote_at=mark_at, now=_int_time(a.now),
                                         max_age=max(a.config.max_mark_age_seconds, age))
        native_realized = self._native_realized(pair)
        native_basis = a.deployed_basis(pair)
        reducer_basis = report['retained_original_basis'] + report['rebuilt_acquisition_cost']
        actual_fees = sum((D(raw['fee_paid']) for raw in a._receipts()
                           if a.state['orders'][raw['order_id']]['pair'] == pair
                           and a.state['orders'][raw['order_id']]['kind'] in {'PSM_EXIT', 'PSM_REBUILD'}), ZERO)
        commitments = a.capital_commitments()
        retained_cap_block = (commitments['total'] > a.config.total_budget_cap
                              or commitments['pairs'][pair] > a.config.pair_budget_cap
                              or commitments['recovery'] > a.config.recovery_cap)
        return dict(report, native_lifetime_realized_pnl=native_realized,
                    native_psm_realized_pnl=native_realized - D(meta['initial_native_realized']),
                    native_remaining_basis=native_basis,
                    reducer_basis_difference=reducer_basis - native_basis,
                    basis_authority='native exact source-owner lots; reducer pro-rata is diagnostic',
                    original_native_basis=D(meta['original_basis']),
                    native_position_chain=tuple(a._asset(pair)['positions']),
                    owner_pool='psm:' + pair, actual_fees=actual_fees,
                    current_native_units=sum((lot.base_remaining for lot in a._lots(pair)), ZERO),
                    original_campaign_units=D(meta['original_units']),
                    campaign_original_basis=D(meta['original_basis']),
                    cohort_owner_cash=self.cohort_cash(pair),
                    funding_owner_cash=self.funding_cash(pair), recovery_owner_cash=self.recovery_cash(),
                    prior_episode_retained_cash=sum((D(v) for v in meta.get('episode_cash_offsets', {}).values()), ZERO),
                    episode=meta['episode'], archived_episodes=len(meta['archived_episodes']),
                    execution_authority=meta['mode'],
                    capital_admission_status='BLOCKED_RETAINED_COMMITMENT' if retained_cap_block else 'WITHIN_CAPS',
                    long_cash_waiting=meta['mode'] == 'PSM' and report['phase'] == 'REBUILD' and report['confirmed_cash'] > ZERO,
                    reducer_episode_status='ARCHIVED_IMMUTABLE' if meta['mode'] == 'REPAIR' else 'ACTIVE',
                    max_fee_rate=self._fee_ceiling(pair), source_sha256=SOURCE_SHA256,
                    reconciliation_error=reconciliation_error,
                    mark_age_seconds=age, mark_is_stale=age > a.config.max_mark_age_seconds,
                    mark_status='STALE_LAST_OBSERVED' if age > a.config.max_mark_age_seconds else 'FRESH',
                    pending_native_children=tuple(o['id'] for o in a._pending(pair)),
                    journal_events=len(self._book(pair).journal))
