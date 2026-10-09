from __future__ import annotations

import sqlite3
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
from mcp_servers.hummingbot_api.tools.partial_recycle import _sell_geometry
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.decimal_math import exact_decimal_difference, exact_decimal_sum
from spectra_core.economic_exit import (
    EconomicExitManifestStore,
    EconomicExitProgressState,
    EconomicPositionExitManifest,
    assess_position_exit_manifest,
    create_or_load_position_exit_manifest,
    validate_manifest_against_ledger,
)
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_accounting import (
    assess_recovery_exposure,
    executor_accounting_observation_from_raw,
)
from spectra_core.executor_provenance_bridge import synchronize_executor_evidence
from spectra_core.fill_reconciliation import LiveFillLedgerReconciler
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import BreakerMode, IntentKind
from spectra_core.partial_recycle import (
    PartialRecycleStore,
    protected_partial_recycle_price,
)
from spectra_core.runtime import get_breaker_mode
from spectra_core.vcap_profit_exit import (
    VCAPProfitExitAction,
    VCAPProfitExitDecision,
)

ZERO = Decimal("0")
BPS = Decimal("10000")
TERMINAL_ORDERS = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}


async def reconcile_economic_exit_owner_accounting(
    client: Any,
    *,
    manifest,
    governor: CapitalGovernor,
) -> dict[str, Any]:
    """Refresh terminal executor accounting for every manifest owner.

    Exact SELL fills remain authoritative for economic inventory/P&L. This
    companion observation only reconciles Governor executor accounting so
    RECOVERY budget is not reported as reusable until its executor is flat.
    """

    rows: list[dict[str, Any]] = []
    all_recovery_budget_released = True

    for owner in manifest.owners:
        row: dict[str, Any] = {
            "proposal_id": owner.proposal_id,
            "executor_id": owner.executor_id,
            "intent": owner.intent.value,
        }
        try:
            raw = await client.executors.get_executor(owner.executor_id)
            observation = executor_accounting_observation_from_raw(
                raw,
                expected_executor_id=owner.executor_id,
            )
            delta = governor.reconcile_executor_accounting(observation)
            row.update(
                {
                    "status": observation.status,
                    "close_type": observation.close_type,
                    "accounting_filled_delta_quote": str(delta.filled_delta_quote),
                    "accounting_fee_delta_quote": str(delta.fee_delta_quote),
                }
            )
            if owner.intent == IntentKind.RECOVERY:
                provenance = governor.get_execution_provenance(owner.proposal_id)
                if provenance is None:
                    raise RuntimeError("recovery owner provenance disappeared")
                exposure = assess_recovery_exposure(
                    observation,
                    provenance,
                )
                if manifest.sell_executor_id is not None:
                    # Held buying executors cannot observe the separate sell.
                    # Their current lots plus the durable sold-basis journal
                    # govern release; never require a false flat observation.
                    released = False
                else:
                    released = exposure.exposure_quote == 0
                all_recovery_budget_released = all_recovery_budget_released and released
                row["remaining_recovery_exposure_quote"] = str(exposure.exposure_quote)
                row["recovery_budget_released"] = released
        except Exception as exc:
            if owner.intent == IntentKind.RECOVERY:
                all_recovery_budget_released = False
            row["error"] = str(exc)
        rows.append(row)

    return {
        "all_recovery_budget_released": all_recovery_budget_released,
        "owners": rows,
    }


def _scope(manifest, governor):
    scope = set()
    for owner in manifest.owners:
        provenance = governor.get_execution_provenance(owner.proposal_id)
        if (
            provenance is None
            or governor.get_execution_receipt(owner.proposal_id) != owner.executor_id
            or provenance.executor_id != owner.executor_id
            or provenance.intent != owner.intent
            or provenance.pair.strip().upper() != manifest.pair
            or provenance.account_name != owner.account_name
            or (provenance.connector_name or "").lower() != owner.connector_name
        ):
            raise RuntimeError("economic exit owner provenance changed")
        scope.add((owner.account_name, owner.connector_name))
    if len(scope) != 1:
        raise RuntimeError("economic exit owners cross account/connector scope")
    return next(iter(scope))


