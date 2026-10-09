import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]/"prepare_request.py"
spec = importlib.util.spec_from_file_location("holdout_request", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = dt.datetime(2026,10,9,16,0,tzinfo=dt.timezone.utc)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.protocol = self.root/"frozen.json"
        self.protocol.write_text('{"status":"FROZEN"}\n')
        self.request = self.root/"2026-10-09.json"
        self.value = {"schema":1,"action":"public_ohlc_capture","capture_date_utc":"2026-10-09","requested_utc":NOW.isoformat(),"protocol_sha256":m.hashlib.sha256(self.protocol.read_bytes()).hexdigest(),"no_strategy_evaluation":True}

    def run_prepare(self):
        self.request.write_text(json.dumps(self.value))
        return m.prepare(self.request,self.protocol,self.root/"output",now=NOW)

    def test_valid_exact_protocol_receipt(self):
        result = self.run_prepare()
        self.assertFalse(result["capture_started"])
        self.assertEqual(result["protocol_sha256"],self.value["protocol_sha256"])
        self.assertTrue((self.root/"output"/"capture-request-receipt.json").exists())

    def test_wrong_protocol_hash_denied(self):
        self.value["protocol_sha256"]="0"*64
        with self.assertRaisesRegex(ValueError,"exact frozen"):
            self.run_prepare()

    def test_extra_authority_and_outcome_request_denied(self):
        self.value["trade_order"]="sell"
        with self.assertRaisesRegex(ValueError,"extra authority"):
            self.run_prepare()
        del self.value["trade_order"]
        self.value["no_strategy_evaluation"]=False
        with self.assertRaisesRegex(ValueError,"no strategy evaluation"):
            self.run_prepare()

    def test_stale_or_naive_dates_denied(self):
        self.value["requested_utc"]="2026-10-06T16:00:00Z"
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.value["requested_utc"]="2026-10-09T16:00:00"
        with self.assertRaisesRegex(ValueError,"explicit UTC"):
            self.run_prepare()

    def test_draft_protocol_denied(self):
        self.protocol.write_text('{"status":"DRAFT_NOT_STARTED"}\n')
        self.value["protocol_sha256"]=m.hashlib.sha256(self.protocol.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,"exact frozen"):
            self.run_prepare()


if __name__=="__main__":
    unittest.main()
