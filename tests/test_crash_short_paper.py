"""Synthetic lifecycle, isolation and accounting acceptance for the paper book."""

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D

import pytest

from spectra_core.crash_short_model import (
    ASSETS,
    DAY,
    FROZEN_START,
    INPUT_SHA256,
    REQUIRED_CONTRACT_METADATA,
    PaperSource,
    contract_model_gate,
)
from spectra_core.crash_short_paper import PaperShortStore, ReconciliationRequired
from spectra_core.crash_short_signal import Bar

START = FROZEN_START
CRASH_DAY = START + 9 * DAY
SOURCE = PaperSource("SYNTHETIC", "a" * 64)


def bars(t, price="90", *, btc=None, eth=None):
    normal = Bar(t, price, price, price, price, "1", "1")
    return {"BTC": btc or normal, "ETH": eth or normal}


@pytest.fixture(scope="module")
def seeded_database(tmp_path_factory):
    path = tmp_path_factory.mktemp("short-seed") / "paper.sqlite3"
    with PaperShortStore(path, SOURCE, run_id="fixture") as store:
        for timestamp in range(START, CRASH_DAY + 300, 300):
            price = "100" if timestamp < START + 8 * DAY else "90"
            store.process_cycle(timestamp, bars(timestamp, price))
        assert store.status()["signal"]["label"] == "CRASH"
        assert not store.status()["positions"]
    return path


@pytest.fixture
def book(tmp_path, seeded_database):
    path = tmp_path / "isolated-paper.sqlite3"
    shutil.copyfile(seeded_database, path)
    with PaperShortStore(path, SOURCE, run_id="fixture") as store:
        yield store


def enter(book):
    return book.process_cycle(CRASH_DAY + 300, bars(CRASH_DAY + 300))


def test_fixed_notional_cash_and_three_accounting_views(book):
    receipt = enter(book)
    account = receipt["account"]
    assert account["aggregate_reference_entry_notional"] == "1500"
    assert D(account["gross_cash"]) == 2000
    assert D(account["cost_overlay"]) == D("6.75")
    assert D(account["adverse_funding"]) == pytest.approx(D("1.5") / 288)
    assert D(account["gross_equity"]) == 2000
    assert D(account["cost_equity"]) == D("1993.25")
    assert D(account["funding_equity"]) == pytest.approx(
        2000 - D(account["cost_overlay"]) - D(account["adverse_funding"]),
        abs=D("1e-24"),
    )
    assert account["margin_requirement"] == account["liquidation"] == "NOT_MODELED"
    assert account["reserve_deployed"] == "0"
    assert {p["reference_entry_notional"] for p in receipt["positions"].values()} == {
        "750"
    }
    original = {a: p["quantity"] for a, p in receipt["positions"].items()}
    receipt = book.process_cycle(CRASH_DAY + 600, bars(CRASH_DAY + 600, "80"))
    assert original == {a: p["quantity"] for a, p in receipt["positions"].items()}
    assert len(book.read_events("FILL")) == 2


def test_current_future_volume_does_not_choose_fill(book):
    t = CRASH_DAY + 300
    current = Bar(t, "90", None, None, None, None, None)
    receipt = book.process_cycle(t, bars(t, btc=current))
    assert "BTC" in receipt["positions"]
    assert receipt["account"]["marks"]["BTC"]["age_seconds"] == 300
    assert receipt["status"] == "STALE_DATA"


def test_missing_previous_candle_delays_one_leg_without_reallocating(book):
    t = CRASH_DAY + 300
    missing = Bar(t, None, None, None, None, None, None)
    receipt = book.process_cycle(t, bars(t, btc=missing))
    assert set(receipt["positions"]) == {"ETH"}
    receipt = book.process_cycle(t + 300, bars(t + 300))
    assert set(receipt["positions"]) == {"ETH"}
    receipt = book.process_cycle(t + 600, bars(t + 600))
    assert set(receipt["positions"]) == {"BTC", "ETH"}
    assert {p["reference_entry_notional"] for p in receipt["positions"].values()} == {
        "750"
    }