async def _existing_sell_executor(client, executor_id):
    try:
        raw = await client.executors.get_executor(executor_id)
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(exc, "status", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", getattr(response, "status", None))
        if isinstance(status, int) and not isinstance(status, bool) and status == 404:
            return None
        raise RuntimeError("economic exit executor lookup is ambiguous") from exc
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError("economic exit executor lookup is ambiguous")
    observed = executor_accounting_observation_from_raw(
        raw, expected_executor_id=executor_id
    )
    if observed.executor_id != executor_id:
        raise RuntimeError("economic exit executor identity changed")
    return raw


def _reject_cross_journal_order(ledger, order_id, manifest_id):
    with sqlite3.connect(ledger.path) as conn:
        for table in (
            "economic_fill_bindings",
            "partial_recycle_fills",
            "economic_exit_fills",
        ):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone():
                predicate = (
                    " AND manifest_id != ?" if table == "economic_exit_fills" else ""
                )
                params = (order_id, manifest_id) if predicate else (order_id,)
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE order_id = ?" + predicate, params
                ).fetchone():
                    raise RuntimeError(
                        "economic exit order is already attributed to another executor"
                    )


def _reject_cross_journal_trade(ledger, trade_id):
    # A synthetic sell cannot also be attributed to a source/partial executor.
    with sqlite3.connect(ledger.path) as conn:
        for table in ("economic_trade_fill_reconciliation", "partial_recycle_fills"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone():
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE trade_id = ?", (trade_id,)
                ).fetchone():
                    raise RuntimeError(
                        "economic exit trade is already attributed to another executor"
                    )


def _release_recovery_basis(manifest, result, governor, ledger, trade_id):
    allocation = dict(ledger.exit_basis_by_lot(result.event_id))
    if exact_decimal_sum(allocation.values()) != result.allocated_cost_basis_quote:
        raise RuntimeError("economic exit lacks exact durable lot basis allocations")
    remaining = result.allocated_cost_basis_quote
    for owner in manifest.owners:
        piece = exact_decimal_sum(
            (allocation.get(lot_id, ZERO) for lot_id in owner.lot_ids)
        )
        remaining = exact_decimal_difference(remaining, piece)
        if owner.intent == IntentKind.RECOVERY and piece > ZERO:
            governor.record_recovery_recycle_release(
                release_id=f"whole-exit:{manifest.manifest_id}:{trade_id}:{owner.proposal_id}",
                proposal_id=owner.proposal_id,
                amount_quote=piece,
            )
    if remaining != ZERO:
        raise RuntimeError("economic exit basis release did not close exactly")


