"""Simulation-only event reducer. No exchange, network, credentials or order submission.

Events are caller attestations, not an exchange reconciliation service. Amounts are
Decimal strings. Quote-currency fees only. The journal is the restart authority.
"""
from copy import deepcopy
from decimal import Decimal, localcontext, Context, ROUND_HALF_EVEN
import json
import sys
from fractions import Fraction

ZERO = Decimal('0')
ARITHMETIC = Context(prec=256, rounding=ROUND_HALF_EVEN)

def canonical_asset(asset):
    require(isinstance(asset, str) and asset.endswith('/USD'), 'explicit USD pair required')
    base = asset[:-4].upper()
    base = {'XBT': 'BTC', 'XXBT': 'BTC', 'XDG': 'DOGE', 'XXDG': 'DOGE',
            'XETH': 'ETH', 'XXRP': 'XRP'}.get(base, base)
    require(base and base.isalnum(), 'invalid asset')
    return base + '/USD'


class Blocked(ValueError):
    """Invalid, stale, contradictory, or unauthorized research transition."""


def dec(value):
    if not isinstance(value, str):
        raise Blocked('amounts must be Decimal strings')
    try:
        result = Decimal(value)
    except Exception as exc:
        raise Blocked('invalid decimal') from exc
    if not result.is_finite():
        raise Blocked('non-finite amount')
    require(len(result.as_tuple().digits) <= 192 and -144 <= result.as_tuple().exponent <= 24,
            'amount exceeds supported research precision')
    return result


