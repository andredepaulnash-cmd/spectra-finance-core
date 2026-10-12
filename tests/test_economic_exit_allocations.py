import sqlite3
from decimal import Decimal

import pytest

from spectra_core.economic_position import EconomicPositionLedger

D = Decimal


def book(tmp_path):
    ledger = EconomicPositionLedger(tmp_path / "allocations.sqlite")
    ledger.open_position(position_id="position", pair="XRP-USD", created_at=1000)
    for lid, amount, basis, fees in (
        ("lot1", "3", "30", ".3"),
        ("lot2", "7", "84", ".84"),
    ):
        ledger.record_base_fill(
            position_id="position",
            lot_id=lid,
            pair="XRP-USD",
            base_amount=D(amount),
            cost_basis_quote=D(basis),
            entry_fees_quote=D(fees),
            occurred_at=1001,
        )
    return ledger


def allocations(ledger):
    with ledger._connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM economic_exit_allocations ORDER BY lot_id"
            )
        ]


def test_exit_allocations_are_exact_idempotent_and_preserve_originals(tmp_path):
    ledger = book(tmp_path)
    kwargs = dict(
        event_id="sale",
        position_id="position",
        base_sold=D("5"),
        gross_proceeds_quote=D("70"),
        exit_fee_quote=D(".07"),
        occurred_at=1002,
    )
    first = ledger.record_partial_exit(**kwargs)
    journal = allocations(ledger)
    assert journal == [
        {
            "event_id": "sale",
            "lot_id": "lot1",
            "position_id": "position",
            "proposal_id": None,
            "executor_id": None,
            "ownership": "ACTIVE_TRADE",
            "base_sold": "1.5",
            "allocated_cost_basis_quote": "15.0",
            "allocated_entry_fees_quote": "0.15",
        },
        {
            "event_id": "sale",
            "lot_id": "lot2",
            "position_id": "position",
            "proposal_id": None,
            "executor_id": None,
            "ownership": "ACTIVE_TRADE",
            "base_sold": "3.5",
            "allocated_cost_basis_quote": "42.0",
            "allocated_entry_fees_quote": "0.420",
        },
    ]
    assert ledger.record_partial_exit(**kwargs) == first
    assert allocations(ledger) == journal
    assert ledger.get_lot("lot1").base_original == 3
    assert ledger.get_lot("lot2").cost_basis_original_quote == 84
    assert (
        sum(D(row["allocated_cost_basis_quote"]) for row in journal)
        == first.allocated_cost_basis_quote
    )
    with pytest.raises(RuntimeError, match="replay conflicts"):
        ledger.record_partial_exit(**{**kwargs, "base_sold": D("4")})
    assert allocations(ledger) == journal


def test_event_write_failure_rolls_back_allocations_and_lot_changes(tmp_path):
    ledger = book(tmp_path)
    before = ledger.snapshot("position")
    with ledger._connect() as conn:
        conn.execute(
            """CREATE TRIGGER reject_event BEFORE INSERT ON economic_position_events
            BEGIN SELECT RAISE(ABORT, 'synthetic journal failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic journal failure"):
        ledger.record_partial_exit(
            event_id="sale",
            position_id="position",
            base_sold=D("5"),
            gross_proceeds_quote=D("70"),
            occurred_at=1002,
        )
    assert allocations(ledger) == []
    assert ledger.snapshot("position") == before
    assert ledger.get_lot("lot1").base_remaining == 3
    assert ledger.get_lot("lot2").base_remaining == 7


def test_legacy_events_are_not_given_invented_allocations(tmp_path):
    ledger = book(tmp_path)
    ledger.record_partial_exit(
        event_id="old-sale",
        position_id="position",
        base_sold=D("5"),
        gross_proceeds_quote=D("70"),
        occurred_at=1002,
    )
    with ledger._connect() as conn:
        conn.execute("DROP TABLE economic_exit_allocations")
    reopened = EconomicPositionLedger(ledger.path)
    assert allocations(reopened) == []
    assert reopened.snapshot("position").active_base == 5
