"""Independent synthetic adversarial review of provenance and durable accounting."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from decimal import Inexact, localcontext
from pathlib import Path

import pytest

import spectra_core.crash_short_runtime as runtime
from spectra_core.crash_short_model import DAY
from spectra_core.crash_short_paper import PaperShortStore, ReconciliationRequired
from spectra_core.crash_short_signal import Bar
from tests.test_crash_short_paper import (
    CRASH_DAY,
    SOURCE,
    START,
    bars,
    book,
    seeded_database,
)


def _synthetic_line(price):
    return (
        json.dumps(
            {
                "timestamp": START,
                "bars": {
                    asset: {
                        field: (
                            price if field in ("open", "high", "low", "close") else "1"
                        )
                        for field in (
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "trades",
                        )
                    }
                    for asset in ("BTC", "ETH")
                },
            }
        )
        + "\n"
    )


def test_parser_consumes_the_same_bytes_as_provenance_hash(
    tmp_path, monkeypatch, capsys
):
    """A source replacement after the hash cannot become the claimed source."""
    path, state = tmp_path / "input.jsonl", tmp_path / "paper.sqlite3"
    original_bytes = _synthetic_line("100").encode()
    path.write_bytes(original_bytes)
    original_read = Path.read_bytes

    def replace_after_read(self):
        contents = original_read(self)
        if self == path:
            self.write_text(_synthetic_line("1"))
        return contents

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    result = runtime.main(
        [
            "--enable-paper",
            "--source",
            "synthetic",
            "--input",
            str(path),
            "--state",
            str(state),
            "--run-id",
            "review",
        ]
    )
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert (
        output["source"]["dataset_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    )
    with sqlite3.connect(state) as connection:
        persisted = json.loads(
            connection.execute("SELECT payload FROM paper_btc").fetchone()[0]
        )
    assert D(persisted["open"]) == 100


@pytest.mark.parametrize("damage", ["cash", "latest_receipt", "btc_history"])
def test_inconsistent_persisted_evidence_fails_closed_on_restart(tmp_path, damage):
    path = tmp_path / "paper.sqlite3"
    with PaperShortStore(path, SOURCE, run_id="review") as store:
        store.process_cycle(START, bars(START, "100"))
        if damage == "cash":
            payload = json.loads(
                store.connection.execute("SELECT payload FROM paper_state").fetchone()[
                    0
                ]
            )
            payload["gross_realized_pnl"] = "1000000"
            store.connection.execute(
                "UPDATE paper_state SET payload=?", (json.dumps(payload),)
            )
        elif damage == "latest_receipt":
            store.connection.execute("DELETE FROM paper_cycles")
        else:
            store.connection.execute("DELETE FROM paper_btc")
    with pytest.raises(ReconciliationRequired):
        with PaperShortStore(path, SOURCE, run_id="review") as restarted:
            restarted.process_cycle(START + 300, bars(START + 300, "100"))


def test_concurrent_conflicting_cycles_have_only_one_winner(book):
    timestamp = CRASH_DAY + 300

    def process(price):
        try:
            with PaperShortStore(book.path, SOURCE, run_id="fixture") as store:
                return store.process_cycle(timestamp, bars(timestamp, price))
        except ReconciliationRequired as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(process, ("80", "100")))
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert sum(isinstance(result, ReconciliationRequired) for result in results) == 1
    assert book.status() == winners[0]
    assert len(book.read_events("FILL")) == 2
    assert D(book.status()["account"]["cost_overlay"]) == D("6.75")


def test_capital_gate_uses_known_open_not_execution_bar_close(book):
    timestamp = CRASH_DAY + 300
    missing = Bar(timestamp, None, None, None, None, None, None)
    receipt = book.process_cycle(timestamp, bars(timestamp, btc=missing))
    assert set(receipt["positions"]) == {"ETH"}
    timestamp += 300
    book.process_cycle(timestamp, bars(timestamp))  # BTC previous candle still missing.
    timestamp += 300
    rebound = Bar(timestamp, "300", "300", "1", "1", "1", "1")
    receipt = book.process_cycle(timestamp, bars(timestamp, eth=rebound))
    assert set(receipt["positions"]) == {"ETH"}
    opportunity = next(
        event for event in receipt["events"] if event["kind"] == "ENTRY_OPPORTUNITY"
    )
    assert opportunity["reason"] == "GROSS_EQUITY_GATE"
    assert D(opportunity["gross_gate_equity"]) == pytest.approx(D("250"))
    assert D(receipt["account"]["gross_equity"]) > 2000
    timestamp += 300
    adverse_close = Bar(timestamp, "90", "900", "90", "900", "1", "1")
    receipt = book.process_cycle(timestamp, bars(timestamp, eth=adverse_close))
    assert set(receipt["positions"]) == {"BTC", "ETH"}
    assert D(receipt["account"]["gross_equity"]) < 0
    assert receipt["account"]["aggregate_reference_entry_notional"] == "1500"


def test_paper_money_context_is_independent_of_ambient_decimal_traps(book):
    timestamp = CRASH_DAY + 300
    with localcontext() as context:
        context.traps[Inexact] = True
        receipt = book.process_cycle(timestamp, bars(timestamp))
    assert len(receipt["positions"]) == 2
    assert D(receipt["account"]["cost_overlay"]) == D("6.75")


def test_pending_exit_survives_new_crash_episode_and_never_reenters_same_bar(book):
    timestamp = CRASH_DAY + 300
    book.process_cycle(timestamp, bars(timestamp))
    cease_at, return_at = CRASH_DAY + DAY, CRASH_DAY + 2 * DAY
    for timestamp in range(timestamp + 300, return_at, 300):
        missing = Bar(timestamp, None, None, None, None, None, None)
        price = "100" if timestamp < cease_at else "90"
        book.process_cycle(timestamp, bars(timestamp, price, eth=missing))
    receipt = book.process_cycle(return_at, bars(return_at))
    assert receipt["signal"]["label"] == "CRASH"
    assert receipt["episode"] == 2
    assert receipt["positions"]["ETH"]["exit_requested_at"] == cease_at
    receipt = book.process_cycle(return_at + 300, bars(return_at + 300))
    assert set(receipt["positions"]) == {"BTC"}
    exits = [
        event
        for event in receipt["events"]
        if event["kind"] == "FILL" and event["action"] == "EXIT"
    ]
    assert len(exits) == 1 and exits[0]["asset"] == "ETH"
    assert exits[0]["decision_timestamp"] == cease_at
    assert exits[0]["exit_reason"] == "DAILY_CRASH_CEASED"
    receipt = book.process_cycle(return_at + 600, bars(return_at + 600))
    assert set(receipt["positions"]) == {"BTC", "ETH"}
    assert receipt["positions"]["ETH"]["episode"] == 2


@pytest.mark.parametrize("reader", ["duplicate", "status"])
def test_corrupted_receipt_account_is_not_presented_as_verified_equity(
    tmp_path, reader
):
    path = tmp_path / "paper.sqlite3"
    with PaperShortStore(path, SOURCE, run_id="review") as store:
        receipt = store.process_cycle(START, bars(START, "100"))
        receipt["account"]["gross_equity"] = "1002000"
        store.connection.execute(
            "UPDATE paper_cycles SET receipt=?", (json.dumps(receipt),)
        )
        with pytest.raises(ReconciliationRequired):
            if reader == "duplicate":
                store.process_cycle(START, bars(START, "100"))
            else:
                runtime.read_paper_short_status(path)
