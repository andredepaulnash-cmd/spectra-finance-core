from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .kraken_snapshot import build_kraken_capital_snapshot
from .models import (
    CapitalDecision,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from .runtime import get_capital_governor, get_dust_lifecycle


@dataclass(frozen=True)
class KrakenCapitalAuthorization:
    proposal: CapitalProposal
    decision: CapitalDecision
    snapshot: CapitalSnapshot
    economic_position_id: str | None = None


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid decimal") from exc

    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be finite and positive")

    return result


def _intent(value: IntentKind | str) -> IntentKind:
    if isinstance(value, IntentKind):
        return value

    try:
        return IntentKind(str(value).strip().upper())
    except ValueError as exc:
        raise ValueError(f"unknown Spectra capital intent: {value!r}") from exc


async def reserve_kraken_capital(
    client: Any,
    *,
    account_name: str,
    trading_pair: str,
    intent: IntentKind | str,
    amount_quote: Decimal | str | int | float,
    governor=None,
    refresh: bool = True,
) -> KrakenCapitalAuthorization:
    """
    Request capital authority for one Kraken USD spot action.

    The caller describes economics; Spectra Core creates the proposal identity.
    This function does NOT execute an order.
    """
    account = account_name.strip()
    pair = trading_pair.strip().upper()

    if not account:
        raise ValueError("account_name is required")

    if not pair.endswith("-USD"):
        raise ValueError(
            "Spectra phase-2 Kraken authorization supports USD quote pairs only"
        )

    amount = _positive_decimal(amount_quote, "amount_quote")
    intent_kind = _intent(intent)

    capital_governor = governor or get_capital_governor()

    snapshot = await build_kraken_capital_snapshot(
        client,
        account_name=account,
        trading_pair=pair,
        recovery_exposure_quote=(capital_governor.current_recovery_exposure_quote()),
        refresh=refresh,
    )

    proposal = CapitalProposal(
        proposal_id=f"spectra-{intent_kind.value.lower()}-{uuid4().hex}",
        pair=pair,
        intent=intent_kind,
        amount_quote=amount,
        leverage=Decimal("1"),
    )

    # The portfolio fetch above is an await boundary. Recheck durable dust and
    # duplicate-position admission afterwards, before reserving new capital.
    dust_lifecycle = get_dust_lifecycle(governor=capital_governor)
    economic_position_id = dust_lifecycle.prepare_entry(
        proposal=proposal, account_name=account, now=time.time()
    )
    decision = capital_governor.reserve(proposal, snapshot)
    if decision.status in {
        DecisionStatus.REJECTED,
        DecisionStatus.EXPIRED,
        DecisionStatus.RELEASED,
    }:
        dust_lifecycle.cancel_unsubmitted_entry(
            proposal_id=proposal.proposal_id, now=time.time()
        )

    return KrakenCapitalAuthorization(
        proposal=proposal,
        decision=decision,
        snapshot=snapshot,
        economic_position_id=economic_position_id,
    )
