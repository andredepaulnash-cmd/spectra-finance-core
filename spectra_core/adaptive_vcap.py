"""Adaptive VCAP 1.0 policy primitives.

This module is intentionally pure: durable fragment ownership and order dispatch
live in the scheduler/ledger layer. It defines the phase boundaries, ladder
amounts, priority scoring and per-asset concurrency selection used by that layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from math import isfinite


ZERO = Decimal("0")
CENT = Decimal("0.01")
PRE_VCAP_DCA_TRIGGERS_BPS = (274, 444, 674)


class AdaptiveVCAPPhase(str, Enum):
    PRE_VCAP = "PRE_VCAP"
    FAT_1 = "FAT_1"
    SKINNY = "SKINNY"
    FAT_2 = "FAT_2"
    DEEP_GOVERNOR = "DEEP_GOVERNOR"
    RECOVERED = "RECOVERED"


class VCAPFragmentStatus(str, Enum):
    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    RECONCILING = "RECONCILING"
    PAUSED = "PAUSED"
    RECOVERED = "RECOVERED"
    EXHAUSTED = "EXHAUSTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class AdaptiveVCAPPolicy:
    """Paper-first default policy; ladder amounts remain configurable."""

    activation_drawdown_bps: int = 777
    fat1_end_drawdown_bps: int = 1174
    skinny_end_drawdown_bps: int = 1777
    fat2_end_drawdown_bps: int = 2400
    fragment_count: int = 5
    max_active_fragments_per_position: int = 2
    base_quote: Decimal = Decimal("20")
    fat1_multipliers: tuple[Decimal, ...] = (
        Decimal("1"),
        Decimal("2"),
        Decimal("4"),
    )
    skinny_multipliers: tuple[Decimal, ...] = (
        Decimal("0.25"),
        Decimal("0.375"),
        Decimal("0.5"),
        Decimal("0.625"),
    )
    fat2_multipliers: tuple[Decimal, ...] = (
        Decimal("8"),
        Decimal("16"),
        Decimal("32"),
    )

    def __post_init__(self) -> None:
        boundaries = (
            self.activation_drawdown_bps,
            self.fat1_end_drawdown_bps,
            self.skinny_end_drawdown_bps,
            self.fat2_end_drawdown_bps,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in boundaries):
            raise TypeError("VCAP drawdown boundaries must be integer basis points")
        if not (0 < boundaries[0] < boundaries[1] < boundaries[2] < boundaries[3]):
            raise ValueError("VCAP drawdown boundaries must be strictly increasing")
        if self.fragment_count not in {4, 5}:
            raise ValueError("VCAP fragment_count must be four or five")
        if (
            isinstance(self.max_active_fragments_per_position, bool)
            or not isinstance(self.max_active_fragments_per_position, int)
            or not 1 <= self.max_active_fragments_per_position <= self.fragment_count
        ):
            raise ValueError("VCAP active fragment limit must fit the fragment count")
        if not isinstance(self.base_quote, Decimal) or self.base_quote <= ZERO:
            raise ValueError("VCAP base_quote must be a positive Decimal")
        self._validate_ladder(self.fat1_multipliers, "FAT 1")
        self._validate_ladder(self.skinny_multipliers, "SKINNY")
        self._validate_ladder(self.fat2_multipliers, "FAT 2")
        if any(
            right != left * Decimal("2")
            for left, right in zip(self.fat1_multipliers, self.fat1_multipliers[1:])
        ):
            raise ValueError("FAT 1 ladder must double at each step")
        if any(
            right != left * Decimal("2")
            for left, right in zip(self.fat2_multipliers, self.fat2_multipliers[1:])
        ):
            raise ValueError("FAT 2 ladder must double at each step")
        if self.fat2_multipliers[0] != self.fat1_multipliers[-1] * Decimal("2"):
            raise ValueError("FAT 2 must continue the FAT 1 doubling sequence")
        if any(
            right >= left * Decimal("2")
            for left, right in zip(self.skinny_multipliers, self.skinny_multipliers[1:])
        ):
            raise ValueError("SKINNY ladder steps must remain below doubling")

    @staticmethod
    def _validate_ladder(values: tuple[Decimal, ...], name: str) -> None:
        if not isinstance(values, tuple) or not values:
            raise ValueError(f"{name} ladder must be a non-empty tuple")
        if any(not isinstance(value, Decimal) or value <= ZERO for value in values):
            raise ValueError(f"{name} ladder multipliers must be positive Decimals")
        if any(right <= left for left, right in zip(values, values[1:])):
            raise ValueError(f"{name} ladder multipliers must be strictly increasing")


@dataclass(frozen=True)
class VCAPFragmentCandidate:
    fragment_id: str
    ordinal: int
    status: VCAPFragmentStatus
    priority_score: Decimal = ZERO
    pending_executor: bool = False
    has_phase_steps: bool = True
    last_completed_at: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.fragment_id, str) or not self.fragment_id.strip():
            raise ValueError("fragment_id is required")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("fragment ordinal must be a non-negative integer")
        if not isinstance(self.status, VCAPFragmentStatus):
            raise TypeError("fragment status must be VCAPFragmentStatus")
        if not isinstance(self.priority_score, Decimal):
            raise TypeError("fragment priority_score must be Decimal")
        if not self.priority_score.is_finite() or not ZERO <= self.priority_score <= Decimal("1"):
            raise ValueError("fragment priority_score must be between zero and one")
        if (
            isinstance(self.last_completed_at, bool)
            or not isinstance(self.last_completed_at, (int, float))
            or not isfinite(float(self.last_completed_at))
            or self.last_completed_at < 0
        ):
            raise ValueError("last_completed_at must be a finite non-negative timestamp")


def drawdown_bps_from_prices(entry_price: Decimal, current_price: Decimal) -> int:
    """Return positive drawdown magnitude in bps; zero means at/above basis."""
    if (
        not isinstance(entry_price, Decimal)
        or not isinstance(current_price, Decimal)
        or not entry_price.is_finite()
        or not current_price.is_finite()
        or entry_price <= ZERO
        or current_price <= ZERO
    ):
        raise ValueError("entry and current prices must be positive finite Decimals")
    if current_price >= entry_price:
        return 0
    return int(
        (((entry_price - current_price) / entry_price) * Decimal("10000")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def classify_adaptive_vcap_phase(
    drawdown_bps: int,
    policy: AdaptiveVCAPPolicy | None = None,
) -> AdaptiveVCAPPhase:
    """Classify the asset's drawdown; -11.74% enters SKINNY and -17.77% FAT 2."""
    selected = policy or AdaptiveVCAPPolicy()
    if isinstance(drawdown_bps, bool) or not isinstance(drawdown_bps, int):
        raise TypeError("drawdown_bps must be integer basis points")
    if drawdown_bps < 0:
        raise ValueError("drawdown_bps is a non-negative drawdown magnitude")
    if drawdown_bps == 0:
        return AdaptiveVCAPPhase.RECOVERED
    if drawdown_bps < selected.activation_drawdown_bps:
        return AdaptiveVCAPPhase.PRE_VCAP
    if drawdown_bps < selected.fat1_end_drawdown_bps:
        return AdaptiveVCAPPhase.FAT_1
    if drawdown_bps < selected.skinny_end_drawdown_bps:
        return AdaptiveVCAPPhase.SKINNY
    if drawdown_bps <= selected.fat2_end_drawdown_bps:
        return AdaptiveVCAPPhase.FAT_2
    return AdaptiveVCAPPhase.DEEP_GOVERNOR


