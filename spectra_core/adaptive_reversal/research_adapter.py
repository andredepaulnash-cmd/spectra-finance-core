"""Explicit provisional equal-count adapter, not a recovered signal architecture.

The caller chooses the flags, defines their feature families, and supplies an
explicitly confirmed reversal. Two correlated flags in one group count once.
This adapter does not compute signals or infer statistical independence.
"""
from dataclasses import dataclass
from decimal import Decimal
try:
    from .policy import CompletedBar, Evidence
except ImportError:
    from policy import CompletedBar, Evidence


@dataclass(frozen=True)
class CausalFlag:
    source_id: str
    feature_family: str
    active: bool
    asof: int
    valid_until: int

    def __post_init__(self) -> None:
        if not self.source_id or not self.feature_family or type(self.active) is not bool:
            raise ValueError("flag needs explicit source, feature family and boolean value")
        if (type(self.asof) is not int or type(self.valid_until) is not int
                or not 0 <= self.asof <= self.valid_until):
            raise ValueError("invalid causal flag validity interval")


def count_evidence(*, evidence_id: str, observed_at: int, bar: CompletedBar,
                   atr: Decimal, flags: tuple[CausalFlag, ...], confirmed_reversal: bool,
                   fee_net_arm_measure: Decimal | None = None,
                   accounting_asof: int | None = None) -> Evidence:
    if not isinstance(flags, tuple):
        raise ValueError("flags must be an immutable tuple")
    if len({f.source_id for f in flags}) != len(flags):
        raise ValueError("duplicate source flag")
    if any(not f.asof <= bar.close_at <= f.valid_until for f in flags):
        raise ValueError("future or expired flag")
    count = len({f.feature_family for f in flags if f.active})
    return Evidence(evidence_id=evidence_id, observed_at=observed_at, bar=bar,
                    feature_asof=max((f.asof for f in flags), default=bar.close_at),
                    causal_confirmed=True, atr=atr, evidence_score=Decimal(count),
                    evidence_count=count, confirmed_reversal=confirmed_reversal,
                    fee_net_arm_measure=fee_net_arm_measure, accounting_asof=accounting_asof)
