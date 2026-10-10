"""Durability and failure boundaries of the pre-submission replay barrier."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

GUARD_PATH = (
    Path(__file__).parents[1]
    / "deploy/hummingbot_core/patches/spectra_attempt_guard.py"
)


def load_guard(name="spectra_journal_test"):
    spec = importlib.util.spec_from_file_location(name, GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def journal(tmp_path, monkeypatch):
    module = load_guard()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "attempts.sqlite3"
    identity = module.initialize_journal(str(path))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", str(path))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", identity)
    return module, path, identity


def executor(module, identity="executor-1", **changes):
    scope = dict(
        executor_id=identity,
        connector_name="kraken",
        trading_pair="XRP-USD",
        side="BUY",
        amount=Decimal("10"),
        price=Decimal("2"),
    )
    scope.update(changes)
    return module.claim_executor(**scope)


def order(module, identity="client-1", **changes):
    scope = dict(
        client_order_id=identity,
        trading_pair="XRP-USD",
        trade_type="BUY",
        amount=Decimal("10"),
        price=Decimal("2"),
    )
    scope.update(changes)
    return module.claim_order(**scope)


def test_committed_claim_is_visible_before_transport_and_survives_reload(journal):
    module, path, identity = journal
    reservation = executor(module)
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT attempt_id, scope FROM claims").fetchone()
    assert row[0] == "executor-1"
    assert json.loads(row[1])["amount"] == "10"
    replacement_module = load_guard("fresh_spectra_journal")
    with pytest.raises(replacement_module.AttemptAlreadyClaimed):
        executor(replacement_module)
    assert reservation.describe()["submission_claimed"] is True
    assert reservation.describe()["venue_finality_established"] is False
    assert module.initialize_journal.__doc__.startswith("Explicit offline")


def test_fresh_process_cannot_reclaim_after_previous_process_exits(journal):
    module, path, identity = journal
    executor(module)
    code = """
import importlib.util,sys
spec=importlib.util.spec_from_file_location('fresh_guard',sys.argv[1])
m=importlib.util.module_from_spec(spec);sys.modules['fresh_guard']=m;spec.loader.exec_module(m)
try:
 m.claim_executor(executor_id='executor-1',connector_name='kraken',trading_pair='XRP-USD',side='BUY',amount='10',price='2')
except m.AttemptAlreadyClaimed:
 sys.exit(0)
sys.exit(10)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(GUARD_PATH)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_competing_claims_have_one_winner(journal):
    module, _, _ = journal

    def compete(_):
        try:
            executor(module)
            return "claimed"
        except module.AttemptAlreadyClaimed:
            return "denied"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(compete, range(12)))
    assert outcomes.count("claimed") == 1
    assert outcomes.count("denied") == 11


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "9"},
        {"price": "3"},
        {"side": "SELL"},
        {"trading_pair": "ADA-USD"},
        {"connector_name": "another"},
    ],
)
def test_same_attempt_cannot_be_reclaimed_with_different_scope(journal, change):
    module, _, _ = journal
    executor(module)
    with pytest.raises(module.AttemptAlreadyClaimed):
        executor(module, **change)


def test_namespaces_independent_but_each_claim_never_releases(journal):
    module, _, _ = journal
    ex = executor(module, "same-id")
    placed = order(module, "same-id")
    placed.record_exchange_order("OTX-EXACT")
    placed.mark_uncertain()
    ex.record_order("same-id")
    for _ in range(3):
        with pytest.raises(module.AttemptAlreadyClaimed):
            order(module, "same-id")
    assert placed.describe()["exchange_order_ids"] == ["OTX-EXACT"]
    assert placed.describe()["uncertain"] is True
    assert not hasattr(placed, "release")


def snapshot(amount):
    return {
        "tracked_orders": [
            {
                "client_order_id": "old",
                "order_fills": {
                    "trade-1": {"fill_base_amount": amount, "fill_quote_amount": "6"}
                },
            }
        ],
        "held_position_orders": [],
        "executed_amount_base": amount,
        "filled_amount_quote": "6",
        "cum_fees_quote": "0.01",
        "reconciliation_required": True,
        "venue_finality_established": False,
    }


