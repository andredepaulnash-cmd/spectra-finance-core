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
    position_hold_snapshot_ttl_seconds: int = 30


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
class ExecutionProvenance:
    proposal_id: str
    executor_id: str
    pair: str
    intent: IntentKind
    authorized_quote: Decimal
    leverage: Decimal
    account_name: str | None = None
    connector_name: str | None = None
    controller_id: str | None = None


@dataclass(frozen=True)
class ExecutorAccountingObservation:
    executor_id: str
    status: str
    filled_amount_quote: Decimal
    cum_fees_quote: Decimal
    position_size_quote: Decimal | None = None
    net_pnl_quote: Decimal | None = None
    realized_pnl_quote: Decimal | None = None
    close_type: str | None = None
    executor_type: str | None = None


@dataclass(frozen=True)
class RecoveryExposureAssessment:
    executor_id: str
    exposure_quote: Decimal
    source: str
    conservative: bool = False
    requires_position_hold_reconciliation: bool = False


@dataclass(frozen=True)
class ExecutorAccountingDelta:
    proposal_id: str
    executor_id: str
    pair: str
    intent: IntentKind
    filled_delta_quote: Decimal
    fee_delta_quote: Decimal
    cumulative_filled_quote: Decimal
    cumulative_fees_quote: Decimal
    position_size_quote: Decimal | None
    status: str
    close_type: str | None
    executor_type: str | None = None


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
