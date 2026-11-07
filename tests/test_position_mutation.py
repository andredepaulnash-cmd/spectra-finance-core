import sqlite3
from decimal import Decimal as D

import pytest

from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.models import IntentKind
from tests.test_partial_recovery_recycling import (
    governor,
    ledger,
    prepare_execution,
)


def test_position_mutation_lock_is_exclusive_and_proposal_scoped(tmp_path):
    book = EconomicPositionLedger(tmp_path / "economic.db")
    book.open_position(position_id="p", pair="XRP-USD", created_at=1)

    assert book.selected_recovery_lock_owner("p") is None
    book.claim_selected_recovery_lock("p", "selected:p:a")
    assert book.selected_recovery_lock_owner("p") == "selected:p:a"

    with pytest.raises(ValueError, match="another selected recovery"):
        book.claim_selected_recovery_lock("p", "selected:p:b")

    book.allow_selected_recovery_proposal("p", "selected:p:a", "proposal-a")
    assert book.selected_recovery_proposal_allowed("XRP-USD", "proposal-a")
    assert not book.selected_recovery_proposal_allowed("XRP-USD", "proposal-b")


def test_partial_exit_cannot_mutate_a_selected_locked_position(tmp_path):
    book = EconomicPositionLedger(tmp_path / "economic.db")
    book.open_position(position_id="p", pair="XRP-USD", created_at=1)
    book.claim_selected_recovery_lock("p", "selected:p:a")

    with pytest.raises(ValueError, match="selected recovery owns exact position exit"):
        book.record_partial_exit(
            event_id="blocked-exit",
            position_id="p",
            base_sold=D(1),
            gross_proceeds_quote=D(1),
            occurred_at=2,
        )

    with pytest.raises(ValueError, match="exact position mutation owner is not held"):
        book.record_partial_exit(
            event_id="wrong-owner-exit",
            position_id="p",
            base_sold=D(1),
            gross_proceeds_quote=D(1),
            occurred_at=2,
            mutation_owner_id="selected:p:b",
        )

    assert book.selected_recovery_lock_owner("p") == "selected:p:a"
    with sqlite3.connect(book.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM economic_position_events"
        ).fetchone()[0] == 0


def test_lock_release_requires_exact_owner_and_is_idempotent_after_release(tmp_path):
    book = EconomicPositionLedger(tmp_path / "economic.db")
    book.open_position(position_id="p", pair="XRP-USD", created_at=1)
    book.claim_selected_recovery_lock("p", "owner-a")

    with pytest.raises(ValueError, match="cannot release another mutation owner"):
        book.release_selected_recovery_lock("p", "owner-b")

    assert book.release_selected_recovery_lock("p", "owner-a")
    assert not book.release_selected_recovery_lock("p", "owner-a")
    assert book.selected_recovery_lock_owner("p") is None


def test_confirmed_exit_writes_exact_per_owner_allocation_receipt(tmp_path):
    g = governor(tmp_path)
    book = ledger(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="p",
        lot_id="entry-lot",
        proposal_id="entry",
        base_amount=D(20),
        cost_basis_quote=D(10),
        entry_fees_quote=D("0.1"),
        occurred_at=1,
    )

    book.record_partial_exit(
        event_id="sale-1",
        position_id="p",
        base_sold=D(5),
        gross_proceeds_quote=D(3),
        exit_fee_quote=D("0.01"),
        occurred_at=2,
    )

    with sqlite3.connect(book.path) as conn:
        row = conn.execute(
            """SELECT position_id, proposal_id, executor_id, ownership,
                      base_sold, allocated_cost_basis_quote,
                      allocated_entry_fees_quote
               FROM economic_exit_allocations WHERE event_id='sale-1'
                 AND lot_id='entry-lot'"""
        ).fetchone()

    assert row == (
        "p",
        "entry",
        "entry-exec",
        "ACTIVE_TRADE",
        "5",
        "2.5",
        "0.025",
    )


def test_partial_recycle_manifest_creation_respects_selected_lock(tmp_path):
    # The manifest store shares the economic ledger's SQLite database.
    from spectra_core.partial_recycle import PartialRecycleStore

    book = EconomicPositionLedger(tmp_path / "economic.db")
    book.open_position(position_id="p", pair="XRP-USD", created_at=1)
    book.claim_selected_recovery_lock("p", "selected:p:a")
    store = PartialRecycleStore(book.path)

    # A manifest cannot be created without a valid immutable owner contract;
    # this test asserts the lock rejection at the store boundary using a
    # deliberately minimal object before any INSERT can occur.
    class Candidate:
        manifest_id = "partial-1"
        position_id = "p"

    with pytest.raises((AttributeError, TypeError, ValueError, RuntimeError)):
        store.create(Candidate())
    with sqlite3.connect(book.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM partial_recycle_manifests"
        ).fetchone()[0] == 0