async def synchronize_economic_position_exit(
    client, *, manifest, governor, ledger, store, now
):
    """Apply only confirmed exact synthetic SELL evidence, including on restart."""
    validate_manifest_against_ledger(manifest, ledger=ledger)
    account, connector = _scope(manifest, governor)
    if manifest.sell_executor_id is None:
        progress = assess_position_exit_manifest(
            manifest, ledger=ledger, store=store, now=now
        )
        return {
            "completed": progress.state == EconomicExitProgressState.COMPLETED,
            "remaining_base": str(progress.remaining_active_base),
        }
    raw = await _existing_sell_executor(client, manifest.sell_executor_id)
    if raw is None:
        raise RuntimeError("economic exit sell executor evidence is unavailable")
    sell_observation = executor_accounting_observation_from_raw(
        raw, expected_executor_id=manifest.sell_executor_id
    )
    trades = await fetch_executor_trade_snapshot(
        client, executor_id=manifest.sell_executor_id
    )
    orders = await fetch_executor_order_snapshot(
        client, executor_id=manifest.sell_executor_id
    )
    if (
        trades.executor_id != manifest.sell_executor_id
        or orders.executor_id != manifest.sell_executor_id
    ):
        raise RuntimeError("economic exit evidence executor identity changed")
    known_orders = {order.order_id: order for order in orders.orders}
    outstanding = ZERO
    conflicts = []
    for order in orders.orders:
        _reject_cross_journal_order(ledger, order.order_id, manifest.manifest_id)
        if order.side != "SELL" or order.amount > manifest.target_active_base:
            raise RuntimeError("economic exit order exceeds immutable SELL scope")
        if (
            order.order_type.strip().upper() != "LIMIT"
            or order.price is None
            or order.price < manifest.protected_limit_price
        ):
            conflicts.append(f"unprotected_order:{order.order_id}")
        if order.status.strip().upper() not in TERMINAL_ORDERS:
            outstanding = exact_decimal_sum(
                (
                    outstanding,
                    max(
                        ZERO,
                        exact_decimal_difference(order.amount, order.filled_amount),
                    ),
                )
            )
    if outstanding > ledger.snapshot(manifest.position_id).active_base:
        conflicts.append("outstanding_orders_exceed_manifest_inventory")
    for conflict in conflicts:
        store.record_protection_conflict(
            manifest_id=manifest.manifest_id, conflict=conflict, now=now
        )
    if (
        sell_observation.status.strip().upper() == "TERMINATED"
        and orders.orders
        and all(
            order.status.strip().upper() in TERMINAL_ORDERS for order in orders.orders
        )
    ):
        store.record_finality(
            manifest_id=manifest.manifest_id,
            executor_id=manifest.sell_executor_id,
            now=now,
        )
    cash_events = {}
    for fill in trades.trades:
        order = known_orders.get(fill.order_id)
        if order is None or fill.side != "SELL" or order.side != "SELL":
            raise RuntimeError("whole cash receipt lacks exact SELL evidence")
        if (fill.pair, fill.account_name, fill.connector_name) != (
            manifest.pair,
            account,
            connector,
        ):
            raise RuntimeError("whole cash receipt crosses immutable execution scope")
        _reject_cross_journal_trade(ledger, fill.trade_id)
        cash_events[fill.trade_id] = prepare_sale_cash_fill(
            governor=governor,
            manifest=manifest,
            fill=fill,
            sale_purpose="WHOLE_EXIT",
            geometry=_sell_geometry(fill),
        )
    cash_results = []
    applied = replayed = 0
    for fill in trades.trades:
        order = known_orders.get(fill.order_id)
        if order is None or fill.side != "SELL" or order.side != "SELL":
            raise RuntimeError("economic exit fill lacks exact SELL order evidence")
        if (
            fill.pair != manifest.pair
            or fill.account_name != account
            or fill.connector_name != connector
        ):
            raise RuntimeError("economic exit fill crosses immutable execution scope")
        _reject_cross_journal_trade(ledger, fill.trade_id)
        if fill.price < manifest.protected_limit_price:
            store.record_protection_conflict(
                manifest_id=manifest.manifest_id,
                conflict=f"profit_floor_breached:{fill.trade_id}",
                now=now,
            )
        base_sold, proceeds, exit_fee = _sell_geometry(fill)
        result = ledger.record_partial_exit(
            event_id=f"whole-exit:{manifest.manifest_id}:{fill.trade_id}",
            position_id=manifest.position_id,
            base_sold=base_sold,
            gross_proceeds_quote=proceeds,
            exit_fee_quote=exit_fee,
            occurred_at=fill.occurred_at,
            eligible_lot_ids=manifest.lot_ids,
        )
        _release_recovery_basis(manifest, result, governor, ledger, fill.trade_id)
        recorded = store.fill_recorded(fill.trade_id)
        store.record_fill(
            trade_id=fill.trade_id,
            manifest_id=manifest.manifest_id,
            order_id=fill.order_id,
            base_sold=result.base_sold,
            gross_proceeds_quote=result.gross_proceeds_quote,
            exit_fee_quote=result.exit_fee_quote,
            allocated_basis_quote=result.allocated_cost_basis_quote,
            allocated_entry_fees_quote=result.allocated_entry_fees_quote,
            realized_pnl_quote=result.realized_pnl_quote,
            price=fill.price,
            occurred_at=fill.occurred_at,
        )
        cash_results.append((fill, result, cash_events[fill.trade_id]))
        with localcontext() as context:
            context.prec = 80
            invested = (
                result.allocated_cost_basis_quote + result.allocated_entry_fees_quote
            )
            if (
                result.realized_pnl_quote
                < invested * manifest.target_net_profit_bps / BPS
            ):
                store.record_protection_conflict(
                    manifest_id=manifest.manifest_id,
                    conflict=f"profit_floor_breached:{fill.trade_id}",
                    now=now,
                )
        replayed += int(recorded)
        applied += int(not recorded)
    if outstanding > ledger.snapshot(manifest.position_id).active_base:
        store.record_protection_conflict(
            manifest_id=manifest.manifest_id,
            conflict="outstanding_orders_exceed_manifest_inventory",
            now=now,
        )
    progress = assess_position_exit_manifest(
        manifest, ledger=ledger, store=store, now=now
    )
    for fill, result, cash_event_id in cash_results:
        finalize_sale_cash_fill(
            governor=governor,
            ledger=ledger,
            manifest=manifest,
            fill=fill,
            result=result,
            cash_event_id=cash_event_id,
            sale_purpose="WHOLE_EXIT",
        )
    require_clear_cash_authority(governor)
    return {
        "manifest_id": manifest.manifest_id,
        "sell_executor_id": manifest.sell_executor_id,
        "applied_fills": applied,
        "replayed_fills": replayed,
        "completed": progress.state == EconomicExitProgressState.COMPLETED,
        "remaining_base": str(progress.remaining_active_base),
        "realized_net_pnl_quote": str(progress.realized_pnl_quote),
    }