def amount(event, key, positive=False):
    value = dec(event[key])
    if key in ('units', 'confirmed_units', 'filled_units'):
        require(len(value.as_tuple().digits) <= 96 and -72 <= value.as_tuple().exponent <= 24,
                'quantity exceeds supported research precision')
    elif key not in ('gross', 'fees', 'fee', 'confirmed_cash'):

        require(len(value.as_tuple().digits) <= 24 and -18 <= value.as_tuple().exponent <= 18,
                'input exceeds supported research precision')
    if value < 0 or (positive and value == 0):
        raise Blocked('invalid ' + key)
    return value


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def floor_step(value, step):
    return Decimal(Fraction(value) // Fraction(step)) * step


class ResearchBook:
    """One controller per account/asset, with segregated owner cash per snapshot.

    Public state access returns copies. Mutations are transactional event applications.
    Replay provides deterministic restart and idempotence, not durable storage or
    concurrency control; production use requires an atomic persisted single writer.
    """
    def __init__(self):
        self._positions = {}
        self._journal = []
        self._seen = {}
        self._claims = {}
        self._last_time = -1

    @property
    def positions(self):
        return deepcopy(self._positions)

    @property
    def journal(self):
        return deepcopy(self._journal)

    def apply(self, event):
        # JSON round-trip forbids mutable aliases; every successful event is retained.
        event = json.loads(json.dumps(event, allow_nan=False))
        require(isinstance(event.get('id'), str) and event['id'], 'event id required')
        payload = json.dumps(event, sort_keys=True, separators=(',', ':'))
        if event['id'] in self._seen:
            require(self._seen[event['id']] == payload, 'event id payload conflict')
            return self.positions
        trial = deepcopy(self)
        with localcontext(ARITHMETIC):
            trial._apply(event)
        trial._seen[event['id']] = payload
        trial._journal.append(event)
        self.__dict__.update(trial.__dict__)
        return self.positions

    def dumps(self):
        return json.dumps({'schema': 3, 'events': self._journal}, sort_keys=True, indent=2)

    @classmethod
    def loads(cls, text):
        data = json.loads(text)
        require(data.get('schema') in (1, 2, 3), 'unsupported journal schema')
        require(data['schema'] >= 2 or all(e.get('kind') != 'next_cycle' for e in data['events']),
                'repeated cycles require schema 2')
        require(data['schema'] == 3 or all(e.get('kind') != 'REALIZED_PROFIT_DISTRIBUTION' for e in data['events']),
                'profit distributions require schema 3')
        canonical = _canonical_replay_class(cls) and _bounded_replay_input(data.get('events'))
        book = cls()
        if canonical and _canonical_replay_book(cls, book):
            try:
                for event in data['events']:
                    _replay_unpublished_event(book, event)
            except Exception:
                # Invalid input keeps the original transactional failure path.
                # Leave this handler before retrying so exception context is not
                # changed. The unpublished partial book never escapes.
                pass
            else:
                return book
            book = cls()
        for event in data['events']:
            book.apply(event)
        return book

    def _quote(self, event):
        require(type(event.get('at')) is int and event['at'] >= 0, 'integer mark time required')
        price = amount(event, 'price', True)
        at, age = event['quote_at'], event['max_age']
        require(type(at) is int and type(age) is int and age >= 0,
                'integer quote time and nonnegative max age required')
        require(0 <= event['at'] - at <= age, 'stale or future quote')
        return price

    def _apply(self, e):
        require(type(e.get('at')) is int and e['at'] >= self._last_time,
                'events must have monotonic integer timestamps')
        self._last_time = e['at']
        kind = e['kind']
        if kind == 'snapshot':
            for key in ('position', 'account', 'asset', 'owner', 'provenance'):
                require(isinstance(e.get(key), str) and e[key], key + ' required')
            require(e['position'] not in self._positions, 'duplicate position')
            require(e.get('inventory_bucket') == 'ACTIVE_TRADE', 'only active-trade inventory allowed')
            normalized_asset = canonical_asset(e['asset'])
            claim = (e['account'], normalized_asset)
            require(claim not in self._claims, 'asset already controlled')
            require(e.get('reconciled') is True and e.get('pending_orders') == [],
                    'holdings and pending orders must be reconciled')
            units, basis = amount(e, 'units', True), amount(e, 'basis')
            require(len(units.as_tuple().digits) <= 24 and -18 <= units.as_tuple().exponent <= 18,
                    'snapshot exceeds supported research precision')
            self._positions[e['position']] = dict(
                owner=e['owner'], account=e['account'], asset=normalized_asset,
                provenance=e['provenance'], snapshot_units=units, snapshot_basis=basis,
                initial_units=units, initial_economic_basis=basis, cycle=1, cycle_history=[],
                cycle_snapshot_ref=e['id'], cycle_start_cash=ZERO,
                cycle_start_totals=dict(sell_gross=ZERO, sell_fees=ZERO, buy_gross=ZERO,
                                        buy_fees=ZERO, released_basis=ZERO, cumulative_distributions=ZERO),
                original_units=units, original_basis=basis, rebuilt_units=ZERO,
                rebuilt_basis=ZERO, sold_units=ZERO, released_basis=ZERO,
                sell_gross=ZERO, sell_fees=ZERO, settled_sell_cash=ZERO, buy_gross=ZERO, buy_fees=ZERO,
                sell_slippage=ZERO, buy_slippage=ZERO, cash=ZERO,
                cumulative_distributions=ZERO, distributions={},
                phase='RECOVERY', rebuild_target=units, pending=None, orders={}, fill_ids={}, signal=None,
                quarantine=[], residual_class='OWNED', last_at=e['at'])
            self._claims[claim] = e['position']
            return
        require(e.get('position') in self._positions, 'unknown position')
        p = self._positions[e['position']]
        require(e.get('owner') == p['owner'], 'owner mismatch')
        p['last_at'] = e['at']
        # Always permit actual fill/reconciliation records after a discrepancy.
        if kind not in ('fill', 'reconcile', 'cancel_requested'):
            require(not p['quarantine'], 'quarantined: reconciliation discrepancy')
        if kind == 'REALIZED_PROFIT_DISTRIBUTION':
            self._distribute(p, e)
        elif kind == 'next_cycle':
            require(p['phase'] == 'COMPLETE' and p['pending'] is None, 'prior cycle incomplete')
            require(e.get('reconciled') is True and e.get('pending_orders') == [] and
                    e.get('inventory_bucket') == 'ACTIVE_TRADE', 'fresh active holdings reconciliation required')
            actual_units = p['original_units'] + p['rebuilt_units']
            require(amount(e, 'confirmed_units') == actual_units and
                    amount(e, 'confirmed_cash') == p['cash'], 'cycle holdings or cash mismatch')
            require(isinstance(e.get('decision_ref'), str) and e['decision_ref'], 'explicit new-cycle decision required')
            # Roll current holdings into the next cycle's snapshot, without a new
            # executable claim, owner transfer, funding injection, or lifetime reset.
            current_basis = p['original_basis'] + p['rebuilt_basis']
            p['cycle'] += 1
            p['snapshot_units'] = actual_units
            p['snapshot_basis'] = current_basis
            p['original_units'] = actual_units
            p['original_basis'] = current_basis
            p['rebuilt_units'] = ZERO
            p['rebuilt_basis'] = ZERO
            p['cycle_snapshot_ref'] = e['id']
            p['cycle_start_cash'] = p['cash']
            p['cycle_start_totals'] = {k: p[k] for k in p['cycle_start_totals']}
            p['rebuild_target'] = actual_units
            p['signal'] = None
            p['residual_class'] = 'OWNED'
            p['phase'] = 'RECOVERY'
        elif kind == 'handoff':
            require(p['phase'] == 'RECOVERY', 'handoff already performed')
            require(e.get('vcap_eligible') is False and e.get('falling_knife') is True,
                    'VCAP priority; explicit fallback eligibility required')
            require(e.get('reconciled') is True and e.get('pending_orders') == [],
                    'cancel and reconcile existing patches first')
            require(amount(e, 'confirmed_units') == p['original_units'], 'holdings changed; new snapshot required')
            require(bool(e.get('decision_ref')), 'caller decision provenance required')
            p['phase'] = 'EXIT'
        elif kind == 'intent':
            require(p['pending'] is None, 'one pending order per position')
            order_id = e['order']
            require(isinstance(order_id, str) and order_id and order_id not in p['orders'], 'duplicate/empty order')
            side = e['side']
            price = self._quote(e)
            step = amount(e, 'step', True)
            minimum_units = amount(e, 'min_units')
            minimum_quote = amount(e, 'min_quote')
            fee_rate = amount(e, 'max_fee_rate')
            require(fee_rate < 1, 'fee rate must be below one')
            if side == 'sell':
                require(p['phase'] == 'EXIT', 'sell requires fallback handoff')
                quantity = floor_step(p['original_units'], step)
                reserve = ZERO
            else:
                require(side == 'buy' and p['phase'] == 'REBUILD', 'buy requires closed exit')
                signal = p['signal']
                require(signal is not None and e['at'] <= signal['expires_at'], 'explicit fresh rebuild signal required')
                require(e['at'] >= signal['at'], 'future signal')
                quantity = Decimal(Fraction(p['cash']) // (Fraction(price) * (1 + Fraction(fee_rate)) * Fraction(step))) * step
                reserve = quantity * price * (1 + fee_rate)
                min_gain = amount(e, 'min_unit_gain')
                target = max(p['rebuild_target'], p['snapshot_units'] * (1 + min_gain))
                projected_units = p['original_units'] + p['rebuilt_units'] + quantity
                require(projected_units > p['snapshot_units'] and
                        projected_units >= target,
                        'configured minimum unit gain not met')
                p['rebuild_target'] = target
            require(quantity > 0 and quantity >= minimum_units and quantity * price >= minimum_quote,
                    'rounded order below exchange minimum')
            p['orders'][order_id] = dict(side=side, quantity=quantity, limit=price,
                fee_rate=fee_rate, reserved=reserve, filled=ZERO, gross=ZERO, fees=ZERO,
                reference=price, status='PENDING', cancel_requested=False)
            p['pending'] = order_id
        elif kind == 'cancel_requested':
            require(p['pending'] == e['order'], 'no matching pending order')
            p['orders'][e['order']]['cancel_requested'] = True
            # No reservation is released until final exchange reconciliation.
        elif kind == 'fill':
            require(e.get('fee_currency') == 'quote', 'only quote-currency fees supported')
            require(e['order'] in p['orders'], 'unknown order')
            fill_id = e['fill']
            require(isinstance(fill_id, str) and fill_id, 'fill id required')
            payload = (e['order'], e['units'], e['price'], e['fee'], e['fee_currency'])
            if fill_id in p['fill_ids']:
                require(p['fill_ids'][fill_id] == payload, 'fill id payload conflict')
                return
            order = p['orders'][e['order']]
            units, price, fee = amount(e, 'units', True), amount(e, 'price', True), amount(e, 'fee')
            gross = units * price
            if order['status'] != 'PENDING':
                p['quarantine'].append('late fill after terminal reconciliation')
            if order['filled'] + units > order['quantity']:
                p['quarantine'].append('fill exceeds authorized quantity')
            if fee > gross * order['fee_rate']:
                p['quarantine'].append('fill fee exceeds authorized rate')
            if order['side'] == 'sell':
                if price < order['limit']:
                    p['quarantine'].append('sell below limit')
                # Pro-rata historical research basis, never silently reset at rebuild.
                basis = (p['original_basis'] if units == p['original_units'] else
                         units * p['snapshot_basis'] / p['snapshot_units'])
                p['original_units'] -= units
                p['original_basis'] -= basis
                p['sold_units'] += units
                p['released_basis'] += basis
                p['sell_gross'] += gross
                p['sell_fees'] += fee
                p['sell_slippage'] += units * (order['reference'] - price)
                # Sell cash remains unavailable until terminal reconciliation.
            else:
                if price > order['limit']:
                    p['quarantine'].append('buy above limit')
                p['rebuilt_units'] += units
                p['rebuilt_basis'] += gross + fee
                p['buy_gross'] += gross
                p['buy_fees'] += fee
                p['buy_slippage'] += units * (price - order['reference'])
                p['cash'] -= gross + fee
                if p['cash'] < 0:
                    p['quarantine'].append('actual cash deficit')
            order['filled'] += units
            order['gross'] += gross
            order['fees'] += fee
            p['fill_ids'][fill_id] = payload
        elif kind == 'reconcile':
            require(p['pending'] == e['order'], 'no matching pending order')
            order = p['orders'][e['order']]
            terminal = e['status']
            require(terminal in ('FILLED', 'CANCELED', 'REJECTED'), 'terminal status required')
            require(e.get('complete_fill_history') is True, 'complete exchange fill history required')
            require(amount(e, 'filled_units') == order['filled'] and amount(e, 'gross') == order['gross']
                    and amount(e, 'fees') == order['fees'], 'reconciliation totals mismatch')
            require(terminal != 'FILLED' or order['filled'] == order['quantity'], 'incomplete filled order')
            require(terminal != 'REJECTED' or order['filled'] == 0, 'rejected order has fills')
            if order['side'] == 'sell':
                p['cash'] += order['gross'] - order['fees']
                p['settled_sell_cash'] += order['gross'] - order['fees']
            order['status'] = terminal
            order['reserved'] = ZERO
            p['pending'] = None
        elif kind == 'close_exit':
            require(p['phase'] == 'EXIT' and p['pending'] is None, 'exit still pending')
            price = self._quote(e)
            step = amount(e, 'step', True)
            minimum_units, minimum_quote = amount(e, 'min_units'), amount(e, 'min_quote')
            rounded = floor_step(p['original_units'], step)
            executable = rounded > 0 and rounded >= minimum_units and rounded * price >= minimum_quote
            require(not executable, 'executable original residual must be resolved')
            p['residual_class'] = 'DUST_OWNED' if p['original_units'] > 0 else 'ZERO'
            p['phase'] = 'REBUILD'
        elif kind == 'rebuild_signal':
            require(p['phase'] == 'REBUILD' and p['pending'] is None, 'not ready for signal')
            require(e.get('qualified') is True and bool(e.get('decision_ref')), 'caller reversal decision required')
            require(type(e.get('expires_at')) is int and e['expires_at'] >= e['at'], 'invalid signal expiry')
            p['signal'] = dict(at=e['at'], expires_at=e['expires_at'], decision_ref=e['decision_ref'])
        elif kind == 'complete':
            require(p['phase'] == 'REBUILD' and p['pending'] is None, 'rebuild pending')
            actual_units = p['original_units'] + p['rebuilt_units']
            require(p['rebuilt_units'] > 0 and actual_units > p['snapshot_units'] and
                    actual_units >= p['rebuild_target'], 'actual reconstruction target not met')
            p['cycle_history'].append(dict(
                cycle=p['cycle'], snapshot_ref=p['cycle_snapshot_ref'], owner=p['owner'],
                provenance=p['provenance'], start_units=p['snapshot_units'],
                start_basis=p['snapshot_basis'], start_cash=p['cycle_start_cash'],
                end_units=actual_units, end_cash=p['cash'], target=p['rebuild_target'],
                retained_basis=p['original_basis'], rebuilt_basis=p['rebuilt_basis'],
                completed_at=e['at'], completion_ref=e['id'],
                accounting_delta={k: p[k]-v for k,v in p['cycle_start_totals'].items()}))
            p['phase'] = 'COMPLETE'
            # Claim intentionally retained. No automatic slot release or MIR cash.
        else:
            raise Blocked('unknown event kind')

    def _distribute(self, p, e):
        """Explicit outward partition; the native bridge supplies verified evidence.

        Like every reducer event this is an attestation, not access to the native
        ledger. Journal bindings and exact native economics are nevertheless
        checked here, independently of caller confirmation flags.
        """
        require(p['pending'] is None, 'distribution requires no pending action')
        for key in ('native_trade_id', 'native_realization_id', 'allocation_event_id',
                    'cash_backing_id', 'snapshot_id', 'source_proof_sha256',
                    'fill_event_id', 'reconcile_event_id', 'source_pool', 'destination_pool',
                    'root_cohort', 'funding_owner'):
            require(isinstance(e.get(key), str) and e[key], key + ' required')
        require(e.get('account') == p['account'] and canonical_asset(e.get('asset')) == p['asset'],
                'distribution account/asset mismatch')
        require(e['source_pool'] == 'psm:' + p['asset'].replace('/', '-') and
                e['destination_pool'] == 'next_entry_profit_escrow', 'invalid cash partition destination')
        require(e['native_realization_id'] == 'trade-fill:' + e['native_trade_id'],
                'native realization/trade mismatch')
        require(e.get('confirmed') is True and e.get('reconciled') is True and
                e.get('pending_orders') == [], 'confirmed terminal native profit required')
        require(e['funding_owner'] in ('seed', 'repair'), 'invalid immutable funding owner')
        require(len(e['source_proof_sha256']) == 64 and
                all(c in '0123456789abcdef' for c in e['source_proof_sha256']), 'invalid source proof digest')
        order = p['orders'].get(e.get('order'))
        require(order is not None and order['side'] == 'sell' and
                order['status'] in ('FILLED', 'CANCELED'), 'sale must be terminal and reconciled')
        fills = [x for x in self._journal if x['id'] == e['fill_event_id']]
        settlements = [x for x in self._journal if x['id'] == e['reconcile_event_id']]
        require(len(fills) == len(settlements) == 1, 'missing unique journal profit backing')
        fill, settlement = fills[0], settlements[0]
        require(fill['kind'] == 'fill' and fill['position'] == e['position'] and
                fill['order'] == e['order'] and fill['fill'] == e['native_trade_id'] and
                fill['fee_currency'] == 'quote', 'journal native fill mismatch')
        require(settlement['kind'] == 'reconcile' and settlement['position'] == e['position'] and
                settlement['order'] == e['order'] and settlement['complete_fill_history'] is True and
                settlement['status'] == order['status'], 'journal settlement mismatch')
        require(type(e.get('settled_at')) is int and
                fill.get('occurred_at', fill['at']) <= e['settled_at'] <= settlement['at'] <= e['at'] and
                fill['at'] <= settlement['at'], 'profit settlement chronology mismatch')
        gross = dec(e['gross_realized_proceeds'])
        principal = dec(e['returned_principal'])
        entry_fees, exit_fees = dec(e['entry_fees']), dec(e['exit_fees'])
        profit, transfer = dec(e['fee_net_realized_profit']), dec(e['amount'])
        require(principal >= 0 and entry_fees >= 0 and exit_fees >= 0 and
                gross == dec(fill['units']) * dec(fill['price']) and exit_fees == dec(fill['fee']),
                'profit economics disagree with native fill')
        require(profit == gross - principal - entry_fees - exit_fees and transfer == profit and profit > 0,
                'only exact positive fee-net realized profit may leave')
        before, after = dec(e['cash_before']), dec(e['cash_after'])
        require(before == p['cash'] and after == before - transfer and after >= 0,
                'distribution cash mismatch or insufficient settled cash')
        for previous in p['distributions'].values():
            for key in ('native_trade_id', 'native_realization_id', 'allocation_event_id', 'cash_backing_id'):
                require(previous[key] != e[key], 'duplicate profit distribution proof: ' + key)
        p['cash'] = after
        p['cumulative_distributions'] += transfer
        p['distributions'][e['id']] = deepcopy(e)

    def report(self, position, *, price, quote_at, now, max_age):
        with localcontext(ARITHMETIC):
            mark = self._quote(dict(price=price, quote_at=quote_at, at=now, max_age=max_age))
            p = self._positions[position]
            require(now >= p['last_at'], 'mark predates position state')
            units = p['original_units'] + p['rebuilt_units']
            unreconciled = p['sell_gross'] - p['sell_fees'] - p['settled_sell_cash']
            marked = units * mark + p['cash'] + unreconciled
            return dict(units=units, unit_gain=units - p['snapshot_units'],
                unit_gain_fraction=units / p['snapshot_units'] - 1,
                original_economic_basis=p['initial_economic_basis'],
                lifetime_unit_gain=units-p['initial_units'],
                lifetime_unit_gain_fraction=units/p['initial_units']-1,
                cycle=p['cycle'], completed_cycles=len(p['cycle_history']),
                cycle_start_units=p['snapshot_units'], cycle_start_basis=p['snapshot_basis'],
                retained_original_basis=p['original_basis'], rebuilt_acquisition_cost=p['rebuilt_basis'],
                realized_sell_pnl=p['sell_gross'] - p['sell_fees'] - p['released_basis'],
                marked_usd=marked, current_wallet_marked_usd=marked,
                cumulative_distributions=p['cumulative_distributions'],
                source_value_including_distributions=marked + p['cumulative_distributions'],
                total_economic_pnl=marked + p['cumulative_distributions'] - p['initial_economic_basis'],
                holding_counterfactual=p['initial_units'] * mark,
                value_vs_holding=marked + p['cumulative_distributions'] - p['initial_units'] * mark,
                confirmed_cash=p['cash'], unreconciled_sell_receivable=unreconciled,
                available_cash=(ZERO if p['pending'] or p['quarantine'] else max(ZERO, p['cash'])),
                fees=p['sell_fees'] + p['buy_fees'],
                signed_slippage=p['sell_slippage'] + p['buy_slippage'],
                residual_class=p['residual_class'], phase=p['phase'], quarantine=tuple(p['quarantine']))


# Only the unchanged canonical class owns this unpublished replay fast path.
# Subclasses, custom constructors/properties/copy hooks, and class monkeypatches
# keep the original public apply loop and its observable behavior.
_REPLAY_CLASS = ResearchBook
_REPLAY_CLASS_NAMESPACE = dict(vars(ResearchBook))
_REPLAY_METHOD_CODES = tuple(
    (function, function.__code__)
    for value in _REPLAY_CLASS_NAMESPACE.values()
    for function in ((value.__func__,) if isinstance(value, classmethod) else
                     (value.fget,) if isinstance(value, property) else (value,))
    if hasattr(function, '__code__'))


def _canonical_replay_class(cls):
    if cls is not _REPLAY_CLASS or cls.__bases__ != (object,):
        return False
    namespace = dict(vars(cls))
    # deepcopy/copyreg lazily adds this benign cache during normal public apply.
    # It must not disable fast replay after the first ordinary mutation.
    if '__slotnames__' in namespace:
        slotnames = namespace.pop('__slotnames__')
        if type(slotnames) is not list or slotnames:
            return False
    return (namespace.keys() == _REPLAY_CLASS_NAMESPACE.keys()
            and all(namespace[name] is value for name, value in _REPLAY_CLASS_NAMESPACE.items())
            and all(function.__code__ is code for function, code in _REPLAY_METHOD_CODES))


def _canonical_replay_book(cls, book):
    if type(book) is not _REPLAY_CLASS or not _canonical_replay_class(cls):
        return False
    state = vars(book)
    return (state.keys() == {'_positions', '_journal', '_seen', '_claims', '_last_time'}
            and all(type(state[name]) is dict and not state[name]
                    for name in ('_positions', '_seen', '_claims'))
            and type(state['_journal']) is list and not state['_journal']
            and type(state['_last_time']) is int and state['_last_time'] == -1)


def _bounded_replay_input(events):
    # Preserve the original path for introspection and adversarially deep JSON.
    # Removing a full-book deepcopy must not newly accept deep metadata that the
    # old loader rejects with RecursionError on a later event.
    if type(events) is not list or sys.gettrace() is not None or sys.getprofile() is not None:
        return False
    frame = sys._getframe()
    depth = 0
    while frame is not None:
        depth += 1
        frame = frame.f_back
    if sys.getrecursionlimit() - depth < 256:
        return False
    for event in events:
        pending = [(event, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 24:
                return False
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
    return True


def _replay_unpublished_event(book, event):
    """Original apply validation without copies of a fresh, unexposed book.

    Call only from canonical loads. On failure loads discards the complete book
    and executes the original transactional path to preserve failure behavior.
    Public apply remains the atomic mutation API for every existing book.
    """
    event = json.loads(json.dumps(event, allow_nan=False))
    require(isinstance(event.get('id'), str) and event['id'], 'event id required')
    payload = json.dumps(event, sort_keys=True, separators=(',', ':'))
    if event['id'] in book._seen:
        require(book._seen[event['id']] == payload, 'event id payload conflict')
        return
    with localcontext(ARITHMETIC):
        book._apply(event)
    book._seen[event['id']] = payload
    book._journal.append(event)