def test_late_fill_snapshots_and_ids_persist_and_deduplicate(journal):
    module, path, _ = journal
    claim = executor(module)
    claim.record_order("old")
    claim.record_order("old")
    claim.record_snapshot(snapshot("0"))
    claim.record_snapshot(snapshot("3"))
    claim.record_snapshot(snapshot("3"))
    fresh = load_guard("reader_after_restart")
    facts = fresh.read_claim(namespace="executor", attempt_id="executor-1")
    assert facts["client_order_ids"] == ["old"]
    assert facts["snapshot"]["executed_amount_base"] == "3"
    assert (
        facts["snapshot"]["tracked_orders"][0]["order_fills"]["trade-1"][
            "fill_base_amount"
        ]
        == "3"
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2


def test_accounting_uncertainty_and_known_fills_survive_restart(journal):
    module, _, _ = journal
    claim = executor(module)
    claim.record_snapshot(
        dict(
            snapshot("3"),
            accounting_available=False,
            accounting_uncertainty_reason="Foreign venue identity",
        )
    )
    fresh = load_guard("uncertain_accounting_reader")
    facts = fresh.find_executor_claim("executor-1")
    assert facts["snapshot"]["accounting_available"] is False
    assert facts["snapshot"]["executed_amount_base"] == "3"
    assert (
        facts["snapshot"]["accounting_uncertainty_reason"] == "Foreign venue identity"
    )
    with pytest.raises(fresh.AttemptAlreadyClaimed):
        executor(fresh)


def test_executor_absence_requires_verified_existing_history(journal):
    module, path, _ = journal
    assert module.find_executor_claim("fresh-executor") is None
    executor(module)
    assert module.find_executor_claim("executor-1")["submission_claimed"] is True
    path.unlink()
    with pytest.raises(module.AttemptJournalError):
        module.find_executor_claim("fresh-executor")
    assert not path.exists()


def test_reported_fill_watermark_preserves_missing_trade_evidence(journal):
    module, _, _ = journal
    claim = executor(module)
    claim.record_order("old")
    claim.record_snapshot(
        dict(
            snapshot("0"),
            accounting_available=False,
            accounting_uncertainty_reason="venue_trade_details_pending",
            venue_fill_watermarks={"old": "3.00"},
        )
    )
    fresh = load_guard("fill_watermark_reader")
    facts = fresh.find_executor_claim("executor-1")
    assert facts["snapshot"]["venue_fill_watermarks"] == {"old": "3.00"}
    assert facts["snapshot"]["accounting_available"] is False
    with pytest.raises(fresh.AttemptAlreadyClaimed):
        executor(fresh)


def test_existing_evidence_checkpoint_never_grants_new_attempt_or_ids(journal):
    module, path, _ = journal
    claim = executor(module)
    claim.record_order("old")
    claim.record_snapshot(snapshot("0"))
    fresh = load_guard("accounting_checkpoint_reader")
    evidence = fresh.open_executor_evidence("executor-1")
    assert not hasattr(evidence, "record_order")
    assert not hasattr(evidence, "record_exchange_order")
    assert not hasattr(evidence, "release")
    evidence.record_snapshot(snapshot("3"))
    evidence.mark_uncertain()
    facts = evidence.describe()
    assert facts["scope"]["amount"] == "10"
    assert facts["client_order_ids"] == ["old"]
    assert facts["snapshot"]["executed_amount_base"] == "3"
    assert facts["uncertain"] is True
    assert facts["venue_finality_established"] is False
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    with pytest.raises(fresh.AttemptAlreadyClaimed):
        executor(fresh)


def test_existing_evidence_cannot_open_an_absent_attempt(journal):
    module, path, _ = journal
    with pytest.raises(module.AttemptJournalError):
        module.open_executor_evidence("missing")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 0


def test_existing_evidence_storage_loss_cannot_be_recreated(journal):
    module, path, _ = journal
    executor(module)
    evidence = module.open_executor_evidence("executor-1")
    path.unlink()
    with pytest.raises(module.AttemptJournalError):
        evidence.record_snapshot(snapshot("3"))
    assert not path.exists()


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-1", "0", "1e200", True, 1.5, "nonsense"]
)
def test_invalid_exact_amount_never_spends_claim(journal, value):
    module, path, _ = journal
    with pytest.raises(module.AttemptJournalError):
        executor(module, amount=value)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("side", "BOTH"),
        ("price", None),
        ("executor_id", ""),
        ("executor_id", "a" * 129),
        ("connector_name", "contains whitespace"),
    ],
)
def test_malformed_scope_denied(journal, field, value):
    module, _, _ = journal
    with pytest.raises(module.AttemptJournalError):
        executor(module, **{field: value})


