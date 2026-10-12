import sqlite3
from decimal import Decimal as D

import pytest
from test_partial_recovery_recycling import (
    add_recovery,
    governor,
    ledger,
    prepare_execution,
)

from spectra_core.cycle_reconciliation import (
    reconcile_explicit_cycle,
    reconcile_position,
)
from spectra_core.models import IntentKind


@pytest.fixture
def books(tmp_path):
    g, book = governor(tmp_path), ledger(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-lot",
        proposal_id="entry",
        base_amount=D(20),
        cost_basis_quote=D(10),
        entry_fees_quote=D("0.1"),
        occurred_at=1010,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery",
        executor_id="recovery-exec",
        lot_id="recovery-lot",
        base="10",
        basis="4",
        occurred_at=1020,
    )
    return book, g


def audit(books, position_id="xrp-position-1"):
    book, g = books
    return reconcile_position(
        ledger_path=book.path, governor_path=g.db_path, position_id=position_id
    )


def sell(book, amount="15", event="sell-1", proceeds="9"):
    return book.record_partial_exit(
        event_id=event,
        position_id="xrp-position-1",
        base_sold=D(amount),
        gross_proceeds_quote=D(proceeds),
        exit_fee_quote=D("0.1"),
        occurred_at=2000,
    )


def test_open_partial_and_closed_positions_reconcile_with_exact_owners(books):
    book, g = books
    original = audit(books)
    assert original.conserved and original.ownership_verified
    assert original.original_investment_quote == D("14.1")
    assert original.net_cashflow_quote == D("-14.1")
    first = sell(book)
    report = audit(books)
    assert report.conserved and report.allocation_journal_complete
    assert report.remaining_base == 15
    assert report.realized_pnl_quote == first.realized_pnl_quote
    assert (
        report.net_cashflow_quote
        == report.realized_pnl_quote - report.remaining_investment_quote
    )
    assert {owner.proposal_id for owner in report.owners} == {"entry", "recovery"}
    sell(book, event="sell-2")
    closed = audit(books)
    assert closed.conserved and closed.status == "CLOSED"
    assert closed.remaining_base == closed.remaining_investment_quote == 0
    assert closed.net_cashflow_quote == closed.realized_pnl_quote


def test_reports_are_read_only_and_never_release_owner_capital(books):
    book, g = books
    sell(book)
    before = []
    for path in (book.path, g.db_path):
        with sqlite3.connect(path) as conn:
            before.append(tuple(conn.iterdump()))
    report = audit(books)
    assert report.owners[1].released_recovery_basis_quote == 0
    assert g.current_recovery_exposure_quote() == 4
    for path, expected in zip((book.path, g.db_path), before):
        with sqlite3.connect(path) as conn:
            assert tuple(conn.iterdump()) == expected


def test_closed_sale_and_reentry_cycle_includes_open_residual_until_final_exit(books):
    book, g = books
    sell(book, amount="30", proceeds="18")
    book.open_position(position_id="xrp-position-2", pair="XRP-USD", created_at=2100)
    prepare_execution(
        g,
        proposal_id="reentry",
        executor_id="reentry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-2",
        lot_id="reentry-lot",
        proposal_id="reentry",
        base_amount=D(20),
        cost_basis_quote=D(10),
        occurred_at=2200,
    )
    args = dict(
        ledger_path=book.path,
        governor_path=g.db_path,
        position_ids=("xrp-position-1", "xrp-position-2"),
    )
    open_cycle = reconcile_explicit_cycle(**args)
    assert open_cycle.conserved and not open_cycle.complete
    assert open_cycle.remaining_investment_quote == 10
    assert "cycle_contains_unsold_inventory" in open_cycle.reasons
    book.record_partial_exit(
        event_id="reentry-exit",
        position_id="xrp-position-2",
        base_sold=D(20),
        gross_proceeds_quote=D(11),
        occurred_at=2300,
    )
    closed_cycle = reconcile_explicit_cycle(**args)
    assert closed_cycle.complete and closed_cycle.conserved
    assert closed_cycle.net_cashflow_quote == closed_cycle.realized_pnl_quote


@pytest.mark.parametrize(
    "sql,reason",
    [
        (
            "UPDATE economic_lots SET base_remaining='31' WHERE lot_id='entry-lot'",
            "base_conservation_failed",
        ),
        (
            "UPDATE economic_lots SET cost_basis_remaining_quote='9' WHERE lot_id='entry-lot'",
            "basis_conservation_failed",
        ),
        (
            "UPDATE economic_position_events SET realized_pnl_quote='999'",
            "event_profit_identity_failed",
        ),
        (
            "UPDATE economic_exit_allocations SET executor_id='foreign' WHERE lot_id='entry-lot'",
            "allocation_owner_scope_conflict",
        ),
        (
            "UPDATE economic_positions SET status='CLOSED'",
            "closed_position_retains_inventory",
        ),
    ],
)
def test_corrupt_economic_evidence_is_detected(books, sql, reason):
    book, _ = books
    sell(book)
    with sqlite3.connect(book.path) as conn:
        conn.execute(sql)
    report = audit(books)
    assert not report.conserved
    assert reason in report.reasons


