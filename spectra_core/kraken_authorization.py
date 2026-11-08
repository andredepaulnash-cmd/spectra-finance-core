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
from .runtime import (
    get_accounting_health_supervisor,
    get_capital_governor,
    get_dust_lifecycle,
    get_economic_position_ledger,
    spectra_paper_only_enabled,
)


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


def _require_reconciled_existing_exposure(*, now: float) -> None:
    """Block additional buy-side exposure while any held position is unverified.

    Empty position shells do not represent exposure and do not need a fill
    watermark yet. Once confirmed inventory exists, however, missing, stale,
    held, or conflicted accounting health blocks new ENTRY/RECOVERY capital.
    Exit-side actions remain available through their separate authorization path.
    """
    ledger = get_economic_position_ledger()
    supervisor = get_accounting_health_supervisor()

    for position_id in ledger.list_active_position_ids():
        snapshot = ledger.snapshot(position_id)
        if snapshot.active_base <= 0:
            continue
        health = supervisor.assess(position_id=position_id, now=now)
        if not health.allows_live_mutation(now=now):
            raise ValueError(
                "accounting health gate blocks new exposure: "
                f"position {position_id} is {health.state.value}"
            )


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
    Request capital authority for one Kraken USD spot or isolated paper action.

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
    connector_name = (
        "kraken_paper_trade" if spectra_paper_only_enabled() else "kraken"
    )

    snapshot = await build_kraken_capital_snapshot(
        client,
        account_name=account,
        trading_pair=pair,
        recovery_exposure_quote=(capital_governor.current_recovery_exposure_quote()),
        connector_name=connector_name,
        refresh=refresh,
    )

    proposal = CapitalProposal(
        proposal_id=f"spectra-{intent_kind.value.lower()}-{uuid4().hex}",
        pair=pair,
        intent=intent_kind,
        amount_quote=amount,
        leverage=Decimal("1"),
        account_name=account,
        connector_name=connector_name,
        quote_asset=pair.rsplit("-", 1)[1],
    )

    # The portfolio fetch above is an await boundary. Recheck durable
    # accounting health and dust admission before the Governor reserves capital.
    authorization_now = time.time()
    if intent_kind in {IntentKind.ENTRY, IntentKind.RECOVERY}:
        _require_reconciled_existing_exposure(now=authorization_now)

    dust_lifecycle = get_dust_lifecycle(governor=capital_governor)
    economic_position_id = dust_lifecycle.prepare_entry(
        proposal=proposal, account_name=account, now=authorization_now
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