def test_market_order_can_claim_with_no_price(journal):
    module, _, _ = journal
    claim = order(module, price=None)
    assert claim.describe()["scope"]["price"] is None


@pytest.mark.parametrize(
    "case",
    [
        "unset",
        "wrong-id",
        "missing",
        "corrupt",
        "symlink",
        "fifo",
        "public-file",
        "public-parent",
    ],
)
def test_unavailable_or_wrong_journal_never_creates_or_claims(
    journal, monkeypatch, case
):
    module, path, identity = journal
    if case == "unset":
        monkeypatch.delenv("SPECTRA_ATTEMPT_JOURNAL")
    elif case == "wrong-id":
        monkeypatch.setenv(
            "SPECTRA_ATTEMPT_JOURNAL_ID", "a" * 32 if identity != "a" * 32 else "b" * 32
        )
    elif case == "missing":
        path.unlink()
    elif case == "corrupt":
        path.write_bytes(b"not a database")
    elif case == "symlink":
        target = path.with_name("real.sqlite3")
        path.rename(target)
        path.symlink_to(target)
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif case == "public-file":
        path.chmod(0o644)
    elif case == "public-parent":
        path.parent.chmod(0o755)
    with pytest.raises(module.AttemptJournalError):
        executor(module)
    if case == "missing":
        assert not path.exists()


def test_initializer_cannot_reset_claimed_journal(journal):
    module, _, _ = journal
    executor(module)
    with pytest.raises(module.AttemptJournalError):
        module.initialize_journal(os.environ["SPECTRA_ATTEMPT_JOURNAL"])
    with pytest.raises(module.AttemptAlreadyClaimed):
        executor(module)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: dict(x, venue_finality_established=True),
        lambda x: dict(x, credentials="never"),
        lambda x: dict(x, tracked_orders=[float("nan")]),
        lambda x: dict(x, tracked_orders=["x" * 2049]),
        lambda x: dict(x, tracked_orders=[object()]),
        lambda x: dict(x, accounting_available="false"),
        lambda x: dict(x, accounting_uncertainty_reason=None),
        lambda x: dict(x, venue_fill_watermarks=None),
        lambda x: dict(x, venue_fill_watermarks={"old": "-1"}),
        lambda x: dict(x, venue_fill_watermarks={"old": "NaN"}),
        lambda x: dict(x, venue_fill_watermarks={"old": 3.0}),
        lambda x: dict(x, venue_fill_watermarks={"old": "3e0"}),
        lambda x: dict(x, venue_fill_watermarks={"invalid client": "3"}),
    ],
)
def test_malformed_snapshot_fails_without_releasing_claim(journal, mutation):
    module, _, _ = journal
    claim = executor(module)
    with pytest.raises(module.AttemptJournalError):
        claim.record_snapshot(mutation(snapshot("0")))
    with pytest.raises(module.AttemptAlreadyClaimed):
        executor(module)


def test_lost_storage_after_claim_cannot_record_or_enable_new_transport(journal):
    module, path, _ = journal
    claim = executor(module)
    path.unlink()
    with pytest.raises(module.AttemptJournalError):
        claim.record_order("old")
    with pytest.raises(module.AttemptJournalError):
        claim.record_snapshot(snapshot("3"))
    with pytest.raises(module.AttemptJournalError):
        executor(module, "next")
    assert not path.exists()