def _result(
    manifest,
    ledger,
    store,
    now,
    *,
    requested=False,
    executed=False,
    owners=(),
    reasons=(),
    requires_rule=False,
):
    progress = assess_position_exit_manifest(
        manifest, ledger=ledger, store=store, now=now
    )
    flat = progress.state == EconomicExitProgressState.COMPLETED
    return {
        "action": "EXIT",
        "exit_requested": requested or flat,
        "executed": executed,
        "manifest_id": manifest.manifest_id,
        "sell_executor_id": manifest.sell_executor_id,
        "position_id": manifest.position_id,
        "pair": manifest.pair,
        "target_active_base": str(manifest.target_active_base),
        "remaining_active_base": str(progress.remaining_active_base),
        "realized_net_pnl_quote": str(progress.realized_pnl_quote),
        "strategic_retained_base": str(progress.strategic_retained_base),
        "retained_inventory_in_manifest": False,
        "all_recovery_budget_released": flat,
        "economic_exit_state": progress.state.value,
        "capital_recycling_state": "RELEASED" if flat else "PENDING_RECONCILIATION",
        "owners": list(owners),
        "requires_fresh_trading_rule": requires_rule,
        "reasons": [*reasons, *progress.reasons],
    }


async def dispatch_protected_economic_exit(
    client,
    *,
    manifest,
    governor,
    ledger,
    now,
    trading_rule=None,
    account_name="master_account",
    controller_id="spectra",
):
    """Finish an unclaimed first dispatch or reconcile a previously claimed sale."""
    store = EconomicExitManifestStore(ledger.path)
    validate_manifest_against_ledger(manifest, ledger=ledger)
    account, connector = _scope(manifest, governor)
    if account != account_name or connector != "kraken":
        raise ValueError("economic exit scope must match the configured Kraken account")
    if manifest.sell_executor_id is None:
        result = _result(
            manifest,
            ledger,
            store,
            now,
            reasons=("legacy_economic_exit_requires_protected_replan",),
        )
        accounting = await reconcile_economic_exit_owner_accounting(
            client, manifest=manifest, governor=governor
        )
        result["all_recovery_budget_released"] = bool(
            accounting["all_recovery_budget_released"]
        )
        if not result["all_recovery_budget_released"]:
            result["capital_recycling_state"] = "PENDING_RECONCILIATION"
        return result
    existing = await _existing_sell_executor(client, manifest.sell_executor_id)
    if existing is not None:
        await synchronize_economic_position_exit(
            client,
            manifest=manifest,
            governor=governor,
            ledger=ledger,
            store=store,
            now=now,
        )
        return _result(
            manifest,
            ledger,
            store,
            now,
            requested=True,
            executed=True,
            reasons=("existing_economic_exit_executor_reconciled",),
        )
    if store.dispatch_claimed(manifest.manifest_id):
        raise RuntimeError(
            "economic exit dispatch outcome is ambiguous; reconciliation required"
        )
    if store.protection_conflicts(manifest.manifest_id):
        raise RuntimeError(
            "economic exit profit protection conflict requires resolution"
        )
    if PartialRecycleStore(ledger.path).open_for_position(manifest.position_id):
        raise RuntimeError("open partial recycle manifest blocks economic exit")
    if get_breaker_mode() == BreakerMode.HALTED:
        return _result(
            manifest, ledger, store, now, reasons=("breaker_halted_new_exit_dispatch",)
        )
    if trading_rule is None:
        return _result(
            manifest,
            ledger,
            store,
            now,
            requires_rule=True,
            reasons=("first_submission_requires_fresh_trading_rule",),
        )
    if trading_rule.trading_pair.strip().upper() != manifest.pair:
        raise ValueError("economic exit trading rule pair changed")
    floor = protected_partial_recycle_price(
        target_base=manifest.target_active_base,
        target_cost_basis_quote=manifest.target_cost_basis_quote,
        target_entry_fees_quote=manifest.target_entry_fees_quote,
        target_net_profit_bps=manifest.target_net_profit_bps,
        expected_exit_cost_bps=manifest.expected_exit_cost_bps,
        trading_rule=trading_rule,
    )
    if (
        floor > manifest.protected_limit_price
        or manifest.protected_limit_price % trading_rule.min_price_increment != ZERO
    ):
        raise RuntimeError("economic exit protection needs replan for changed rules")
    source_evidence = []
    owner_rows = []
    for owner in manifest.owners:
        raw = await client.executors.get_executor(owner.executor_id)
        obs = executor_accounting_observation_from_raw(
            raw, expected_executor_id=owner.executor_id
        )
        if (
            obs.status.strip().upper() != "TERMINATED"
            or (obs.close_type or "").strip().upper() != "POSITION_HOLD"
        ):
            await client.executors.stop_executor(
                executor_id=owner.executor_id, keep_position=True
            )
            raw = await client.executors.get_executor(owner.executor_id)
            obs = executor_accounting_observation_from_raw(
                raw, expected_executor_id=owner.executor_id
            )
        trades = await fetch_executor_trade_snapshot(
            client, executor_id=owner.executor_id
        )
        orders = await fetch_executor_order_snapshot(
            client, executor_id=owner.executor_id
        )
        passive = (
            obs.status.strip().upper() == "TERMINATED"
            and (obs.close_type or "").strip().upper() == "POSITION_HOLD"
            and all(
                order.status.strip().upper() in TERMINAL_ORDERS
                for order in orders.orders
            )
        )
        covered = all(
            exact_decimal_sum(
                (
                    fill.amount_base
                    for fill in trades.trades
                    if fill.order_id == order.order_id
                )
            )
            == order.filled_amount
            for order in orders.orders
        )
        if not covered:
            raise RuntimeError(
                "economic exit source order fills lack exact confirmed trade coverage"
            )
        owner_rows.append(
            {
                "proposal_id": owner.proposal_id,
                "executor_id": owner.executor_id,
                "intent": owner.intent.value,
                "active_lot_ids": list(owner.lot_ids),
                "state": "PASSIVE" if passive else "AWAITING_SOURCE_FINALITY",
            }
        )
        source_evidence.append((owner, obs, trades, orders, passive))
    if not all(row[-1] for row in source_evidence):
        return _result(
            manifest,
            ledger,
            store,
            now,
            requested=True,
            owners=owner_rows,
            reasons=("source_executor_not_passive_or_has_live_orders",),
        )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    for owner, obs, trades, orders, _ in source_evidence:
        governor.reconcile_executor_accounting(obs)
        report = synchronize_executor_evidence(
            reconciler=reconciler,
            position_id=manifest.position_id,
            proposal_id=owner.proposal_id,
            order_snapshot=orders,
            trade_snapshot=trades,
            now=now,
        )
        if not report.fully_reconciled:
            raise RuntimeError("economic exit source evidence is not fully reconciled")
    validate_manifest_against_ledger(manifest, ledger=ledger)
    if ledger.snapshot(manifest.position_id).active_base != manifest.target_active_base:
        raise RuntimeError("economic exit inventory changed before first submission")
    bind_sale_cash_authority(
        governor=governor,
        manifest=manifest,
        account_name=account_name,
        connector_name="kraken",
        sale_purpose="WHOLE_EXIT",
        for_dispatch=True,
    )
    require_clear_cash_authority(governor)
    if not store.claim_dispatch(manifest_id=manifest.manifest_id, now=now):
        raise RuntimeError(
            "economic exit dispatch already claimed; reconciliation required"
        )
    ack = await client.executors.create_executor(
        executor_config={
            "id": manifest.sell_executor_id,
            "type": "order_executor",
            "connector_name": "kraken",
            "trading_pair": manifest.pair,
            "side": 2,
            "amount": str(manifest.target_active_base),
            "execution_strategy": "LIMIT",
            "price": str(manifest.protected_limit_price),
            "leverage": 1,
            "level_id": "whole-exit-" + manifest.manifest_id.rsplit(":", 1)[-1][:12],
        },
        account_name=account_name,
        controller_id=controller_id,
    )
    if (
        not isinstance(ack, dict)
        or (ack.get("executor_id") or ack.get("id")) != manifest.sell_executor_id
    ):
        raise RuntimeError("economic exit executor identity is ambiguous")
    await synchronize_economic_position_exit(
        client,
        manifest=manifest,
        governor=governor,
        ledger=ledger,
        store=store,
        now=now,
    )
    return _result(
        manifest, ledger, store, now, requested=True, executed=True, owners=owner_rows
    )