def test_stale_marks_have_age_and_funding_debits_use_prior_close(book):
    enter(book)
    t = CRASH_DAY + 600
    missing = Bar(t, None, None, None, None, None, None)
    receipt = book.process_cycle(t, bars(t, btc=missing))
    assert receipt["account"]["maximum_open_mark_age_seconds"] == 300
    assert receipt["funding_sources"]["BTC"]["age_seconds"] == 0
    missing = Bar(t + 300, None, None, None, None, None, None)
    receipt = book.process_cycle(t + 300, bars(t + 300, btc=missing))
    assert receipt["account"]["maximum_open_mark_age_seconds"] == 600
    assert receipt["funding_sources"]["BTC"]["age_seconds"] == 300


def test_negative_equity_and_liability_never_clipped(book):
    enter(book)
    receipt = book.process_cycle(CRASH_DAY + 600, bars(CRASH_DAY + 600, "900"))
    account = receipt["account"]
    assert D(account["gross_equity"]) == -11500
    assert D(account["gross_unfunded_liability"]) == 11500
    assert D(account["funding_equity"]) < -11500
    assert D(account["gross_cash"]) == 2000
    assert account["policy_undeployed_reserve"] == "500"


def test_pause_and_irrevocable_close_only_and_no_same_episode_reentry(book):
    enter(book)
    t = CRASH_DAY + 600
    receipt = book.process_cycle(t, bars(t), control="CLOSE_ONLY")
    assert receipt["status"] == "EXIT_PENDING"
    assert len(receipt["positions"]) == 2
    t += 300
    missing = Bar(t, None, None, None, None, None, None)
    book.process_cycle(t, bars(t, btc=missing))
    receipt = book.process_cycle(t + 300, bars(t + 300))
    assert set(receipt["positions"]) == {"BTC"}
    receipt = book.process_cycle(t + 600, bars(t + 600, "100"))
    assert not receipt["positions"]
    assert receipt["account"]["active_positions"] == 0
    receipt = book.process_cycle(t + 900, bars(t + 900))
    assert not receipt["positions"]
    assert len(book.read_events("FILL")) == 4
    assert all(
        f["exit_reason"] == "OPERATIONAL_CLOSE_ONLY"
        for f in book.read_events("FILL")
        if f["action"] == "EXIT"
    )


def test_pause_skips_but_does_not_consume_episode_entry(book):
    t = CRASH_DAY + 300
    receipt = book.process_cycle(t, bars(t), control="PAUSE")
    assert not receipt["positions"]
    assert {e["reason"] for e in receipt["events"]} == {"PAUSED"}
    receipt = book.process_cycle(t + 300, bars(t + 300))
    assert len(receipt["positions"]) == 2


def test_false_fresh_gate_does_not_exit_existing_position(book):
    enter(book)
    # Rebound modestly: 7d drawdown remains enough for CRASH but 24h decline
    # is now zero, so no new positions and no exit of owned liabilities.
    for t in range(CRASH_DAY + 600, CRASH_DAY + DAY + 300, 300):
        receipt = book.process_cycle(t, bars(t, "89"))
    assert receipt["signal"]["label"] == "CRASH"
    assert D(receipt["signal"]["return_1d"]) > D("-0.05")
    assert len(receipt["positions"]) == 2
    assert all(p["exit_requested_at"] is None for p in receipt["positions"].values())


def test_daily_exit_first_later_open_and_no_terminal_fake_close(book):
    enter(book)
    for t in range(CRASH_DAY + 600, CRASH_DAY + DAY + 300, 300):
        receipt = book.process_cycle(t, bars(t, "100"))
    assert receipt["signal"]["label"] != "CRASH"
    assert receipt["status"] == "EXIT_PENDING"
    assert len(receipt["positions"]) == 2
    receipt = book.process_cycle(
        CRASH_DAY + DAY + 300, bars(CRASH_DAY + DAY + 300, "101")
    )
    assert not receipt["positions"]
    exits = [f for f in book.read_events("FILL") if f["action"] == "EXIT"]
    assert {f["fill_timestamp"] - f["decision_timestamp"] for f in exits} == {300}


def test_duplicate_restart_and_conflict_do_not_duplicate_money(book):
    receipt = enter(book)
    counts = [
        book.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("paper_cycles", "paper_events")
    ]
    with PaperShortStore(book.path, SOURCE, run_id="fixture") as restarted:
        assert enter(restarted) == receipt
        assert restarted.status() == receipt
        with pytest.raises(ReconciliationRequired, match="conflicting"):
            restarted.process_cycle(CRASH_DAY + 300, bars(CRASH_DAY + 300, "99"))
    assert counts == [
        book.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("paper_cycles", "paper_events")
    ]
    assert book.status() == receipt