def test_owner_provenance_cannot_be_inferred_from_same_pair(books):
    _, g = books
    with sqlite3.connect(g.db_path) as conn:
        conn.execute(
            "UPDATE execution_receipts SET executor_id='foreign' WHERE proposal_id='entry'"
        )
    report = audit(books)
    assert not report.ownership_verified and not report.conserved
    assert "exact_execution_provenance_conflict" in report.reasons


def test_missing_legacy_provenance_is_incomplete_not_false_accounting_conflict(books):
    _, g = books
    with sqlite3.connect(g.db_path) as conn:
        conn.execute("DELETE FROM execution_provenance WHERE proposal_id='entry'")
    report = audit(books)
    assert report.conserved and not report.ownership_verified
    assert "ownership_evidence_incomplete" in report.reasons
    assert "exact_execution_provenance_conflict" not in report.reasons


def test_recovery_release_cannot_exceed_consumed_owner_basis(books):
    _, g = books
    g.record_recovery_recycle_release(
        release_id="unbacked", proposal_id="recovery", amount_quote=D(1)
    )
    report = audit(books)
    assert not report.conserved
    assert "recovery_release_exceeds_consumed_owner_basis" in report.reasons


def test_legacy_events_report_incomplete_owner_coverage_without_fabrication(books):
    book, _ = books
    sell(book)
    with sqlite3.connect(book.path) as conn:
        conn.execute("DELETE FROM economic_exit_allocations")
    report = audit(books)
    assert report.conserved and not report.allocation_journal_complete
    assert "legacy_allocation_journal_incomplete" in report.reasons


def test_duplicate_cycle_position_cannot_double_count(books):
    book, g = books
    with pytest.raises(ValueError, match="distinct"):
        reconcile_explicit_cycle(
            ledger_path=book.path,
            governor_path=g.db_path,
            position_ids=("xrp-position-1", "xrp-position-1"),
        )


def test_nonfinite_ledger_data_never_returns_pass(books):
    book, _ = books
    with sqlite3.connect(book.path) as conn:
        conn.execute(
            "UPDATE economic_lots SET base_remaining='NaN' WHERE lot_id='entry-lot'"
        )
    with pytest.raises(ValueError, match="non-finite"):
        audit(books)


def test_balanced_negative_owner_allocations_cannot_report_complete(books):
    book, g = books
    sell(book)
    sell(book, event="sell-2")
    # Equal and opposite tampering preserves global row/column sums.
    with sqlite3.connect(book.path) as conn:
        conn.execute(
            "UPDATE economic_exit_allocations SET base_sold='-1' WHERE event_id='sell-1' AND lot_id='entry-lot'"
        )
        conn.execute(
            "UPDATE economic_exit_allocations SET base_sold='16' WHERE event_id='sell-1' AND lot_id='recovery-lot'"
        )
        conn.execute(
            "UPDATE economic_exit_allocations SET base_sold='21' WHERE event_id='sell-2' AND lot_id='entry-lot'"
        )
        conn.execute(
            "UPDATE economic_exit_allocations SET base_sold='-6' WHERE event_id='sell-2' AND lot_id='recovery-lot'"
        )
    report = reconcile_explicit_cycle(
        ledger_path=book.path, governor_path=g.db_path, position_ids=("xrp-position-1",)
    )
    assert not report.conserved and not report.complete
    assert "invalid_owner_allocation_geometry" in report.reasons


def test_balanced_totals_do_not_hide_impossible_event_remaining_or_closed_flags(books):
    book, _ = books
    sell(book, amount="30", proceeds="18")
    with sqlite3.connect(book.path) as conn:
        conn.execute(
            "UPDATE economic_position_events SET active_base_remaining='123', position_closed=0"
        )
    result = audit(books)
    assert not result.conserved
    assert "event_remaining_or_closed_state_conflict" in result.reasons


def test_nonfinite_owner_allocation_never_reports_pass(books):
    book, _ = books
    sell(book)
    with sqlite3.connect(book.path) as conn:
        conn.execute("UPDATE economic_exit_allocations SET base_sold='NaN'")
    with pytest.raises(ValueError, match="non-finite allocation"):
        audit(books)


def test_same_timestamp_buy_after_sale_is_incomplete_chronology_not_false_conflict(
    books,
):
    book, g = books
    sell(book)
    add_recovery(
        g,
        book,
        proposal_id="same-time",
        executor_id="same-time-exec",
        lot_id="same-time-lot",
        base="10",
        basis="4",
        occurred_at=2000,
    )
    result = audit(books)
    assert result.conserved
    assert not result.chronology_complete
    assert "chronology_evidence_incomplete" in result.reasons
    assert "event_remaining_or_closed_state_conflict" not in result.reasons
