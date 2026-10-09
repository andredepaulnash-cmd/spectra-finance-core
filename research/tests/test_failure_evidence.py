"""Inject export failures without loading a market tape or strategy archive."""

import ast
import contextlib
import copy
from dataclasses import dataclass
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


RUNNER_PATH = Path(__file__).resolve().parents[1] / "parked_cash/run_comparison.py"
SPEC = importlib.util.spec_from_file_location("failure_evidence_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@dataclass
class Fill:
    order_id: int


class FailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.error = TimeoutError("original simulation budget reached")
        self.latest = {"fills": [Fill(7)], "positions": {}}
        self.dataset = SimpleNamespace(
            release=SimpleNamespace(events=[], audit_snapshot=lambda: {"pending": []}),
            ledger=SimpleNamespace(lots={}),
        )

    def save(self, sample_file=None):
        # Match the runner's actual except contract: helper then bare re-raise.
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                raise self.error
            except TimeoutError as error:
                runner.preserve_failure_evidence(
                    self.output, error, arm="BOTH", completed_frames=3,
                    simulation_seconds=1.0, latest=self.latest,
                    dataset=self.dataset, receipt={"original_fill_count": 0},
                    sample_file=sample_file or io.StringIO(),
                )
                raise

    def receipt(self):
        return json.loads((self.output / "failure_receipt.json").read_text())

    def test_corrupt_audit_cannot_hide_original_failure_or_receipt(self):
        def corrupt_snapshot():
            # The original receipt must exist BEFORE this validating export.
            receipt = self.receipt()
            self.assertEqual(receipt["exception"], "TimeoutError")
            self.assertEqual(receipt["reason"], str(self.error))
            raise ArithmeticError("owned source corrupted")

        self.dataset.release.audit_snapshot = corrupt_snapshot
        before = copy.deepcopy(self.latest)
        with self.assertRaises(TimeoutError) as caught:
            self.save()
        self.assertIs(caught.exception, self.error)
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "FAIL")
        self.assertTrue(receipt["no_performance_claim"])
        self.assertEqual(receipt["evidence_export_errors"][0]["export"], "partial_release")
        self.assertEqual(self.latest, before)
        self.assertFalse((self.output / "result.json").exists())

    def test_one_failed_payload_does_not_stop_other_exports(self):
        self.latest["fills"] = [object()]  # asdict must reject this export.
        with self.assertRaises(TimeoutError) as caught:
            self.save()
        self.assertIs(caught.exception, self.error)
        self.assertEqual(self.receipt()["evidence_export_errors"][0]["export"],
                         "partial_new_fills")
        self.assertTrue((self.output / "partial_release_events.json").exists())
        self.assertTrue((self.output / "partial_release.json").exists())
        self.assertFalse((self.output / "result.json").exists())

    def test_csv_close_failure_preserves_original_failure(self):
        def close_failure():
            self.assertTrue((self.output / "failure_receipt.json").exists())
            raise OSError("CSV flush failed")

        with self.assertRaises(TimeoutError) as caught:
            self.save(SimpleNamespace(close=close_failure))
        self.assertIs(caught.exception, self.error)
        self.assertEqual(self.receipt()["evidence_export_errors"][0]["export"],
                         "account_csv_close")

    def test_unwritable_receipt_never_replaces_original_failure(self):
        with patch.object(runner, "dump", side_effect=OSError("storage failed")):
            with self.assertRaises(TimeoutError) as caught:
                self.save()
        self.assertIs(caught.exception, self.error)
        self.assertFalse((self.output / "result.json").exists())

    def test_failed_amendment_preserves_first_valid_receipt(self):
        original_dump = runner.dump
        receipt_writes = 0

        def fail_amendment(path, value):
            nonlocal receipt_writes
            if path.name == "failure_receipt.pending":
                receipt_writes += 1
                if receipt_writes == 2:
                    path.write_text("incomplete amendment")
                    raise OSError("storage failed during amendment")
            return original_dump(path, value)

        with patch.object(runner, "dump", side_effect=fail_amendment):
            with self.assertRaises(TimeoutError) as caught:
                self.save()
        self.assertIs(caught.exception, self.error)
        self.assertEqual(self.receipt()["exception"], "TimeoutError")
        self.assertEqual(self.receipt()["reason"], str(self.error))
        self.assertFalse((self.output / "result.json").exists())

    def test_actual_runner_handler_rethrows_without_expiring_claims(self):
        tree = ast.parse(RUNNER_PATH.read_text())
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        handler = next(node.handlers[0] for node in ast.walk(main)
                       if isinstance(node, ast.Try) and node.handlers
                       and any(isinstance(call, ast.Call)
                               and isinstance(call.func, ast.Name)
                               and call.func.id == "preserve_failure_evidence"
                               for call in ast.walk(node.handlers[0])))
        self.assertIsInstance(handler.body[-1], ast.Raise)
        self.assertIsNone(handler.body[-1].exc)
        self.assertNotIn("finalize_window", ast.unparse(handler))


if __name__ == "__main__":
    unittest.main()