def adaptive_vcap_ladder_amounts(
    phase: AdaptiveVCAPPhase,
    policy: AdaptiveVCAPPolicy | None = None,
) -> tuple[Decimal, ...]:
    """Return quote-sized ladder steps for one fragment in the selected phase."""
    selected = policy or AdaptiveVCAPPolicy()
    if not isinstance(phase, AdaptiveVCAPPhase):
        raise TypeError("phase must be AdaptiveVCAPPhase")
    multipliers = {
        AdaptiveVCAPPhase.FAT_1: selected.fat1_multipliers,
        AdaptiveVCAPPhase.SKINNY: selected.skinny_multipliers,
        AdaptiveVCAPPhase.FAT_2: selected.fat2_multipliers,
    }.get(phase, ())
    return tuple((selected.base_quote * value).quantize(CENT) for value in multipliers)


def next_adaptive_vcap_step(
    phase: AdaptiveVCAPPhase,
    completed_steps: int,
    policy: AdaptiveVCAPPolicy | None = None,
) -> Decimal | None:
    if isinstance(completed_steps, bool) or not isinstance(completed_steps, int) or completed_steps < 0:
        raise ValueError("completed_steps must be a non-negative integer")
    amounts = adaptive_vcap_ladder_amounts(phase, policy)
    if completed_steps >= len(amounts):
        return None
    return amounts[completed_steps]


