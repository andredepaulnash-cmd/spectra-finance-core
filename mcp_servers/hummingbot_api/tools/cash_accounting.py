from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal

from spectra_core.decimal_math import exact_decimal_sum


def bind_sale_cash_authority(
    *,
    governor,
    manifest,
    account_name,
    connector_name,
    sale_purpose,
    for_dispatch=False,
):
    """Preserve sale purpose before dispatch; never infer a legacy cash route."""
    if governor.cash_authority_snapshot() is None:
        return False
    route = "PARK" if sale_purpose == "RECOVERY_PARTIAL" else "PRODUCTIVE"
    if manifest.cash_proceeds_route != route:
        governor.record_cash_conflict(
            manifest.manifest_id, "sale manifest lacks an exact durable cash route"
        )
        if for_dispatch:
            raise RuntimeError("sale cash route requires an explicit migration")
        return False
    bound = governor.bind_cash_sale(
        manifest_id=manifest.manifest_id,
        sell_executor_id=manifest.sell_executor_id,
        pair=manifest.pair,
        sale_purpose=sale_purpose,
        account_name=account_name,
        connector_name=connector_name,
        quote_asset=manifest.pair.rsplit("-", 1)[-1],
    )
    if not bound and for_dispatch:
        raise RuntimeError("sale cash scope or purpose conflicts with authority")
    return bound


def prepare_sale_cash_fill(*, governor, manifest, fill, sale_purpose, geometry):
    """Hold spending before the first economic inventory mutation."""
    authority = governor.cash_authority_snapshot()
    if authority is None or fill.occurred_at <= authority.activated_at:
        return None
    if not bind_sale_cash_authority(
        governor=governor,
        manifest=manifest,
        account_name=fill.account_name,
        connector_name=fill.connector_name,
        sale_purpose=sale_purpose,
    ):
        return None
    base, gross, fee = geometry
    event_id = f"cash-sale:{manifest.manifest_id}:{fill.trade_id}"
    accepted = governor.begin_cash_fill(
        event_id=event_id,
        manifest_id=manifest.manifest_id,
        trade_id=fill.trade_id,
        order_id=fill.order_id,
        executor_id=manifest.sell_executor_id,
        sale_purpose=sale_purpose,
        pair=fill.pair,
        account_name=fill.account_name,
        connector_name=fill.connector_name,
        gross_proceeds_quote=gross,
        exit_fee_quote=fee,
        base_sold=base,
        price=fill.price,
        fee_currency=fill.fee_currency,
        fee_paid=fill.fee_paid,
        occurred_at=fill.occurred_at,
    )
    return event_id if accepted else None


def finalize_sale_cash_fill(
    *, governor, ledger, manifest, fill, result, cash_event_id, sale_purpose
):
    """Credit only after exact inventory, fill and basis journals agree."""
    if cash_event_id is None:
        return
    table = (
        "partial_recycle_fills"
        if sale_purpose == "RECOVERY_PARTIAL"
        else "economic_exit_fills"
    )
    with sqlite3.connect(ledger.path) as conn:
        conn.row_factory = sqlite3.Row
        event = conn.execute(
            "SELECT * FROM economic_position_events WHERE event_id = ?",
            (result.event_id,),
        ).fetchone()
        journal = conn.execute(
            f"SELECT * FROM {table} WHERE trade_id = ?", (fill.trade_id,)
        ).fetchone()
        allocations = conn.execute(
            "SELECT * FROM economic_exit_lot_allocations WHERE event_id = ? ORDER BY lot_id",
            (result.event_id,),
        ).fetchall()
    if event is None or journal is None or not allocations:
        raise RuntimeError("cash sale lacks durable inventory and fill journals")
    expected = {
        "base_sold": result.base_sold,
        "gross_proceeds_quote": result.gross_proceeds_quote,
        "exit_fee_quote": result.exit_fee_quote,
        "realized_pnl_quote": result.realized_pnl_quote,
    }
    if (
        journal["manifest_id"] != manifest.manifest_id
        or journal["order_id"] != fill.order_id
        or event["position_id"] != manifest.position_id
        or Decimal(event["base_amount"]) != result.base_sold
        or Decimal(event["quote_amount"]) != result.gross_proceeds_quote
        or Decimal(event["fee_quote"]) != result.exit_fee_quote
        or Decimal(event["allocated_cost_basis_quote"])
        != result.allocated_cost_basis_quote
        or Decimal(event["allocated_entry_fees_quote"])
        != result.allocated_entry_fees_quote
        or Decimal(event["realized_pnl_quote"]) != result.realized_pnl_quote
        or any(Decimal(journal[key]) != value for key, value in expected.items())
        or exact_decimal_sum(Decimal(row["base_sold"]) for row in allocations)
        != result.base_sold
        or exact_decimal_sum(Decimal(row["cost_basis_quote"]) for row in allocations)
        != result.allocated_cost_basis_quote
        or exact_decimal_sum(Decimal(row["entry_fees_quote"]) for row in allocations)
        != result.allocated_entry_fees_quote
        or any(row["lot_id"] not in manifest.lot_ids for row in allocations)
    ):
        governor.record_cash_conflict(
            cash_event_id, "cash sale journal evidence conflicts"
        )
        raise RuntimeError("cash sale journal evidence conflicts")
    allocation_by_lot = {
        row["lot_id"]: Decimal(row["cost_basis_quote"]) for row in allocations
    }
    release_evidence = []
    with sqlite3.connect(governor.db_path) as conn:
        conn.row_factory = sqlite3.Row
        for owner in manifest.owners:
            if sale_purpose != "RECOVERY_PARTIAL" and owner.intent.value != "RECOVERY":
                continue
            basis = exact_decimal_sum(
                allocation_by_lot.get(lot_id, Decimal("0")) for lot_id in owner.lot_ids
            )
            if basis <= 0:
                continue
            prefix = (
                manifest.manifest_id
                if sale_purpose == "RECOVERY_PARTIAL"
                else "whole-exit:" + manifest.manifest_id
            )
            release_id = f"{prefix}:{fill.trade_id}:{owner.proposal_id}"
            release = conn.execute(
                "SELECT * FROM recovery_recycle_releases WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            if (
                release is None
                or release["proposal_id"] != owner.proposal_id
                or Decimal(release["amount_quote"]) != basis
            ):
                raise RuntimeError(
                    "cash sale lacks exact durable recovery basis release"
                )
            release_evidence.append(dict(release))
    evidence = json.dumps(
        {
            "event": dict(event),
            "fill": dict(journal),
            "lot_allocations": [dict(row) for row in allocations],
            "manifest_id": manifest.manifest_id,
            "cash_route": manifest.cash_proceeds_route,
            "recovery_releases": release_evidence,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(evidence.encode()).hexdigest()
    if not governor.settle_cash_fill(cash_event_id, settlement_evidence=digest):
        raise RuntimeError("cash sale requires reconciliation before spending")


def require_clear_cash_authority(governor):
    authority = governor.cash_authority_snapshot()
    if authority is not None and authority.blocked:
        raise RuntimeError(
            "cash authority requires reconciliation: " + authority.reason
        )
