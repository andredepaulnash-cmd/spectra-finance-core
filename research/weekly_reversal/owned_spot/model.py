"""New weekly paper configuration; original annual control stays untouched."""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import math

D = Decimal
ZERO = D('0')
ONE = D('1')
BPS = D('10000')


class Blocked(ValueError):
    pass


@dataclass(frozen=True)
class RepairEvidence:
    """Frozen admission evidence, supplied by the native-backed adapter.

    All guards are explicit. An error string, missing observation or mere
    absence of a repair order is never evidence of economic fallback eligibility.
    """
    pair: str
    decision_at: int
    completed_bar_at: int
    cohort_id: str
    next_rung: int
    trigger_price: Decimal
    completed_close: Decimal
    requested_target: Decimal
    reason: str
    accounting_healthy: bool
    no_pending_action: bool
    mark_fresh: bool
    prior_bar_liquidity_valid: bool
    cohort_profit_allows_fallback: bool
    seed_barrier_complete: bool


def decimal(value, *, positive=False):
    if isinstance(value, (float, bool)):
        raise Blocked('Exact accounting inputs must be Decimal, string or integer')
    result = D(value)
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise Blocked('Invalid nonnegative accounting value')
    return result


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise Blocked('Positive finite timestamp required')
    return float(value)


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class TradingRule:
    base_step: Decimal
    min_base: Decimal
    min_quote: Decimal
    price_tick: Decimal = D("0.01")

    def __post_init__(self):
        for name in ('base_step', 'min_base', 'min_quote', 'price_tick'):
            object.__setattr__(self, name, decimal(getattr(self, name), positive=name in ('base_step','price_tick')))


class FrozenRules(dict):
    """Defensive immutable copy; still supports dataclasses.asdict serialization."""
    def _blocked(self,*args,**kwargs): raise TypeError('Trading rules are immutable')
    __setitem__=_blocked
    __delitem__=_blocked
    clear=_blocked
    pop=_blocked
    popitem=_blocked
    setdefault=_blocked
    update=_blocked
    __ior__=_blocked


@dataclass(frozen=True)
class AdapterConfig:
    fee_rate: Decimal
    fee_quantum: Decimal
    rules: dict[str, TradingRule]
    initial_cash: Decimal = D('2000')
    protected_cash: Decimal = D('500')
    total_budget_cap: Decimal = D('1500')
    pair_budget_cap: Decimal = D('750')
    recovery_cap: Decimal = D('500')
    seed_ticket: Decimal = D('50')
    seed_count: int = 10
    ladder_fraction: Decimal = D('0.05')
    participation: Decimal = D('0.01')
    max_mark_age_seconds: int = 300
    execution_delay_seconds: int = 1
    max_order_age_seconds: int = 30
    expected_slippage_bps: Decimal = D('0')
    target_net_profit_bps: Decimal = D('174')
    account: str = 'research-paper'
    connector: str = 'isolated-paper'
    owner: str = 'owned-spot-campaign'

    def __post_init__(self):
        for name in ('fee_rate','fee_quantum','initial_cash','protected_cash','total_budget_cap',
                     'pair_budget_cap','recovery_cap','seed_ticket','ladder_fraction','participation',
                     'expected_slippage_bps','target_net_profit_bps'):
            object.__setattr__(self,name,decimal(getattr(self,name),positive=name not in ('fee_rate','expected_slippage_bps')))
        object.__setattr__(self,'rules',FrozenRules(self.rules))
        reference = {'BTC','ETH','SOL','BCH','LTC','DOGE','AVAX','LINK','DOT','SHIB','XLM','HBAR','ADA','AAVE','XTZ'}
        if not self.rules or not set(self.rules) <= {asset+'-USD' for asset in reference}:
            raise Blocked('Asset outside the explicitly declared reference roster')
        if self.fee_rate >= 1 or not 0 < self.participation <= 1 or not 0 < self.ladder_fraction < 1:
            raise Blocked('Invalid fraction configuration')
        if type(self.seed_count) is not int or not 1 <= self.seed_count <= 100:
            raise Blocked('Explicit positive finite seed cohort count required')
        if self.seed_ticket * self.seed_count * len(self.rules) > D('1000'):
            raise Blocked('Fee-inclusive initial allocations exceed the $1000 seed pool')
        if self.initial_cash != 2000 or self.protected_cash != 500 or self.total_budget_cap != 1500 or self.pair_budget_cap != 750:
            raise Blocked('Frozen capital setup cannot be silently changed')
        if self.max_mark_age_seconds <= 0 or self.execution_delay_seconds < 1 or self.max_order_age_seconds <= 0:
            raise Blocked('Invalid paper clock configuration')

    def fee(self, gross):
        # Conservative, declared quote-currency fee rounding.
        return (gross * self.fee_rate / self.fee_quantum).to_integral_value(rounding=ROUND_UP) * self.fee_quantum

    def all_in(self, quantity, price):
        gross = quantity * price
        return gross + self.fee(gross)

    def quantity_for_budget(self, pair, budget, price):
        rule = self.rules[pair]
        quantity = floor_step(budget / (price * (ONE + self.fee_rate)), rule.base_step)
        while quantity > 0 and self.all_in(quantity, price) > budget:
            quantity -= rule.base_step
        return quantity


@dataclass(frozen=True)
class CompletedBar:
    pair: str
    opened_at: int
    completed_at: int
    close: Decimal
    quote_volume: Decimal
    observed_at: int
    valid: bool = True

    def __post_init__(self):
        decimal(self.close, positive=True)
        decimal(self.quote_volume)
        for value in (self.opened_at,self.completed_at,self.observed_at): timestamp(value)
        if not self.opened_at < self.completed_at <= self.observed_at:
            raise Blocked('Bar must be complete and observed causally')

    def validate_at(self, now, max_age):
        if not self.valid or self.observed_at > now or now - self.completed_at > max_age:
            raise Blocked('Missing, future or stale completed bar')


@dataclass(frozen=True)
class ObservedOpen:
    price: Decimal
    observed_at: int
    opened_at: int

    def __post_init__(self):
        decimal(self.price,positive=True)
        if self.opened_at > self.observed_at: raise Blocked('Future open is not observable')

class CapitalBlocked(Blocked):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)
