"""Independent causal-boundary checks. No strategy execution or checkpoint load."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

FEATURE = Path(__file__).resolve().parents[1] / "quality_gate.py"
SPEC = importlib.util.spec_from_file_location("independently_tested_gate", FEATURE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
START = 1767225600
INDEX = 300


def oracle(tape, pair_index, kind, timestamp):
    """Independent scalar, exact-time interpretation of frozen gate doctrine."""
    elapsed = timestamp - START
    i = elapsed // 300 - 1
    if elapsed % 300 or i < 0 or i >= len(tape):
        return False
    indices = [i, i - 12, i - 288] if kind == "ENTRY" else list(range(i - 5, i + 1))
    if kind not in {"ENTRY", "RECOVERY"} or min(indices) < 0:
        return False
    for j in indices:
        row = tape[j, pair_index]
        if not all(np.isfinite(x) for x in row) or not all(x > 0 for x in row[:4]):
            return False
    if kind == "ENTRY":
        return bool(tape[i, pair_index, 3] > tape[i - 12, pair_index, 3] and
                    tape[i, pair_index, 3] > tape[i - 288, pair_index, 3])
    current_low = min(float(tape[j, pair_index, 2]) for j in range(i - 2, i + 1))
    previous_low = min(float(tape[j, pair_index, 2]) for j in range(i - 5, i - 2))
    return bool(tape[i, pair_index, 3] > tape[i - 3, pair_index, 3] and current_low > previous_low)


class CausalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.tape = np.empty((320, 2, 6), dtype=np.float64)
        self.tape[:] = [100, 102, 90, 100, 1000, 10]
        self.tape[INDEX, 0, :4] = [110, 112, 105, 110]
        self.tape[INDEX-2:INDEX+1, 0, 2] = [101, 102, 105]

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, tape=None):
        array = self.tape if tape is None else tape
        np.save(self.path / "ohlcv.npy", array)
        (self.path / "metadata.json").write_text(json.dumps(dict(
            start=START, interval_seconds=300, pairs=["TEST/USD", "OTHER/USD"])))
        return MODULE.CausalGate(self.path)

    def evaluate(self, kind, index=INDEX, tape=None, pair="TEST/USD"):
        return self.gate(tape).evaluate(pair, kind, START + (index+1)*300)

    def test_entry_positive_exact_anchors(self):
        result = self.evaluate("ENTRY")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["anchor_indices"], [INDEX, INDEX-12, INDEX-288])
        self.assertEqual(result["maximum_feature_index"], INDEX)

    def test_entry_each_horizon_has_strict_boundary(self):
        for anchor in (INDEX-12, INDEX-288):
            for value in (110, 120):
                with self.subTest(anchor=anchor, value=value):
                    tape = self.tape.copy()
                    tape[anchor, 0, 3] = value
                    self.assertFalse(self.evaluate("ENTRY", tape=tape)["eligible"])

    def test_entry_missing_anchor_cannot_use_neighbor(self):
        for anchor in (INDEX, INDEX-12, INDEX-288):
            with self.subTest(anchor=anchor):
                tape = self.tape.copy()
                tape[anchor, 0] = np.nan
                result = self.evaluate("ENTRY", tape=tape)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["reason"], "missing_actual_anchor_or_candle")

    def test_entry_missing_intermediate_not_an_extra_gate(self):
        tape = self.tape.copy()
        tape[INDEX-25, 0] = np.nan
        self.assertTrue(self.evaluate("ENTRY", tape=tape)["eligible"])

    def test_recovery_positive_at_any_five_minute_close(self):
        result = self.evaluate("RECOVERY")
        self.assertTrue(result["eligible"])
        self.assertNotEqual((START+(INDEX+1)*300) % 900, 0)
        self.assertEqual(result["anchor_indices"], list(range(INDEX-5, INDEX+1)))

    def test_recovery_missing_any_contiguous_actual_bar_denies(self):
        for missing in range(INDEX-5, INDEX+1):
            with self.subTest(missing=missing):
                tape = self.tape.copy()
                tape[missing, 0] = np.nan
                self.assertFalse(self.evaluate("RECOVERY", tape=tape)["eligible"])

    def test_recovery_close_boundary(self):
        for current in (100, 99):
            with self.subTest(current=current):
                tape = self.tape.copy()
                tape[INDEX, 0, 3] = current
                self.assertFalse(self.evaluate("RECOVERY", tape=tape)["eligible"])

    def test_recovery_low_boundary(self):
        for recent_low in (90, 89):
            with self.subTest(recent_low=recent_low):
                tape = self.tape.copy()
                tape[INDEX-1, 0, 2] = recent_low
                self.assertFalse(self.evaluate("RECOVERY", tape=tape)["eligible"])

    def test_insufficient_history(self):
        for kind, index in (("ENTRY", 287), ("RECOVERY", 4)):
            with self.subTest(kind=kind):
                self.assertFalse(self.evaluate(kind, index=index)["eligible"])

    def test_unrelated_asset_gaps_do_not_deny_candidate(self):
        self.tape[:, 1] = np.nan
        self.assertTrue(self.evaluate("ENTRY")["eligible"])
        self.assertTrue(self.evaluate("RECOVERY")["eligible"])

    def test_invalid_actual_fields(self):
        for field, invalid in ((0,0), (1,-1), (2,np.inf), (3,np.nan), (4,np.nan), (5,np.nan)):
            with self.subTest(field=field, invalid=invalid):
                tape = self.tape.copy()
                tape[INDEX, 0, field] = invalid
                self.assertFalse(self.evaluate("ENTRY", tape=tape)["eligible"])
                self.assertFalse(self.evaluate("RECOVERY", tape=tape)["eligible"])

    def test_zero_recorded_volume_is_not_unstated_extra_signal(self):
        self.tape[INDEX, 0, 4:] = 0
        self.assertTrue(self.evaluate("ENTRY")["eligible"])

    def test_exact_timestamp_grid_and_unknown_candidates(self):
        gate = self.gate()
        for pair, kind, stamp, reason in (
            ("TEST/USD", "ENTRY", START+(INDEX+1)*300+1, "outside_exact_close_grid"),
            ("UNKNOWN/USD", "ENTRY", START+(INDEX+1)*300, "unknown_pair"),
            ("TEST/USD", "EXIT", START+(INDEX+1)*300, "unsupported_purpose")):
            with self.subTest(reason=reason):
                result = gate.evaluate(pair, kind, stamp)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["reason"], reason)

    def test_truncated_prefix_same_decision_and_record(self):
        for kind in ("ENTRY", "RECOVERY"):
            with self.subTest(kind=kind):
                full = self.evaluate(kind)
                prefix = self.evaluate(kind, tape=self.tape[:INDEX+1].copy())
                self.assertEqual(full, prefix)

    def test_future_execution_candle_mutation_has_no_effect(self):
        for kind in ("ENTRY", "RECOVERY"):
            with self.subTest(kind=kind):
                before = self.evaluate(kind)
                tape = self.tape.copy()
                tape[INDEX+1:] = [1e12, 1e15, .001, 1e14, 1e20, 1e20]
                after = self.evaluate(kind, tape=tape)
                self.assertEqual(before, after)

    def test_scalar_oracle_on_deterministic_boundary_cases(self):
        for kind in ("ENTRY", "RECOVERY"):
            for i in (0,4,5,12,287,288,INDEX,319):
                stamp = START+(i+1)*300
                with self.subTest(kind=kind,index=i):
                    self.assertEqual(self.gate().eligible("TEST/USD",kind,stamp),
                                     oracle(self.tape,0,kind,stamp))

    def test_cached_decision_does_not_duplicate_receipts(self):
        gate = self.gate()
        stamp = START+(INDEX+1)*300
        a = gate.evaluate("TEST/USD","ENTRY",stamp)
        b = gate.evaluate("TEST/USD","ENTRY",stamp)
        self.assertEqual(a,b)
        self.assertEqual(len(gate.records),1)
        self.assertTrue(gate.summary()["maximum_feature_index_never_after_decision"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CausalGateTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt = dict(passed=result.wasSuccessful(), tests=result.testsRun,
        feature_sha256=hashlib.sha256(FEATURE.read_bytes()).hexdigest(),
        failures=[str(test) for test,_ in result.failures],
        errors=[str(test) for test,_ in result.errors],
        scope="Constructed exact-time causal gates; no historical strategy replay, fitted model or profitability claim.")
    Path(__file__).with_name("Independent_Causal_Gate_Test_Receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
