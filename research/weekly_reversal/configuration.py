"""Outcome-blind weekly configuration; historical admission remains separate."""
from datetime import datetime
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from spectra_core.next_entry_allocator import Member, MembershipSnapshot
from spectra_core.reversal_signals import SignalPolicy
from .causal_tape import ROSTER
from .owned_spot.model import AdapterConfig, TradingRule


def utc(value):return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp())
WARMUP_START=utc('2026-05-03T00:00:00Z')
START=utc('2026-06-03T00:00:00Z')
END=utc('2026-06-10T00:00:00Z')
DATA_START=utc('2025-12-01T00:00:00Z')
INPUT_SHA256='53dd88099aaf140d677dfd72ef88e1caa626c9651319d194798558bca8f45972'


def configuration(rule_file,*,expected_sha256):
    raw=Path(rule_file).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('Frozen rule source changed')
    records=json.loads(raw)
    if len(records)!=len(ROSTER) or {r['asset'] for r in records}!=set(ROSTER):raise ValueError('Complete unchanged reference roster required')
    if any(r['status']!='online' or r['execution_venue']!='international' for r in records):raise ValueError('Unexpected observed rule scope')
    rules={r['asset']+'-USD':TradingRule(D(r['base_increment']),D(r['minimum_base']),D(r['minimum_quote_usd']),D(r['price_tick_usd'])) for r in records}
    return AdapterConfig(fee_rate=D('.008'),fee_quantum=D('.00001'),rules=rules,
        seed_count=4,seed_ticket=D('16.66666666'),expected_slippage_bps=D('15'),
        execution_delay_seconds=300,max_order_age_seconds=300,
        owner='weekly-reversal-research')


def membership(config):
    return MembershipSnapshot('weekly-20260603-fixed-asset-slots-plus-vcap',config.account,'USD',START,END,
        tuple(Member(p,config.owner+':'+p,'POSITION_ALLOCATION') for p in sorted(config.rules))+
        (Member('VCAP',config.owner+':VCAP','VCAP'),))


def arm_policy(arm):
    if arm not in {'CONTROL_BBWP50_SOFT_RSI','LOOSE_BBWP20_SOFT_RSI'}:raise ValueError('Unregistered weekly arm')
    return SignalPolicy(enabled=True,bbwp_minimum=50 if arm.startswith('CONTROL') else 20,
        bearish_evidence_minimum=2,bearish_confirmation_hours=2,rsi_soft_weight=1)
