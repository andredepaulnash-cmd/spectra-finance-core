"""Conservation, causal warmup, pin failures and real native fixture replay."""
from __future__ import annotations

from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import fixture
import pins
import replay

REPO_ROOT = Path(os.environ.get("SPECTRA_FRESH_FREEZE_ROOT", str(HERE.parents[1]))).resolve()
NATIVE_RECEIPT = Path(os.environ.get("SPECTRA_FRESH_NATIVE_RECEIPT", str(HERE / "vendor/source-recovery.receipt.json"))).resolve()


class FreezeAndBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_all_eleven_source_candidates_are_verified(self):
        frozen = pins.verify_freeze(REPO_ROOT)
        self.assertEqual(len(frozen["verified_candidates"]), 11)
        self.assertEqual(frozen["financial"]["initial_state"]["initial_cash_quote"], "2000")
        self.assertEqual(frozen["financial"]["initial_state"]["initial_inventory"], [])
        self.assertEqual(tuple(frozen["financial"]["comparison_arms"]), pins.ARMS)

    def test_mutated_financial_spec_is_rejected(self):
        frozen = pins.verify_freeze(REPO_ROOT)
        root = self.directory / "repo"
        for entry in [*frozen["protocol"]["candidates"], frozen["protocol"]["collector"]]:
            destination = root / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((REPO_ROOT / entry["path"]).read_bytes())
        shutil.copyfile(REPO_ROOT / "research/holdout/frozen-protocol.json", root / "research/holdout/frozen-protocol.json")
        path = root / "research/holdout/financial-spec.json"
        path.write_bytes(path.read_bytes().replace(b'"2000"', b'"2001"', 1))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            pins.verify_freeze(root)

    def test_native_member_tamper_is_rejected_before_import(self):
        frozen = pins.verify_freeze(REPO_ROOT)
        receipt = json.loads(NATIVE_RECEIPT.read_text())
        # Alter expected identity, never the shared recovered source file.
        receipt["members"][0]["original_member"] = "unfrozen_strategy.py"
        path = self.directory / "tampered_receipt.json"
        path.write_text(json.dumps(receipt))
        with patch.object(replay, "load_native") as load:
            with self.assertRaisesRegex(ValueError, "outside frozen allowlist"):
                replay.verify_native_sources(path, frozen)
            load.assert_not_called()

    def test_snapshot_contains_only_verified_source_bytes(self):
        frozen = pins.verify_freeze(REPO_ROOT)
        binding = replay.verify_native_sources(NATIVE_RECEIPT, frozen)
        staged, cloned = replay.isolated_source_snapshot(binding, frozen)
        for key, path in staged["verified"].items():
            self.assertEqual(path.read_bytes(), binding["payloads"][key])
        self.assertEqual(len(list((staged["core"] / "spectra_core").glob("*.py"))), 23)
        self.assertEqual(cloned["verified_payloads"], frozen["verified_payloads"])
        staged["_snapshot_guard"].cleanup()

    def test_fixture_rejects_embargo_before_tape_open(self):
        prepared = self.directory / "future"
        fixture.create_fixture(REPO_ROOT, prepared)
        metadata = json.loads((prepared / "metadata.json").read_text())
        metadata["end"] = 1794182400
        (prepared / "metadata.json").write_text(json.dumps(metadata))
        receipt = json.loads((prepared / "fixture-receipt.json").read_text())
        receipt["sha256"]["metadata.json"] = pins.digest(prepared / "metadata.json")
        (prepared / "fixture-receipt.json").write_text(json.dumps(receipt))
        with patch.object(replay, "digest", side_effect=AssertionError("Tape must not open")):
            with self.assertRaisesRegex(ValueError, "sealed prospective window"):
                replay.validate_fixture(prepared, pins.verify_freeze(REPO_ROOT))

    def test_nonfinite_or_boolean_timestamps_reject_before_tape_open(self):
        for value in (float("nan"), float("inf"), True, "1794182400"):
            with self.subTest(value=value):
                prepared = self.directory / ("bad" + str(len(list(self.directory.iterdir()))))
                fixture.create_fixture(REPO_ROOT, prepared)
                metadata = json.loads((prepared / "metadata.json").read_text())
                metadata["end"] = value
                (prepared / "metadata.json").write_text(json.dumps(metadata))
                receipt = json.loads((prepared / "fixture-receipt.json").read_text())
                receipt["sha256"]["metadata.json"] = pins.digest(prepared / "metadata.json")
                (prepared / "fixture-receipt.json").write_text(json.dumps(receipt))
                with patch.object(replay, "digest", side_effect=AssertionError("Tape must not open")):
                    with self.assertRaises(ValueError):
                        replay.validate_fixture(prepared, pins.verify_freeze(REPO_ROOT))

    def test_tape_bytes_tamper_rejects(self):
        prepared = self.directory / "tampered"
        fixture.create_fixture(REPO_ROOT, prepared)
        path = prepared / "ohlcv.npy"
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "tape bytes changed"):
            replay.validate_fixture(prepared, pins.verify_freeze(REPO_ROOT))

    def test_real_evaluation_rejects_before_any_prepared_capture_read(self):
        # Future terminal date test remains meaningful after the real window
        # closes without overriding the public CLI's clock or source freeze.
        frozen = pins.verify_freeze(REPO_ROOT)
        frozen["protocol"] = dict(frozen["protocol"], end_utc="2099-11-09T00:00:00Z")
        with patch.object(replay, "verify_freeze", return_value=frozen), patch.object(replay, "verify_source_archives") as archives:
            with self.assertRaises(PermissionError):
                replay.fixture_arm(REPO_ROOT, NATIVE_RECEIPT, self.directory / "not_read",
                    "OFF", self.directory / "not_created", prospective=True)
            archives.assert_not_called()
        self.assertFalse((self.directory / "not_created").exists())

    def test_source_boundary_drift_fails_closed(self):
        source = "        pending = exit_orders + partial_orders + recovery_orders + entry_orders"
        with self.assertRaisesRegex(ArithmeticError, "source boundary changed"):
            replay.instrument_fresh(source)

    def test_matrix_failure_preserves_completed_failed_and_unrun_arms(self):
        output = self.directory / "matrix_failure"
        def completed(command, **kwargs):
            arm = command[command.index("--arm")+1]
            if arm == "OFF":
                arm_output = output / arm
                arm_output.mkdir()
                (arm_output / "result.json").write_text(json.dumps(dict(status="PASS", arm=arm, process_id=123)))
                return subprocess.CompletedProcess(command, 0, "pass", "")
            return subprocess.CompletedProcess(command, 1, "", "original integrity failure")
        with patch.object(replay.subprocess, "run", side_effect=completed):
            with self.assertRaisesRegex(RuntimeError, "arm failed"):
                replay.fixture_matrix(REPO_ROOT, NATIVE_RECEIPT, self.directory / "unused", output)
        receipt = json.loads((output / "matrix.receipt.json").read_text())
        self.assertEqual(receipt["completed_arms"], ["OFF"])
        self.assertEqual(receipt["arm_states"]["SHARED_RAW"], "FAIL")
        self.assertEqual(receipt["arm_states"]["SPLIT_RAW"], "NOT_RUN")
        self.assertFalse(receipt["performance_claim"])
        self.assertEqual((output / "SHARED_RAW.log").read_text(), "original integrity failure")

    def test_matrix_timeout_preserves_incomplete_receipt_and_partial_log(self):
        output = self.directory / "matrix_timeout"
        with patch.object(replay.subprocess, "run", side_effect=subprocess.TimeoutExpired("native child", 1, output=b"partial stdout", stderr=b"original timeout")):
            with self.assertRaises(subprocess.TimeoutExpired):
                replay.fixture_matrix(REPO_ROOT, NATIVE_RECEIPT, self.directory / "unused", output)
        receipt = json.loads((output / "matrix.receipt.json").read_text())
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["failing_arm"], "OFF")
        self.assertEqual(receipt["arm_states"]["SPLIT_QUALITY"], "NOT_RUN")
        self.assertFalse(receipt["performance_claim"])
        self.assertEqual((output / "OFF.log").read_text(), "partial stdoutoriginal timeout")


class RealNativeFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.prepared = cls.directory / "fixture"
        fixture.create_fixture(REPO_ROOT, cls.prepared)
        cls.output = cls.directory / "matrix"
        cls.rows = replay.fixture_matrix(REPO_ROOT, NATIVE_RECEIPT, cls.prepared, cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_native_kernel_and_all_five_arms_use_exact_fresh_seed(self):
        self.assertEqual([r["arm"] for r in self.rows], list(pins.ARMS))
        for row in self.rows:
            with self.subTest(arm=row["arm"]):
                self.assertEqual(row["native_generated_sha256"], pins.NATIVE_STRATEGY_SHA256)
                self.assertEqual(row["initial_seed"]["cash"], "2000")
                self.assertEqual(row["initial_seed"]["ordinary"], "2000")
                self.assertEqual(row["initial_seed"]["parked"], "0")
                self.assertFalse(row["initial_seed"]["initialized_from_checkpoint"])
                self.assertEqual(row["initial_seed"]["positions"], 0)
                self.assertEqual(row["initial_seed"]["fills"], 0)
                self.assertEqual(row["initial_seed"]["warmup_frames"], 288)
                self.assertEqual(row["completed_evaluation_frames"], 256)
                self.assertFalse(row["performance_claim"])
                self.assertFalse(row["live_authority"])

    def test_native_entry_recovery_partial_and_whole_fills_exercised(self):
        for row in self.rows:
            for kind in ("ENTRY", "RECOVERY", "PARTIAL_RECYCLE", "EXIT"):
                self.assertGreater(row["fill_counts"].get(kind, 0), 0)

    def test_enabled_arms_commit_real_parked_source_buys(self):
        for row in self.rows:
            spent = D(row["source_release"]["parked_spent"])
            self.assertEqual(spent, 0) if row["arm"] == "OFF" else self.assertGreater(spent, 0)
            self.assertEqual(D(row["source_release"]["total_cash_minted"]), 0)
            self.assertEqual(D(row["source_release"]["reserved_parked"]), 0)

    def test_shared_and_split_source_caps_conserve_ownership(self):
        for row in self.rows:
            release = row["source_release"]
            for kind in ("ENTRY", "RECOVERY"):
                spent = D(release["parked_spent_by_kind"][kind])
                retired = D(release["parked_basis_retired_by_kind"][kind])
                owned = D(release["owned_parked_basis_by_kind"][kind])
                self.assertLessEqual(abs(spent-retired-owned), D("1e-18"))
                cap = D(50) if row["arm"].startswith("SPLIT") else D(100)
                self.assertLessEqual(D(release["peak_committed_by_kind"][kind]), cap + D("1e-18"))

    def test_every_frame_conserves_cash_and_marked_equity(self):
        for row in self.rows:
            accounts = json.loads((self.output / row["arm"] / "accounts.json").read_text())
            for account in accounts:
                self.assertLessEqual(abs(D(account["cash"])+D(account["basis"])-D(2000)-D(account["realized"])), D("1e-18"))
                self.assertEqual(D(account["equity"]), D(account["cash"])+D(account["marked"]))
                self.assertEqual(D(account["cash"]), D(account["ordinary"])+D(account["parked"]))
                self.assertGreaterEqual(D(account["ordinary"]), -D("1e-18"))

    def test_lot_quantities_basis_and_source_purpose_are_preserved(self):
        for row in self.rows:
            lots = json.loads((self.output / row["arm"] / "lots.json").read_text())
            active = {lot["lot_id"]:lot for lot in lots if D(lot["base_remaining"]) > 0}
            for source in row["source_release"]["active_source_lots"]:
                lot = active[source["lot_id"]]
                self.assertEqual(D(source["quantity"]), D(lot["base_remaining"]))
                self.assertEqual(D(source["total_basis"]), D(lot["cost_basis_remaining_quote"])+D(lot["entry_fees_remaining_quote"]))
                self.assertIn(source["purpose"], ("ENTRY", "RECOVERY"))

    def test_fees_charged_once_and_exit_floors_respected(self):
        for row in self.rows:
            fills = json.loads((self.output / row["arm"] / "fills.json").read_text())
            self.assertEqual(sum((D(fill["fee_quote"]) for fill in fills), D(0)), D(row["fees"]))
            for fill in fills:
                self.assertEqual(D(fill["fee_quote"]), D(fill["gross_quote"])*D(".004"))
                if fill["kind"] in ("EXIT", "PARTIAL_RECYCLE"):
                    self.assertGreaterEqual(D(fill["realized_pnl"]), D(fill["basis_released"])*D(".01"))

    def test_no_financial_observations_or_fills_during_warmup(self):
        metadata = json.loads((self.prepared / "metadata.json").read_text())
        first_close = metadata["start"] + 289*300
        for row in self.rows:
            accounts = json.loads((self.output / row["arm"] / "accounts.json").read_text())
            fills = json.loads((self.output / row["arm"] / "fills.json").read_text())
            self.assertEqual(accounts[0]["timestamp"], first_close)
            self.assertEqual(D(accounts[0]["cash"]), D(2000))
            self.assertEqual(accounts[0]["positions"], 0)
            self.assertTrue(all(fill["timestamp"] > first_close for fill in fills))

    def test_terminal_inventory_preserves_units_and_actual_mark_times(self):
        for row in self.rows:
            inventory = json.loads((self.output / row["arm"] / "terminal_inventory.json").read_text())
            self.assertEqual(sum((D(asset["marked_value"]) for asset in inventory), D(0)), D(row["ending_marked_inventory"]))
            for asset in inventory:
                self.assertGreater(D(asset["quantity"]), 0)
                self.assertEqual(D(asset["marked_value"]), D(asset["quantity"])*D(asset["mark"]))
                self.assertGreaterEqual(asset["mark_age_seconds"], 0)

    def test_modeled_slippage_is_separate_from_once_charged_fees(self):
        import numpy as np
        tape = np.load(self.prepared / "ohlcv.npy", mmap_mode="r")
        metadata = json.loads((self.prepared / "metadata.json").read_text())
        for row in self.rows:
            fills = json.loads((self.output / row["arm"] / "fills.json").read_text())
            slippage = D(0)
            for fill in fills:
                index = (fill["timestamp"]-metadata["start"])//300-1
                open_price = D(str(float(tape[index, metadata["pairs"].index(fill["pair"]), 0])))
                slippage += abs(D(fill["price"])-open_price)*D(fill["quantity"])
            self.assertEqual(slippage, D(row["modeled_adverse_slippage_quote"]))

    def test_quality_gate_never_uses_future_execution_bars(self):
        for row in self.rows:
            decisions = json.loads((self.output / row["arm"] / "quality_decisions.json").read_text())
            for decision in decisions:
                if decision["maximum_feature_index"] is not None:
                    self.assertLessEqual(decision["maximum_feature_index"], decision["decision_index"])
                    self.assertEqual(max(decision["anchor_indices"]), decision["decision_index"])

    def test_arm_interpreters_do_not_share_mutable_state(self):
        self.assertEqual(len({row["process_id"] for row in self.rows}), 5)
        output = self.directory / "repeat_off"
        command = [sys.executable, str(HERE / "replay.py"), "fixture-arm",
            "--repo-root", str(REPO_ROOT), "--native-receipt", str(NATIVE_RECEIPT),
            "--prepared", str(self.prepared), "--arm", "OFF", "--output", str(output)]
        subprocess.run(command, check=True, capture_output=True, timeout=150)
        for name in ("accounts.json", "fills.json", "lots.json"):
            self.assertEqual((output / name).read_bytes(), (self.output / "OFF" / name).read_bytes())

    def test_future_price_mutation_cannot_change_earlier_decisions(self):
        import numpy as np
        prepared = self.directory / "changed_future"
        shutil.copytree(self.prepared, prepared)
        rows = np.load(prepared / "ohlcv.npy")
        cutoff_index = 288 + 96
        rows[cutoff_index:, :60, :4] *= .55
        np.save(prepared / "ohlcv.npy", rows)
        receipt = json.loads((prepared / "fixture-receipt.json").read_text())
        receipt["sha256"]["ohlcv.npy"] = pins.digest(prepared / "ohlcv.npy")
        (prepared / "fixture-receipt.json").write_text(json.dumps(receipt))
        output = self.directory / "changed_off"
        subprocess.run([sys.executable, str(HERE / "replay.py"), "fixture-arm",
            "--repo-root", str(REPO_ROOT), "--native-receipt", str(NATIVE_RECEIPT),
            "--prepared", str(prepared), "--arm", "OFF", "--output", str(output)],
            check=True, capture_output=True, timeout=150)
        metadata = json.loads((prepared / "metadata.json").read_text())
        cutoff_close = metadata["start"] + cutoff_index*300
        for name in ("accounts.json", "fills.json"):
            original = json.loads((self.output / "OFF" / name).read_text())
            modified = json.loads((output / name).read_text())
            self.assertEqual([x for x in original if x["timestamp"] <= cutoff_close],
                             [x for x in modified if x["timestamp"] <= cutoff_close])


if __name__ == "__main__":
    unittest.main()
