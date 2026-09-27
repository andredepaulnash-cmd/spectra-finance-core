from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from spectra_core.accounting_health import (
    AccountingHealthDecision,
    AccountingHealthSupervisor,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import synchronize_executor_evidence
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
)
from spectra_core.models import IntentKind

from mcp_servers.hummingbot_api.tools.executor_provenance import (
    ExecutorProvenanceConflict,
    ExecutorProvenanceUnavailable,
    fetch_executor_order_snapshot,
    fetch_executor_trade_snapshot,
)


def _proposal_ids(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("proposal_id must be a non-empty string")
        proposal_id = value.strip()
        if proposal_id not in seen:
            seen.add(proposal_id)
            result.append(proposal_id)
    return tuple(result)


def _expected_role(intent: IntentKind, side: str) -> EconomicFillRole:
    if side == "SELL":
        return EconomicFillRole.POSITION_EXIT
    if intent == IntentKind.ENTRY:
        return EconomicFillRole.BASE_ENTRY
    if intent == IntentKind.RECOVERY:
        return EconomicFillRole.VCAP_RECOVERY
    raise ValueError("proposal intent cannot own active economic inventory")


async def synchronize_position_accounting_health(
    client: Any,
    *,
    position_id: str,
    proposal_ids: Iterable[str],
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    supervisor: AccountingHealthSupervisor,
    now: float,
) -> AccountingHealthDecision:
    """Refresh one position's exact accounting proof before live mutation.

    Every remote snapshot is fetched and validated before any economic binding
    or fill is applied. Network failure therefore cannot leave a multi-executor
    position half-synchronized.
    """

    proposals = _proposal_ids(proposal_ids)
    if not proposals:
        return supervisor.record_hold(
            position_id=position_id,
            now=now,
            reason="active_position_has_no_exact_proposal_ownership",
        )

    evidence: list[tuple[str, object, object, object]] = []
    executors: dict[str, str] = {}

    try:
        for proposal_id in proposals:
            provenance = governor.get_execution_provenance(proposal_id)
            if provenance is None:
                return supervisor.record_hold(
                    position_id=position_id,
                    now=now,
                    reason=f"missing_execution_provenance:{proposal_id}",
                )
            receipt = governor.get_execution_receipt(proposal_id)
            if receipt is None:
                return supervisor.record_hold(
                    position_id=position_id,
                    now=now,
                    reason=f"missing_execution_receipt:{proposal_id}",
                )
            if receipt != provenance.executor_id:
                return supervisor.record_conflict(
                    position_id=position_id,
                    now=now,
                    reason=f"execution_receipt_conflict:{proposal_id}",
                )

            prior = executors.get(provenance.executor_id)
            if prior is not None and prior != proposal_id:
                return supervisor.record_conflict(
                    position_id=position_id,
                    now=now,
                    reason=(
                        "executor_owned_by_multiple_live_proposals:"
                        f"{provenance.executor_id}"
                    ),
                )
            executors[provenance.executor_id] = proposal_id

            # Trades first, then orders, so every observed fill should have an
            # order visible in the immediately-following ownership snapshot.
            trades = await fetch_executor_trade_snapshot(
                client,
                executor_id=provenance.executor_id,
            )
            orders = await fetch_executor_order_snapshot(
                client,
                executor_id=provenance.executor_id,
            )
            evidence.append((proposal_id, provenance, orders, trades))
    except ExecutorProvenanceUnavailable as exc:
        return supervisor.record_hold(
            position_id=position_id,
            now=now,
            reason=str(exc),
        )
    except ExecutorProvenanceConflict as exc:
        return supervisor.record_conflict(
            position_id=position_id,
            now=now,
            reason=str(exc),
        )

    # Cross-executor identity duplication is impossible to reconcile safely.
    order_owners: dict[str, str] = {}
    trade_owners: dict[str, str] = {}
    for proposal_id, _, orders, trades in evidence:
        for order in orders.orders:
            prior = order_owners.get(order.order_id)
            if prior is not None and prior != proposal_id:
                return supervisor.record_conflict(
                    position_id=position_id,
                    now=now,
                    reason=f"order_id_crosses_proposals:{order.order_id}",
                )
            order_owners[order.order_id] = proposal_id
        for trade in trades.trades:
            prior = trade_owners.get(trade.trade_id)
            if prior is not None and prior != proposal_id:
                return supervisor.record_conflict(
                    position_id=position_id,
                    now=now,
                    reason=f"trade_id_crosses_proposals:{trade.trade_id}",
                )
            trade_owners[trade.trade_id] = proposal_id

    reconciler = LiveFillLedgerReconciler(
        ledger=ledger,
        governor=governor,
    )

    # Preflight all pre-existing bindings across the whole position before any
    # new binding/fill mutation in this sweep.
    for proposal_id, provenance, orders, _ in evidence:
        account = provenance.account_name
        connector = provenance.connector_name
        if account is None or connector is None:
            return supervisor.record_conflict(
                position_id=position_id,
                now=now,
                reason=f"unscoped_execution_provenance:{proposal_id}",
            )
        pair = provenance.pair.strip().upper()
        for order in orders.orders:
            existing = reconciler.get_binding(order.order_id)
            if existing is None:
                continue
            try:
                role = _expected_role(provenance.intent, order.side)
            except ValueError as exc:
                return supervisor.record_conflict(
                    position_id=position_id,
                    now=now,
                    reason=f"{proposal_id}:{exc}",
                )
            if (
                existing.position_id != position_id
                or existing.proposal_id != proposal_id
                or existing.executor_id != provenance.executor_id
                or existing.pair != pair
                or existing.account_name != account
                or existing.connector_name != connector.lower()
                or existing.role != role
            ):
                return supervisor.record_conflict(
                    position_id=position_id,
                    now=now,
                    reason=f"persisted_order_binding_conflict:{order.order_id}",
                )

    reports = []
    try:
        for proposal_id, _, orders, trades in evidence:
            reports.append(
                synchronize_executor_evidence(
                    reconciler=reconciler,
                    position_id=position_id,
                    proposal_id=proposal_id,
                    order_snapshot=orders,
                    trade_snapshot=trades,
                    now=now,
                )
            )
    except (ValueError, RuntimeError) as exc:
        return supervisor.record_conflict(
            position_id=position_id,
            now=now,
            reason=f"deterministic_accounting_conflict:{exc}",
        )

    return supervisor.record_reports(
        position_id=position_id,
        reports=reports,
        now=now,
    )


async def synchronize_all_active_position_accounting(
    client: Any,
    *,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    supervisor: AccountingHealthSupervisor,
    now: float,
    extra_proposal_ids_by_position: Mapping[
        str,
        Iterable[str],
    ] | None = None,
) -> dict[str, AccountingHealthDecision]:
    """Refresh accounting proof for every active Spectra economic position.

    Active proposal ownership is discovered from exact ACTIVE_TRADE lots.
    Callers may additionally supply checkpointed proposals that have an
    execution receipt but have not produced a fill lot yet.
    """

    extras = extra_proposal_ids_by_position or {}
    decisions: dict[str, AccountingHealthDecision] = {}

    for position_id in ledger.list_active_position_ids():
        proposal_ids = _proposal_ids(
            (
                *ledger.active_proposal_ids(position_id),
                *tuple(extras.get(position_id, ())),
            )
        )
        decisions[position_id] = await synchronize_position_accounting_health(
            client,
            position_id=position_id,
            proposal_ids=proposal_ids,
            governor=governor,
            ledger=ledger,
            supervisor=supervisor,
            now=now,
        )

    return decisions
