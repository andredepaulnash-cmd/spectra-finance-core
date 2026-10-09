"""Bounded mocked collector checks. No real exchange requests or strategies."""
import datetime as dt
import gzip
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "collect_public_ohlc.py"
ROOT = SOURCE.parents[2]
spec = importlib.util.spec_from_file_location("holdout_collector", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 9, 15, 30, tzinfo=UTC)
NOW = START + dt.timedelta(minutes=15)


def row(stamp, price="1"):
    return [int(stamp), price, price, price, price, price, "1", 1]


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.candidate = self.directory / "candidate.py"
        self.candidate.write_text("# frozen financial research candidate\n")
        self.seed = self.directory / "freeze"
        self.protocol = m.freeze(SOURCE.parent / "protocol.json", self.seed, ROOT,
                                 [("candidate", self.candidate.relative_to(ROOT))], now=START)
        self.protocol_path = self.seed / "frozen-protocol.json"
        self.output = self.directory / "capture"
        self.calls = []
        self.ohlc = {"error": [], "result": {"AAAUSD": [
            row(START.timestamp()-300), row(START.timestamp()),
            row(START.timestamp()+300), row(START.timestamp()+600),
            row(NOW.timestamp())], "last": int(NOW.timestamp()-300)}}

    def fetch(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint == "Time":
            value = {"error": [], "result": {"unixtime": int(NOW.timestamp())}}
        elif endpoint == "AssetPairs":
            value = {"error": [], "result": {
                "AAAUSD": {"aclass_base": "currency", "quote": "ZUSD", "status": "online", "altname": "AAAUSD"},
                "AAAEUR": {"aclass_base": "currency", "quote": "ZEUR", "status": "online", "altname": "AAAEUR"},
                "STOCKUSD": {"aclass_base": "tokenized_asset", "quote": "ZUSD", "status": "online", "altname": "STOCKUSD"},
                "CLOSEDUSD": {"aclass_base": "currency", "quote": "ZUSD", "status": "cancel_only", "altname": "CLOSEDUSD"}}}
        else:
            value = self.ohlc
        return 200, m.encoded(value), "Fri, 09 Oct 2026 15:45:00 GMT"

    def run_poll(self, **kwargs):
        options = dict(fetch=self.fetch, clock=lambda: NOW, sleeper=lambda seconds: None)
        options.update(kwargs)
        return m.poll(self.protocol_path, self.output, ROOT, **options)

    def db(self, query):
        with sqlite3.connect(self.output / "capture.sqlite3") as connection:
            return connection.execute(query).fetchall()

    def test_actual_freeze_excludes_prior_and_current_bars(self):
        status = self.run_poll()
        stamps = [item[0] for item in self.db("SELECT timestamp FROM bars ORDER BY timestamp")]
        self.assertEqual(stamps, [int(START.timestamp()+i*300) for i in range(3)])
        self.assertTrue(status["complete_universe_attempt"])
        self.assertFalse(status["strategy_evaluated"])
        self.assertEqual(status["stored_pair_count"], 1)
        self.assertEqual(m.verify(self.output)["integrity"], "PASS")

    def test_fractional_freeze_does_not_admit_partly_prior_candle(self):
        value = json.loads(self.protocol_path.read_bytes())
        value["start_utc"] = m.iso(START + dt.timedelta(seconds=1))
        self.protocol_path.write_bytes(m.encoded(value))
        self.run_poll()
        stamps = [item[0] for item in self.db("SELECT timestamp FROM bars ORDER BY timestamp")]
        self.assertEqual(stamps, [int(START.timestamp()+300), int(START.timestamp()+600)])

    def test_resume_deduplicates_and_reuses_cursor(self):
        self.run_poll()
        self.run_poll()
        self.assertEqual(len(self.db("SELECT timestamp FROM bars")), 3)
        self.assertEqual(self.calls[-1][1]["since"], int(NOW.timestamp()-300))
        self.assertEqual(m.verify(self.output)["integrity"], "PASS")

    def test_http_failure_keeps_raw_and_does_not_advance_cursor(self):
        def failed(endpoint, params):
            if endpoint == "OHLC":
                return 503, b'{"maintenance":true}', None
            return self.fetch(endpoint, params)
        status = self.run_poll(fetch=failed)
        self.assertEqual(status["failures_this_invocation"], 1)
        self.assertEqual(self.db("SELECT pair FROM cursors"), [])
        attempt = self.db("SELECT status,http_status,raw_sha256 FROM attempts WHERE endpoint='OHLC'")[0]
        self.assertEqual(attempt[:2], ("FAILED", 503))
        self.assertEqual(gzip.decompress((self.output/"raw"/(attempt[2]+".json.gz")).read_bytes()), b'{"maintenance":true}')
        self.assertEqual(m.verify(self.output)["integrity"], "PASS")

    def test_transport_timeout_is_failure_not_execution_budget(self):
        def failed(endpoint, params):
            if endpoint == "OHLC":
                raise TimeoutError("mock public network timeout")
            return self.fetch(endpoint, params)
        status = self.run_poll(fetch=failed)
        self.assertEqual(status["failures_this_invocation"], 1)
        self.assertNotIn("budget_exhausted", status)
        self.assertTrue(status["complete_universe_attempt"])
        self.assertEqual(self.db("SELECT status FROM attempts WHERE endpoint='OHLC'"), [("FAILED",)])

    def test_budget_records_incomplete_universe_and_no_fake_bars(self):
        status = self.run_poll(max_requests=2)
        self.assertTrue(status["budget_exhausted"])
        self.assertFalse(status["complete_universe_attempt"])
        self.assertEqual(status["attempted_pairs"], 0)
        self.assertEqual(status["stored_bar_count"], 0)

    def test_source_change_rejected_before_any_network(self):
        self.candidate.write_text("# modified source\n")
        with self.assertRaisesRegex(ValueError, "Frozen source hash mismatch"):
            self.run_poll()
        self.assertEqual(self.calls, [])

    def test_draft_rejected_before_network(self):
        self.protocol_path.write_bytes((SOURCE.parent/"protocol.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "FROZEN"):
            self.run_poll()
        self.assertEqual(self.calls, [])

    def test_unobserved_slots_remain_gaps(self):
        self.ohlc["result"]["AAAUSD"].pop(2)
        self.run_poll()
        observation = json.loads(self.db("SELECT observation FROM attempts WHERE endpoint='OHLC'")[0][0])
        self.assertEqual(observation["unobserved_grid_slots"], 1)
        self.assertEqual(len(self.db("SELECT timestamp FROM bars")), 2)
        self.assertIn("no-trade", observation["gap_interpretation"])

    def test_raw_corruption_fails_integrity(self):
        self.run_poll()
        capture = next((self.output/"raw").glob("*.json.gz"))
        capture.write_bytes(gzip.compress(b"corrupted"))
        with self.assertRaisesRegex(ValueError, "Raw response checksum mismatch"):
            m.verify(self.output)

    def test_orphan_raw_is_verified_without_admitting_it(self):
        self.run_poll()
        raw = b'{"orphan":true}'
        path = self.output/"raw"/(m.sha(raw)+".json.gz")
        path.write_bytes(gzip.compress(raw))
        self.assertEqual(m.verify(self.output)["orphan_raw_objects"], 1)
        self.assertEqual(len(self.db("SELECT timestamp FROM bars")), 3)
        path.write_bytes(gzip.compress(b"changed orphan"))
        with self.assertRaisesRegex(ValueError, "Raw response checksum mismatch"):
            m.verify(self.output)

    def test_point_in_time_universe_discovery_is_saved_for_final_gate(self):
        status = self.run_poll()
        observations = self.db("SELECT pair,observed_utc FROM universe_observations")
        self.assertEqual(observations, [("AAAUSD",m.iso(NOW))])
        self.assertTrue(status["evaluation_requires_first_observed_universe_gate"])
        self.assertEqual(status["universe_snapshot_received_utc"],m.iso(NOW))

    def test_committed_candle_conflict_preserves_old_row_and_cursor(self):
        self.run_poll()
        before = self.db("SELECT row_sha256 FROM bars ORDER BY timestamp")
        self.ohlc["result"]["AAAUSD"][-2][1] = "9"
        status = self.run_poll()
        self.assertEqual(status["failures_this_invocation"], 1)
        self.assertEqual(before, self.db("SELECT row_sha256 FROM bars ORDER BY timestamp"))
        self.assertIn("Previously committed candle changed", self.db("SELECT error FROM attempts ORDER BY id DESC LIMIT 1")[0][0])

    def test_interrupted_attempt_is_preserved_on_resume(self):
        store = m.Store(self.output, self.protocol_path.read_bytes())
        store.begin("OHLC", {"pair":"AAAUSD", "interval":5, "since":int(START.timestamp())})
        store.close()
        self.run_poll()
        self.assertEqual(self.db("SELECT status FROM attempts ORDER BY id LIMIT 1"), [("INTERRUPTED",)])

    def test_end_is_exclusive(self):
        value = json.loads(self.protocol_path.read_bytes())
        value["end_utc"] = value["evaluation_after_utc"] = m.iso(START+dt.timedelta(minutes=5))
        self.protocol_path.write_bytes(m.encoded(value))
        self.run_poll()
        self.assertEqual(self.db("SELECT timestamp FROM bars"), [(int(START.timestamp()),)])

    def test_empty_committed_window_does_not_invent_candle_or_regress(self):
        self.ohlc["result"]["AAAUSD"] = [row(NOW.timestamp())]
        self.ohlc["result"]["last"] = int(START.timestamp()-300)
        status = self.run_poll()
        self.assertEqual(status["stored_bar_count"], 0)
        self.assertEqual(status["failures_this_invocation"], 0)
        self.assertEqual(self.db("SELECT since FROM cursors"), [(int(START.timestamp()-300),)])

    def test_later_market_requests_use_receive_time_not_initial_time(self):
        progressed = [False]
        def delayed(endpoint, params):
            if endpoint == "OHLC":
                progressed[0] = True
                self.ohlc["result"]["AAAUSD"] = [row(NOW.timestamp()),row(NOW.timestamp()+300),row(NOW.timestamp()+600)]
                self.ohlc["result"]["last"] = int(NOW.timestamp()+300)
            return self.fetch(endpoint,params)
        status = self.run_poll(fetch=delayed,clock=lambda: NOW+dt.timedelta(minutes=10 if progressed[0] else 0))
        self.assertEqual(status["failures_this_invocation"],0)
        self.assertEqual(self.db("SELECT timestamp FROM bars ORDER BY timestamp"),[(int(NOW.timestamp()),),(int(NOW.timestamp()+300),)])

    def test_new_output_cannot_silently_change_freeze(self):
        self.run_poll()
        value = json.loads(self.protocol_path.read_bytes())
        value["start_utc"] = m.iso(START+dt.timedelta(seconds=1))
        self.protocol_path.write_bytes(m.encoded(value))
        with self.assertRaisesRegex(ValueError, "different frozen protocol"):
            self.run_poll()

    def test_private_endpoints_and_extra_parameters_are_impossible(self):
        for endpoint, params in [("Balance",{}),("AddOrder",{}),("Time",{"nonce":1}),("OHLC",{"API-Key":"secret"})]:
            with self.assertRaises(ValueError):
                m.public_url(endpoint,params)
        self.assertEqual(m.public_url("Time",{}),"https://api.kraken.com/0/public/Time")

    def test_wrong_pair_identity_is_not_admitted(self):
        self.ohlc["result"]["OTHERUSD"] = self.ohlc["result"].pop("AAAUSD")
        status = self.run_poll()
        self.assertEqual(status["failures_this_invocation"],1)
        self.assertEqual(status["stored_bar_count"],0)
        self.assertEqual(self.db("SELECT pair FROM cursors"),[])

    def test_freeze_cannot_overwrite(self):
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            m.freeze(SOURCE.parent/"protocol.json", self.seed, ROOT,
                     [("candidate",self.candidate.relative_to(ROOT))],now=START)

    def test_clock_skew_stops_before_market_outcomes(self):
        status = self.run_poll(clock=lambda: NOW+dt.timedelta(minutes=5))
        self.assertIn("Clock skew", status["capture_error"])
        self.assertEqual([call[0] for call in self.calls], ["Time"])


if __name__ == "__main__":
    unittest.main()
