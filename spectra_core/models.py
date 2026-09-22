from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class IntentKind(StrEnum):
    ENTRY = "ENTRY"
    PRESS = "PRESS"
    RECOVERY = "RECOVERY"
    ROTATION = "ROTATION"


class BreakerMode(StrEnum):
    NORMAL = "NORMAL"
    DEFENSIVE = "DEFENSIVE"
    HALTED = "HALTED"


class DecisionStatus(StrEnum):
    RESERVED = "RESERVED"
    REJECTED = "REJECTED"
    COMMITTED = "COMMITTED"
    RECONCILED = "RECONCILED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class CapitalPolicy:
    reserve_floor_quote: Decimal
    max_total_committed_quote: Decimal
    max_pair_quote: Decimal
    max_position_quote: Decimal
    max_recovery_quote: Decimal
    max_leverage: Decimal = Decimal("1")
    reservation_ttl_seconds: int = 120


@dataclass(frozen=True)
class CapitalSnapshot:
    free_quote: Decimal
    committed_quote: Decimal = Decimal("0")
    pair_exposure_quote: Decimal = Decimal("0")
    position_exposure_quote: Decimal = Decimal("0")
    recovery_exposure_quote: Decimal = Decimal("0")


@dataclass(frozen=True)
class CapitalProposal:
    proposal_id: str
    pair: str
    intent: IntentKind
    amount_quote: Decimal
    leverage: Decimal = Decimal("1")


@dataclass(frozen=True)
class CapitalDecision:
    proposal_id: str
    status: DecisionStatus
    reason: str
    reserved_quote: Decimal
    available_after_quote: Decimal

    @property
    def approved(self) -> bool:
        return self.status in {
            DecisionStatus.RESERVED,
            DecisionStatus.COMMITTED,
        }
