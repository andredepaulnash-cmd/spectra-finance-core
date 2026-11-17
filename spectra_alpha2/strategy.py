"""Deterministic drawdown-to-action planner; no execution side effects."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .policy import (
    DCA_STEPS,
    VCAP_OUTER_BOUNDARY,
    VCAP_STEPS,
    LadderStep,
    VCAPPhase,
    next_eligible_vcap_step,
)


class PlannedActionType(StrEnum):
    DCA = "DCA"
    VCAP = "VCAP"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action_type: PlannedActionType
    step: LadderStep | None
    drawdown: Decimal
    reason: str


def calculate_drawdown(reference_price: Decimal, current_price: Decimal) -> Decimal:
    for value, name in ((reference_price, "reference_price"), (current_price, "current_price")):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite positive Decimal")
    return current_price / reference_price - Decimal("1")


def next_eligible_dca_step(
    drawdown: Decimal, completed_step_ids: set[str] | frozenset[str]
) -> LadderStep | None:
    if not isinstance(drawdown, Decimal) or not drawdown.is_finite():
        raise TypeError("drawdown must be a finite Decimal")
    if drawdown <= VCAP_OUTER_BOUNDARY:
        return None
    for step in DCA_STEPS:
        if step.step_id in completed_step_ids:
            continue
        return step if drawdown <= step.drawdown else None
    return None


def plan_next_action(
    drawdown: Decimal,
    *,
    completed_dca_steps: set[str] | frozenset[str],
    completed_vcap_steps: set[str] | frozenset[str],
) -> PlannedAction:
    if not isinstance(drawdown, Decimal) or not drawdown.is_finite():
        raise TypeError("drawdown must be a finite Decimal")
    if drawdown <= VCAP_OUTER_BOUNDARY:
        return PlannedAction(
            PlannedActionType.MANUAL_REVIEW,
            None,
            drawdown,
            "outer_boundary_reached_no_automatic_patching",
        )
    dca = next_eligible_dca_step(drawdown, completed_dca_steps)
    if dca is not None:
        return PlannedAction(PlannedActionType.DCA,dca,drawdown,"next_uncompleted_dca_threshold_reached")
    vcap = next_eligible_vcap_step(drawdown,completed_vcap_steps)
    if vcap is not None:
        return PlannedAction(PlannedActionType.VCAP,vcap,drawdown,"next_sequential_vcap_threshold_reached")
    return PlannedAction(PlannedActionType.NONE,None,drawdown,"no_uncompleted_threshold_reached")


def vcap_step_phase(step_id: str) -> VCAPPhase:
    for step in VCAP_STEPS:
        if step.step_id == step_id:
            return step.phase
    raise ValueError("unknown VCAP step")