def test_concurrent_duplicate_single_transaction(book):
    def run(_):
        with PaperShortStore(book.path, SOURCE, run_id="fixture") as other:
            return enter(other)

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(run, range(3)))
    assert receipts[0] == receipts[1] == receipts[2]
    assert len(book.read_events("FILL")) == 2


def test_atomic_failure_rolls_back_state_and_evidence(book):
    before = book.status()
    book.connection.execute(
        "CREATE TRIGGER test_abort BEFORE INSERT ON paper_cycles BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        enter(book)
    assert book.status() == before
    assert not book.read_events("FILL")
    book.connection.execute("DROP TRIGGER test_abort")
    assert len(enter(book)["positions"]) == 2


def test_gap_out_of_order_source_identity_and_foreign_db_rejected(book, tmp_path):
    before = book.status()
    with pytest.raises(ReconciliationRequired, match="contiguous"):
        book.process_cycle(CRASH_DAY + 600, bars(CRASH_DAY + 600))
    assert book.status() == before
    with pytest.raises(ReconciliationRequired, match="identity"):
        PaperShortStore(book.path, SOURCE, run_id="different")
    with pytest.raises(ReconciliationRequired, match="identity"):
        PaperShortStore(book.path, PaperSource("SYNTHETIC", "b" * 64), run_id="fixture")
    path = tmp_path / "spot.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE spot_owner (balance TEXT)")
    db.execute("INSERT INTO spot_owner VALUES ('2000')")
    db.commit()
    db.close()
    original = path.read_bytes()
    with pytest.raises(ReconciliationRequired, match="not this isolated"):
        PaperShortStore(path, SOURCE, run_id="fixture")
    assert path.read_bytes() == original


def test_contract_metadata_never_grants_venue_or_live_authority():
    assert contract_model_gate(None)["status"] == "BLOCKED_UNKNOWN_METADATA"
    complete = {key: "declared" for key in REQUIRED_CONTRACT_METADATA}
    result = contract_model_gate(complete)
    assert result["status"] == "UNSUPPORTED_MODEL"
    assert result["live_dispatch_available"] is False
    with pytest.raises(ValueError):
        PaperSource("LIVE", "a" * 64)
    with pytest.raises(ValueError):
        PaperSource("FROZEN_JAN_JUNE_2026", "a" * 64)
    source = PaperSource("FROZEN_JAN_JUNE_2026", INPUT_SHA256)
    with pytest.raises(ValueError, match="Outside"):
        source.validate_time(1791504000)


def test_daily_exit_survives_renewed_crash_and_unknown(book):
    enter(book)
    # Daily label becomes UNKNOWN when the immediately preceding BTC endpoint
    # is stale; ETH remains unfillable until a later CRASH episode.
    for t in range(CRASH_DAY + 600, CRASH_DAY + DAY + 300, 300):
        btc = (
            Bar(t, None, None, None, None, None, None)
            if t >= CRASH_DAY + DAY - 900
            else None
        )
        eth = Bar(t, None, None, None, None, None, None)
        receipt = book.process_cycle(t, bars(t, "89", btc=btc, eth=eth))
    assert receipt["signal"]["label"] == "UNKNOWN"
    assert receipt["positions"]["ETH"]["exit_requested_at"] == CRASH_DAY + DAY
    # BTC returns to observed data. Three days later a valid 24h anchor exists.
    for t in range(CRASH_DAY + DAY + 300, CRASH_DAY + 3 * DAY + 300, 300):
        eth = Bar(t, None, None, None, None, None, None)
        receipt = book.process_cycle(t, bars(t, "88", eth=eth))
    assert receipt["signal"]["label"] == "CRASH"
    assert receipt["positions"]["ETH"]["episode"] == 1
    assert receipt["positions"]["ETH"]["exit_requested_at"] == CRASH_DAY + DAY
    # The first valid full candle is not eligible to execute; its next open is.
    t = CRASH_DAY + 3 * DAY + 300
    receipt = book.process_cycle(t, bars(t, "88"))
    assert "ETH" in receipt["positions"]
    receipt = book.process_cycle(t + 300, bars(t + 300, "87"))
    assert "ETH" not in receipt["positions"]
    fills = [
        e for e in receipt["events"] if e["kind"] == "FILL" and e["asset"] == "ETH"
    ]
    assert len(fills) == 1 and fills[0]["action"] == "EXIT"
    assert fills[0]["decision_timestamp"] == CRASH_DAY + DAY
