"""Exact native fingerprint warmup without a SQLite write for every observation.

Only the unowned, empty initialization state is eligible. Every actual observed
five-minute bar is fed to the native update in the original order. Missing bars
stay absent. A single final state transaction publishes the complete warmup.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
import hashlib
import json

from .owned_spot import native as N
from .owned_spot.adapter import serial
from .owned_spot.model import CompletedBar, Blocked, ZERO
from spectra_core.reversal_signals import IndicatorHistory, SignalGate


def prime_empty_account(adapter,tape,*,signal_policy=None):
    if (adapter.state['assets'] or adapter.state['orders'] or adapter._receipts()
            or adapter.state['marks'] or adapter.state.get('warmup_receipt')):
        raise Blocked('Warmup requires untouched empty initialization state')
    if set(adapter.config.rules)!=set(tape.pairs) or adapter.now>tape.warmup_start:
        raise Blocked('Warmup clock/universe mismatch')
    state=deepcopy(adapter.state)
    histories={p:IndicatorHistory(p) for p in tape.pairs}
    counts={p:0 for p in tape.pairs}
    last_at=tape.warmup_start
    for at,bars in tape.warmup_bars():
        last_at=at
        for raw in bars:
            if raw is None: continue
            bar=CompletedBar(raw.pair,raw.opened_at,raw.completed_at,raw.close,raw.quote_volume_proxy,raw.completed_at)
            bar.validate_at(at,adapter.config.max_mark_age_seconds)
            state['marks'][bar.pair]=serial(asdict(bar))
            history=state.setdefault('volume_history',{}).setdefault(bar.pair,[])
            history.append([bar.completed_at,str(bar.quote_volume)])
            history[:]=[item for item in history if item[0]>bar.completed_at-86400]
            observation=N.asset_fingerprint.AssetObservation(trading_pair=bar.pair,timestamp=float(bar.completed_at),
                price=float(bar.close),quote_volume_24h=float(sum((D(item[1]) for item in history),ZERO)))
            fingerprints=state.setdefault('fingerprints',{});previous=fingerprints.get(bar.pair)
            fingerprint=N.asset_fingerprint.AssetFingerprint.from_dict(previous).update(observation) if previous else N.asset_fingerprint.AssetFingerprint.cold_start(observation)
            fingerprints[bar.pair]=fingerprint.to_dict();counts[bar.pair]+=1
    hours={p:[] for p in tape.pairs}
    gate=SignalGate(signal_policy) if signal_policy is not None else None
    observations={}
    for at,bars in tape.warmup_hours():
        for pair,bar in zip(tape.pairs,bars):
            if bar is None: continue
            observation=histories[pair].push(bar,observed_at=at)
            observations[pair]=asdict(observation)
            if gate is not None: gate.evaluate(observation,now=at)
            hours[pair].append(asdict(bar))
    state['next_entry_completed_hours']=hours
    state['weekly_signal_histories']={p:h.dumps() for p,h in histories.items()}
    state['weekly_last_signal_observations']=observations
    if gate is not None: state['weekly_signal_gate']=gate.dumps()
    state['warmup_receipt']={'start':tape.warmup_start,'end':tape.start,'counts':counts,
        'method':'EVERY_OBSERVED_BAR_NATIVE_UPDATE_SINGLE_ATOMIC_PUBLICATION',
        'synthetic':tape.synthetic}
    if last_at!=tape.start: raise Blocked('Warmup did not reach exact evaluation boundary')
    adapter.state=state;adapter.advance(tape.start);adapter._save('weekly_warmup_complete',state['warmup_receipt'])
    return histories