def next_pre_vcap_dca_trigger(
    drawdown_bps: int,
    completed_steps: int,
) -> int | None:
    """Return the next conventional DCA trigger before VCAP activates."""
    if isinstance(drawdown_bps, bool) or not isinstance(drawdown_bps, int) or drawdown_bps < 0:
        raise ValueError("drawdown_bps must be a non-negative integer")
    if isinstance(completed_steps, bool) or not isinstance(completed_steps, int) or completed_steps < 0:
        raise ValueError("completed_steps must be a non-negative integer")
    if completed_steps >= len(PRE_VCAP_DCA_TRIGGERS_BPS):
        return None
    trigger = PRE_VCAP_DCA_TRIGGERS_BPS[completed_steps]
    if drawdown_bps >= trigger:
        return trigger
    return None


def adaptive_vcap_priority_score(
    *,
    mean_reversion_score: Decimal,
    confidence: Decimal,
    liquidity_score: Decimal,
    volatility_score: Decimal,
    recovery_efficiency: Decimal,
    drawdown_bps: int,
    policy: AdaptiveVCAPPolicy | None = None,
) -> Decimal:
    """Rank fragments by expected recovery quality, not simply deepest loss."""
    selected = policy or AdaptiveVCAPPolicy()
    scores = (
        mean_reversion_score,
        confidence,
        liquidity_score,
        volatility_score,
        recovery_efficiency,
    )
    if any(
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < ZERO
        or value > Decimal("1")
        for value in scores
    ):
        raise ValueError("VCAP priority inputs must be Decimals between zero and one")
    if isinstance(drawdown_bps, bool) or not isinstance(drawdown_bps, int) or drawdown_bps < 0:
        raise ValueError("drawdown_bps must be a non-negative integer")
    urgency = min(
        Decimal("1"),
        max(
            ZERO,
            Decimal(drawdown_bps - selected.activation_drawdown_bps)
            / Decimal(selected.fat2_end_drawdown_bps - selected.activation_drawdown_bps),
        ),
    )
    signal = mean_reversion_score * confidence
    volatility_quality = Decimal("1") - volatility_score
    score = (
        Decimal("0.30") * signal
        + Decimal("0.25") * liquidity_score
        + Decimal("0.20") * volatility_quality
        + Decimal("0.15") * recovery_efficiency
        + Decimal("0.10") * urgency
    )
    return score.quantize(Decimal("0.000001"))


def select_active_vcap_fragments(
    candidates: tuple[VCAPFragmentCandidate, ...],
    *,
    phase: AdaptiveVCAPPhase,
    policy: AdaptiveVCAPPolicy | None = None,
) -> tuple[VCAPFragmentCandidate, ...]:
    """Select up to two active fragments per asset, preserving pending orders.

    Pending executor reconciliation always takes precedence. If corrupt legacy
    state already has more pending fragments than the configured cap, all are
    returned for reconciliation and no new fragment is activated.
    """
    selected = policy or AdaptiveVCAPPolicy()
    if not isinstance(phase, AdaptiveVCAPPhase):
        raise TypeError("phase must be AdaptiveVCAPPhase")
    if not isinstance(candidates, tuple):
        raise TypeError("candidates must be a tuple")
    if len({candidate.fragment_id for candidate in candidates}) != len(candidates):
        raise ValueError("fragment candidates must have unique IDs")

    pending = sorted(
        (candidate for candidate in candidates if candidate.pending_executor),
        key=lambda candidate: candidate.ordinal,
    )
    if len(pending) >= selected.max_active_fragments_per_position:
        return tuple(pending)
    if phase in {
        AdaptiveVCAPPhase.PRE_VCAP,
        AdaptiveVCAPPhase.RECOVERED,
        AdaptiveVCAPPhase.DEEP_GOVERNOR,
    }:
        return tuple(pending)

    slots = selected.max_active_fragments_per_position - len(pending)
    active = sorted(
        (
            candidate
            for candidate in candidates
            if not candidate.pending_executor
            and candidate.status in {VCAPFragmentStatus.ACTIVE, VCAPFragmentStatus.RECONCILING}
            and candidate.has_phase_steps
        ),
        key=lambda candidate: (
            -candidate.priority_score,
            candidate.last_completed_at,
            candidate.ordinal,
        ),
    )
    keep_active = active[:slots]
    slots -= len(keep_active)
    waiting = sorted(
        (
            candidate
            for candidate in candidates
            if not candidate.pending_executor
            and candidate.status in {VCAPFragmentStatus.WAITING, VCAPFragmentStatus.PAUSED}
            and candidate.has_phase_steps
        ),
        key=lambda candidate: (-candidate.priority_score, candidate.ordinal),
    )
    activated = waiting[:slots]
    return tuple((*pending, *keep_active, *activated))
