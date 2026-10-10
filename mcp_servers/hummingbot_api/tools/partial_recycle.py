from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal, localcontext
from typing import Any

from mcp_servers.hummingbot_api.tools.cash_accounting import (
    bind_sale_cash_authority,
    finalize_sale_cash_fill,
    prepare_sale_cash_fill,
    require_clear_cash_authority,
)
from mcp_servers.hummingbot_api.tools.executor_provenance import (
    fetch_executor_order_snapshot,
    fetch_executor_trade_snapshot,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.decimal_math import exact_decimal_difference, exact_decimal_sum
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
    protected_terminal_buy_source_is_passive,
    remaining_partial_recycle_base,
    validate_partial_recycle_manifest,
)
from spectra_core.runtime import get_breaker_mode

ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


async def _existing_sell_executor(client: Any, executor_id: str) -> Any | None:
    """Only an explicit HTTP 404 establishes absence before first dispatch."""
    try:
        result = await client.executors.get_executor(executor_id)
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(exc, "status", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", getattr(response, "status", None))
        if isinstance(status, int) and not isinstance(status, bool) and status == 404:
            return None
        raise RuntimeError("partial recycle executor lookup is ambiguous") from exc
    if not isinstance(result, dict) or not result:
        raise RuntimeError("partial recycle executor lookup is ambiguous")
    observation = executor_accounting_observation_from_raw(
        result, expected_executor_id=executor_id
    )
    if observation.executor_id != executor_id:
        raise RuntimeError("partial recycle executor identity changed")
    return result


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
    *,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
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
        passive = (
            status == "TERMINATED"
            and close_type == "POSITION_HOLD"
            and observation.executor_type in {"dca_executor", "grid_executor"}
        )
        if observation.executor_type == "order_executor" and status == "TERMINATED":
            orders = await fetch_executor_order_snapshot(
                client, executor_id=owner.executor_id
            )
            trades = await fetch_executor_trade_snapshot(
                client, executor_id=owner.executor_id
            )
            passive = protected_terminal_buy_source_is_passive(
                proposal_id=owner.proposal_id,
                executor_id=owner.executor_id,
                position_id=manifest.position_id,
                pair=manifest.pair,
                governor=governor,
                ledger=ledger,
                observation=observation,
                order_snapshot=orders,
                trade_snapshot=trades,
                executor_config=raw.get("config"),
            )
        if not passive:
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
        base_sold = exact_decimal_sum((base_sold, fill.fee_paid))
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
    reconciliation_only: bool = False,
) -> dict[str, Any]:
    """Reconcile exact SELL evidence from the deterministic recycle executor.

    A source policy HOLD can permit known SELL truth to reach its journals;
    reconciliation_only preserves its pending cash and lifecycle until review.
    """
    if not isinstance(reconciliation_only, bool):
        raise ValueError("reconciliation_only must be bool")

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
    try:
        raw = await client.executors.get_executor(manifest.sell_executor_id)
        observation = executor_accounting_observation_from_raw(
            raw, expected_executor_id=manifest.sell_executor_id
        )
    except Exception:
        raw, observation = None, None

    conflicts: list[str] = []
    if (
        manifest.protected_limit_price is not None
        and isinstance(raw, dict)
        and isinstance(raw.get("config"), dict)
    ):
        config = raw["config"]
        try:
            valid_config = (
                config.get("execution_strategy") == "LIMIT"
                and Decimal(str(config.get("price"))) >= manifest.protected_limit_price
                and config.get("side") == 2
                and not isinstance(config.get("side"), bool)
                and config.get("trading_pair") == manifest.pair
                and config.get("connector_name") == connector_name
                and ZERO < Decimal(str(config.get("amount"))) <= manifest.target_base
            )
        except Exception:
            valid_config = False
        if not valid_config:
            conflicts.append("unprotected_executor_config")
    outstanding = ZERO

    for order in orders.orders:
        if order.executor_id != manifest.sell_executor_id:
            raise RuntimeError("partial recycle order crosses executor scope")
        if order.side != "SELL":
            raise RuntimeError("partial recycle executor emitted a non-SELL order")
        if order.amount > manifest.target_base:
            raise RuntimeError("partial recycle order exceeds immutable manifest base")
        if manifest.protected_limit_price is not None:
            if (
                order.order_type.strip().upper() != "LIMIT"
                or order.price is None
                or order.price < manifest.protected_limit_price
            ):
                conflicts.append(f"unprotected_order:{order.order_id}")
        if order.status.strip().upper() not in {
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "FAILED",
        }:
            outstanding = exact_decimal_sum(
                (
                    outstanding,
                    max(
                        ZERO,
                        exact_decimal_difference(order.amount, order.filled_amount),
                    ),
                )
            )
    if outstanding > remaining_partial_recycle_base(manifest, ledger=ledger):
        conflicts.append("outstanding_orders_exceed_manifest_inventory")

    # Persist known order violations before any fill/accounting mutation.
    for conflict in conflicts:
        store.record_protection_conflict(
            manifest_id=manifest.manifest_id, conflict=conflict, now=now
        )

    cash_events = {}
    for fill in trades.trades:
        order = known_orders.get(fill.order_id)
        if order is None or fill.side != "SELL" or order.side != "SELL":
            raise RuntimeError("partial cash receipt lacks exact SELL evidence")
        if (fill.pair, fill.account_name, fill.connector_name) != (
            manifest.pair,
            account_name,
            connector_name,
        ):
            raise RuntimeError("partial cash receipt crosses immutable execution scope")
        cash_events[fill.trade_id] = prepare_sale_cash_fill(
            governor=governor,
            manifest=manifest,
            fill=fill,
            sale_purpose="RECOVERY_PARTIAL",
            geometry=_sell_geometry(fill),
        )
    cash_results = []
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
        if (
            manifest.protected_limit_price is not None
            and fill.price < manifest.protected_limit_price
        ):
            store.record_protection_conflict(
                manifest_id=manifest.manifest_id,
                conflict=f"profit_floor_breached:{fill.trade_id}",
                now=now,
            )
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

        authority = governor.cash_authority_snapshot()
        if authority is not None and fill.occurred_at > authority.activated_at:
            allocation = dict(ledger.exit_basis_by_lot(result.event_id))
            if (
                exact_decimal_sum(allocation.values())
                != result.allocated_cost_basis_quote
            ):
                raise RuntimeError(
                    "partial cash release lacks exact lot basis allocations"
                )
            release_rows = tuple(
                (
                    owner.proposal_id,
                    exact_decimal_sum(
                        allocation.get(lot_id, ZERO) for lot_id in owner.lot_ids
                    ),
                )
                for owner in manifest.owners
            )
        else:
            release_rows = _release_basis_by_owner(
                manifest, result.allocated_cost_basis_quote
            )
        for proposal_id, released_basis in release_rows:
            if released_basis <= ZERO:
                continue
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
        cash_results.append((fill, result, cash_events[fill.trade_id]))
        # Confirmed money and inventory are recorded even if the venue violates
        # protection. Never hide a real loss to preserve a successful-exit claim.
        if manifest.protected_limit_price is not None:
            with localcontext() as context:
                context.prec = 80
                invested = (
                    result.allocated_cost_basis_quote
                    + result.allocated_entry_fees_quote
                )
                required_pnl = invested * manifest.target_net_profit_bps / TEN_THOUSAND
                if (
                    fill.price < manifest.protected_limit_price
                    or result.realized_pnl_quote < required_pnl
                ):
                    conflicts.append(f"profit_floor_breached:{fill.trade_id}")
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

    if outstanding > remaining_partial_recycle_base(manifest, ledger=ledger):
        conflicts.append("outstanding_orders_exceed_manifest_inventory")
    for conflict in conflicts:
        store.record_protection_conflict(
            manifest_id=manifest.manifest_id, conflict=conflict, now=now
        )
    if store.protection_conflicts(manifest.manifest_id):
        raise RuntimeError(
            "partial recycle profit protection conflict requires resolution"
        )

    if reconciliation_only:
        raise RuntimeError(
            "partial recycle reconciliation-only pending source policy HOLD"
        )

    if any(
        governor.get_protected_buy_conflicts(owner.proposal_id)
        for owner in manifest.owners
    ):
        raise RuntimeError("protected BUY source conflict requires reconciliation")

    progress = assess_partial_recycle_manifest(
        manifest,
        ledger=ledger,
        governor=governor,
        store=store,
        now=now,
    )
    for fill, result, cash_event_id in cash_results:
        finalize_sale_cash_fill(
            governor=governor,
            ledger=ledger,
            manifest=manifest,
            fill=fill,
            result=result,
            cash_event_id=cash_event_id,
            sale_purpose="RECOVERY_PARTIAL",
        )
    require_clear_cash_authority(governor)

    terminal = False
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
        == exact_decimal_sum(
            (
                trade.amount_base
                for trade in trades.trades
                if trade.order_id == order.order_id
            ),
        )
        for order in orders.orders
    )
    if (
        observation is not None
        and observation.status.strip().upper() == "TERMINATED"
        and all_terminal
        and exact_order_match
        and observation.filled_amount_quote
        == exact_decimal_sum(trade.quote_amount for trade in trades.trades)
    ):
        proof = {
            "schema": "spectra-partial-terminal-v1",
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "executor_id": manifest.sell_executor_id,
            "executor_status": "TERMINATED",
            "executor_filled_quote": str(observation.filled_amount_quote),
            "protected_limit_price": (
                None
                if manifest.protected_limit_price is None
                else str(manifest.protected_limit_price)
            ),
            "cash_proceeds_route": manifest.cash_proceeds_route,
            "all_orders_terminal": True,
            "exact_order_fill_match": True,
            "orders": [
                {
                    "order_id": order.order_id,
                    "side": order.side,
                    "order_type": order.order_type.strip().upper(),
                    "limit_price": None if order.price is None else str(order.price),
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
                    "price": str(trade.price),
                    "quote_amount": str(trade.quote_amount),
                    "base_fee": str(
                        exact_decimal_difference(
                            _sell_geometry(trade)[0], trade.amount_base
                        )
                    ),
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
        existing = await _existing_sell_executor(client, manifest.sell_executor_id)
    except Exception:
        return blocked(
            "recycle_executor_lookup_unknown", "LOOKUP_UNKNOWN_REQUIRES_RECONCILIATION"
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
    if manifest.protected_limit_price is None:
        return blocked("legacy_partial_recycle_requires_protected_replan")
    if store.protection_conflicts(manifest.manifest_id):
        raise RuntimeError(
            "partial recycle profit protection conflict requires resolution"
        )
    require_clear_cash_authority(governor)
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
        governor=governor,
        ledger=ledger,
    )
    if not passive:
        return {
            "action": "PARTIAL_RECYCLE",
            "executed": False,
            "manifest_id": manifest.manifest_id,
            "sell_executor_id": manifest.sell_executor_id,
            "reasons": list(passive_reasons),
        }

    bind_sale_cash_authority(
        governor=governor,
        manifest=manifest,
        account_name=owner_account,
        connector_name=owner_connector,
        sale_purpose="RECOVERY_PARTIAL",
        for_dispatch=True,
    )
    require_clear_cash_authority(governor)
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
                "execution_strategy": "LIMIT",
                "price": str(manifest.protected_limit_price),
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
