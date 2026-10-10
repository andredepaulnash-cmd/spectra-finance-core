"""Fictional collector pages only; no preserved capture payload is opened."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assemble_captures as assembly
from pins import FROZEN_PROTOCOL_SHA256

REPO_ROOT = Path(os.environ.get("SPECTRA_FROZEN_SOURCE_ROOT", Path(__file__).resolve().parents[3]))
UTC = dt.timezone.utc
AFTER_END = dt.datetime(2026, 11, 9, 0, 0, 1, tzinfo=UTC)
BEFORE_END = dt.datetime(2026, 10, 10, tzinfo=UTC)


def stamp(value):
    return int(assembly.instant(value).timestamp())


def candle(value, price="100"):
    return [stamp(value), price, "110", "90", price, "100", "500", 10]


def capture(*, day=9, rows=None, price="100", pair="0GUSD"):
    moment = dt.datetime(2026, 10, day, 15, 50, 0, tzinfo=UTC)
    text = lambda seconds: (moment + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    since = int(moment.timestamp()-48*3600)//300*300
    rows = rows or [candle("2026-10-08T15:40:00Z"), candle("2026-10-09T15:30:00Z"),
                    candle("2026-10-09T15:35:00Z"), candle("2026-10-09T15:40:00Z"),
                    candle("2026-10-09T15:45:00Z"), candle(text(0))]
    records = [
        dict(id=1, endpoint="Time", params={}, requested_utc=text(0), received_utc=text(1),
             status="SUCCESS", http_status=200, error=None,
             observation={"server_utc": text(1), "clock_skew_seconds": 0},
             universe_observations=[], stored_bars=[],
             fixture_payload={"error": [], "result": {"unixtime": int(moment.timestamp())+1}}),
        dict(id=2, endpoint="AssetPairs", params={"aclass_base": "currency"},
             requested_utc=text(1), received_utc=text(2), status="SUCCESS", http_status=200,
             error=None, observation={"online_usd_currency_pairs": 1},
             universe_observations=[{"pair": pair, "observed_utc": text(1)}], stored_bars=[],
             fixture_payload={"error": [], "result": {pair: {"quote": "ZUSD", "aclass_base": "currency", "status": "online", "altname": pair}}}),
        dict(id=3, endpoint="OHLC", params={"pair": pair, "interval": 5, "since": since},
             requested_utc=text(2), received_utc=text(3), status="SUCCESS", http_status=200,
             error=None, observation={}, universe_observations=[], stored_bars=[],
             fixture_payload={"error": [], "result": {pair: rows, "last": int(moment.timestamp())}}),
    ]
    return {"input_class": "constructed_fixture", "uses_market_observations": False, "records": records}


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="spectra_fictional_capture_")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build_fixture(self, fixtures, name="prepared"):
        return assembly.assemble_fixture_captures(fixtures, REPO_ROOT, self.root/name, fixture_now=AFTER_END)

    def assert_fixture_rejected(self, mutate, message=None):
        fixture = capture()
        mutate(fixture)
        with self.assertRaisesRegex(ValueError, message or ".+"):
            self.build_fixture([fixture])

    def test_real_assembly_embargo_precedes_all_file_reads(self):
        with patch.object(assembly, "utcnow", return_value=BEFORE_END), patch.object(assembly, "_read", side_effect=AssertionError("capture opened")), patch.object(assembly, "_freeze", side_effect=AssertionError("source opened")):
            with self.assertRaisesRegex(ValueError, "remains sealed"):
                assembly.assemble_captures([self.root/"actual_capture_must_not_open"], REPO_ROOT, self.root/"out")
        self.assertFalse((self.root/"out").exists())

    def test_real_prepared_validation_embargo_precedes_receipt_and_tape_reads(self):
        with patch.object(assembly, "utcnow", return_value=BEFORE_END), patch.object(assembly, "_read", side_effect=AssertionError("tape opened")):
            with self.assertRaisesRegex(ValueError, "remains sealed"):
                assembly.validate_prepared(REPO_ROOT, self.root/"must_not_open")

    def test_fixture_api_rejects_capture_paths_and_unlabelled_data(self):
        with self.assertRaisesRegex(ValueError, "constructed in-memory"):
            self.build_fixture([str(self.root/"no_capture_path")])
        self.assert_fixture_rejected(lambda f: f.update(uses_market_observations=True), "constructed in-memory")
        self.assert_fixture_rejected(lambda f: f.update(path="anything"), "constructed in-memory")

    def test_fixture_clock_cannot_precede_simulated_end(self):
        with self.assertRaisesRegex(ValueError, "simulated embargo"):
            assembly.assemble_fixture_captures([capture()], REPO_ROOT, self.root/"out", fixture_now=BEFORE_END)

    def test_no_public_now_option(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            assembly.main(["assemble", "--capture-dir", "x", "--repo-root", "x", "--output", "x", "--now", "2027-01-01"])

    def test_warmup_and_scoring_have_separate_causal_boundaries(self):
        import numpy as np
        first = capture()
        second = capture(day=10, rows=[candle("2026-10-09T15:25:00Z"), candle("2026-10-09T15:30:00Z"),
                                             candle("2026-10-09T15:40:00Z"), candle("2026-10-09T15:55:00Z"),
                                             candle("2026-10-10T15:50:00Z")])
        receipt = self.build_fixture([second, first])  # Source argument order is not a causal assumption.
        metadata = json.loads((self.root/"prepared/metadata.json").read_text())
        tape = np.load(self.root/"prepared/ohlcv.npy", mmap_mode="r")
        j = metadata["pairs"].index("0G-USD")
        row = lambda ts: (stamp(ts)-metadata["start"])//300
        self.assertEqual(list(tape.shape), receipt["shape"])
        self.assertEqual(tape.dtype, np.dtype("float64"))
        self.assertEqual(len(metadata["pairs"]), 619)
        self.assertEqual(receipt["warmup_frames"], 288)
        self.assertTrue(np.isfinite(tape[0,j]).all())
        self.assertTrue(np.isfinite(tape[row("2026-10-09T15:30:00Z"),j]).all())
        self.assertTrue(np.isnan(tape[287]).all())  # 15:35 candle crosses the actual 15:36:31 freeze.
        self.assertTrue(np.isnan(tape[row("2026-10-09T15:25:00Z"),j]).all())  # Not in the original first page.
        self.assertTrue(np.isnan(tape[288,j]).all())  # 15:40 timeline retained; status is not backdated.
        self.assertTrue(np.isnan(tape[row("2026-10-09T15:45:00Z"),j]).all())
        self.assertTrue(np.isfinite(tape[row("2026-10-09T15:55:00Z"),j]).all())
        self.assertTrue(np.isnan(tape[:,1:]).all())  # Unobserved fixed identities remain present.
        self.assertEqual(receipt["per_pair"]["0G-USD"]["warmup_rows"], 2)
        self.assertEqual(receipt["per_pair"]["0G-USD"]["evaluation_rows"], 1)
        self.assertEqual(receipt["counts"]["identical_overlap_rows"], 2)
        self.assertEqual(receipt["counts"]["pre_first_observation_evaluation_rows_excluded"], 2)
        self.assertEqual(receipt["counts"]["later_page_only_warmup_rows_excluded"], 1)
        self.assertEqual(receipt["counts"]["excluded_current_rows"], 2)
        self.assertFalse(receipt["uses_market_observations"])
        self.assertFalse(receipt["strategy_evaluated"])
        self.assertFalse(receipt["performance_claim"])
        del tape

    def test_conflicting_committed_overlap_is_rejected(self):
        first = capture()
        second = capture(day=10, rows=[candle("2026-10-09T15:30:00Z", "101"), candle("2026-10-10T15:50:00Z")])
        with self.assertRaisesRegex(ValueError, "Conflicting previously committed"):
            self.build_fixture([first, second])
        self.assertFalse((self.root/"prepared").exists())

    def test_conflicting_warmup_detected_even_when_input_order_reversed(self):
        first = capture()
        second = capture(day=10, rows=[candle("2026-10-09T15:30:00Z", "101"), candle("2026-10-10T15:50:00Z")])
        with self.assertRaisesRegex(ValueError, "Conflicting previously committed"):
            self.build_fixture([second, first])

    def test_duplicate_or_unordered_page_rows_rejected(self):
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"].insert(1, candle("2026-10-08T15:40:00Z")), "duplicated or unordered")
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"].reverse(), "duplicated or unordered")

    def test_nonfinite_or_invalid_ohlc_rejected(self):
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"][0].__setitem__(1,"NaN"), "finite")
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"][0].__setitem__(2,"50"), "high/low")
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"][0].__setitem__(7,"1.5"), "integer")
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"][0].__setitem__(slice(1,6),["1e-400"]*5), "underflow")
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"][0].__setitem__(7,2**53+1), "exact native")

    def test_misaligned_timestamp_and_wrong_interval_rejected(self):
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"]["0GUSD"][0].__setitem__(0,stamp("2026-10-08T15:40:01Z")), "aligned")
        self.assert_fixture_rejected(lambda f: f["records"][2]["params"].update(interval=1), "five-minute")
        self.assert_fixture_rejected(lambda f: f["records"][2]["params"].update(since=0), "bounded 48-hour")

    def test_api_identity_must_match_snapshot_without_guessed_alias(self):
        def mutate(f):
            p=f["records"][2]["fixture_payload"]["result"]
            p["OTHERUSD"]=p.pop("0GUSD")
        self.assert_fixture_rejected(mutate, "identity differs")
        self.assert_fixture_rejected(lambda f: f["records"][2]["params"].update(pair="OTHERUSD"), "captured five-minute")

    def test_request_and_observation_clock_provenance_rejected_when_false(self):
        self.assert_fixture_rejected(lambda f: f["records"][2].update(received_utc="2026-10-09T15:49:00Z"), "receipt time")
        self.assert_fixture_rejected(lambda f: f["records"][1]["universe_observations"][0].update(observed_utc="2026-10-09T15:49:00Z"), "outside request")
        self.assert_fixture_rejected(lambda f: f["records"][0]["observation"].update(clock_skew_seconds=91), "clock skew")
        self.assert_fixture_rejected(lambda f: f["records"][2]["fixture_payload"]["result"].update(last=stamp("2026-10-09T16:00:00Z")), "cursor exceeds")

    def test_failed_conflict_cannot_be_ignored(self):
        self.assert_fixture_rejected(lambda f: f["records"][2].update(status="FAILED", error="Previously committed candle changed; retain conflict and stop this pair"), "committed-candle conflict")

    def test_output_not_overwritten(self):
        (self.root/"prepared").mkdir()
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.build_fixture([capture()])

    def make_fictional_store(self, fixture, directory):
        """Create the exact collector SQLite/raw layout using fictional pages."""
        directory.mkdir()
        protocol=(REPO_ROOT/"research/holdout/frozen-protocol.json").read_bytes()
        (directory/"frozen-protocol.json").write_bytes(protocol)
        (directory/"freeze-receipt.json").write_bytes(assembly.encoded({"protocol_sha256":FROZEN_PROTOCOL_SHA256}))
        db=sqlite3.connect(directory/"capture.sqlite3")
        db.executescript("""CREATE TABLE attempts(id INTEGER PRIMARY KEY,endpoint TEXT,params TEXT,requested_utc TEXT,received_utc TEXT,status TEXT,http_status INTEGER,http_date TEXT,raw_sha256 TEXT,error TEXT,observation TEXT);CREATE TABLE bars(pair TEXT,timestamp INTEGER,row_json TEXT,row_sha256 TEXT,attempt_id INTEGER,PRIMARY KEY(pair,timestamp));CREATE TABLE universe_observations(pair TEXT,observed_utc TEXT,attempt_id INTEGER,PRIMARY KEY(pair,attempt_id));CREATE TABLE cursors(pair TEXT PRIMARY KEY,since INTEGER);CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);""")
        (directory/"raw").mkdir()
        for record in fixture["records"]:
            raw=assembly.encoded(record["fixture_payload"]);sha=hashlib.sha256(raw).hexdigest()
            (directory/"raw"/(sha+".json.gz")).write_bytes(gzip.compress(raw,mtime=0))
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(record["id"],record["endpoint"],assembly.encoded(record["params"]).decode(),record["requested_utc"],record["received_utc"],record["status"],record["http_status"],None,sha,record["error"],assembly.encoded(record["observation"]).decode()))
            for observed in record["universe_observations"]:
                db.execute("INSERT INTO universe_observations VALUES(?,?,?)",(observed["pair"],observed["observed_utc"],record["id"]))
            if record["endpoint"]=="OHLC":
                for row in record["fixture_payload"]["result"][record["params"]["pair"]][:-1]:
                    if row[0]>=stamp("2026-10-09T15:40:00Z") and row[0]+300<=stamp(record["received_utc"]):
                        encoded=assembly.encoded(row)
                        db.execute("INSERT INTO bars VALUES(?,?,?,?,?)",(record["params"]["pair"],row[0],encoded.decode(),hashlib.sha256(encoded).hexdigest(),record["id"]))
        db.commit();bars=db.execute("SELECT COUNT(*) FROM bars").fetchone()[0];pairs=db.execute("SELECT COUNT(DISTINCT pair) FROM bars").fetchone()[0];db.close()
        status=dict(protocol_sha256=FROZEN_PROTOCOL_SHA256,strategy_evaluated=False,financial_outcomes_printed=False,written_utc=fixture["records"][-1]["received_utc"],attempt_status_counts={"SUCCESS":len(fixture["records"])},stored_bar_count=bars,stored_pair_count=pairs,sqlite_sha256=assembly.digest(directory/"capture.sqlite3"))
        (directory/"status.json").write_bytes(assembly.encoded(status))

    def test_real_loader_hashes_fictional_store_and_validates_prepared(self):
        directory=self.root/"fictional_store";self.make_fictional_store(capture(),directory)
        with patch.object(assembly,"utcnow",return_value=AFTER_END):
            receipt=assembly.assemble_captures([directory],REPO_ROOT,self.root/"real_api_fixture")
            metadata,validated,frames=assembly.validate_prepared(REPO_ROOT,self.root/"real_api_fixture")
        self.assertEqual(validated["prepared_sha256"],receipt["prepared_sha256"])
        self.assertEqual(frames,receipt["shape"][0])
        self.assertEqual(len(metadata["pairs"]),619)
        self.assertEqual(receipt["source_hashes"]["captures"][0]["verified_stored_bars"],2)
        self.assertEqual(len(receipt["source_hashes"]["captures"][0]["raw_objects"]),3)
        # A forged receipt and metadata can agree internally; trusted immutable
        # pins must still reject them before any native evaluation.
        prepared=self.root/"real_api_fixture"
        meta=copy.deepcopy(metadata);meta["source_hashes"]["frozen_candidates"]["financial-spec"]="0"*64
        raw=assembly.encoded(meta);(prepared/"metadata.json").write_bytes(raw)
        forged=copy.deepcopy(receipt);forged["source_hashes"]=meta["source_hashes"]
        forged["prepared_sha256"]["metadata.json"]=hashlib.sha256(raw).hexdigest()
        (prepared/"assembly-receipt.json").write_bytes(assembly.encoded(forged))
        with patch.object(assembly,"utcnow",return_value=AFTER_END):
            with self.assertRaisesRegex(ValueError,"trusted frozen source pins"):
                assembly.validate_prepared(REPO_ROOT,prepared)

    def test_real_loader_rejects_fictional_raw_corruption_before_output(self):
        directory=self.root/"fictional_store";self.make_fictional_store(capture(),directory)
        raw=next((directory/"raw").glob("*.json.gz"));raw.write_bytes(gzip.compress(b"{}",mtime=0))
        with patch.object(assembly,"utcnow",return_value=AFTER_END):
            with self.assertRaisesRegex(ValueError,"checksum"):
                assembly.assemble_captures([directory],REPO_ROOT,self.root/"out")
        self.assertFalse((self.root/"out").exists())

    def test_real_loader_rejects_fictional_sqlite_corruption(self):
        directory=self.root/"fictional_store";self.make_fictional_store(capture(),directory)
        db=sqlite3.connect(directory/"capture.sqlite3");db.execute("UPDATE attempts SET error='tampered'");db.commit();db.close()
        with patch.object(assembly,"utcnow",return_value=AFTER_END):
            with self.assertRaisesRegex(ValueError,"SQLite snapshot checksum"):
                assembly.assemble_captures([directory],REPO_ROOT,self.root/"out")

    def refresh_fictional_sqlite_checksum(self,directory):
        status=json.loads((directory/"status.json").read_text())
        status["sqlite_sha256"]=assembly.digest(directory/"capture.sqlite3")
        (directory/"status.json").write_bytes(assembly.encoded(status))

    def test_internally_rehashed_sqlite_row_must_exist_in_its_exact_raw_page(self):
        directory=self.root/"fictional_store";self.make_fictional_store(capture(),directory)
        forged=candle("2026-10-09T15:40:00Z","101");raw=assembly.encoded(forged)
        db=sqlite3.connect(directory/"capture.sqlite3")
        db.execute("UPDATE bars SET row_json=?,row_sha256=? WHERE timestamp=?",(raw.decode(),hashlib.sha256(raw).hexdigest(),forged[0]));db.commit();db.close()
        self.refresh_fictional_sqlite_checksum(directory)
        with patch.object(assembly,"utcnow",return_value=AFTER_END):
            with self.assertRaisesRegex(ValueError,"exact referenced committed raw row"):
                assembly.assemble_captures([directory],REPO_ROOT,self.root/"out")

    def test_exact_raw_warmup_row_cannot_masquerade_as_sqlite_evaluation_row(self):
        directory=self.root/"fictional_store";self.make_fictional_store(capture(),directory)
        historical=candle("2026-10-09T15:30:00Z");raw=assembly.encoded(historical)
        db=sqlite3.connect(directory/"capture.sqlite3")
        db.execute("UPDATE bars SET timestamp=?,row_json=?,row_sha256=? WHERE timestamp=?",(historical[0],raw.decode(),hashlib.sha256(raw).hexdigest(),stamp("2026-10-09T15:40:00Z")));db.commit();db.close()
        self.refresh_fictional_sqlite_checksum(directory)
        with patch.object(assembly,"utcnow",return_value=AFTER_END):
            with self.assertRaisesRegex(ValueError,"source time bounds"):
                assembly.assemble_captures([directory],REPO_ROOT,self.root/"out")


if __name__ == "__main__":
    unittest.main()
