"""Read-only conservation checks for economic positions and explicit cycles.

These reports are accounting observations, never execution, slot or cash
authorization. Legacy events without per-lot journals are labeled incomplete;
they are not retroactively assigned to owners from pair or wallet balances.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .economic_position import normalized_base_asset

ZERO = Decimal(0)


@dataclass(frozen=True)
class OwnerReconciliation:
    proposal_id: str | None
    executor_id: str | None
    lot_ids: tuple[str, ...]
    original_base: Decimal
    remaining_base: Decimal
    original_basis_quote: Decimal
    remaining_basis_quote: Decimal
    original_entry_fees_quote: Decimal
    remaining_entry_fees_quote: Decimal
    released_recovery_basis_quote: Decimal
    provenance_verified: bool


@dataclass(frozen=True)
class PositionReconciliation:
    position_id: str
    pair: str
    status: str
    conserved: bool
    ownership_verified: bool
    allocation_journal_complete: bool
    chronology_complete: bool
    reasons: tuple[str, ...]
    owners: tuple[OwnerReconciliation, ...]
    event_count: int
    original_base: Decimal
    remaining_base: Decimal
    original_investment_quote: Decimal
    remaining_investment_quote: Decimal
    net_sale_proceeds_quote: Decimal
    realized_pnl_quote: Decimal
    net_cashflow_quote: Decimal


@dataclass(frozen=True)
class CycleReconciliation:
    positions: tuple[PositionReconciliation, ...]
    conserved: bool
    complete: bool
    reasons: tuple[str, ...]
    net_cashflow_quote: Decimal
    realized_pnl_quote: Decimal
    remaining_investment_quote: Decimal


def _sum(rows, key):
    return sum((Decimal(str(row[key])) for row in rows), ZERO)


def _has_table(conn, table):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


@contextmanager
def _read_only(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.close()


def reconcile_position(
    *,
    ledger_path: str | Path,
    governor_path: str | Path,
    position_id: str,
    tolerance: Decimal = Decimal("1e-18"),
) -> PositionReconciliation:
    """Check inventory/basis/fees/cash identities without changing either DB.

    Governor and economic databases are independent read snapshots. This API
    deliberately grants no mutation authority; a live mutation must perform
    its own current transactional validation after any asynchronous boundary.
    """
    if not isinstance(position_id, str) or not position_id.strip():
        raise ValueError("position_id must be non-empty")
    if (
        not isinstance(tolerance, Decimal)
        or not tolerance.is_finite()
        or tolerance < ZERO
    ):
        raise ValueError("tolerance must be finite non-negative Decimal")
    with _read_only(ledger_path) as book, _read_only(governor_path) as governor:
        position = book.execute(
            "SELECT * FROM economic_positions WHERE position_id=?", (position_id,)
        ).fetchone()
        if position is None:
            raise ValueError("unknown economic position")
        lots = book.execute(
            "SELECT * FROM economic_lots WHERE position_id=? ORDER BY created_at, lot_id",
            (position_id,),
        ).fetchall()
        events = book.execute(
            "SELECT * FROM economic_position_events WHERE position_id=? ORDER BY occurred_at, rowid",
            (position_id,),
        ).fetchall()
        allocations = (
            book.execute(
                "SELECT * FROM economic_exit_allocations WHERE position_id=? ORDER BY event_id, lot_id",
                (position_id,),
            ).fetchall()
            if _has_table(book, "economic_exit_allocations")
            else []
        )
        reasons = []
        numeric_lot_fields = (
            "base_original",
            "base_remaining",
            "cost_basis_original_quote",
            "cost_basis_remaining_quote",
            "entry_fees_original_quote",
            "entry_fees_remaining_quote",
        )
        numeric_event_fields = (
            "base_amount",
            "quote_amount",
            "fee_quote",
            "allocated_cost_basis_quote",
            "allocated_entry_fees_quote",
            "realized_pnl_quote",
            "active_base_remaining",
        )
        for rows, fields in (
            (lots, numeric_lot_fields),
            (events, numeric_event_fields),
        ):
            if any(
                not Decimal(str(row[field])).is_finite()
                for row in rows
                for field in fields
            ):
                raise ValueError("non-finite economic accounting evidence")

        def same(left, right):
            return abs(left - right) <= tolerance

        for lot in lots:
            if lot["pair"] != position["pair"] or lot["ownership"] != "ACTIVE_TRADE":
                reasons.append("lot_position_or_ownership_scope_conflict")
            for original, remaining in (
                ("base_original", "base_remaining"),
                ("cost_basis_original_quote", "cost_basis_remaining_quote"),
                ("entry_fees_original_quote", "entry_fees_remaining_quote"),
            ):
                if (
                    Decimal(lot[original]) < ZERO
                    or Decimal(lot[remaining]) < ZERO
                    or Decimal(lot[remaining]) > Decimal(lot[original]) + tolerance
                ):
                    reasons.append("invalid_remaining_inventory")
        original_base = _sum(lots, "base_original")
        remaining_base = _sum(lots, "base_remaining")
        original_basis = _sum(lots, "cost_basis_original_quote")
        remaining_basis = _sum(lots, "cost_basis_remaining_quote")
        original_fees = _sum(lots, "entry_fees_original_quote")
        remaining_fees = _sum(lots, "entry_fees_remaining_quote")
        for name, original, remaining, consumed in (
            ("base", original_base, remaining_base, _sum(events, "base_amount")),
            (
                "basis",
                original_basis,
                remaining_basis,
                _sum(events, "allocated_cost_basis_quote"),
            ),
            (
                "entry_fees",
                original_fees,
                remaining_fees,
                _sum(events, "allocated_entry_fees_quote"),
            ),
        ):
            if not same(original, remaining + consumed):
                reasons.append(name + "_conservation_failed")
        previously_sold = ZERO
        chronology_complete = True
        for event in events:
            earlier = sum(
                (
                    Decimal(lot["base_original"])
                    for lot in lots
                    if lot["created_at"] < event["occurred_at"]
                ),
                ZERO,
            )
            simultaneous = sum(
                (
                    Decimal(lot["base_original"])
                    for lot in lots
                    if lot["created_at"] == event["occurred_at"]
                ),
                ZERO,
            )
            lower_remaining = earlier - previously_sold - Decimal(event["base_amount"])
            upper_remaining = lower_remaining + simultaneous
            recorded_remaining = Decimal(event["active_base_remaining"])
            if simultaneous > ZERO:
                # Separate lot/event tables cannot prove which same-time write
                # happened first. Preserve conservation but label chronology
                # incomplete rather than invent an ordering or a conflict.
                chronology_complete = False
            if (
                upper_remaining < -tolerance
                or recorded_remaining < lower_remaining - tolerance
                or recorded_remaining > upper_remaining + tolerance
                or event["position_closed"] not in (0, 1)
                or bool(event["position_closed"]) != (recorded_remaining == ZERO)
                or (event["event_type"] == "FINAL_EXIT")
                != bool(event["position_closed"])
            ):
                reasons.append("event_remaining_or_closed_state_conflict")
            previously_sold += Decimal(event["base_amount"])
            if event["event_type"] not in {"PARTIAL_EXIT", "FINAL_EXIT"}:
                reasons.append("unknown_economic_event_type")
            if any(
                Decimal(event[key]) < ZERO
                for key in numeric_event_fields
                if key != "realized_pnl_quote"
            ):
                reasons.append("negative_exit_geometry")
            expected = (
                Decimal(event["quote_amount"])
                - Decimal(event["fee_quote"])
                - Decimal(event["allocated_cost_basis_quote"])
                - Decimal(event["allocated_entry_fees_quote"])
            )
            if not same(expected, Decimal(event["realized_pnl_quote"])):
                reasons.append("event_profit_identity_failed")
        if position["status"] == "CLOSED" and remaining_base != ZERO:
            reasons.append("closed_position_retains_inventory")

        by_event = {}
        allocation_fields = (
            "base_sold",
            "allocated_cost_basis_quote",
            "allocated_entry_fees_quote",
        )
        for allocation in allocations:
            amounts = tuple(Decimal(str(allocation[key])) for key in allocation_fields)
            if any(not amount.is_finite() for amount in amounts):
                raise ValueError("non-finite allocation accounting evidence")
            if amounts[0] <= ZERO or any(amount < ZERO for amount in amounts[1:]):
                reasons.append("invalid_owner_allocation_geometry")
            by_event.setdefault(allocation["event_id"], []).append(allocation)
        event_ids = {event["event_id"] for event in events}
        if set(by_event) - event_ids:
            reasons.append("allocation_references_unknown_event")
        journal_complete = all(event["event_id"] in by_event for event in events)
        lot_map = {lot["lot_id"]: lot for lot in lots}
        for event in events:
            entries = by_event.get(event["event_id"], ())
            if not entries:
                continue
            for event_field, allocation_field in (
                ("base_amount", "base_sold"),
                ("allocated_cost_basis_quote", "allocated_cost_basis_quote"),
                ("allocated_entry_fees_quote", "allocated_entry_fees_quote"),
            ):
                if not same(
                    Decimal(event[event_field]), _sum(entries, allocation_field)
                ):
                    reasons.append("event_allocation_conservation_failed")
            for entry in entries:
                lot = lot_map.get(entry["lot_id"])
                if lot is None or any(
                    entry[key] != lot[key]
                    for key in (
                        "position_id",
                        "proposal_id",
                        "executor_id",
                        "ownership",
                    )
                ):
                    reasons.append("allocation_owner_scope_conflict")
        if journal_complete:
            running = {
                lot["lot_id"]: [
                    Decimal(lot[key])
                    for key in (
                        "base_original",
                        "cost_basis_original_quote",
                        "entry_fees_original_quote",
                    )
                ]
                for lot in lots
            }
            for event in events:
                for entry in by_event.get(event["event_id"], ()):
                    lot = lot_map.get(entry["lot_id"])
                    if lot is None:
                        continue
                    amounts = [Decimal(entry[key]) for key in allocation_fields]
                    current = running[entry["lot_id"]]
                    if lot["created_at"] > event["occurred_at"] or any(
                        amount > balance + tolerance
                        for amount, balance in zip(amounts, current)
                    ):
                        reasons.append("allocation_exceeds_owner_inventory_at_event")
                    if current[0] > ZERO:
                        for index in (1, 2):
                            if not same(
                                amounts[index], current[index] * amounts[0] / current[0]
                            ):
                                reasons.append("allocation_owner_basis_ratio_conflict")
                    running[entry["lot_id"]] = [
                        balance - amount for balance, amount in zip(current, amounts)
                    ]
            for lot in lots:
                entries = [
                    entry for entry in allocations if entry["lot_id"] == lot["lot_id"]
                ]
                for original, remaining, consumed in (
                    ("base_original", "base_remaining", "base_sold"),
                    (
                        "cost_basis_original_quote",
                        "cost_basis_remaining_quote",
                        "allocated_cost_basis_quote",
                    ),
                    (
                        "entry_fees_original_quote",
                        "entry_fees_remaining_quote",
                        "allocated_entry_fees_quote",
                    ),
                ):
                    if not same(
                        Decimal(lot[original]),
                        Decimal(lot[remaining]) + _sum(entries, consumed),
                    ):
                        reasons.append("owner_allocation_conservation_failed")
        grouped = {}
        for lot in lots:
            grouped.setdefault((lot["proposal_id"], lot["executor_id"]), []).append(lot)
        owners = []
        for (proposal_id, executor_id), owner_lots in grouped.items():
            provenance = governor.execute(
                "SELECT * FROM execution_provenance WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            receipt = governor.execute(
                "SELECT executor_id FROM execution_receipts WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            verified = bool(
                provenance is not None
                and receipt is not None
                and provenance["executor_id"] == executor_id == receipt["executor_id"]
                and provenance["pair"] == position["pair"]
                and provenance["account_name"]
                and provenance["connector_name"]
                and all(
                    (
                        lot["source"] == "VCAP_RECOVERY"
                        and provenance["intent"] == "RECOVERY"
                    )
                    or (
                        lot["source"] == "BASE_ENTRY"
                        and provenance["intent"] == "ENTRY"
                    )
                    for lot in owner_lots
                )
            )
            conflicting = (
                receipt is not None and receipt["executor_id"] != executor_id
            ) or (
                provenance is not None
                and (
                    provenance["executor_id"] != executor_id
                    or provenance["pair"] != position["pair"]
                    or any(
                        (
                            lot["source"] == "VCAP_RECOVERY"
                            and provenance["intent"] != "RECOVERY"
                        )
                        or (
                            lot["source"] == "BASE_ENTRY"
                            and provenance["intent"] != "ENTRY"
                        )
                        for lot in owner_lots
                    )
                )
            )
            if conflicting:
                reasons.append("exact_execution_provenance_conflict")
            releases = (
                governor.execute(
                    "SELECT amount_quote FROM recovery_recycle_releases WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchall()
                if _has_table(governor, "recovery_recycle_releases")
                else []
            )
            released = _sum(releases, "amount_quote")
            consumed_basis = _sum(owner_lots, "cost_basis_original_quote") - _sum(
                owner_lots, "cost_basis_remaining_quote"
            )
            if (
                not released.is_finite()
                or released < ZERO
                or released > consumed_basis + tolerance
            ):
                reasons.append("recovery_release_exceeds_consumed_owner_basis")
            owners.append(
                OwnerReconciliation(
                    proposal_id,
                    executor_id,
                    tuple(lot["lot_id"] for lot in owner_lots),
                    _sum(owner_lots, "base_original"),
                    _sum(owner_lots, "base_remaining"),
                    _sum(owner_lots, "cost_basis_original_quote"),
                    _sum(owner_lots, "cost_basis_remaining_quote"),
                    _sum(owner_lots, "entry_fees_original_quote"),
                    _sum(owner_lots, "entry_fees_remaining_quote"),
                    released,
                    verified,
                )
            )
        net_proceeds = _sum(events, "quote_amount") - _sum(events, "fee_quote")
        realized = _sum(events, "realized_pnl_quote")
        cashflow = net_proceeds - original_basis - original_fees
        if not same(cashflow, realized - remaining_basis - remaining_fees):
            reasons.append("position_cashflow_identity_failed")
        conserved = not reasons
        ownership_verified = bool(owners) and all(
            owner.provenance_verified for owner in owners
        )
        if not ownership_verified:
            reasons.append("ownership_evidence_incomplete")
        if not journal_complete:
            reasons.append("legacy_allocation_journal_incomplete")
        if not chronology_complete:
            reasons.append("chronology_evidence_incomplete")
        return PositionReconciliation(
            position_id,
            position["pair"],
            position["status"],
            conserved,
            ownership_verified,
            journal_complete,
            chronology_complete,
            tuple(dict.fromkeys(reasons)),
            tuple(owners),
            len(events),
            original_base,
            remaining_base,
            original_basis + original_fees,
            remaining_basis + remaining_fees,
            net_proceeds,
            realized,
            cashflow,
        )


def reconcile_explicit_cycle(
    *, ledger_path: str | Path, governor_path: str | Path, position_ids: tuple[str, ...]
) -> CycleReconciliation:
    """Audit explicitly named sell/reentry positions, never infer cash ownership.

    Summed identities show accounting conservation only. They do not establish
    that predecessor sale proceeds funded the successor; that requires exact
    reservation/cash-receipt attribution in the execution path.
    """
    if not position_ids or len(set(position_ids)) != len(position_ids):
        raise ValueError("cycle requires distinct explicit position identities")
    positions = tuple(
        reconcile_position(
            ledger_path=ledger_path, governor_path=governor_path, position_id=pid
        )
        for pid in position_ids
    )
    if len({normalized_base_asset(position.pair) for position in positions}) != 1:
        raise ValueError(
            "cycle positions must reference the same normalized base asset"
        )
    # Quote currencies cannot be summed without explicit, confirmed conversion.
    if len({position.pair.rsplit("-", 1)[1] for position in positions}) != 1:
        raise ValueError(
            "cycle quote currencies require explicit conversion accounting"
        )
    reasons = [reason for position in positions for reason in position.reasons]
    closed = all(
        position.status == "CLOSED" and position.remaining_base == ZERO
        for position in positions
    )
    if not closed:
        reasons.append("cycle_contains_unsold_inventory")
    conserved = all(position.conserved for position in positions)
    complete = (
        conserved
        and closed
        and all(
            position.ownership_verified
            and position.allocation_journal_complete
            and position.chronology_complete
            for position in positions
        )
    )
    return CycleReconciliation(
        positions,
        conserved,
        complete,
        tuple(dict.fromkeys(reasons)),
        sum((position.net_cashflow_quote for position in positions), ZERO),
        sum((position.realized_pnl_quote for position in positions), ZERO),
        sum((position.remaining_investment_quote for position in positions), ZERO),
    )
