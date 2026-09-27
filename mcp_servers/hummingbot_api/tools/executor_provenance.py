from __future__ import annotations

from typing import Any

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    ExecutorLedgerSyncReport,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
    executor_order_snapshot_from_raw,
    executor_trade_snapshot_from_raw,
    synchronize_executor_evidence,
)
from spectra_core.fill_reconciliation import LiveFillLedgerReconciler


class ExecutorProvenanceUnavailable(RuntimeError):
    """The authoritative executor provenance surface could not be reached."""


class ExecutorProvenanceConflict(RuntimeError):
    """Authoritative executor evidence failed deterministic ownership checks."""


async def _response_json(response: Any) -> Any:
    value = response.json()
    if hasattr(value, "__await__"):
        return await value
    return value


async def fetch_executor_order_snapshot(
    client: Any,
    *,
    executor_id: str,
) -> ExecutorOrderSnapshot:
    try:
        response = await client.executors.session.get(
            f"{client.executors.base_url}/executors/{executor_id}/orders"
        )
        response.raise_for_status()
        raw = await _response_json(response)
    except Exception as exc:
        raise ExecutorProvenanceUnavailable(
            f"executor order provenance unavailable for {executor_id}"
        ) from exc
    try:
        return executor_order_snapshot_from_raw(
            raw,
            expected_executor_id=executor_id,
        )
    except (ValueError, RuntimeError) as exc:
        raise ExecutorProvenanceConflict(
            f"executor order provenance conflicts for {executor_id}: {exc}"
        ) from exc


async def fetch_executor_trade_snapshot(
    client: Any,
    *,
    executor_id: str,
) -> ExecutorTradeSnapshot:
    try:
        response = await client.executors.session.get(
            f"{client.executors.base_url}/executors/{executor_id}/trades"
        )
        response.raise_for_status()
        raw = await _response_json(response)
    except Exception as exc:
        raise ExecutorProvenanceUnavailable(
            f"executor trade provenance unavailable for {executor_id}"
        ) from exc
    try:
        return executor_trade_snapshot_from_raw(
            raw,
            expected_executor_id=executor_id,
        )
    except (ValueError, RuntimeError) as exc:
        raise ExecutorProvenanceConflict(
            f"executor trade provenance conflicts for {executor_id}: {exc}"
        ) from exc


async def synchronize_executor_economic_ledger(
    client: Any,
    *,
    position_id: str,
    proposal_id: str,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    now: float,
) -> ExecutorLedgerSyncReport:
    """Fetch both immutable evidence surfaces before mutating economic state.

    If either API request or strict parser fails, no order binding, fill lot, or
    exit event is written. Once both snapshots are valid, the deterministic Core
    bridge performs an idempotent synchronization.
    """

    provenance = governor.get_execution_provenance(proposal_id)
    if provenance is None:
        raise ValueError("proposal has no execution provenance")
    receipt = governor.get_execution_receipt(proposal_id)
    if receipt is None or receipt != provenance.executor_id:
        raise ValueError("proposal has no exact execution receipt")

    executor_id = provenance.executor_id

    # Fetch trades first, then orders. A trade can only exist after its order;
    # reading in this direction avoids falsely treating a concurrently-created
    # order/fill pair as an ownership conflict.
    trade_snapshot = await fetch_executor_trade_snapshot(
        client,
        executor_id=executor_id,
    )
    order_snapshot = await fetch_executor_order_snapshot(
        client,
        executor_id=executor_id,
    )

    reconciler = LiveFillLedgerReconciler(
        ledger=ledger,
        governor=governor,
    )
    try:
        return synchronize_executor_evidence(
            reconciler=reconciler,
            position_id=position_id,
            proposal_id=proposal_id,
            order_snapshot=order_snapshot,
            trade_snapshot=trade_snapshot,
            now=now,
        )
    except (ValueError, RuntimeError) as exc:
        raise ExecutorProvenanceConflict(
            f"executor economic provenance conflicts for {executor_id}: {exc}"
        ) from exc
