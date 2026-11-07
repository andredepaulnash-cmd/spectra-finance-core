"""Native ledger ownership locks and exact selected-child exit manifests.

All state below lives in the EconomicPositionLedger transaction domain. No
method authorizes spending or accepts wallet balances as ownership evidence.
"""

from __future__ import annotations

import json
from decimal import Decimal, localcontext
from fractions import Fraction as F

from .models import IntentKind
from .position_mutation import lock_owner
from .selected_recovery import _digest, _json, _number, _provenance, _text

ZERO = Decimal(0)


def exact_decimal(value) -> Decimal:
    """Reject an unrepresentable rational rather than silently rounding it."""
    value = F(value)
    with localcontext() as ctx:
        ctx.prec = max(
            80, len(str(abs(value.numerator))) + len(str(value.denominator)) + 8
        )
        result = Decimal(value.numerator) / Decimal(value.denominator)
    if F(result) != value:
        raise ValueError("amount is not exactly representable as Decimal")
    return result


def _allocation_decimal(value) -> Decimal:
    """Finite internal allocation precision; every residual stays source-owned."""
    value = F(value)
    return exact_decimal(F((value * 10**24).__floor__(), 10**24))


def initialize_selected_recovery(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS selected_recovery_exit_scopes (
            manifest_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, initial_json TEXT NOT NULL, remaining_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS selected_recovery_native_exit_receipts (
            event_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL, allocations_json TEXT NOT NULL);
    """)


def create_exit_scope(ledger, *, governor, book, manifest_id, owner_id, scope, slices):
    """Persist immutable quotas against exact remaining source ownership."""
    _text(manifest_id)
    _text(owner_id)
    state = book.snapshot()
    expected_owner = (
        "selected:"
        + state["manifest_id"]
        + ":"
        + _digest(str(book.path.resolve()))[:16]
    )
    if (
        owner_id != expected_owner
        or scope != state["manifest"]["scope"]
        or state["selected"] is None
        or slices != state["children"][state["selected"]]
    ):
        raise ValueError("exact durable selected-child partition proof required")
    position = scope["parent"]
    rows = []
    ids = set()
    for source in slices:
        lid = _text(source["id"])
        if lid in ids:
            raise ValueError("duplicate selected source lot")
        ids.add(lid)
        _provenance(
            governor,
            source["proposal"],
            source["executor"],
            scope,
            IntentKind.ENTRY if source["kind"] == "ORIGINAL" else IntentKind.RECOVERY,
        )
        row = {
            "id": lid,
            "proposal": source["proposal"],
            "executor": source["executor"],
            "owner": source["owner"],
            "kind": source["kind"],
        }
        row.update(
            {
                key: str(exact_decimal(_number(F(source[key]), positive=key != "fees")))
                for key in ("base", "cost", "fees")
            }
        )
        rows.append(row)
    if not rows:
        raise ValueError("nonempty selected exit manifest required")
    initial = _json({"scope": scope, "slices": rows})
    with ledger._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if lock_owner(conn, position) != owner_id:
            raise ValueError("selected recovery lock required")
        prior = conn.execute(
            "SELECT owner_id, initial_json FROM selected_recovery_exit_scopes WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if prior:
            if tuple(prior) != (owner_id, initial):
                raise ValueError("immutable selected exit manifest changed")
            return
        for row in rows:
            raw = conn.execute(
                "SELECT * FROM economic_lots WHERE lot_id=?", (row["id"],)
            ).fetchone()
            if raw is None:
                raise ValueError("unknown selected source")
            lot = ledger._lot_from_row(raw)
            if (
                lot.position_id != position
                or lot.pair != scope["pair"]
                or lot.ownership.value != "ACTIVE_TRADE"
                or lot.proposal_id != row["proposal"]
                or lot.executor_id != row["executor"]
                or lot.source.value
                != ("BASE_ENTRY" if row["kind"] == "ORIGINAL" else "VCAP_RECOVERY")
            ):
                raise ValueError("selected source ownership changed")
            if (
                F(row["base"]) > F(lot.base_remaining)
                or F(row["cost"]) > F(lot.cost_basis_remaining_quote)
                or F(row["fees"]) > F(lot.entry_fees_remaining_quote)
            ):
                raise ValueError("selected quota exceeds source remainder")
            row["expected_base"] = str(lot.base_remaining)
            row["expected_cost"] = str(lot.cost_basis_remaining_quote)
            row["expected_fees"] = str(lot.entry_fees_remaining_quote)
        conn.execute(
            "INSERT INTO selected_recovery_exit_scopes VALUES (?, ?, ?, ?, ?)",
            (manifest_id, position, owner_id, initial, _json(rows)),
        )


def record_exit(ledger, *, manifest_id, owner_id, event_id, fill):
    """Atomically settle one exact SELL against bounded child/source quotas."""
    from .economic_position import EconomicExitResult, EconomicPositionStatus
    from .selected_recovery import _fill_payload, _validate_fill

    payload = _json(_fill_payload(fill))
    with ledger._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        scope_row = conn.execute(
            "SELECT * FROM selected_recovery_exit_scopes WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if scope_row is None or scope_row["owner_id"] != owner_id:
            raise ValueError("exact selected exit manifest required")
        scope = json.loads(scope_row["initial_json"])["scope"]
        _validate_fill(fill, scope, "SELL")
        position = scope["parent"]
        prior = conn.execute(
            "SELECT * FROM selected_recovery_native_exit_receipts WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if prior:
            if prior["manifest_id"] != manifest_id or prior["evidence_json"] != payload:
                raise ValueError("selected native fill replay conflicts")
            return json.loads(prior["allocations_json"])
        if lock_owner(conn, position) != owner_id:
            raise ValueError("selected recovery lock changed")
        if conn.execute(
            "SELECT 1 FROM economic_position_events WHERE event_id=?", (event_id,)
        ).fetchone():
            raise ValueError("native exit event already belongs to another path")
        position_row = ledger._position_row(conn, position)
        if position_row["status"] != EconomicPositionStatus.ACTIVE.value:
            raise ValueError("selected exit position is not active")
        rows = json.loads(scope_row["remaining_json"])
        sold = F(fill.amount_base)
        total = sum((F(row["base"]) for row in rows), F(0))
        if sold > total:
            raise ValueError("fill exceeds remaining selected-child inventory")
        # Pro rata at fixed internal precision, then close residuals without
        # changing the immutable source quotas. Last fills consume exact remainders.
        amounts = [
            min(F(row["base"]), F(_allocation_decimal(sold * F(row["base"]) / total)))
            for row in rows
        ]
        residual = sold - sum(amounts, F(0))
        for index in reversed(range(len(rows))):
            extra = min(residual, F(rows[index]["base"]) - amounts[index])
            amounts[index] += extra
            residual -= extra
        if residual:
            raise ValueError("selected quantity allocation did not close")
        allocations = []
        allocated_cost = allocated_fees = F(0)
        for row, amount in zip(rows, amounts):
            raw = conn.execute(
                "SELECT * FROM economic_lots WHERE lot_id=?", (row["id"],)
            ).fetchone()
            lot = ledger._lot_from_row(raw)
            if (
                lot.position_id != position
                or lot.proposal_id != row["proposal"]
                or lot.executor_id != row["executor"]
                or lot.ownership.value != "ACTIVE_TRADE"
                or lot.pair != scope["pair"]
                or lot.source.value
                != ("BASE_ENTRY" if row["kind"] == "ORIGINAL" else "VCAP_RECOVERY")
                or F(lot.base_remaining) != F(row["expected_base"])
                or F(lot.cost_basis_remaining_quote) != F(row["expected_cost"])
                or F(lot.entry_fees_remaining_quote) != F(row["expected_fees"])
            ):
                raise ValueError("selected source changed outside locked manifest")
            if not amount:
                continue
            full = amount == F(row["base"])
            cost = (
                F(row["cost"])
                if full
                else F(_allocation_decimal(F(row["cost"]) * amount / F(row["base"])))
            )
            fees = (
                F(row["fees"])
                if full
                else F(_allocation_decimal(F(row["fees"]) * amount / F(row["base"])))
            )
            piece = {
                "id": row["id"],
                "proposal": row["proposal"],
                "executor": row["executor"],
                "owner": row["owner"],
                "base": str(exact_decimal(amount)),
                "cost": str(exact_decimal(cost)),
                "fees": str(exact_decimal(fees)),
            }
            allocations.append(piece)
            allocated_cost += cost
            allocated_fees += fees
            for key, value in (("base", amount), ("cost", cost), ("fees", fees)):
                row[key] = str(exact_decimal(F(row[key]) - value))
                row[f"expected_{key}"] = str(
                    exact_decimal(F(row[f"expected_{key}"]) - value)
                )
            conn.execute(
                """UPDATE economic_lots SET base_remaining=?, cost_basis_remaining_quote=?,
                entry_fees_remaining_quote=?, updated_at=? WHERE lot_id=?""",
                (
                    row["expected_base"],
                    row["expected_cost"],
                    row["expected_fees"],
                    fill.occurred_at,
                    row["id"],
                ),
            )
            conn.execute(
                "INSERT INTO economic_exit_allocations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    row["id"],
                    position,
                    row["proposal"],
                    row["executor"],
                    "ACTIVE_TRADE",
                    piece["base"],
                    piece["cost"],
                    piece["fees"],
                ),
            )
        remaining_base = sum(
            (
                F(row[0])
                for row in conn.execute(
                    "SELECT base_remaining FROM economic_lots WHERE position_id=? AND ownership='ACTIVE_TRADE'",
                    (position,),
                ).fetchall()
            ),
            F(0),
        )
        gross, fee = F(fill.amount_base) * F(fill.price), F(fill.fee_paid)
        if fee > gross:
            raise ValueError("exit fee exceeds gross proceeds")
        realized = gross - fee - allocated_cost - allocated_fees
        closed = remaining_base == 0
        values = (
            event_id,
            position,
            "FINAL_EXIT" if closed else "PARTIAL_EXIT",
            str(exact_decimal(sold)),
            str(exact_decimal(gross)),
            str(exact_decimal(fee)),
            str(exact_decimal(allocated_cost)),
            str(exact_decimal(allocated_fees)),
            str(exact_decimal(realized)),
            str(exact_decimal(remaining_base)),
            int(closed),
            fill.occurred_at,
        )
        conn.execute(
            "INSERT INTO economic_position_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        conn.execute(
            "UPDATE economic_positions SET status=?, updated_at=? WHERE position_id=?",
            ("CLOSED" if closed else "ACTIVE", fill.occurred_at, position),
        )
        conn.execute(
            "UPDATE selected_recovery_exit_scopes SET remaining_json=? WHERE manifest_id=?",
            (_json(rows), manifest_id),
        )
        conn.execute(
            "INSERT INTO selected_recovery_native_exit_receipts VALUES (?, ?, ?, ?)",
            (event_id, manifest_id, payload, _json(allocations)),
        )
        return allocations


def seal_opening_basis(
    governor, *, ledger, book, owner_id, proposal_id, order, fills, observation
):
    """Seal selected-mode gross opening basis from fully closed exact evidence.

    Fees remain explicit cash expense. This proves the gross baseline for the
    existing principal-only recycle-release mechanism; it invents no hold.
    """
    from .fill_reconciliation import EconomicFillRole, LiveFillLedgerReconciler
    from .selected_recovery import (
        _fill_payload,
        _order_payload,
        _persisted_trade,
        _validate_fill,
    )

    state = book.snapshot()
    scope = state["manifest"]["scope"]
    expected_owner = (
        "selected:"
        + state["manifest_id"]
        + ":"
        + _digest(str(book.path.resolve()))[:16]
    )
    if (
        owner_id != expected_owner
        or ledger.selected_recovery_lock_owner(scope["parent"]) != owner_id
    ):
        raise ValueError("selected opening basis requires exact locked book owner")
    proof = _provenance(
        governor, proposal_id, order.executor_id, scope, IntentKind.RECOVERY
    )
    if (
        order.side != "BUY"
        or order.status not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "FAILED"}
        or observation.executor_id != order.executor_id
        or observation.executor_type != "dca_executor"
        or observation.status != "TERMINATED"
        or observation.close_type != "POSITION_HOLD"
    ):
        raise ValueError(
            "sealed selected basis requires terminal passive exact BUY evidence"
        )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    binding = reconciler.get_binding(order.order_id)
    if (
        binding is None
        or binding.proposal_id != proposal_id
        or binding.executor_id != order.executor_id
    ):
        raise ValueError("sealed selected order binding differs")
    ids = set()
    base = gross = fees = F(0)
    for fill in fills:
        _validate_fill(fill, scope, "BUY")
        if fill.order_id != order.order_id or fill.trade_id in ids:
            raise ValueError("sealed trade/order identity differs")
        ids.add(fill.trade_id)
        _persisted_trade(
            reconciler, fill, EconomicFillRole.VCAP_RECOVERY, scope["parent"]
        )
        source_id = "trade-fill:" + fill.trade_id
        source = state["patch_sources"].get(source_id)
        lot = ledger.get_lot(source_id)
        if (
            source is None
            or source["proposal"] != proposal_id
            or source["executor"] != order.executor_id
            or lot.proposal_id != proposal_id
            or lot.executor_id != order.executor_id
            or F(lot.base_original) != F(fill.amount_base)
            or F(lot.cost_basis_original_quote) != F(fill.amount_base) * F(fill.price)
            or F(lot.entry_fees_original_quote) != F(fill.fee_paid)
        ):
            raise ValueError(
                "sealed opening basis differs from exact native/source lots"
            )
        base += F(fill.amount_base)
        gross += F(fill.amount_base) * F(fill.price)
        fees += F(fill.fee_paid)
    with ledger._connect() as conn:
        native_ids = {
            row[0]
            for row in conn.execute(
                "SELECT lot_id FROM economic_lots WHERE proposal_id=?", (proposal_id,)
            )
        }
    if native_ids != {"trade-fill:" + tid for tid in ids}:
        raise ValueError("sealed source contains unknown or missing native lots")
    if (
        base != F(order.filled_amount)
        or base > F(order.amount)
        or (order.status == "FILLED" and base != F(order.amount))
        or (order.status in {"REJECTED", "FAILED"} and base != 0)
        or gross != F(observation.filled_amount_quote)
        or fees != F(observation.cum_fees_quote)
        or gross + fees > F(proof.authorized_quote)
    ):
        raise ValueError("sealed source closure or all-in accounting is inconsistent")
    payload = _json(
        {
            "scope": scope,
            "owner_id": owner_id,
            "proposal_id": proposal_id,
            "authorized_quote": str(proof.authorized_quote),
            "order": _order_payload(order),
            "fills": [_fill_payload(fill) for fill in fills],
            "gross": str(exact_decimal(gross)),
            "fees": str(exact_decimal(fees)),
        }
    )
    with governor._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_json FROM selected_recovery_sealed_basis WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row:
            if row[0] != payload:
                raise ValueError("sealed selected opening basis replay conflicts")
            return
        conn.execute(
            "INSERT INTO selected_recovery_sealed_basis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                order.executor_id,
                scope["pair"],
                scope["account"],
                scope["connector"],
                scope["controller"],
                str(proof.authorized_quote),
                str(exact_decimal(gross)),
                str(exact_decimal(fees)),
                payload,
            ),
        )
