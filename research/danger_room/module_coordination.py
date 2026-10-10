"""Pure research admission contract for one continuously funded paper account.

This module neither prices orders nor executes them. Its accepted decisions are
reservations, never confirmed fills, sale proceeds, or terminal certificates.
Production capital, execution, ownership and economic-exit guards remain the
authority for the product. Only caller-supplied, already admissible proposals
may enter this research arbitration layer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Mapping


ZERO = Decimal("0")
PORTFOLIO_HEDGE_PARENT = "PORTFOLIO_HEDGE"


class Action(StrEnum):
    EXIT = "EXIT"
    CANCEL = "CANCEL"
    HEDGE = "HEDGE"
    RECOVERY = "RECOVERY"
    ENTRY = "ENTRY"


class Owner(StrEnum):
    PRODUCTIVE = "PRODUCTIVE"
    VCAP = "VCAP"
    HEDGE = "HEDGE"
    PARK = "PARK"


class ParentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FLAT_PENDING = "FLAT_PENDING"
    RESIDUAL = "RESIDUAL"
    RETIRED = "RETIRED"


class Reason(StrEnum):
    ACCOUNTING_UNHEALTHY = "ACCOUNTING_UNHEALTHY"
    ONE_INTENT_PER_PARENT = "ONE_INTENT_PER_PARENT"
    PENDING_PARENT = "PENDING_PARENT"
    PENDING_ASSET_IDENTITY = "PENDING_ASSET_IDENTITY"
    ACTIVE_ASSET_IDENTITY = "ACTIVE_ASSET_IDENTITY"
    PARENT_NOT_ACTIVE = "PARENT_NOT_ACTIVE"
    PARENT_SCOPE_MISMATCH = "PARENT_SCOPE_MISMATCH"
    PARENT_ID_REUSE = "PARENT_ID_REUSE"
    CANCEL_WITHOUT_PENDING = "CANCEL_WITHOUT_PENDING"
    PROTECTED_PROFIT = "PROTECTED_PROFIT"
    RUNG_ALREADY_CONSUMED = "RUNG_ALREADY_CONSUMED"
    SPOT_RISK_GATE = "SPOT_RISK_GATE"
    DEFENSIVE_HEDGE_GATE = "DEFENSIVE_HEDGE_GATE"
    HEDGE_NOT_ADMITTED = "HEDGE_NOT_ADMITTED"
    HEDGE_IDENTITY = "HEDGE_IDENTITY"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    OWNER_CASH = "OWNER_CASH"
    RESERVE_FLOOR = "RESERVE_FLOOR"
    MAXIMUM_SLOTS = "MAXIMUM_SLOTS"
    EXCLUDED_SPOT_ASSET = "EXCLUDED_SPOT_ASSET"


class ShortfallReason(StrEnum):
    CAPITAL = "CAPITAL"
    RESERVE = "RESERVE"
    PENDING_IDENTITY = "PENDING_IDENTITY"
    LIQUIDITY = "LIQUIDITY"
    INSUFFICIENT_ELIGIBLE_COINS = "INSUFFICIENT_ELIGIBLE_COINS"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    RISK_OR_ACCOUNTING_GATE = "RISK_OR_ACCOUNTING_GATE"
    RESEARCH_UNSPECIFIED = "RESEARCH_UNSPECIFIED"


def _money(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be a finite nonnegative Decimal")
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty canonical text")
    return value


def base_asset(pair: str) -> str:
    """Require explicit BASE-QUOTE identity; XBT is the BTC alias only."""
    _text(pair, "pair")
    parts = pair.split("-")
    if len(parts) != 2 or any(not part for part in parts) or pair != pair.upper():
        raise ValueError("pair must be canonical uppercase BASE-QUOTE")
    if any(not all(c.isalnum() or c in "_." for c in part) for part in parts):
        raise ValueError("pair contains unsupported identity characters")
    return "BTC" if parts[0] == "XBT" else parts[0]


def _sum(values: Iterable[Decimal]) -> Decimal:
    """Preserve even small reserved ownership amounts in wide Decimal states."""
    items = tuple(values)
    if any(not isinstance(v, Decimal) or not v.is_finite() for v in items):
        raise ValueError("cash arithmetic requires finite Decimal values")
    nonzero = tuple(v for v in items if v)
    if not nonzero:
        return ZERO
    low = min(v.as_tuple().exponent for v in nonzero)
    high = max(v.adjusted() for v in nonzero)
    with localcontext() as context:
        context.prec = max(28, high - low + len(str(len(items))) + 2)
        return sum(items, ZERO)


def _difference(left: Decimal, right: Decimal) -> Decimal:
    return _sum((left, right.copy_negate()))


def _cash_map(values: Mapping[Owner, Decimal], name: str) -> Mapping[Owner, Decimal]:
    result = {owner: ZERO for owner in Owner}
    for key, value in values.items():
        try:
            owner = Owner(key)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{name} has unknown cash owner") from exc
        result[owner] = _money(value, f"{name}.{owner.value}")
    return MappingProxyType(result)


@dataclass(frozen=True)
class CashBudgets:
    """Free physical cash split by exact owner, including outstanding holds.

    Held and protected amounts are disjoint subsets of each owner's cash.
    Existing reservations/canceled-but-unreleased burns belong in held cash.
    A settled sale's owner and proceeds must be established before this input
    is constructed. No mark, short proceeds, or expected fill is free cash.
    """

    cash_by_owner: Mapping[Owner, Decimal]
    held_by_owner: Mapping[Owner, Decimal] = field(default_factory=dict)
    protected_by_owner: Mapping[Owner, Decimal] = field(default_factory=dict)
    physical_reserve_floor: Decimal = ZERO
    observed_physical_cash: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("cash_by_owner", "held_by_owner", "protected_by_owner"):
            object.__setattr__(self, name, _cash_map(getattr(self, name), name))
        _money(self.physical_reserve_floor, "physical_reserve_floor")
        if self.observed_physical_cash is not None:
            _money(self.observed_physical_cash, "observed_physical_cash")
            if self.observed_physical_cash != self.physical_cash:
                raise ValueError("observed physical cash differs from exact cash-owner total")
        for owner in Owner:
            locked = _sum((self.held_by_owner[owner], self.protected_by_owner[owner]))
            if locked > self.cash_by_owner[owner]:
                raise ValueError("held plus protected cash exceeds its owner balance")

    @property
    def physical_cash(self) -> Decimal:
        return _sum(self.cash_by_owner.values())

    def spendable(self, owner: Owner) -> Decimal:
        return _sum((self.cash_by_owner[owner], self.held_by_owner[owner].copy_negate(),
                     self.protected_by_owner[owner].copy_negate()))


@dataclass(frozen=True)
class ParentState:
    parent_id: str
    pair: str
    status: ParentStatus = ParentStatus.ACTIVE
    terminal_confirmed: bool = False
    protected_profit: bool = False
    consumed_rungs: tuple[int, ...] = ()
    rung_intent_ids: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        _text(self.parent_id, "parent_id")
        if self.parent_id == PORTFOLIO_HEDGE_PARENT:
            raise ValueError("portfolio hedge identity cannot be a spot parent")
        base_asset(self.pair)
        object.__setattr__(self, "status", ParentStatus(self.status))
        object.__setattr__(self, "consumed_rungs", tuple(self.consumed_rungs))
        object.__setattr__(self, "rung_intent_ids", tuple(tuple(item) for item in self.rung_intent_ids))
        if type(self.terminal_confirmed) is not bool or type(self.protected_profit) is not bool:
            raise ValueError("parent proof flags must be bool")
        if self.terminal_confirmed and self.status not in {ParentStatus.RESIDUAL, ParentStatus.RETIRED}:
            raise ValueError("only residual or retired parents can have terminal retirement proof")
        if any(type(rung) is not int or rung < 0 for rung in self.consumed_rungs):
            raise ValueError("consumed rungs must be nonnegative integers")
        if len(set(self.consumed_rungs)) != len(self.consumed_rungs):
            raise ValueError("rungs cannot be consumed twice")
        receipt_rungs = []
        for item in self.rung_intent_ids:
            if len(item) != 2:
                raise ValueError("rung receipt must name exact rung and proposal identity")
            rung, proposal_id = item
            if type(rung) is not int or rung not in self.consumed_rungs:
                raise ValueError("rung receipt has no consumed rung")
            _text(proposal_id, "rung proposal identity")
            receipt_rungs.append(rung)
        if len(set(receipt_rungs)) != len(receipt_rungs):
            raise ValueError("rung cannot have two proposal receipts")

    @property
    def holds_identity(self) -> bool:
        return not (self.terminal_confirmed and self.status in {ParentStatus.RESIDUAL, ParentStatus.RETIRED})


@dataclass(frozen=True)
class PendingOrder:
    parent_id: str
    pair: str
    action: Action

    def __post_init__(self) -> None:
        _text(self.parent_id, "parent_id")
        base_asset(self.pair)
        object.__setattr__(self, "action", Action(self.action))
        if (self.action == Action.HEDGE) != (self.parent_id == PORTFOLIO_HEDGE_PARENT):
            raise ValueError("pending hedge must use the canonical portfolio risk identity")
        if self.action == Action.HEDGE and base_asset(self.pair) != "BTC":
            raise ValueError("this research hedge contract supports only BTC risk")


@dataclass(frozen=True)
class CoordinationState:
    parents: tuple[ParentState, ...] = ()
    pending: tuple[PendingOrder, ...] = ()
    max_positions: int = 50
    accounting_healthy: bool = False
    risk_increase_allowed: bool = True
    hedge_admitted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "parents", tuple(self.parents))
        object.__setattr__(self, "pending", tuple(self.pending))
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 50:
            raise ValueError("research spot maximum must be an integer from 1 to 50")
        for name in ("accounting_healthy", "risk_increase_allowed", "hedge_admitted"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be bool")
        parent_ids = [p.parent_id for p in self.parents]
        if len(set(parent_ids)) != len(parent_ids):
            raise ValueError("duplicate economic parent identity")
        claimed_assets = [base_asset(p.pair) for p in self.parents if p.holds_identity]
        if len(set(claimed_assets)) != len(claimed_assets):
            raise ValueError("multiple current spot parents claim the same base asset")
        parents = {p.parent_id: p for p in self.parents}
        claimed_parent_by_asset = {base_asset(p.pair): p.parent_id for p in self.parents if p.holds_identity}
        for pending in self.pending:
            parent = parents.get(pending.parent_id)
            if parent is not None and parent.pair != pending.pair:
                raise ValueError("pending order crosses economic parent scope")
            if parent is not None and not parent.holds_identity:
                raise ValueError("terminal-retired parent cannot retain a pending order")
            if pending.action != Action.HEDGE:
                asset = base_asset(pending.pair)
                claimed_parent = claimed_parent_by_asset.get(asset)
                if claimed_parent is not None and claimed_parent != pending.parent_id:
                    raise ValueError("pending order duplicates another base-asset owner")
                claimed_parent_by_asset[asset] = pending.parent_id


@dataclass(frozen=True)
class Intent:
    intent_id: str
    parent_id: str
    pair: str
    action: Action
    owner: Owner
    requested_cost: Decimal = ZERO
    rung: int | None = None
    priority: int = 0
    blocks_spot_increase: bool = False

    def __post_init__(self) -> None:
        _text(self.intent_id, "intent_id")
        _text(self.parent_id, "parent_id")
        base_asset(self.pair)
        object.__setattr__(self, "action", Action(self.action))
        object.__setattr__(self, "owner", Owner(self.owner))
        _money(self.requested_cost, "requested_cost")
        if type(self.priority) is not int:
            raise ValueError("priority must be an integer tie-break within an action")
        if type(self.blocks_spot_increase) is not bool:
            raise ValueError("blocks_spot_increase must be bool")
        if self.action in {Action.ENTRY, Action.RECOVERY, Action.HEDGE}:
            if self.requested_cost <= ZERO:
                raise ValueError("capital intent requires a positive all-in reservation")
        elif self.requested_cost != ZERO:
            raise ValueError("exit/cancel cannot spend an invented cash budget")
        if self.action == Action.RECOVERY:
            if type(self.rung) is not int or self.rung < 0:
                raise ValueError("recovery intent requires an exact nonnegative rung")
        elif self.rung is not None:
            raise ValueError("only recovery may name a ladder rung")
        if self.blocks_spot_increase and self.action != Action.HEDGE:
            raise ValueError("only an explicit portfolio hedge may pause spot increases")


@dataclass(frozen=True)
class Rejection:
    intent_id: str
    reason: Reason


@dataclass(frozen=True)
class DecisionBatch:
    accepted: tuple[Intent, ...]
    rejected: tuple[Rejection, ...]
    remaining_cash: Mapping[Owner, Decimal]
    occupied_slots: int
    maximum_slots: int

    @property
    def vacant_slots(self) -> int:
        return max(0, self.maximum_slots - self.occupied_slots)


_PRECEDENCE = {Action.EXIT: 0, Action.CANCEL: 1, Action.HEDGE: 2,
               Action.RECOVERY: 3, Action.ENTRY: 4}
_SPENDING_OWNER = {Action.ENTRY: Owner.PRODUCTIVE, Action.RECOVERY: Owner.VCAP,
                   Action.HEDGE: Owner.HEDGE}


def decide_batch(intents: Iterable[Intent], state: CoordinationState,
                 budgets: CashBudgets) -> DecisionBatch:
    """Choose one reservation per parent without assuming any execution.

    Profit/legal exit proposals precede recovery. This function does not decide
    whether an exit is economically or legally admissible; callers must supply
    exact frozen-policy proposals. Research loss exits must be identified in
    the campaign protocol before outcome evaluation.
    """
    proposals = tuple(intents)
    ids = [i.intent_id for i in proposals]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate proposal ID requires upstream idempotent reconciliation")
    parents = {p.parent_id: p for p in state.parents}
    claims = {base_asset(p.pair) for p in state.parents if p.holds_identity}
    pending_parents = {p.parent_id for p in state.pending}
    pending_assets = {base_asset(p.pair) for p in state.pending if p.action != Action.HEDGE}
    claims.update(pending_assets)
    if len(claims) > state.max_positions:
        raise ValueError("current plus pending spot claims already exceed the configured maximum")
    cash = dict(budgets.cash_by_owner)
    accepted: list[Intent] = []
    rejected: list[Rejection] = []
    decided_parents: set[str] = set()
    defensive_hedge = False
    held_total = _sum(budgets.held_by_owner.values())

    def rejection(intent: Intent) -> Reason | None:
        nonlocal defensive_hedge
        action, parent_id = intent.action, intent.parent_id
        asset = base_asset(intent.pair)
        parent = parents.get(parent_id)
        if not state.accounting_healthy:
            return Reason.ACCOUNTING_UNHEALTHY
        if parent_id in decided_parents:
            return Reason.ONE_INTENT_PER_PARENT
        if action == Action.CANCEL:
            if parent_id not in pending_parents:
                return Reason.CANCEL_WITHOUT_PENDING
            return (None if any(p.parent_id == parent_id and p.pair == intent.pair for p in state.pending)
                    else Reason.PARENT_SCOPE_MISMATCH)
        if parent_id in pending_parents:
            return Reason.PENDING_PARENT
        if action in {Action.EXIT, Action.RECOVERY}:
            if parent is None or parent.status != ParentStatus.ACTIVE:
                return Reason.PARENT_NOT_ACTIVE
            if parent.pair != intent.pair:
                return Reason.PARENT_SCOPE_MISMATCH
        if action == Action.RECOVERY and parent is not None:
            if parent.protected_profit:
                return Reason.PROTECTED_PROFIT
            if intent.rung in parent.consumed_rungs:
                return Reason.RUNG_ALREADY_CONSUMED
        if action in {Action.ENTRY, Action.RECOVERY}:
            if asset in {"BTC", "ETH"}:
                return Reason.EXCLUDED_SPOT_ASSET
            if not state.risk_increase_allowed:
                return Reason.SPOT_RISK_GATE
            if defensive_hedge:
                return Reason.DEFENSIVE_HEDGE_GATE
        if action == Action.ENTRY:
            if parent is not None:
                return Reason.PARENT_ID_REUSE
            if asset in pending_assets:
                return Reason.PENDING_ASSET_IDENTITY
            if asset in claims:
                return Reason.ACTIVE_ASSET_IDENTITY
            if len(claims) >= state.max_positions:
                return Reason.MAXIMUM_SLOTS
        if action == Action.HEDGE:
            if not state.hedge_admitted:
                return Reason.HEDGE_NOT_ADMITTED
            if parent_id != PORTFOLIO_HEDGE_PARENT:
                return Reason.HEDGE_IDENTITY
            if parent is not None or asset not in {"BTC"}:
                return Reason.PARENT_SCOPE_MISMATCH
        elif parent_id == PORTFOLIO_HEDGE_PARENT:
            return Reason.HEDGE_IDENTITY
        expected_owner = _SPENDING_OWNER.get(action)
        if expected_owner is not None:
            if intent.owner != expected_owner:
                return Reason.OWNER_MISMATCH
            spendable = _sum((cash[intent.owner], budgets.held_by_owner[intent.owner].copy_negate(),
                              budgets.protected_by_owner[intent.owner].copy_negate()))
            if intent.requested_cost > spendable:
                return Reason.OWNER_CASH
            physical_after = _sum((*cash.values(), held_total.copy_negate(),
                                   intent.requested_cost.copy_negate()))
            if physical_after < budgets.physical_reserve_floor:
                return Reason.RESERVE_FLOOR
        return None

    for intent in sorted(proposals, key=lambda i: (_PRECEDENCE[i.action], i.priority, i.intent_id)):
        reason = rejection(intent)
        if reason is not None:
            rejected.append(Rejection(intent.intent_id, reason))
            continue
        accepted.append(intent)
        decided_parents.add(intent.parent_id)
        if intent.requested_cost:
            cash[intent.owner] = _difference(cash[intent.owner], intent.requested_cost)
        if intent.action == Action.ENTRY:
            claims.add(base_asset(intent.pair))
        if intent.action == Action.HEDGE and intent.blocks_spot_increase:
            defensive_hedge = True
    return DecisionBatch(tuple(accepted), tuple(rejected), MappingProxyType(cash),
                         len(claims), state.max_positions)


def confirm_recovery(parent: ParentState, intent: Intent, *, confirmed_fill_base: Decimal,
                     confirmed_quote_spent: Decimal, exact_receipt: bool) -> ParentState:
    """Record a consumed rung only for an exact, positive confirmed fill.

    Exact replay is idempotent. Partial positive fill consumes one rung and does
    not authorize a second purchase at that rung. Unused reservation remains
    held until a separately proven cash-release protocol updates the budgets.
    """
    _money(confirmed_fill_base, "confirmed_fill_base")
    _money(confirmed_quote_spent, "confirmed_quote_spent")
    if type(exact_receipt) is not bool:
        raise ValueError("exact_receipt must be bool")
    if intent.action != Action.RECOVERY or intent.parent_id != parent.parent_id or intent.pair != parent.pair:
        raise ValueError("recovery confirmation crosses immutable proposal scope")
    if intent.owner != Owner.VCAP or parent.status != ParentStatus.ACTIVE:
        raise ValueError("recovery confirmation has no active VCAP parent")
    if not exact_receipt or confirmed_fill_base == ZERO or confirmed_quote_spent == ZERO:
        return parent
    if confirmed_quote_spent > intent.requested_cost:
        raise ValueError("confirmed recovery cost exceeds its approved all-in reservation")
    if intent.rung in parent.consumed_rungs:
        if dict(parent.rung_intent_ids).get(intent.rung) != intent.intent_id:
            raise ValueError("consumed rung confirmation differs from exact original proposal")
        return parent
    return replace(parent, consumed_rungs=(*parent.consumed_rungs, intent.rung),
                   rung_intent_ids=(*parent.rung_intent_ids, (intent.rung, intent.intent_id)))


@dataclass(frozen=True)
class SlotShortfall:
    vacant_slots: int
    reasons: tuple[ShortfallReason, ...]
    rejected_intents: tuple[str, ...]


@dataclass(frozen=True)
class ConfirmedRecycle:
    """Caller-proven, same-owner partial recovery proceeds already held in cash.

    This contract does not verify an exchange receipt. The engine must first
    bind actual fills, exact owned lots, cash settlement and budget release to
    this immutable receipt ID. It cannot represent a canceled-buy refund or a
    whole exit. Native partial recovery belongs to VCAP, not productive ENTRY.
    """

    receipt_id: str
    parent_id: str
    original_owner: Owner
    recorded_owner: Owner
    amount: Decimal
    exact_partial_settlement: bool

    def __post_init__(self) -> None:
        _text(self.receipt_id, "receipt_id")
        _text(self.parent_id, "parent_id")
        object.__setattr__(self, "original_owner", Owner(self.original_owner))
        object.__setattr__(self, "recorded_owner", Owner(self.recorded_owner))
        _money(self.amount, "recycle amount")
        if self.amount <= ZERO:
            raise ValueError("recycle release must be positive")
        if type(self.exact_partial_settlement) is not bool:
            raise ValueError("partial settlement proof flag must be bool")


def unlock_confirmed_recycle(budgets: CashBudgets, release: ConfirmedRecycle, *,
                            declared_redeploy: bool = False,
                            previous_releases: tuple[ConfirmedRecycle, ...] = ()) -> tuple[CashBudgets, tuple[ConfirmedRecycle, ...]]:
    """Research-only availability release with zero cash creation or transfer.

    Controls leave proceeds held. The candidate can unlock already-recorded
    VCAP proceeds only for future RECOVERY. All protected amounts and the
    physical reserve floor remain unchanged. The exact prior-release journal
    is part of the continuous engine's durable state; dropping it is invalid.
    """
    if type(declared_redeploy) is not bool:
        raise ValueError("declared_redeploy must be bool")
    journal = tuple(previous_releases)
    journal_ids = [item.receipt_id for item in journal]
    if len(set(journal_ids)) != len(journal_ids):
        raise ValueError("recycle release journal contains duplicate identities")
    if not declared_redeploy:
        return budgets, journal
    previous = next((item for item in journal if item.receipt_id == release.receipt_id), None)
    if previous is not None:
        if previous != release:
            raise ValueError("recycle receipt identity conflicts with previous release")
        return budgets, journal
    if not release.exact_partial_settlement:
        raise ValueError("unconfirmed partial proceeds cannot be released")
    if release.original_owner != Owner.VCAP or release.recorded_owner != Owner.VCAP:
        raise ValueError("native recovery recycle remains VCAP-owned; no cash owner transfer")
    if release.amount > budgets.held_by_owner[Owner.VCAP]:
        raise ValueError("recycle release exceeds already recorded held VCAP cash")
    held = dict(budgets.held_by_owner)
    held[Owner.VCAP] = _difference(held[Owner.VCAP], release.amount)
    updated = replace(budgets, held_by_owner=held)
    return updated, (*journal, release)


def slot_shortfall(batch: DecisionBatch, *, insufficient_eligible: bool = False,
                   liquidity_blocked: bool = False, stale_data: bool = False) -> SlotShortfall:
    """Explain actual vacancies; an occupancy target never creates a fill."""
    if any(type(v) is not bool for v in (insufficient_eligible, liquidity_blocked, stale_data)):
        raise ValueError("shortfall evidence flags must be bool")
    if not batch.vacant_slots:
        return SlotShortfall(0, (), tuple(r.intent_id for r in batch.rejected))
    reasons: set[ShortfallReason] = set()
    counts = Counter(r.reason for r in batch.rejected)
    if counts[Reason.OWNER_CASH]:
        reasons.add(ShortfallReason.CAPITAL)
    if counts[Reason.RESERVE_FLOOR]:
        reasons.add(ShortfallReason.RESERVE)
    if counts[Reason.PENDING_PARENT] or counts[Reason.PENDING_ASSET_IDENTITY]:
        reasons.add(ShortfallReason.PENDING_IDENTITY)
    if any(counts[r] for r in (Reason.ACCOUNTING_UNHEALTHY, Reason.SPOT_RISK_GATE, Reason.DEFENSIVE_HEDGE_GATE)):
        reasons.add(ShortfallReason.RISK_OR_ACCOUNTING_GATE)
    for present, reason in ((insufficient_eligible, ShortfallReason.INSUFFICIENT_ELIGIBLE_COINS),
                            (liquidity_blocked, ShortfallReason.LIQUIDITY),
                            (stale_data, ShortfallReason.STALE_MARKET_DATA)):
        if present:
            reasons.add(reason)
    if not reasons:
        reasons.add(ShortfallReason.RESEARCH_UNSPECIFIED)
    return SlotShortfall(batch.vacant_slots, tuple(sorted(reasons)),
                         tuple(r.intent_id for r in batch.rejected))
