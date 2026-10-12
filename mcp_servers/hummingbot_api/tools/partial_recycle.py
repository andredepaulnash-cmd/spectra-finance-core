from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal
from typing import Any, Awaitable, Callable

from aiohttp import ClientResponseError

from mcp_servers.hummingbot_api.tools.executor_provenance import (
    fetch_executor_order_snapshot,
    fetch_executor_trade_snapshot,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_accounting import (
    executor_accounting_observation_from_raw,
)
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import BreakerMode
from spectra_core.partial_attempt import (
    PartialAttemptAssessment,
    PartialAttemptDisposition,
)
from spectra_core.partial_recycle import (
    PartialRecycleAction,
    PartialRecycleDecision,
    PartialRecycleManifest,
    PartialRecycleStore,
    assess_partial_recycle_manifest,
    remaining_partial_recycle_base,
    validate_partial_recycle_manifest,
)
from spectra_core.runtime import get_breaker_mode

ZERO = Decimal("0")


def _owner_scope(
    manifest: PartialRecycleManifest,
    governor: CapitalGovernor,
) -> tuple[str, str]:
    account_name: str | None = None
    connector_name: str | None = None
    for owner in manifest.owners:
        provenance = governor.get_execution_provenance(owner.proposal_id)
        receipt = governor.get_execution_receipt(owner.proposal_id)
        if (
            provenance is None
            or receipt != owner.executor_id
            or provenance.executor_id != owner.executor_id
        ):
            raise RuntimeError("partial recycle owner provenance changed")
        if provenance.account_name is None or provenance.connector_name is None:
            raise RuntimeError("partial recycle owner scope is incomplete")
        if account_name is None:
            account_name = provenance.account_name
            connector_name = provenance.connector_name.lower()
        elif (
            account_name != provenance.account_name
            or connector_name != provenance.connector_name.lower()
        ):
            raise RuntimeError("partial recycle owners cross account/connector scope")
    if account_name is None or connector_name is None:
        raise RuntimeError("partial recycle manifest has no owner scope")
    return account_name, connector_name


async def _sources_are_passive(
    client: Any,
    manifest: PartialRecycleManifest,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    for owner in manifest.owners:
        raw = await client.executors.get_executor(owner.executor_id)
        observation = executor_accounting_observation_from_raw(
            raw,
            expected_executor_id=owner.executor_id,
        )
        status = observation.status.strip().upper()
        close_type = (
            ""
            if observation.close_type is None
            else observation.close_type.strip().upper()
        )
        if status != "TERMINATED" or close_type != "POSITION_HOLD":
            reasons.append(
                f"source_executor_not_passive:{owner.executor_id}:"
                f"{status}:{close_type or 'NONE'}"
            )
    return not reasons, tuple(reasons)


def _sell_geometry(fill) -> tuple[Decimal, Decimal, Decimal]:
    base_asset, quote_asset = fill.pair.split("-", 1)
    base_sold = fill.amount_base
    exit_fee_quote = ZERO
    if fill.fee_paid > ZERO and fill.fee_currency not in {
        base_asset,
        quote_asset,
    }:
        raise ValueError("third-currency recycle fee cannot be valued exactly")
    if fill.fee_paid > ZERO and fill.fee_currency == base_asset:
        base_sold += fill.fee_paid
    elif fill.fee_paid > ZERO and fill.fee_currency == quote_asset:
        exit_fee_quote = fill.fee_paid
    return base_sold, fill.quote_amount, exit_fee_quote


def _release_basis_by_owner(
    manifest: PartialRecycleManifest,
    allocated_basis: Decimal,
    *,
    event_id: str,
    ledger: EconomicPositionLedger,
) -> tuple[tuple[str, Decimal], ...]:
    """Release actual sold-lot owners, never stale manifest proportions."""
    with closing(
        sqlite3.connect(ledger.path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        rows = conn.execute(
            "SELECT lot_id, proposal_id, executor_id, ownership, allocated_cost_basis_quote "
            "FROM economic_exit_allocations WHERE event_id=? AND position_id=?",
            (event_id, manifest.position_id),
        ).fetchall()
    owners = {owner.proposal_id: owner.executor_id for owner in manifest.owners}
    allocated: dict[str, Decimal] = {}
    for lot_id, proposal_id, executor_id, ownership, raw_basis in rows:
        basis = Decimal(raw_basis)
        if (
            lot_id not in manifest.lot_ids
            or proposal_id not in owners
            or owners[proposal_id] != executor_id
            or ownership != "ACTIVE_TRADE"
            or not basis.is_finite()
            or basis < ZERO
        ):
            raise RuntimeError(
                "partial recycle exact allocation owner evidence conflicts"
            )
        allocated[proposal_id] = allocated.get(proposal_id, ZERO) + basis
    if not rows or sum(allocated.values(), ZERO) != allocated_basis:
        # Legacy events without a native allocation journal need explicit
        # reconciliation. Original manifest weights cannot prove their owners.
        raise RuntimeError("partial recycle exact allocation evidence is incomplete")
    return tuple(
        (proposal_id, basis)
        for proposal_id, basis in sorted(allocated.items())
        if basis > ZERO
    )


async def synchronize_partial_recycle_executor(
    client: Any,
    *,
    manifest: PartialRecycleManifest,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    store: PartialRecycleStore,
    now: float,
) -> dict[str, Any]:
    """Reconcile exact SELL evidence from the deterministic recycle executor."""

    validate_partial_recycle_manifest(
        manifest,
        ledger=ledger,
        governor=governor,
    )
    account_name, connector_name = _owner_scope(manifest, governor)

    # Fetch both evidence surfaces before mutating the economic ledger.
    trades = await fetch_executor_trade_snapshot(
        client,
        executor_id=manifest.sell_executor_id,
    )
    orders = await fetch_executor_order_snapshot(
        client,
        executor_id=manifest.sell_executor_id,
    )
    known_orders = {order.order_id: order for order in orders.orders}
    if len(known_orders) != len(orders.orders):
        raise RuntimeError("partial recycle order evidence contains duplicates")
    if len({fill.trade_id for fill in trades.trades}) != len(trades.trades):
        raise RuntimeError("partial recycle trade evidence contains duplicates")
    for fill in trades.trades:
        if fill.order_id not in known_orders:
            raise RuntimeError("partial recycle fill references unknown executor order")
    for order in orders.orders:
        filled = sum(
            (
                fill.amount_base
                for fill in trades.trades
                if fill.order_id == order.order_id
            ),
            ZERO,
        )
        if (
            order.filled_amount > order.amount
            or filled != order.filled_amount
            or (
                order.status.upper() == "FILLED" and order.filled_amount != order.amount
            )
        ):
            raise RuntimeError(
                "partial recycle order and exact fill totals do not reconcile"
            )

    for order in orders.orders:
        if order.side != "SELL":
            raise RuntimeError("partial recycle executor emitted a non-SELL order")
        if order.amount > manifest.target_base:
            raise RuntimeError("partial recycle order exceeds immutable manifest base")

    applied = 0
    replayed = 0
    for fill in trades.trades:
        order = known_orders.get(fill.order_id)
        if order is None:
            raise RuntimeError("partial recycle fill references unknown executor order")
        if fill.side != "SELL" or order.side != "SELL":
            raise RuntimeError("partial recycle evidence is not SELL-only")
        if (
            fill.pair != manifest.pair
            or fill.account_name != account_name
            or fill.connector_name != connector_name
        ):
            raise RuntimeError("partial recycle fill crosses immutable execution scope")

        base_sold, proceeds, exit_fee = _sell_geometry(fill)
        event_id = f"partial-recycle:{manifest.manifest_id}:{fill.trade_id}"
        result = ledger.record_partial_exit(
            event_id=event_id,
            position_id=manifest.position_id,
            base_sold=base_sold,
            gross_proceeds_quote=proceeds,
            exit_fee_quote=exit_fee,
            occurred_at=fill.occurred_at,
            eligible_lot_ids=manifest.lot_ids,
            mutation_owner_id=(
                "partial:" + manifest.manifest_id
                if ledger.selected_recovery_lock_owner(manifest.position_id)
                == "partial:" + manifest.manifest_id
                else None
            ),
        )

        for proposal_id, released_basis in _release_basis_by_owner(
            manifest,
            result.allocated_cost_basis_quote,
            event_id=event_id,
            ledger=ledger,
        ):
            governor.record_recovery_recycle_release(
                release_id=(
                    f"{manifest.manifest_id}:{fill.trade_id}:" f"{proposal_id}"
                ),
                proposal_id=proposal_id,
                amount_quote=released_basis,
            )

        was_recorded = store.fill_recorded(fill.trade_id)
        store.record_fill(
            trade_id=fill.trade_id,
            manifest_id=manifest.manifest_id,
            order_id=fill.order_id,
            base_sold=result.base_sold,
            gross_proceeds_quote=result.gross_proceeds_quote,
            exit_fee_quote=result.exit_fee_quote,
            realized_pnl_quote=result.realized_pnl_quote,
            occurred_at=fill.occurred_at,
        )
        if was_recorded:
            replayed += 1
        else:
            applied += 1

    progress = assess_partial_recycle_manifest(
        manifest,
        ledger=ledger,
        governor=governor,
        store=store,
        now=now,
    )
    if (
        progress.completed
        and orders.orders
        and all(
            order.status.upper()
            in {"FILLED", "CANCELED", "CANCELLED", "FAILED", "REJECTED"}
            for order in orders.orders
        )
        and ledger.selected_recovery_lock_owner(manifest.position_id)
        == "partial:" + manifest.manifest_id
    ):
        ledger.release_selected_recovery_lock(
            manifest.position_id, "partial:" + manifest.manifest_id
        )
    return {
        "manifest_id": manifest.manifest_id,
        "sell_executor_id": manifest.sell_executor_id,
        "applied_fills": applied,
        "replayed_fills": replayed,
        "remaining_base": str(progress.remaining_base),
        "realized_net_pnl_quote": str(progress.realized_pnl_quote),
        "completed": progress.completed,
    }


async def execute_partial_recycle(
    client: Any,
    *,
    decision: PartialRecycleDecision,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    now: float,
    account_name: str = "master_account",
    controller_id: str = "spectra",
    attempt_guard: Callable[[], Awaitable[PartialAttemptAssessment]] | None = None,
) -> dict[str, Any]:
    """Dispatch one exact partial-recycle SELL executor.

    The sell executor ID is deterministic and checkpointed in the manifest
    before dispatch, closing the crash window that would otherwise leave an
    unidentifiable sell order after a transport failure.
    """

    if decision.action != PartialRecycleAction.RECYCLE:
        return {
            "action": decision.action.value,
            "executed": False,
            "reasons": list(decision.reasons),
        }
    manifest = decision.manifest
    if manifest is None:
        raise ValueError("RECYCLE decision is missing immutable manifest")

    mutation_owner = "partial:" + manifest.manifest_id
    lock_owner = ledger.selected_recovery_lock_owner(manifest.position_id)
    if lock_owner is not None and lock_owner != mutation_owner:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "reasons": ["another_exact_position_mutation_pending"],
        }
    if lock_owner == mutation_owner and attempt_guard is None:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "reasons": ["guarded_partial_mutation_requires_reconciliation"],
        }

    if get_breaker_mode() == BreakerMode.HALTED:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "reasons": ["breaker_halted_new_recycle_dispatch"],
        }

    validate_partial_recycle_manifest(
        manifest,
        ledger=ledger,
        governor=governor,
    )
    owner_account, owner_connector = _owner_scope(manifest, governor)
    if account_name != owner_account or owner_connector != "kraken":
        raise ValueError(
            "partial recycle execution scope must match Kraken owner scope"
        )
    if trading_rule.trading_pair.strip().upper() != manifest.pair:
        raise ValueError("partial recycle trading rule pair changed")

    remaining = remaining_partial_recycle_base(
        manifest,
        ledger=ledger,
    )
    price = Decimal(str(current_price))
    if remaining <= ZERO:
        store = PartialRecycleStore(ledger.path)
        progress = assess_partial_recycle_manifest(
            manifest,
            ledger=ledger,
            governor=governor,
            store=store,
            now=now,
        )
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "completed": progress.completed,
            "realized_net_pnl_quote": str(progress.realized_pnl_quote),
            "reasons": ["partial_recycle_already_complete"],
        }
    if (
        remaining < trading_rule.min_order_size
        or remaining * price < trading_rule.effective_min_quote
    ):
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "reasons": ["current_exchange_minimum_blocks_recycle"],
        }

    passive, passive_reasons = await _sources_are_passive(
        client,
        manifest,
    )
    if not passive:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "reasons": list(passive_reasons),
        }

    existing = None
    try:
        existing = await client.executors.get_executor(manifest.sell_executor_id)
    except Exception as error:
        if attempt_guard is not None:
            # The opted-in safe path cannot interpret transport failure as
            # proof that a deterministic executor has never been dispatched.
            if not isinstance(error, ClientResponseError) or error.status != 404:
                return {
                    "action": "PARTIAL_RECYCLE",
                    "executed": False,
                    "manifest_id": manifest.manifest_id,
                    "sell_executor_id": manifest.sell_executor_id,
                    "reasons": ["partial_executor_lookup_requires_reconciliation"],
                }
        existing = None

    if existing is None:
        if attempt_guard is not None:
            assessment = await attempt_guard()
            if (
                assessment.manifest_id != manifest.manifest_id
                or assessment.disposition != PartialAttemptDisposition.ELIGIBLE
            ):
                return {
                    "action": "PARTIAL_RECYCLE",
                    "executed": False,
                    "manifest_id": manifest.manifest_id,
                    "sell_executor_id": manifest.sell_executor_id,
                    "reasons": [
                        "partial_final_dispatch_guard_blocked",
                        *assessment.reasons,
                    ],
                }
            ledger.claim_selected_recovery_lock(manifest.position_id, mutation_owner)
            validate_partial_recycle_manifest(
                manifest, ledger=ledger, governor=governor
            )
            remaining = remaining_partial_recycle_base(manifest, ledger=ledger)
            if remaining != assessment.quantity:
                return {
                    "action": "PARTIAL_RECYCLE",
                    "executed": False,
                    "manifest_id": manifest.manifest_id,
                    "sell_executor_id": manifest.sell_executor_id,
                    "reasons": ["partial_inventory_changed_after_assessment"],
                }
            store = PartialRecycleStore(ledger.path)
            if not store.claim_dispatch_once(manifest, now=now):
                return {
                    "action": "PARTIAL_RECYCLE",
                    "executed": False,
                    "manifest_id": manifest.manifest_id,
                    "sell_executor_id": manifest.sell_executor_id,
                    "reasons": ["partial_dispatch_outcome_requires_reconciliation"],
                }
        # No further awaits occur between exact local revalidation/claim and
        # the single external create call. Unknown outcomes retain the claim.
        result = await client.executors.create_executor(
            executor_config={
                "id": manifest.sell_executor_id,
                "type": "order_executor",
                "connector_name": "kraken",
                "trading_pair": manifest.pair,
                "side": 2,
                "amount": str(remaining),
                "execution_strategy": "MARKET",
                "leverage": 1,
                "level_id": (
                    "partial-recycle-" + manifest.manifest_id.rsplit(":", 1)[-1][:12]
                ),
            },
            account_name=account_name,
            controller_id=controller_id,
        )
        executor_id = result.get("executor_id") or result.get("id")
        if executor_id != manifest.sell_executor_id:
            raise RuntimeError("partial recycle executor identity is ambiguous")

    store = PartialRecycleStore(ledger.path)
    sync = await synchronize_partial_recycle_executor(
        client,
        manifest=manifest,
        governor=governor,
        ledger=ledger,
        store=store,
        now=now,
    )
    return {
        "action": "PARTIAL_RECYCLE",
        "executed": True,
        "manifest_id": manifest.manifest_id,
        "sell_executor_id": manifest.sell_executor_id,
        "result": sync,
        "reasons": list(decision.reasons),
    }
