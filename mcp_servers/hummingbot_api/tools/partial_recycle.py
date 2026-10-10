from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

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
from spectra_core.partial_recycle import (
    PartialRecycleAction,
    PartialRecycleDecision,
    PartialRecycleManifest,
    PartialRecycleStore,
    assess_partial_recycle_dispatch,
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
) -> tuple[tuple[str, Decimal], ...]:
    if allocated_basis <= ZERO:
        return ()
    total = manifest.target_cost_basis_quote
    if total <= ZERO:
        raise RuntimeError("partial recycle manifest has no recovery basis")
    remaining = allocated_basis
    rows: list[tuple[str, Decimal]] = []
    for index, owner in enumerate(manifest.owners):
        if index == len(manifest.owners) - 1:
            piece = remaining
        else:
            piece = allocated_basis * owner.target_cost_basis_quote / total
            piece = min(piece, remaining)
        remaining -= piece
        if piece > ZERO:
            rows.append((owner.proposal_id, piece))
    if remaining != ZERO:
        raise RuntimeError("partial recycle basis release did not close exactly")
    return tuple(rows)


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
    if (
        orders.executor_id != manifest.sell_executor_id
        or trades.executor_id != manifest.sell_executor_id
    ):
        raise RuntimeError("partial recycle snapshots cross executor scope")
    known_orders = {order.order_id: order for order in orders.orders}

    for order in orders.orders:
        if order.executor_id != manifest.sell_executor_id:
            raise RuntimeError("partial recycle order crosses executor scope")
        if order.side != "SELL":
            raise RuntimeError("partial recycle executor emitted a non-SELL order")
        if order.amount > manifest.target_base:
            raise RuntimeError("partial recycle order exceeds immutable manifest base")

    applied = 0
    replayed = 0
    durable_fills = []
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
        )

        for proposal_id, released_basis in _release_basis_by_owner(
            manifest,
            result.allocated_cost_basis_quote,
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

        durable_fills.append(
            {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "base_sold": str(result.base_sold),
                "gross_proceeds_quote": str(result.gross_proceeds_quote),
                "exit_fee_quote": str(result.exit_fee_quote),
            }
        )

    terminal = False
    try:
        raw = await client.executors.get_executor(manifest.sell_executor_id)
        observation = executor_accounting_observation_from_raw(
            raw, expected_executor_id=manifest.sell_executor_id
        )
    except Exception:
        observation = None
    terminal_statuses = {
        "FILLED",
        "CANCELED",
        "CANCELLED",
        "FAILED",
        "REJECTED",
        "EXPIRED",
    }
    all_terminal = all(
        order.status.strip().upper() in terminal_statuses for order in orders.orders
    )
    exact_order_match = all(
        order.filled_amount <= order.amount
        and (
            order.status.strip().upper() != "FILLED"
            or order.filled_amount == order.amount
        )
        and order.filled_amount
        == sum(
            (
                trade.amount_base
                for trade in trades.trades
                if trade.order_id == order.order_id
            ),
            ZERO,
        )
        for order in orders.orders
    )
    if (
        observation is not None
        and observation.status.strip().upper() == "TERMINATED"
        and all_terminal
        and exact_order_match
        and observation.filled_amount_quote
        == sum((trade.quote_amount for trade in trades.trades), ZERO)
    ):
        proof = {
            "schema": "spectra-partial-terminal-v1",
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "executor_id": manifest.sell_executor_id,
            "executor_status": "TERMINATED",
            "executor_filled_quote": str(observation.filled_amount_quote),
            "all_orders_terminal": True,
            "exact_order_fill_match": True,
            "orders": [
                {
                    "order_id": order.order_id,
                    "status": order.status.strip().upper(),
                    "amount_base": str(order.amount),
                    "filled_base": str(order.filled_amount),
                }
                for order in orders.orders
            ],
            "trades": [
                {
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "amount_base": str(trade.amount_base),
                    "quote_amount": str(trade.quote_amount),
                    "base_fee": str(_sell_geometry(trade)[0] - trade.amount_base),
                    "exit_fee_quote": str(_sell_geometry(trade)[2]),
                }
                for trade in trades.trades
            ],
            "durable_fills": durable_fills,
        }
        proof["sha256"] = hashlib.sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        store.record_terminal_dispatch(manifest.manifest_id, proof=proof, now=now)
        terminal = True

    progress = assess_partial_recycle_manifest(
        manifest,
        ledger=ledger,
        governor=governor,
        store=store,
        now=now,
    )
    return {
        "manifest_id": manifest.manifest_id,
        "sell_executor_id": manifest.sell_executor_id,
        "applied_fills": applied,
        "replayed_fills": replayed,
        "remaining_base": str(progress.remaining_base),
        "realized_net_pnl_quote": str(progress.realized_pnl_quote),
        "completed": progress.completed,
        "terminal": terminal,
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
    expected_exit_cost_bps: Decimal | None = None,
    market_observed_at: float | None = None,
) -> dict[str, Any]:
    """Dispatch one exact partial-recycle SELL executor.

    Existing executors reconcile before new-dispatch price/breaker gates. Only
    a confirmed HTTP 404 establishes absence. A durable one-shot claim precedes
    create, so an ambiguous response or crash can never cause an automatic
    second create even if a later lookup returns 404.
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

    store = PartialRecycleStore(ledger.path)
    durable = store.load(manifest.manifest_id)
    if (
        durable is None
        or replace(durable, status=manifest.status, completed_at=manifest.completed_at)
        != manifest
    ):
        raise RuntimeError("partial recycle immutable durable manifest changed")
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

    def blocked(reason: str, state: str = "NEW_DISPATCH_BLOCKED") -> dict[str, Any]:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "dispatch_state": state,
            "reasons": [reason],
        }

    try:
        existing = await client.executors.get_executor(manifest.sell_executor_id)
    except ClientResponseError as exc:
        if exc.status != 404:
            return blocked(
                "recycle_executor_lookup_unknown",
                "LOOKUP_UNKNOWN_REQUIRES_RECONCILIATION",
            )
        existing = None
    except Exception:
        return blocked(
            "recycle_executor_lookup_unknown", "LOOKUP_UNKNOWN_REQUIRES_RECONCILIATION"
        )
    else:
        if (
            not isinstance(existing, dict)
            or existing.get("id") != manifest.sell_executor_id
        ):
            return blocked(
                "recycle_executor_lookup_ambiguous",
                "LOOKUP_UNKNOWN_REQUIRES_RECONCILIATION",
            )

    if existing is not None:
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
            "dispatch_state": "EXISTING_RECONCILED",
            "result": sync,
            "reasons": ["existing_partial_recycle_executor_reconciled"],
        }

    remaining = remaining_partial_recycle_base(
        manifest,
        ledger=ledger,
    )
    if remaining <= ZERO:
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
    if store.dispatch_claimed(manifest.manifest_id):
        return blocked(
            "recycle_dispatch_claim_requires_reconciliation",
            "CLAIMED_REQUIRES_RECONCILIATION",
        )
    if get_breaker_mode() == BreakerMode.HALTED:
        return blocked("breaker_halted_new_recycle_dispatch")
    guard = assess_partial_recycle_dispatch(
        manifest,
        ledger=ledger,
        governor=governor,
        trading_rule=trading_rule,
        current_price=current_price,
        expected_exit_cost_bps=expected_exit_cost_bps,
    )
    if not guard.allowed:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "dispatch_state": "NEW_DISPATCH_BLOCKED",
            "estimated_net_return_bps": str(guard.estimated_net_return_bps),
            "target_net_profit_bps": str(guard.target_net_profit_bps),
            "reasons": list(guard.reasons),
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

    guard = store.claim_dispatch(
        manifest,
        now=now,
        ledger=ledger,
        governor=governor,
        trading_rule=trading_rule,
        current_price=current_price,
        expected_exit_cost_bps=expected_exit_cost_bps,
        market_observed_at=market_observed_at,
    )
    if guard is None:
        return blocked(
            "recycle_dispatch_claim_requires_reconciliation",
            "CLAIMED_REQUIRES_RECONCILIATION",
        )
    if not guard.allowed:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "dispatch_state": "NEW_DISPATCH_BLOCKED",
            "estimated_net_return_bps": str(guard.estimated_net_return_bps),
            "target_net_profit_bps": str(guard.target_net_profit_bps),
            "reasons": list(guard.reasons),
        }
    # The final ownership/economic guard and durable claim above run in one
    # ledger write transaction. Do not add any external await before create.
    try:
        result = await client.executors.create_executor(
            executor_config={
                "id": manifest.sell_executor_id,
                "type": "order_executor",
                "connector_name": "kraken",
                "trading_pair": manifest.pair,
                "side": 2,
                "amount": str(guard.sell_base),
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
    except Exception:
        return blocked(
            "recycle_create_outcome_requires_reconciliation",
            "AMBIGUOUS_REQUIRES_RECONCILIATION",
        )

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
        "dispatch_state": "NEW_DISPATCH_CONFIRMED",
        "result": sync,
        "reasons": list(decision.reasons),
    }