def test_reading_unknown_claim_and_invalid_namespace_fails(journal):
    module, _, _ = journal
    with pytest.raises(module.AttemptJournalError):
        module.read_claim(namespace="executor", attempt_id="missing")
    with pytest.raises(module.AttemptJournalError):
        module.read_claim(namespace="other", attempt_id="anything")


def test_process_cannot_switch_to_another_initialized_store(journal, monkeypatch):
    module, path, _ = journal
    executor(module)
    other = path.parent / "other.sqlite3"
    other_id = module.initialize_journal(str(other))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", str(other))
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", other_id)
    with pytest.raises(module.AttemptJournalError, match="rotate"):
        executor(module)


def test_database_only_backup_restore_cannot_remove_a_spent_claim(journal):
    module, path, _ = journal
    empty_backup = path.read_bytes()
    executor(module)
    path.write_bytes(empty_backup)
    fresh = load_guard("after_stale_database_restore")
    with pytest.raises(fresh.AttemptJournalError, match="anchor"):
        executor(fresh)


def test_coherent_in_place_rollback_is_detected_in_current_process(journal):
    module, path, _ = journal
    anchor = path.with_name(path.name + ".claims")
    empty_db, empty_anchor = path.read_bytes(), anchor.read_bytes()
    executor(module)
    path.write_bytes(empty_db)
    anchor.write_bytes(empty_anchor)
    with pytest.raises(module.AttemptJournalError, match="rolled back"):
        executor(module)


def test_weakened_primary_key_schema_is_rejected(journal):
    module, path, _ = journal
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "DROP TABLE claims; CREATE TABLE claims(namespace TEXT, attempt_id TEXT, scope TEXT, uncertain INTEGER DEFAULT 1);"
        )
    with pytest.raises(module.AttemptJournalError, match="schema"):
        executor(module)


@pytest.mark.parametrize(
    "case", ["missing", "public", "symlink", "partial", "wrong-header", "bad-chain"]
)
def test_claim_anchor_loss_or_corruption_fails_closed(journal, case):
    module, path, _ = journal
    anchor = path.with_name(path.name + ".claims")
    executor(module)
    if case == "missing":
        anchor.unlink()
    elif case == "public":
        anchor.chmod(0o644)
    elif case == "symlink":
        target = anchor.with_name("actual-anchor")
        anchor.rename(target)
        anchor.symlink_to(target)
    elif case == "partial":
        anchor.write_bytes(anchor.read_bytes()[:-1])
    elif case == "wrong-header":
        anchor.write_bytes(b'{"journal_id":"wrong"}\n')
    else:
        lines = anchor.read_bytes().splitlines(keepends=True)
        row = json.loads(lines[-1])
        row["previous"] = "a" * 64
        lines[-1] = module._line(row)
        anchor.write_bytes(b"".join(lines))
    with pytest.raises(module.AttemptJournalError):
        executor(module, "another")


def test_mid_claim_anchor_fsync_failure_prohibits_transport_and_recovery(
    journal, monkeypatch
):
    module, _, _ = journal

    def fail(_):
        raise OSError("forced storage failure")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(module.AttemptJournalError):
        executor(module)
    with pytest.raises(module.AttemptJournalError):
        executor(module)


def test_parent_permissions_are_revalidated_for_existing_reservation(journal):
    module, path, _ = journal
    reservation = executor(module)
    path.parent.chmod(0o755)
    with pytest.raises(module.AttemptJournalError):
        reservation.record_order("old")


def test_competing_distinct_claims_form_one_valid_anchor_chain(journal):
    module, _, _ = journal
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(
            pool.map(lambda i: executor(module, f"executor-{i}"), range(8))
        )
    assert len(reservations) == 8
    module.verify_journal()
    assert len(module._BOUND_ANCHOR_PREFIX) == 9