async def execute_economic_position_exit(
    client,
    *,
    decision,
    governor,
    ledger,
    position_id,
    trading_rule,
    now,
    account_name="master_account",
    controller_id="spectra",
):
    if decision.action != VCAPProfitExitAction.EXIT:
        return {
            "action": decision.action.value,
            "exit_requested": False,
            "reasons": list(decision.reasons),
        }
    if decision.pair != trading_rule.trading_pair.strip().upper():
        raise ValueError("profit exit pair does not match Kraken trading rule")
    store = EconomicExitManifestStore(ledger.path)
    existing = store.open_for_position(position_id) or store.latest_for_position(
        position_id
    )
    if existing is not None and (
        existing.status.value == "OPEN"
        or ledger.snapshot(position_id).active_base == ZERO
    ):
        manifest = existing
    elif (
        decision.target_net_profit_bps is None
        or decision.expected_exit_cost_bps is None
    ):
        return {
            "action": "EXIT",
            "exit_requested": False,
            "executed": False,
            "reasons": ["economic_exit_requires_explicit_profit_contract"],
        }
    else:
        manifest = create_or_load_position_exit_manifest(
            ledger=ledger,
            governor=governor,
            position_id=position_id,
            trading_rule=trading_rule,
            current_price=decision.current_price,
            now=now,
            store=store,
            target_net_profit_bps=decision.target_net_profit_bps,
            expected_exit_cost_bps=decision.expected_exit_cost_bps,
            protected_floor_price=decision.protected_floor_price,
        )
    return await dispatch_protected_economic_exit(
        client,
        manifest=manifest,
        governor=governor,
        ledger=ledger,
        trading_rule=trading_rule,
        now=now,
        account_name=account_name,
        controller_id=controller_id,
    )
