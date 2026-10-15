"""Deterministic, paper-only DTSS/DTSSR mechanics; no broker or signal discovery.

All time values are integer UTC seconds. Prices, quantities and scores use Decimal.
An upstream causal signal service supplies completed evidence and accounting supplies
fee-net arming values. A cover intent is neither an order nor a simulated fill.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal, Context, localcontext
from enum import Enum
import hashlib
import json
from typing import TypeAlias

D = Decimal
MATH_CONTEXT = Context(prec=34)


def _decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite Decimal" + (" > 0" if positive else ""))


def _time(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be nonnegative integer UTC seconds")


class Phase(str, Enum):
    SHORT_RIDE = "SHORT_RIDE"
    REVERSAL_WATCH = "REVERSAL_WATCH"
    REVERSAL_LIKELY = "REVERSAL_LIKELY"
    COVER = "COVER"
    COOLDOWN = "COOLDOWN"


class AttemptStatus(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"


@dataclass(frozen=True)
class Config:
    # Deliberately no implicit generic research thresholds.
    bar_seconds: int
    max_evidence_age_seconds: int
    watch_score: Decimal
    likely_score: Decimal
    watch_count: int
    likely_count: int
    confirmations_required: int
    ride_atr: Decimal
    watch_atr: Decimal
    likely_atr: Decimal
    cooldown_bars: int
    execution_delay_seconds: int
    arm_threshold: Decimal | None = None
    paper_only: bool = True

    def __post_init__(self) -> None:
        for name in ("bar_seconds", "max_evidence_age_seconds", "watch_count", "likely_count",
                     "confirmations_required", "execution_delay_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.cooldown_bars) is not int or self.cooldown_bars < 0:
            raise ValueError("cooldown_bars must be a nonnegative integer")
        for name in ("watch_score", "likely_score", "ride_atr", "watch_atr", "likely_atr"):
            _decimal(getattr(self, name), name, positive=True)
        if self.arm_threshold is not None:
            _decimal(self.arm_threshold, "arm_threshold")
        if not (self.watch_score <= self.likely_score and self.watch_count <= self.likely_count
                and self.ride_atr >= self.watch_atr >= self.likely_atr):
            raise ValueError("thresholds must progress and ATR distances must tighten")
        if self.paper_only is not True:
            raise ValueError("this component supports paper mechanics only")


PROVISIONAL_RESEARCH_CONFIG = Config(
    bar_seconds=3600, max_evidence_age_seconds=300,
    watch_score=D("1"), likely_score=D("2"), watch_count=1, likely_count=2,
    confirmations_required=2, ride_atr=D("3"), watch_atr=D("2"), likely_atr=D("1"),
    cooldown_bars=2, execution_delay_seconds=1, arm_threshold=None,
)


@dataclass(frozen=True)
class CompletedBar:
    open_at: int
    close_at: int
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        _time(self.open_at, "open_at")
        _time(self.close_at, "close_at")
        for name in ("high", "low", "close"):
            _decimal(getattr(self, name), name, positive=True)
        if self.close_at <= self.open_at or not self.low <= self.close <= self.high:
            raise ValueError("invalid completed-bar interval or OHLC bounds")


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    observed_at: int
    bar: CompletedBar
    feature_asof: int  # Latest source timestamp used by every upstream feature.
    causal_confirmed: bool
    atr: Decimal
    evidence_score: Decimal
    evidence_count: int  # Explicit correlated feature families, not statistical independence.
    confirmed_reversal: bool
    fee_net_arm_measure: Decimal | None = None
    accounting_asof: int | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("evidence_id is required")
        _time(self.observed_at, "observed_at")
        _time(self.feature_asof, "feature_asof")
        _decimal(self.atr, "atr")
        if self.atr < 0:
            raise ValueError("atr must be nonnegative")
        _decimal(self.evidence_score, "evidence_score")
        if self.evidence_score < 0 or type(self.evidence_count) is not int or self.evidence_count < 0:
            raise ValueError("evidence score/count must be nonnegative")
        if type(self.causal_confirmed) is not bool or type(self.confirmed_reversal) is not bool:
            raise ValueError("causal and reversal confirmations must be explicit booleans")
        if (self.fee_net_arm_measure is None) != (self.accounting_asof is None):
            raise ValueError("fee-net measure requires an accounting timestamp")
        if self.fee_net_arm_measure is not None:
            _decimal(self.fee_net_arm_measure, "fee_net_arm_measure")
            _time(self.accounting_asof, "accounting_asof")


@dataclass(frozen=True)
class EntryFill:
    cycle_id: str
    fill_id: str
    filled_at: int
    received_at: int
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        _validate_fill(self)


@dataclass(frozen=True)
class CoverFill:
    cycle_id: str
    intent_id: str
    attempt: int
    fill_id: str
    filled_at: int
    received_at: int
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        _validate_fill(self)
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("invalid attempt")


def _validate_fill(fill: EntryFill | CoverFill) -> None:
    if not fill.cycle_id or not fill.fill_id:
        raise ValueError("cycle_id/fill_id are required")
    _time(fill.filled_at, "filled_at")
    _time(fill.received_at, "received_at")
    _decimal(fill.quantity, "quantity", positive=True)
    _decimal(fill.price, "price", positive=True)
    if fill.filled_at > fill.received_at:
        raise ValueError("fill cannot come from the future")


@dataclass(frozen=True)
class SealEntry:
    at: int  # Caller confirms all entry fills are reconciled; no more entry orders.


@dataclass(frozen=True)
class Clock:
    at: int


@dataclass(frozen=True)
class Dispatch:
    intent_id: str
    attempt: int
    at: int


@dataclass(frozen=True)
class Acknowledge:
    intent_id: str
    attempt: int
    at: int


@dataclass(frozen=True)
class RequestCancel:
    intent_id: str
    attempt: int
    at: int


@dataclass(frozen=True)
class CancelAcknowledged:
    intent_id: str
    attempt: int
    at: int
    fills_reconciled: bool = False
    filled_quantity: Decimal | None = None


@dataclass(frozen=True)
class Retry:
    intent_id: str
    at: int


@dataclass(frozen=True)
class Reconcile:
    at: int
    anchor_close_at: int
    attestation: str  # Explicit external reconciliation, never automatic gap skipping.
    execution_reconciled: bool = False
    confirmed_open_quantity: Decimal | None = None


@dataclass(frozen=True)
class Reset:
    cycle_id: str
    at: int
    signal_reset_confirmed: bool


@dataclass(frozen=True)
class Attempt:
    number: int
    requested_at: int
    eligible_at: int
    quantity: Decimal
    status: AttemptStatus = AttemptStatus.PENDING
    dispatched_at: int | None = None
    acknowledged_at: int | None = None
    cancel_requested_at: int | None = None
    cancelled_at: int | None = None
    fills_reconciled: bool = False


@dataclass(frozen=True)
class CoverIntent:
    intent_id: str
    cycle_id: str
    instrument: str
    decision_at: int
    source_bar_close_at: int
    reason: str
    quantity: Decimal
    attempts: tuple[Attempt, ...]
    reduce_only: bool = True
    paper_only: bool = True


@dataclass(frozen=True)
class StopRevision:
    price: Decimal
    effective_at: int
    source_bar_close_at: int


@dataclass(frozen=True)
class State:
    config_fingerprint: str
    cycle_id: str
    instrument: str
    started_at: int
    last_event_at: int
    phase: Phase = Phase.SHORT_RIDE
    entry_quantity: Decimal = D("0")
    entry_notional: Decimal = D("0")
    covered_quantity: Decimal = D("0")
    cover_notional: Decimal = D("0")
    last_entry_fill_at: int | None = None
    entry_sealed: bool = False
    lowest_completed_low: Decimal | None = None
    active_stop: Decimal | None = None
    stop_effective_after: int | None = None
    stop_revisions: tuple[StopRevision, ...] = ()
    armed: bool = False
    fee_net_arm_measure: Decimal | None = None
    last_bar_close_at: int | None = None
    confirmations: int = 0
    cooldown_remaining: int = 0
    halted_reason: str | None = None
    execution_reconciliation_required: bool = False
    intent: CoverIntent | None = None
    fills: tuple[EntryFill | CoverFill, ...] = ()
    evidence_receipts: tuple[tuple[str, str], ...] = ()
    cycles_seen: tuple[str, ...] = ()
    reconciliations: tuple[Reconcile, ...] = ()
    control_receipts: tuple[str, ...] = ()

    @property
    def actual_entry_price(self) -> Decimal | None:
        with localcontext(MATH_CONTEXT):
            return self.entry_notional / self.entry_quantity if self.entry_quantity else None

    @property
    def open_quantity(self) -> Decimal:
        with localcontext(MATH_CONTEXT):
            return self.entry_quantity - self.covered_quantity


@dataclass(frozen=True)
class Result:
    state: State
    new_intents: tuple[CoverIntent, ...] = ()
    notes: tuple[str, ...] = ()


Event: TypeAlias = (EntryFill | CoverFill | SealEntry | Evidence | Clock | Dispatch |
                    Acknowledge | RequestCancel | CancelAcknowledged | Retry | Reconcile | Reset)


def fingerprint(value: object) -> str:
    return hashlib.sha256(_json(_encode(value)).encode()).hexdigest()


def start(config: Config, cycle_id: str, instrument: str, at: int) -> State:
    _time(at, "at")
    if not cycle_id or not instrument:
        raise ValueError("nonempty cycle_id and instrument required")
    return State(config_fingerprint=fingerprint(config), cycle_id=cycle_id,
                 instrument=instrument, started_at=at, last_event_at=at, cycles_seen=(cycle_id,))


def _halt(state: State, reason: str, at: int | None = None) -> Result:
    # First failure remains visible until an explicit reconcile.
    data_only = reason in {"stale_clock", "stale_evidence", "stale_retry", "noncausal_or_uncompleted_evidence",
                           "wrong_bar_interval", "accounting_not_aligned_to_bar", "duplicate_or_gapped_bar",
                           "missing_initial_completed_bar", "zero_atr_unusable", "conflicting_evidence_id",
                           "invalid_reconciliation"}
    return Result(replace(state, halted_reason=state.halted_reason or reason,
                          execution_reconciliation_required=state.execution_reconciliation_required or not data_only,
                          confirmations=0, last_event_at=max(state.last_event_at, at or 0)),
                  notes=(reason,))


def _fresh(config: Config, state: State, now: int) -> bool:
    anchor = state.last_bar_close_at
    if anchor is None:
        entry_at = state.last_entry_fill_at if state.last_entry_fill_at is not None else state.started_at
        # The entry-containing partial bar is intentionally excluded; wait for the
        # first complete post-entry interval before declaring its close missing.
        anchor = ((entry_at + config.bar_seconds - 1) // config.bar_seconds) * config.bar_seconds
    # Between scheduled closes the most recent completed bar is necessarily older.
    return now <= anchor + config.bar_seconds + config.max_evidence_age_seconds


def execution_eligible(config: Config, state: State, at: int) -> bool:
    """Dispatch guard only; this never creates a fill or sends an order."""
    if state.config_fingerprint != fingerprint(config) or state.halted_reason or not state.intent:
        return False
    attempt = state.intent.attempts[-1]
    return (state.phase == Phase.COVER and state.open_quantity > 0 and _fresh(config, state, at)
            and attempt.status == AttemptStatus.PENDING and attempt.quantity == state.open_quantity
            and at >= attempt.eligible_at
            and at >= state.last_event_at)


def _set_attempt(state: State, attempt: Attempt) -> State:
    assert state.intent is not None
    attempts = tuple(attempt if old.number == attempt.number else old for old in state.intent.attempts)
    return replace(state, intent=replace(state.intent, attempts=attempts))


def _cover(config: Config, state: State, evidence: Evidence, reason: str) -> Result:
    if state.intent is not None:
        return Result(state)
    attempt = Attempt(number=1, requested_at=evidence.observed_at,
                      eligible_at=evidence.observed_at + config.execution_delay_seconds,
                      quantity=state.open_quantity)
    intent = CoverIntent(intent_id=f"{state.cycle_id}:cover", cycle_id=state.cycle_id,
                         instrument=state.instrument, decision_at=evidence.observed_at,
                         source_bar_close_at=evidence.bar.close_at, reason=reason,
                         quantity=state.open_quantity, attempts=(attempt,))
    return Result(replace(state, phase=Phase.COVER, intent=intent), (intent,))


def reduce(config: Config, state: State, event: Event) -> Result:
    """Pure reducer with a fixed decimal context and immutable inputs/outputs."""
    if state.config_fingerprint != fingerprint(config):
        raise ValueError("configuration changed; explicit migration is required")
    with localcontext(MATH_CONTEXT):
        control = not isinstance(event, (EntryFill, CoverFill, Evidence))
        key = fingerprint(event) if control else None
        if key is not None and key in state.control_receipts:
            return Result(state, notes=("duplicate_control_ignored",))
        result = _reduce(config, state, event)
        if control and (not result.notes or result.notes == ("same_intent_retry_ready",)):
            result = replace(result, state=replace(result.state, control_receipts=result.state.control_receipts + (key,)))
        return result


def _reduce(config: Config, state: State, event: Event) -> Result:
    # Exact economic fill identity ignores re-delivery receipt time.
    if isinstance(event, (EntryFill, CoverFill)):
        previous = next((f for f in state.fills if f.fill_id == event.fill_id), None)
        if previous is not None:
            if type(previous) is type(event) and replace(previous, received_at=event.received_at) == event:
                return Result(state, notes=("duplicate_fill_ignored",))
            return _halt(state, "conflicting_fill_id")
    if isinstance(event, Evidence):
        receipt = next((p for p in state.evidence_receipts if p[0] == event.evidence_id), None)
        # Identical causal payload may be redelivered later without changing state.
        digest = fingerprint(replace(event, observed_at=event.bar.close_at))
        if receipt is not None:
            if receipt[1] == digest:
                return Result(state, notes=("duplicate_evidence_ignored",))
            return _halt(state, "conflicting_evidence_id")
    at = event.observed_at if isinstance(event, Evidence) else (
        event.received_at if isinstance(event, (EntryFill, CoverFill)) else event.at)
    _time(at, "event time")
    if at < state.last_event_at:
        return _halt(state, "out_of_order_event")
    state = replace(state, last_event_at=at)

    if isinstance(event, EntryFill):
        if event.cycle_id != state.cycle_id or state.entry_sealed or state.intent:
            return _halt(state, "unexpected_entry_fill")
        if event.filled_at < state.started_at:
            return _halt(state, "entry_before_cycle")
        return Result(replace(state, entry_quantity=state.entry_quantity + event.quantity,
                              entry_notional=state.entry_notional + event.quantity * event.price,
                              last_entry_fill_at=max(state.last_entry_fill_at or 0, event.filled_at),
                              fills=state.fills + (event,)))
    if isinstance(event, CoverFill):
        intent = state.intent
        if not intent or event.cycle_id != state.cycle_id or event.intent_id != intent.intent_id:
            return _halt(state, "unexpected_cover_fill")
        attempt = next((a for a in intent.attempts if a.number == event.attempt), None)
        if (attempt is None or attempt.dispatched_at is None or event.filled_at < attempt.eligible_at
                or event.filled_at < attempt.dispatched_at
                or (attempt.cancelled_at is not None and event.filled_at > attempt.cancelled_at)):
            return _halt(state, "ineligible_cover_fill")
        filled_in_attempt = sum((f.quantity for f in state.fills if isinstance(f, CoverFill)
                                and f.intent_id == intent.intent_id and f.attempt == event.attempt), D("0"))
        if event.quantity > state.open_quantity or filled_in_attempt + event.quantity > attempt.quantity:
            return _halt(state, "cover_overfill")
        state = replace(state, covered_quantity=state.covered_quantity + event.quantity,
                        cover_notional=state.cover_notional + event.quantity * event.price,
                        fills=state.fills + (event,))
        late_after_reconciliation = attempt.fills_reconciled
        # Terminal status is per attempt, never inferred from aggregate flatness.
        if filled_in_attempt + event.quantity == attempt.quantity:
            attempt = replace(attempt, status=AttemptStatus.FILLED)
        elif late_after_reconciliation:
            attempt = replace(attempt, fills_reconciled=False)
        state = _set_attempt(state, attempt)
        if state.open_quantity == 0:
            state = replace(state, phase=Phase.COOLDOWN, cooldown_remaining=config.cooldown_bars,
                            confirmations=0)
            if not _all_attempts_settled(state):
                return _halt(state, "flat_with_unsettled_attempts")
        if late_after_reconciliation:
            return _halt(state, "fill_after_reported_reconciliation")
        return Result(state)
    if isinstance(event, RequestCancel):
        # Cancellation remains available when halted; it cannot increase exposure.
        return _attempt_event(state, event)
    if isinstance(event, (Acknowledge, CancelAcknowledged)):
        return _attempt_event(state, event)
    if isinstance(event, Clock):
        if state.open_quantity > 0 and not _fresh(config, state, at):
            return _halt(state, "stale_clock", at)
        return Result(state)
    if isinstance(event, Reconcile):
        _time(event.anchor_close_at, "anchor_close_at")
        if (not event.attestation.strip() or event.anchor_close_at > at
                or at - event.anchor_close_at > config.max_evidence_age_seconds
                or event.anchor_close_at % config.bar_seconds
                or event.anchor_close_at < (state.last_bar_close_at or state.started_at)):
            return _halt(state, "invalid_reconciliation")
        if state.execution_reconciliation_required:
            if (event.execution_reconciled is not True or event.confirmed_open_quantity != state.open_quantity
                    or _live_outstanding_quantity(state) > state.open_quantity
                    or (state.open_quantity == 0 and not _all_attempts_settled(state))
                    or (state.intent is not None and any(a.status == AttemptStatus.CANCELLED and not a.fills_reconciled
                                                       for a in state.intent.attempts))):
                return _halt(state, "execution_reconciliation_required")
        return Result(replace(state, halted_reason=None, execution_reconciliation_required=False,
                              last_bar_close_at=event.anchor_close_at, confirmations=0,
                              reconciliations=state.reconciliations + (event,)))
    if state.halted_reason:
        return Result(state, notes=("blocked_until_explicit_reconciliation",))
    if isinstance(event, SealEntry):
        if state.entry_quantity <= 0 or state.intent:
            return _halt(state, "cannot_seal_empty_or_exiting_entry")
        return Result(replace(state, entry_sealed=True))
    if isinstance(event, Dispatch):
        if not execution_eligible(config, state, at):
            return Result(state, notes=("dispatch_ineligible",))
        return _attempt_event(state, event)
    if isinstance(event, Retry):
        intent = state.intent
        if (intent is None or event.intent_id != intent.intent_id or state.phase != Phase.COVER
                or state.open_quantity <= 0 or intent.attempts[-1].status != AttemptStatus.CANCELLED
                or not intent.attempts[-1].fills_reconciled):
            return Result(state, notes=("retry_requires_cancel_and_fill_reconciliation",))
        if not _fresh(config, state, at):
            return _halt(state, "stale_retry")
        attempt = Attempt(number=len(intent.attempts) + 1, requested_at=at,
                          eligible_at=at + config.execution_delay_seconds, quantity=state.open_quantity)
        # Transport retry keeps the same logical intent ID; no second economic intent.
        return Result(replace(state, intent=replace(intent, attempts=intent.attempts + (attempt,))),
                      notes=("same_intent_retry_ready",))
    if isinstance(event, Reset):
        if (state.phase != Phase.COOLDOWN or state.open_quantity != 0 or state.cooldown_remaining
                or event.signal_reset_confirmed is not True or event.cycle_id in state.cycles_seen
                or not event.cycle_id or not _fresh(config, state, at) or not _all_attempts_settled(state)):
            return Result(state, notes=("reset_ineligible",))
        fresh = start(config, event.cycle_id, state.instrument, at)
        return Result(replace(fresh, fills=state.fills, evidence_receipts=state.evidence_receipts,
                              cycles_seen=state.cycles_seen + (event.cycle_id,),
                              reconciliations=state.reconciliations, control_receipts=state.control_receipts))
    if not isinstance(event, Evidence):
        raise TypeError(f"unsupported event: {type(event).__name__}")
    bar = event.bar
    if event.atr == 0:
        return _halt(state, "zero_atr_unusable")
    if not event.causal_confirmed or event.feature_asof > bar.close_at or bar.close_at > at:
        return _halt(state, "noncausal_or_uncompleted_evidence")
    if at - bar.close_at > config.max_evidence_age_seconds:
        return _halt(state, "stale_evidence")
    if bar.close_at - bar.open_at != config.bar_seconds or bar.close_at % config.bar_seconds:
        return _halt(state, "wrong_bar_interval")
    if event.accounting_asof is not None and event.accounting_asof != bar.close_at:
        return _halt(state, "accounting_not_aligned_to_bar")
    if state.last_bar_close_at is not None and bar.open_at != state.last_bar_close_at:
        return _halt(state, "duplicate_or_gapped_bar")
    if (not state.entry_sealed or state.entry_quantity == 0
            or bar.open_at < (state.last_entry_fill_at or state.started_at)):
        return _halt(state, "entry_unsealed_or_bar_predates_actual_entry")
    if state.last_bar_close_at is None and bar.open_at >= (state.last_entry_fill_at or 0) + config.bar_seconds:
        return _halt(state, "missing_initial_completed_bar")
    state = replace(state, last_bar_close_at=bar.close_at,
                    evidence_receipts=state.evidence_receipts + ((event.evidence_id, digest),))
    if state.phase == Phase.COOLDOWN:
        return Result(replace(state, cooldown_remaining=max(0, state.cooldown_remaining - 1)))
    if state.phase == Phase.COVER:
        return Result(replace(state, lowest_completed_low=min(state.lowest_completed_low, bar.low)))  # New bars refresh causal health, never duplicate intents.
    previous_stop = state.active_stop
    at_open = next((r.price for r in reversed(state.stop_revisions) if r.effective_at <= bar.open_at), None)
    # A completed high has no intrabar timestamp. Use it only against a stop already
    # known at the open; the completed close has a known causal timestamp.
    crossed_prior_stop = ((at_open is not None and bar.high >= at_open) or
                          (previous_stop is not None and state.stop_effective_after <= bar.close_at
                           and bar.close >= previous_stop))
    low = min(state.lowest_completed_low, bar.low) if state.lowest_completed_low is not None else bar.low
    armed = state.armed or config.arm_threshold is None or (
        event.fee_net_arm_measure is not None and event.fee_net_arm_measure >= config.arm_threshold)
    phase = state.phase
    likely = event.evidence_score >= config.likely_score and event.evidence_count >= config.likely_count
    watch = event.evidence_score >= config.watch_score and event.evidence_count >= config.watch_count
    if armed:
        if likely:
            phase = Phase.REVERSAL_LIKELY
        elif watch and phase == Phase.SHORT_RIDE:
            phase = Phase.REVERSAL_WATCH
    multiplier = {Phase.SHORT_RIDE: config.ride_atr, Phase.REVERSAL_WATCH: config.watch_atr,
                  Phase.REVERSAL_LIKELY: config.likely_atr}[phase]
    candidate = low + multiplier * event.atr
    stop = min(previous_stop, candidate) if previous_stop is not None else candidate
    confirmations = state.confirmations + 1 if armed and likely and event.confirmed_reversal else 0
    revisions = state.stop_revisions
    if previous_stop is None or stop < previous_stop:
        revisions += (StopRevision(stop, event.observed_at, bar.close_at),)
    state = replace(state, phase=phase, lowest_completed_low=low, active_stop=stop,
                    stop_effective_after=revisions[-1].effective_at, stop_revisions=revisions, armed=armed,
                    fee_net_arm_measure=event.fee_net_arm_measure, confirmations=confirmations)
    if crossed_prior_stop:
        return _cover(config, state, event, "prior_active_stop_crossed")
    if confirmations >= config.confirmations_required:
        return _cover(config, state, event, "confirmed_reversal")
    return Result(state)


def _live_outstanding_quantity(state: State) -> Decimal:
    if state.intent is None:
        return D("0")
    total = D("0")
    for attempt in state.intent.attempts:
        if attempt.dispatched_at is not None and attempt.status in (
                AttemptStatus.DISPATCHED, AttemptStatus.ACKNOWLEDGED, AttemptStatus.CANCEL_PENDING):
            filled = sum((f.quantity for f in state.fills if isinstance(f, CoverFill)
                          and f.intent_id == state.intent.intent_id and f.attempt == attempt.number), D("0"))
            total += attempt.quantity - filled
    return total


def _all_attempts_settled(state: State) -> bool:
    if state.intent is None:
        return True
    return all(a.status == AttemptStatus.FILLED or
               (a.status == AttemptStatus.CANCELLED and a.fills_reconciled) for a in state.intent.attempts)


def _attempt_event(state: State, event: Dispatch | Acknowledge | RequestCancel | CancelAcknowledged) -> Result:
    intent = state.intent
    if intent is None or event.intent_id != intent.intent_id:
        return _halt(state, "unknown_intent_acknowledgement")
    attempt = next((a for a in intent.attempts if a.number == event.attempt), None)
    if attempt is None:
        return _halt(state, "unknown_attempt_acknowledgement")
    if isinstance(event, Dispatch):
        if event.attempt != intent.attempts[-1].number or attempt.status != AttemptStatus.PENDING:
            return Result(state, notes=("duplicate_or_invalid_dispatch_ignored",))
        attempt = replace(attempt, status=AttemptStatus.DISPATCHED, dispatched_at=event.at)
    elif isinstance(event, Acknowledge):
        if attempt.dispatched_at is None:
            return _halt(state, "acknowledgement_before_dispatch")
        if attempt.status == AttemptStatus.DISPATCHED:
            attempt = replace(attempt, status=AttemptStatus.ACKNOWLEDGED, acknowledged_at=event.at)
    elif isinstance(event, RequestCancel):
        if attempt.status in (AttemptStatus.PENDING, AttemptStatus.DISPATCHED, AttemptStatus.ACKNOWLEDGED):
            attempt = replace(attempt, status=AttemptStatus.CANCEL_PENDING, cancel_requested_at=event.at)
    elif isinstance(event, CancelAcknowledged):
        if attempt.status not in (AttemptStatus.CANCEL_PENDING, AttemptStatus.CANCELLED, AttemptStatus.FILLED):
            return _halt(state, "unsolicited_cancel_acknowledgement")
        if event.fills_reconciled is True:
            if event.filled_quantity is None:
                return _halt(state, "cancel_reconciliation_quantity_missing")
            _decimal(event.filled_quantity, "filled_quantity")
            recorded = sum((f.quantity for f in state.fills if isinstance(f, CoverFill)
                            and f.intent_id == intent.intent_id and f.attempt == event.attempt), D("0"))
            if event.filled_quantity != recorded:
                return _halt(state, "cancel_fill_reconciliation_mismatch")
        if attempt.status in (AttemptStatus.CANCEL_PENDING, AttemptStatus.CANCELLED):
            attempt = replace(attempt, status=AttemptStatus.CANCELLED,
                              cancelled_at=attempt.cancelled_at if attempt.cancelled_at is not None else event.at,
                              fills_reconciled=attempt.fills_reconciled or event.fills_reconciled is True)
    return Result(_set_attempt(state, attempt))


@dataclass(frozen=True)
class ReconstructionAccounting:
    """Independent owned-spot ledger; never inferred from short price changes."""
    cycle_id: str
    instrument: str
    snapshot_units: Decimal
    affordable_units_after_all_costs: Decimal
    asof: int
    valid_until: int
    accounting_confirmed: bool
    source: str

    def __post_init__(self) -> None:
        _decimal(self.snapshot_units, "snapshot_units", positive=True)
        _decimal(self.affordable_units_after_all_costs, "affordable_units_after_all_costs")
        _time(self.asof, "asof")
        _time(self.valid_until, "valid_until")
        if self.affordable_units_after_all_costs < 0 or self.valid_until < self.asof:
            raise ValueError("invalid reconstruction accounting")


def reconstruction_eligible(state: State, accounting: ReconstructionAccounting, now: int) -> bool:
    """Boolean eligibility only, not permission, an order, or a borrowed-short P&L."""
    last_cover_at = max((f.filled_at for f in state.fills
                         if isinstance(f, CoverFill) and f.cycle_id == state.cycle_id), default=0)
    return (state.phase == Phase.COOLDOWN and state.entry_quantity > 0 and state.open_quantity == 0
            and accounting.cycle_id == state.cycle_id and accounting.instrument == state.instrument
            and accounting.asof >= last_cover_at and now >= state.last_event_at
            and state.halted_reason is None and _all_attempts_settled(state) and accounting.accounting_confirmed is True
            and bool(accounting.source.strip()) and accounting.asof <= now <= accounting.valid_until
            and accounting.affordable_units_after_all_costs > accounting.snapshot_units)


# Versioned, lossless, canonical snapshots. No pickle or executable payloads.
_TYPES = {cls.__name__: cls for cls in (Config, CompletedBar, Evidence, EntryFill, CoverFill,
          SealEntry, Clock, Dispatch, Acknowledge, RequestCancel, CancelAcknowledged, Retry,
          Reconcile, Reset, Attempt, CoverIntent, StopRevision, State, ReconstructionAccounting)}
_ENUMS = {cls.__name__: cls for cls in (Phase, AttemptStatus)}


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, Enum):
        return {"$enum": type(value).__name__, "value": value.value}
    if is_dataclass(value):
        return {"$type": type(value).__name__, **{f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {"$tuple": [_encode(v) for v in value]}
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"unsupported snapshot type {type(value)}")


def _decode(value: object) -> object:
    if not isinstance(value, dict):
        if value is None or type(value) in (str, int, bool):
            return value
        raise ValueError("invalid snapshot primitive")
    if set(value) == {"$decimal"}:
        number = D(value["$decimal"])
        _decimal(number, "snapshot decimal")
        return number
    if set(value) == {"$enum", "value"} and value["$enum"] in _ENUMS:
        return _ENUMS[value["$enum"]](value["value"])
    if set(value) == {"$tuple"} and isinstance(value["$tuple"], list):
        return tuple(_decode(v) for v in value["$tuple"])
    cls = _TYPES.get(value.get("$type"))
    if cls is None or set(value) != {"$type", *(f.name for f in fields(cls))}:
        raise ValueError("unknown snapshot type or fields")
    return cls(**{key: _decode(v) for key, v in value.items() if key != "$type"})


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def snapshot(config: Config, state: State) -> str:
    if fingerprint(config) != state.config_fingerprint:
        raise ValueError("snapshot config mismatch")
    return _json({"schema_version": 1, "config": _encode(config), "state": _encode(state)})


def restore(raw: str) -> tuple[Config, State]:
    payload = json.loads(raw)
    if set(payload) != {"schema_version", "config", "state"} or payload["schema_version"] != 1:
        raise ValueError("unsupported snapshot schema")
    config, state = _decode(payload["config"]), _decode(payload["state"])
    if not isinstance(config, Config) or not isinstance(state, State) or fingerprint(config) != state.config_fingerprint:
        raise ValueError("invalid snapshot configuration")
    with localcontext(MATH_CONTEXT):
        validate_state(config, state)
    return config, state


def validate_state(config: Config, state: State) -> None:
    """Reject corrupt/incompatible snapshots rather than restore unsafe mechanics."""
    if not isinstance(state.phase, Phase) or state.config_fingerprint != fingerprint(config):
        raise ValueError("invalid state phase/config")
    if not state.cycle_id or not state.instrument or state.cycle_id not in state.cycles_seen:
        raise ValueError("invalid cycle identity")
    for name in ("cycles_seen", "fills", "evidence_receipts", "stop_revisions", "reconciliations", "control_receipts"):
        if not isinstance(getattr(state, name), tuple):
            raise ValueError("mutable state sequence")
    if len(state.cycles_seen) != len(set(state.cycles_seen)):
        raise ValueError("reused cycle identity")
    for name in ("entry_quantity", "covered_quantity", "entry_notional", "cover_notional"):
        _decimal(getattr(state, name), name)
        if getattr(state, name) < 0:
            raise ValueError("negative state ledger")
    for name in ("started_at", "last_event_at"):
        _time(getattr(state, name), name)
    if state.last_event_at < state.started_at or state.covered_quantity > state.entry_quantity:
        raise ValueError("invalid state chronology or position")
    for name in ("armed", "entry_sealed", "execution_reconciliation_required"):
        if type(getattr(state, name)) is not bool:
            raise ValueError("invalid state boolean")
    for name in ("confirmations", "cooldown_remaining"):
        if type(getattr(state, name)) is not int or getattr(state, name) < 0:
            raise ValueError("invalid state counter")
    if state.cooldown_remaining > config.cooldown_bars:
        raise ValueError("invalid cooldown counter")
    for name in ("last_entry_fill_at", "last_bar_close_at", "stop_effective_after"):
        value = getattr(state, name)
        if value is not None:
            _time(value, name)
            if value > state.last_event_at:
                raise ValueError("future state timestamp")
    fill_ids = [f.fill_id for f in state.fills]
    if len(fill_ids) != len(set(fill_ids)):
        raise ValueError("duplicate ledger fill identity")
    current = [f for f in state.fills if f.cycle_id == state.cycle_id]
    entries = [f for f in current if isinstance(f, EntryFill)]
    covers = [f for f in current if isinstance(f, CoverFill)]
    for name, value in (("entry_quantity", sum((f.quantity for f in entries), D("0"))),
                        ("entry_notional", sum((f.quantity * f.price for f in entries), D("0"))),
                        ("covered_quantity", sum((f.quantity for f in covers), D("0"))),
                        ("cover_notional", sum((f.quantity * f.price for f in covers), D("0")))):
        if getattr(state, name) != value:
            raise ValueError("state/confirmed-fill ledger mismatch")
    if state.entry_sealed and not entries:
        raise ValueError("sealed entry has no fills")
    if entries and state.last_entry_fill_at != max(f.filled_at for f in entries):
        raise ValueError("entry timestamp does not match fills")
    if any(f.received_at > state.last_event_at or f.filled_at < state.started_at for f in current):
        raise ValueError("invalid fill chronology")
    prior = None
    for revision in state.stop_revisions:
        if not isinstance(revision, StopRevision):
            raise ValueError("invalid stop revision")
        _decimal(revision.price, "stop", positive=True)
        _time(revision.effective_at, "stop effective time")
        _time(revision.source_bar_close_at, "stop source time")
        if revision.source_bar_close_at > revision.effective_at or revision.effective_at > state.last_event_at:
            raise ValueError("noncausal stop revision")
        if prior and (revision.price >= prior.price or revision.effective_at < prior.effective_at):
            raise ValueError("stop revision loosens or reorders")
        prior = revision
    if prior:
        if (state.active_stop, state.stop_effective_after) != (prior.price, prior.effective_at):
            raise ValueError("active stop differs from revision history")
    elif state.active_stop is not None or state.stop_effective_after is not None:
        raise ValueError("stop has no causal revision")
    if state.lowest_completed_low is not None:
        _decimal(state.lowest_completed_low, "lowest completed low", positive=True)
    if state.fee_net_arm_measure is not None:
        _decimal(state.fee_net_arm_measure, "fee net arm measure")
    intent = state.intent
    if intent is None:
        if state.phase in (Phase.COVER, Phase.COOLDOWN) or covers:
            raise ValueError("exit state has no intent")
        return
    if (intent.reduce_only is not True or intent.paper_only is not True
            or intent.cycle_id != state.cycle_id or intent.instrument != state.instrument
            or intent.intent_id != f"{state.cycle_id}:cover" or intent.quantity != state.entry_quantity
            or state.phase not in (Phase.COVER, Phase.COOLDOWN) or not state.entry_sealed):
        raise ValueError("invalid paper reduce-only intent")
    if not state.started_at <= intent.source_bar_close_at <= intent.decision_at <= state.last_event_at:
        raise ValueError("invalid intent chronology")
    if not isinstance(intent.attempts, tuple) or not intent.attempts:
        raise ValueError("intent has no immutable attempts")
    previous_attempt = None
    for number, attempt in enumerate(intent.attempts, 1):
        _decimal(attempt.quantity, "attempt quantity", positive=True)
        _time(attempt.requested_at, "attempt requested_at")
        _time(attempt.eligible_at, "attempt eligible_at")
        if (attempt.number != number or not isinstance(attempt.status, AttemptStatus)
                or attempt.quantity > intent.quantity or attempt.requested_at < intent.decision_at
                or attempt.eligible_at < attempt.requested_at + config.execution_delay_seconds
                or attempt.requested_at > state.last_event_at):
            raise ValueError("invalid intent attempt")
        if previous_attempt and (previous_attempt.cancelled_at is None
                                 or (not previous_attempt.fills_reconciled and not state.execution_reconciliation_required)
                                 or attempt.requested_at < previous_attempt.cancelled_at):
            raise ValueError("retry preceded cancel reconciliation")
        for name in ("dispatched_at", "acknowledged_at", "cancel_requested_at", "cancelled_at"):
            value = getattr(attempt, name)
            if value is not None:
                _time(value, name)
                if not attempt.requested_at <= value <= state.last_event_at:
                    raise ValueError("invalid acknowledgement chronology")
        if attempt.dispatched_at is not None and attempt.dispatched_at < attempt.eligible_at:
            raise ValueError("dispatch before eligibility")
        if attempt.status in (AttemptStatus.DISPATCHED, AttemptStatus.ACKNOWLEDGED, AttemptStatus.FILLED) and attempt.dispatched_at is None:
            raise ValueError("attempt status lacks dispatch")
        if attempt.acknowledged_at is not None and (attempt.dispatched_at is None or attempt.acknowledged_at < attempt.dispatched_at):
            raise ValueError("acknowledged before dispatch")
        if attempt.cancelled_at is not None and (attempt.cancel_requested_at is None or attempt.cancelled_at < attempt.cancel_requested_at):
            raise ValueError("cancel acknowledged before request")
        if attempt.status == AttemptStatus.CANCELLED and attempt.cancelled_at is None:
            raise ValueError("cancelled attempt lacks acknowledgement")
        if type(attempt.fills_reconciled) is not bool or (attempt.fills_reconciled and attempt.cancelled_at is None):
            raise ValueError("invalid fill reconciliation")
        attempt_fills = [f for f in covers if f.attempt == number]
        filled_quantity = sum((f.quantity for f in attempt_fills), D("0"))
        if filled_quantity > attempt.quantity:
            raise ValueError("attempt overfill")
        if attempt.status == AttemptStatus.FILLED and filled_quantity != attempt.quantity:
            raise ValueError("attempt marked filled without its entire quantity")
        for fill in attempt_fills:
            if (fill.intent_id != intent.intent_id or attempt.dispatched_at is None
                    or fill.filled_at < attempt.dispatched_at or fill.filled_at < attempt.eligible_at
                    or (attempt.cancelled_at is not None and fill.filled_at > attempt.cancelled_at)):
                raise ValueError("invalid attempt fill")
        previous_attempt = attempt
    if any(f.attempt > len(intent.attempts) for f in covers):
        raise ValueError("unknown fill attempt")
    if (state.phase == Phase.COOLDOWN) != (state.open_quantity == 0):
        raise ValueError("invalid cover/cooldown position")
    if not state.execution_reconciliation_required and _live_outstanding_quantity(state) > state.open_quantity:
        raise ValueError("unblocked live attempt exceeds confirmed remaining position")
