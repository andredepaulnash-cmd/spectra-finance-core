"""Admission and provenance tests using explicitly constructed tiny sources."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


BTC = module("prepare_existing_btc_history")
PARK = module("prepare_saved_park_history")


class OwnedBTCPreparationTests(unittest.TestCase):
    def fixture(self, directory, rows_override=None, omit_member=False):
        pins = {}
        for month, name in enumerate(BTC.PINNED_ARCHIVES, 1):
            start = BTC.calendar.timegm((2026, month, 1, 0, 0, 0))
            rows = [[str(start), "100", "105", "95", "102", "3", "2"]]
            if month == 1 and rows_override is not None:
                rows = rows_override
            raw = "".join(",".join(row) + "\n" for row in rows).encode()
            path = directory / name
            with zipfile.ZipFile(path, "w") as archive:
                if not (omit_member and month == 1):
                    archive.writestr(BTC.MEMBER, raw)
                # This must never be parsed or treated as Bitcoin.
                archive.writestr("ohlcvt/WBTCUSD_5.csv", b"not a candle")
            payload = path.read_bytes()
            pins[name] = (hashlib.sha256(payload).hexdigest(), len(payload))
        return pins

    def test_calendar_missingness_and_completed_cutoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory)
            with patch.object(BTC, "PINNED_ARCHIVES", pins):
                receipt = BTC.prepare(directory, directory / "out")
            self.assertEqual(receipt["full_calendar_rows"], 52128)
            self.assertEqual(receipt["actual_observed_rows"], 6)
            self.assertEqual(receipt["missing_rows"], 52122)
            with (directory / "out" / receipt["tape_filename"]).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["completed_timestamp"], str(BTC.START + 300))
            self.assertEqual(rows[0]["close"], "102")
            self.assertEqual(rows[1]["present"], "0")
            self.assertTrue(all(rows[1][key] == "" for key in ("open", "high", "low", "close", "volume", "trades")))
            self.assertEqual(len(rows), 52128)

    def test_pin_mutation_refused_before_archive_parse(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory)
            path = directory / next(iter(pins))
            path.write_bytes(b"mutated")
            with patch.object(BTC, "PINNED_ARCHIVES", pins):
                with self.assertRaisesRegex(ValueError, "Unverified archive"):
                    BTC.prepare(directory, directory / "out")
            self.assertFalse((directory / "out").exists())

    def test_missing_exact_bitcoin_member_refuses_wrapped_substitute(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory, omit_member=True)
            with patch.object(BTC, "PINNED_ARCHIVES", pins):
                with self.assertRaisesRegex(ValueError, "exact benchmark member"):
                    BTC.prepare(directory, directory / "out")

    def test_duplicate_bar_refused(self):
        row = [str(BTC.START), "100", "105", "95", "102", "3", "2"]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory, [row, row])
            with patch.object(BTC, "PINNED_ARCHIVES", pins):
                with self.assertRaisesRegex(ValueError, "Nonincreasing"):
                    BTC.prepare(directory, directory / "out")

    def test_out_of_month_bar_refused(self):
        row = [str(BTC.START - 300), "100", "105", "95", "102", "3", "2"]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory, [row])
            with patch.object(BTC, "PINNED_ARCHIVES", pins):
                with self.assertRaisesRegex(ValueError, "Off-grid/out-of-month"):
                    BTC.prepare(directory, directory / "out")

    def test_nonfinite_and_incoherent_ohlc_refused(self):
        for row in ([str(BTC.START), "100", "NaN", "95", "102", "3", "2"],
                    [str(BTC.START), "100", "101", "95", "102", "3", "2"]):
            with self.subTest(row=row):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    pins = self.fixture(directory, [row])
                    with patch.object(BTC, "PINNED_ARCHIVES", pins):
                        with self.assertRaises(ValueError):
                            BTC.prepare(directory, directory / "out")

    def test_symlink_archive_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory)
            name = next(iter(pins))
            path = directory / name
            path.rename(directory / "real")
            path.symlink_to(directory / "real")
            with patch.object(BTC, "PINNED_ARCHIVES", pins):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    BTC.prepare(directory, directory / "out")


class SavedPARKPreparationTests(unittest.TestCase):
    def fixture(self, directory, change=None):
        times = [PARK.START + 300, *range(PARK.START + 3600, PARK.END, 3600)]
        rows = []
        for timestamp in times:
            rows.append(dict(zip(PARK.FIELDNAMES, (str(timestamp), "2000", "0", "0", "2000", "0", "0"))))
        if change:
            change(rows)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=PARK.FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        # A later observation is deliberately invalid; only the H1 prefix may
        # be interpreted. Opaque hash binding still covers all source bytes.
        writer.writerow(dict(zip(PARK.FIELDNAMES, (str(PARK.END + 3600), "not-a-value", "0", "0", "0", "0", "0"))))
        summary = {"cash": "1700", "committed_basis": "400", "marked_inventory": "300",
                   "equity": "2000", "parents": 1}
        inventory = {"timestamp": PARK.END, "case": "hard_100_park", "summary": summary,
                     "active_lots": [{"pair": "FICTIONAL-USD"}]}
        boundary = {"timestamp": PARK.END, "summary": summary}
        values = {"hard_100_park_equity.csv": buffer.getvalue().encode(),
                  "boundary_inventory_plain.json": json.dumps(inventory).encode(),
                  "boundary_summary.json": json.dumps(boundary).encode()}
        pins = {}
        for name, raw in values.items():
            (directory / name).write_bytes(raw)
            pins[name] = (hashlib.sha256(raw).hexdigest(), len(raw))
        return pins

    def test_exact_terminal_boundary_does_not_replace_initial(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory)
            with patch.object(PARK, "SOURCE_PINS", pins):
                receipt = PARK.prepare(directory, directory / "out")
            self.assertEqual(receipt["saved_hourly_rows"], 4344)
            self.assertEqual(receipt["total_observations"], 4345)
            with (directory / "out" / receipt["output_filename"]).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["cash"], "2000")
            self.assertEqual(rows[0]["marked_value"], "0")
            self.assertEqual(rows[-1]["marked_value"], "300")
            self.assertEqual(rows[-1]["timestamp"], str(PARK.END))
            self.assertEqual(rows[-1]["observation_source"], "EXACT_JUNE_BOUNDARY")

    def test_missing_saved_hour_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory, lambda rows: rows.pop(10))
            with patch.object(PARK, "SOURCE_PINS", pins):
                with self.assertRaisesRegex(ValueError, "calendar"):
                    PARK.prepare(directory, directory / "out")

    def test_cash_inventory_equity_nonconservation_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory, lambda rows: rows[10].update(equity="2001"))
            with patch.object(PARK, "SOURCE_PINS", pins):
                with self.assertRaisesRegex(ValueError, "reconciliation"):
                    PARK.prepare(directory, directory / "out")

    def test_source_pin_mutation_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory)
            (directory / "boundary_summary.json").write_text("{}")
            with patch.object(PARK, "SOURCE_PINS", pins):
                with self.assertRaisesRegex(ValueError, "Unverified saved PARK"):
                    PARK.prepare(directory, directory / "out")

    def test_symlink_saved_source_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pins = self.fixture(directory)
            path = directory / "boundary_summary.json"
            path.rename(directory / "real")
            path.symlink_to(directory / "real")
            with patch.object(PARK, "SOURCE_PINS", pins):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    PARK.prepare(directory, directory / "out")


if __name__ == "__main__":
    unittest.main()

