"""Adversarial v3 integration checks using synthetic data only.

The native fixture explicitly injects a deterministic research gate to isolate
execution/ownership mechanics. The separate rotation-policy tests exercise the
real causal history gate. No historical outcome or sealed holdout is read here.
"""
from collections import Counter
from dataclasses import dataclass, replace, asdict
from decimal import Decimal as D
from fractions import Fraction
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "research/danger_room/rotation_profitability_campaign_v3.py"
spec = importlib.util.spec_from_file_location("rotation_campaign_under_test", PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@dataclass(frozen=True)
class Order:
    order_id: int
    created_at: int = 300
    pair: str = "AAA-USD"
    kind: str = "EXIT"
    budget: D = D(0)
    position_id: int | None = 1
    depends_on_order_id: int | None = None


@dataclass(frozen=True)
class Fill:
    timestamp: int
    order_id: int
    pair: str
    kind: str
    position_id: int
    quantity: D
    price: D
    gross_quote: D
    fee_quote: D
    basis_released: D
    realized_pnl: D
    dependency_order_id: int | None = None


class SyntheticTracker:
    def __init__(self, pairs):
        self.timestamp = None
    def update(self, timestamp, rows):
        self.timestamp = timestamp
    def stale_gate(self, pair, opened_at):
        return True, {"allowed": True, "reason": "synthetic_injected_gate"}
    def daily_forecast(self, pair):
        return Fraction(1, 10), {"available": True, "synthetic_injected_gate": True}


class RotationStateTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        policy = SimpleNamespace(RotationTracker=SyntheticTracker,
            fee_hurdle=lambda *args: (True, {"allowed": True, "synthetic_injected_gate": True}))
        self.hook = module.RotationHooks(policy, mode="stale", pairs=["AAA-USD", "BBB-USD"],
            rows=[None] * 5, emit=self.events.append)
        self.position = SimpleNamespace(position_id=1, pair="AAA-USD", quantity=D(2),
            basis=D(20), opened_at=0)
        self.order = Order(1)
        self.cfg = SimpleNamespace(simulated_min_order_quote=D(5), simulated_base_step=D("0.00000001"))
        self.hook.update(0, 300)
        self.offer = self.hook.offer(self.position, D(8), timestamp=300, index=0,
            occupied=50, ranked_candidates=["BBB-USD"])

    def submit(self):
        self.hook.submit(self.order, self.position, self.offer)

    def fill(self):
        return Fill(600, 1, "AAA-USD", "EXIT", 1, D(2), D(8), D(16), D("0.064"), D(20), D("-4.064"))

    def confirm(self):
        self.hook.confirm_sale(self.fill(), owner_before={"PRODUCTIVE": D(10), "VCAP": D(10)},
            owner_after={"PRODUCTIVE": D("17.968"), "VCAP": D("17.968")},
            parked_before=D(7), parked_after=D(7))

    def test_disabled_source_is_identical(self):
        before = "native financial body"
        self.assertEqual(module.build_rotation_body(before, rotation=None), (before, []))

    def test_unknown_mode_and_missing_boundary_fail_closed(self):
        with self.assertRaises(ValueError):
            module.build_rotation_body("body", rotation="invented")
        with self.assertRaises(ValueError):
            module.build_rotation_body("body", rotation="stale")

    def test_armed_native_floor_vetoes_even_stale_gate(self):
        self.assertIsNone(self.hook.offer(self.position, D(8), timestamp=300, index=0,
            occupied=50, ranked_candidates=["BBB-USD"], armed_floor=True))
        self.assertEqual(self.hook.counts["ARMED_PROFIT_FLOOR_VETO"], 1)

    def test_gate_and_tag_do_not_release_cash_slot_or_entry(self):
        self.submit()
        self.assertEqual(self.hook.closed, [])
        self.assertEqual(self.hook.entry_links, {})
        event = self.events[-1]
        self.assertFalse(event["anticipated_cash_released"])
        self.assertFalse(event["anticipated_slot_released"])

    def test_order_and_parent_quantity_are_immutable(self):
        self.submit()
        self.assertFalse(self.hook.preflight(replace(self.order, pair="OTHER-USD"), self.position, 1, D(16), self.cfg)[0])
        changed = SimpleNamespace(**vars(self.position)); changed.quantity = D(1)
        self.assertFalse(self.hook.preflight(self.order, changed, 1, D(16), self.cfg)[0])
        self.assertFalse(self.hook.bypass(self.order, changed))

    def test_only_tagged_order_can_bypass_the_profit_floor(self):
        self.submit()
        self.assertTrue(self.hook.bypass(self.order, self.position))
        self.assertFalse(self.hook.bypass(replace(self.order, order_id=2), self.position))

    def test_only_immediate_next_actual_bar_and_minima_allowed(self):
        self.submit()
        self.assertTrue(self.hook.preflight(self.order, self.position, 1, D(16), self.cfg)[0])
        self.assertFalse(self.hook.preflight(self.order, self.position, 2, D(16), self.cfg)[0])
        self.assertFalse(self.hook.preflight(self.order, self.position, 1, D(4), self.cfg)[0])

    def test_profit_then_partial_then_rotation_then_recovery_priority(self):
        self.submit()
        pending = [Order(4, kind="RECOVERY"), self.order, Order(3, kind="PARTIAL_RECYCLE"), Order(2)]
        self.assertEqual([item.order_id for item in sorted(pending, key=self.hook.priority)], [2, 3, 1, 4])

    def test_rejection_keeps_parent_and_blocks_replayed_confirmation(self):
        self.submit()
        self.hook.reject(self.order, "absent_immediate_next_candle")
        self.assertEqual(self.hook.closed, [])
        self.assertFalse(self.hook.bypass(self.order, self.position))
        with self.assertRaises(ValueError):
            self.confirm()
        with self.assertRaises(ValueError):
            self.hook.reject(self.order, "absent_immediate_next_candle")

    def test_complete_sale_receipt_only_and_no_unrelated_park_release(self):
        self.submit()
        with self.assertRaises(ValueError):
            self.hook.confirm_sale(replace(self.fill(), quantity=D(1)), owner_before={}, owner_after={},
                parked_before=D(7), parked_after=D(7))
        with self.assertRaises(ArithmeticError):
            self.hook.confirm_sale(self.fill(), owner_before={}, owner_after={}, parked_before=D(7), parked_after=D(0))
        self.confirm()
        self.assertEqual(self.hook.tags[1]["status"], "CONFIRMED")
        with self.assertRaises(ValueError):
            self.confirm()

    def test_replacement_follows_confirmed_closure_and_later_open(self):
        self.submit(); self.confirm()
        self.assertEqual(self.hook.replacement_candidates(["BBB-USD"], 600), ["BBB-USD"])
        entry = Order(2, created_at=600, pair="BBB-USD", kind="ENTRY", budget=D("10.25"), position_id=None)
        self.hook.note_entry(entry)
        with self.assertRaises(ValueError):
            self.hook.confirm_entry(Fill(600, 2, "BBB-USD", "ENTRY", 2, D(1), D(10), D(10), D(".04"), D(0), D(0)))
        # The failed fixture confirmation must not consume a valid link.
        self.assertEqual(self.hook.tags[1]["replacement_status"], "SUBMITTED")

    def test_fee_forecast_identity_expires_when_current_guards_fail(self):
        self.offer["candidate"] = "BBB-USD"
        self.submit(); self.confirm()
        self.assertEqual(self.hook.replacement_candidates(["OTHER-USD"], 600), ["OTHER-USD"])
        self.assertEqual(self.hook.tags[1]["replacement_status"], "EXPIRED")
        self.assertEqual(self.hook.entry_links, {})

    def test_no_funded_proposal_expires_unfunded_intention(self):
        self.submit(); self.confirm()
        self.hook.replacement_candidates(["BBB-USD"], 600)
        self.hook.finish_frame(600)
        self.assertEqual(self.hook.tags[1]["replacement_status"], "EXPIRED")

    def test_fee_gate_requires_full_fleet_and_unique_candidate_claims(self):
        self.hook.mode = "fee_hurdle"
        self.assertIsNone(self.hook.offer(self.position, D(8), timestamp=300, index=0,
            occupied=49, ranked_candidates=["BBB-USD"]))
        first = self.hook.offer(self.position, D(8), timestamp=300, index=0,
            occupied=50, ranked_candidates=["BBB-USD"])
        self.assertEqual(first["candidate"], "BBB-USD")
        self.assertFalse(first["funded"])
        self.assertIsNone(self.hook.offer(self.position, D(8), timestamp=300, index=0,
            occupied=50, ranked_candidates=["BBB-USD"]))

    def test_tracking_cannot_skip_or_reuse_completed_frame(self):
        with self.assertRaises(ValueError):
            self.hook.update(0, 300)
        with self.assertRaises(ValueError):
            self.hook.update(2, 900)


class FrameRankingCacheTests(unittest.TestCase):
    def test_one_native_ranking_per_completed_frame(self):
        cache = module.FrameRankingCache()
        calls = []
        bars = {"AAA-USD": 1, "BBB-USD": 2, "CCC-USD": 3}
        def rank(pair):
            calls.append(pair)
            return (-bars[pair], pair)
        first = cache.ordered(0, bars, rank)
        for _ in range(50):
            self.assertEqual(cache.ordered(0, bars, rank), first)
        self.assertEqual(len(calls), len(bars))
        self.assertEqual(first, ("CCC-USD", "BBB-USD", "AAA-USD"))
        self.assertEqual(cache.builds, 1)

    def test_next_completed_frame_invalidates_identity_and_order(self):
        cache = module.FrameRankingCache()
        first = {"AAA-USD": 1, "BBB-USD": 2}
        self.assertEqual(cache.ordered(0, first, lambda pair: -first[pair]), ("BBB-USD", "AAA-USD"))
        second = {"AAA-USD": 4, "CCC-USD": 3}
        self.assertEqual(cache.ordered(1, second, lambda pair: -second[pair]), ("AAA-USD", "CCC-USD"))
        self.assertEqual(cache.builds, 2)
        with self.assertRaises(ValueError):
            cache.ordered(0, first, lambda pair: -first[pair])

    def test_pending_position_and_liquidity_filters_remain_current(self):
        cache = module.FrameRankingCache()
        bars = {"AAA-USD": 3, "BBB-USD": 2, "CCC-USD": 1}
        ordered = cache.ordered(0, bars, lambda pair: -bars[pair])
        held = {"AAA-USD"};pending = set()
        self.assertEqual([pair for pair in ordered if pair not in held | pending], ["BBB-USD", "CCC-USD"])
        pending.add("BBB-USD")
        self.assertEqual([pair for pair in ordered if pair not in held | pending], ["CCC-USD"])
        self.assertEqual(ordered, ("AAA-USD", "BBB-USD", "CCC-USD"))

    def test_future_mutation_never_enters_current_ranking(self):
        rows = [{"AAA-USD": 1, "BBB-USD": 2}, {"AAA-USD": 3, "BBB-USD": 1}]
        cache = module.FrameRankingCache()
        before = cache.ordered(0, rows[0], lambda pair: -rows[0][pair])
        rows[1] = {"FUTURE-USD": 10**100}
        self.assertEqual(cache.ordered(0, rows[0], lambda pair: -rows[0][pair]), before)
        self.assertEqual(before, ("BBB-USD", "AAA-USD"))


def run_native_fixture():
    """Actual preserved controller; fixture gates/inputs are explicitly synthetic."""
    import contextlib
    import csv
    import gzip
    import io
    import tempfile
    import numpy as np
    sys.path.insert(0, str(ROOT))
    from research.danger_room import rotation_policy, owned_cash
    vendor = ROOT / "research/holdout_replay/vendor"
    recovered = vendor / "Spectra_2000_Recovery_Recycling_Evidence"
    core = recovered / "recovered/original-plan/Spectra_Exit_Strategy_Evidence/Spectra_Exit_Strategy_Evidence/spectra-finance-core/spectra_core"
    manifest = ROOT / "research/danger_room/native_prerequisites/native_source_manifest.json"
    adapter = recovered / "baseline-recovery-20261001/adapter/sparse_adapter.py"
    bridge = vendor / "Spectra_One_Percent_Comparison_Evidence/one-percent-research-20261006/one_percent_test.py"
    inventory = module.verify_native(core, manifest, adapter, bridge)
    native = module.load_native(core, adapter, bridge, inventory)
    # Reuse these already byte-verified modules only inside this fixture. The
    # production CLI's fresh-process loader remains unmodified.
    module.load_native = lambda *args: native
    parent_spec = importlib.util.spec_from_file_location("rotation_fixture_v2", PATH.with_name("owned_profitability_campaign_v2.py"))
    parent = importlib.util.module_from_spec(parent_spec);sys.modules[parent_spec.name] = parent;parent_spec.loader.exec_module(parent)
    parent_body, _ = parent.build_campaign_body(native.GENERATED_SOURCE)
    parent_body, _ = parent.build_owned_body(parent_body)
    neutral_body, _ = module.build_campaign_body(native.GENERATED_SOURCE)
    neutral_body, _ = module.build_owned_body(neutral_body)
    neutral_body, changes = module.build_rotation_body(neutral_body, rotation=None)
    assert neutral_body == parent_body and changes == []
    active_body, _ = module.build_rotation_body(neutral_body, rotation="stale")
    compile(active_body, "rotation-synthetic-controller", "exec")
    assert 'rotation_orders' in active_body and 'rotation_floor_bypass' in active_body
    original_tracker = rotation_policy.RotationTracker
    original_hurdle = rotation_policy.fee_hurdle
    rows_cases = ["mixed_loss", "unrelated_park", "missing_sale_bar", "sale_capacity", "replacement_missing", "replacement_capacity", "fee_identity", "native_whole_priority", "native_partial_priority", "armed_floor", "native_alias_rejected"]
    completed = []
    for case in rows_cases:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp);prepared = tmp / "prepared";prepared.mkdir()
            pairs = [f"C{i:03}-USD" for i in range(52)]
            if case == "native_alias_rejected":
                pairs[0], pairs[1] = "DOGE-USD", "XDG-USD"
            prices = [100, 100, 90, 90, 80, 80, 80, 80]
            if case == "native_whole_priority":
                prices = [100, 100, 90, 120, 120, 120, 120, 120]
            elif case == "native_partial_priority":
                prices = [100, 100, 80, 89, 89, 89, 89, 89]
            elif case == "armed_floor":
                prices = [100, 100, 30, 72, 20, 20, 20, 20]
            rows = np.zeros((len(prices), len(pairs), 6), dtype="float64")
            for index, price in enumerate(prices):
                rows[index, :, :] = [price, price * 1.005, price * .995, price, 100000, 1000]
            rows[:, 50:, :] = [100, 100 * 1.005, 100 * .995, 100, 10000, 1000]
            if case == "native_partial_priority":
                rows[3, :50, :] = [80, 89.5, 79.5, 89, 100000, 1000]
            if case == "armed_floor":
                rows[3, :50, :] = [30, 72.5, 29.5, 72, 100000, 1000]
            if case == "unrelated_park":
                rows[2, 1, :] = [80, 80.5, 79.5, 80, 100000, 1000]
                rows[3, 1, :] = [80, 89.5, 79.5, 89, 100000, 1000]
                rows[4:, 1, :] = [89, 89.5, 88.5, 89, 100000, 1000]
            if case == "missing_sale_bar":
                rows[4, 0, :] = np.nan
            if case == "sale_capacity":
                rows[4, 0, 4] = .001
            if case == "replacement_missing":
                rows[5, 50, :] = np.nan
            if case == "replacement_capacity":
                rows[5, 50, 4] = .001
            if case == "fee_identity":
                rows[4, 50, :] = np.nan
            np.save(prepared / "ohlcv.npy", rows)
            (prepared / "metadata.json").write_text(json.dumps({"pairs": pairs,
                "start": 1767225600, "end": 1767225600 + len(prices) * 300, "interval_seconds": 300}))
            class FixtureTracker(SyntheticTracker):
                def __init__(self, pairs):
                    self.index = -1
                def update(self, timestamp, bars):
                    self.index += 1;self.timestamp = timestamp
                def stale_gate(self, pair, opened_at):
                    allowed = self.index == (4 if case == "armed_floor" else 3) and pair == "C000-USD"
                    return allowed, {"allowed": allowed, "reason": "synthetic_injected_gate"}
                def daily_forecast(self, pair):
                    if self.index != 3:
                        return None, {"available": False, "reason": "synthetic_injected_gate"}
                    return Fraction(-1, 10) if pair == "C000-USD" else Fraction(1, 10), {"available": True, "synthetic_injected_gate": True}
            rotation_policy.RotationTracker = FixtureTracker
            rotation_policy.fee_hurdle = lambda qty, close, old, new: (
                old < 0, {"allowed": old < 0, "reason": "synthetic_injected_gate"})
            module.verify_reference = lambda path: {"synthetic_fixture": True, "holdout_accessed": False}
            policy_paths = {name: PATH.with_name(name + ".py") for name in
                ("owned_cash", "module_coordination", "productive_sizing", "opportunity_selector", "market_regime", "rotation_policy")}
            policy_paths["parent_v2"] = PATH.with_name("owned_profitability_campaign_v2.py")
            policy_sources = {name: module.sha256(path) for name, path in policy_paths.items()}
            gate = tmp / "synthetic-admission.json"
            gate.write_text(json.dumps({"status": "SYNTHETIC_ROTATION_INTEGRATION_PASS",
                "research_adapter_sha256": module.sha256(PATH), "policy_sources": policy_sources,
                "rotation_protocol_sha256": module.ROTATION_PROTOCOL_SHA256,
                "neutral_v2_financial_body_identical": True, "fixture_only": True}))
            control = tmp / "runs/rotation_owned_control_174";control.mkdir(parents=True)
            control_result = control / "results.json"
            control_result.write_text(json.dumps({"research_adapter_sha256": module.sha256(PATH),
                "policy_sources": policy_sources, "fixture_only": True}))
            receipt = tmp / "synthetic-control-receipt.json"
            receipt.write_text(json.dumps({"passed": True, "full_cash_owner_adapter_verified": True,
                "strategy_imported": False, "arm": "rotation_owned_control_174",
                "input_array_sha256": module.REFERENCE_ARRAY_SHA256,
                "input_metadata_sha256": module.REFERENCE_METADATA_SHA256,
                "results_sha256": module.sha256(control_result), "fixture_only": True}))
            args = SimpleNamespace(prepared=prepared, native_core=core, native_manifest=manifest,
                native_adapter=adapter, native_bridge=bridge,
                protocol=PATH.with_name("profitability_protocol.json"),
                protocol_sha256="a10def9591d19bc8c775d136b5c845383d42778f274e824ad1d77af88552e374",
                owned_protocol=PATH.with_name("owned_cash_protocol.json"), sizing_protocol=None,
                profit_protocol=None, rotation_protocol=PATH.with_name("rotation_protocol.json"),
                owner_adapter_admission=gate, owner_adapter_admission_sha256=module.sha256(gate),
                matched_owned_control_receipt=receipt, matched_owned_control_receipt_sha256=module.sha256(receipt),
                arm="fee_hurdle_rotation" if case == "fee_identity" else "stale_inventory_rotation",
                output=tmp / "runs" / case)
            with contextlib.redirect_stdout(io.StringIO()):
                if case == "native_alias_rejected":
                    try:
                        module.run_campaign(args)
                    except ValueError as exc:
                        assert str(exc) == "duplicate normalized base asset in universe"
                    else:
                        raise AssertionError("native alias exposure was accepted")
                    completed.append(case);continue
                payload = module.run_campaign(args)
            with (args.output / "fills.csv").open() as stream:
                fills = list(csv.DictReader(stream))
            with gzip.open(args.output / "rotation_events.jsonl.gz", "rt") as stream:
                events = [json.loads(line) for line in stream]
            with gzip.open(args.output / "ownership_events.json.gz", "rt") as stream:
                native_events = json.load(stream)
            with gzip.open(args.output / "owner_transactions.jsonl.gz", "rt") as stream:
                owner_events = [json.loads(line) for line in stream]
            attempts = [event for event in events if event["event"] == "ROTATION_SUBMIT"]
            confirmed = [event for event in events if event["event"] == "ROTATION_CONFIRMED"]
            if case in {"native_whole_priority", "native_partial_priority", "armed_floor"}:
                assert not attempts, (case, events)
                if case == "native_whole_priority":
                    assert any(fill["kind"] == "EXIT" and fill["pair"] == "C000-USD" for fill in fills)
                elif case == "native_partial_priority":
                    assert any(fill["kind"] == "PARTIAL_RECYCLE" and fill["pair"] == "C000-USD" for fill in fills)
                else:
                    assert payload["rotation_counts"].get("ARMED_PROFIT_FLOOR_VETO", 0) > 0
                completed.append(case);continue
            assert len(attempts) == 1, (case, events)
            order_id = attempts[0]["order"]["order_id"]
            if case in {"missing_sale_bar", "sale_capacity"}:
                assert not confirmed and not any(int(fill["order_id"]) == order_id for fill in fills)
                assert any(event["event"] == "ROTATION_REJECTED" for event in events)
                assert any(lot["parent_id"] == str(attempts[0]["parent_id"]) for lot in json.loads((args.output / "owner_terminal.json").read_text())["lots"].values())
                completed.append(case);continue
            assert len(confirmed) == 1 and D(confirmed[0]["full_signed_realized_pnl"]) < 0
            full_event = next(event for event in native_events if event["event"] == "FULL_EXIT" and event["fill"]["order_id"] == order_id)
            assert {lot["source"] for lot in full_event["lots"]} == {"BASE_ENTRY", "VCAP_RECOVERY"}
            sale_event = next(event for event in owner_events if event["event"] == "SALE" and event["fill"]["order_id"] == order_id)
            assert sale_event["sale_kind"] == "WHOLE" and sale_event["deployable"]
            if case == "unrelated_park":
                assert D(sale_event["after"]["parked_cash"]) > 0
                assert not confirmed[0]["unrelated_parked_proceeds_released"]
            assert not any(lot["parent_id"] == str(attempts[0]["parent_id"]) for lot in json.loads((args.output / "owner_terminal.json").read_text())["lots"].values())
            replacement = [event for event in events if event["event"] == "ROTATION_REPLACEMENT_SUBMIT"]
            if case == "fee_identity":
                assert not replacement
                assert any(event["event"] == "ROTATION_REPLACEMENT_EXPIRED" for event in events)
            else:
                assert len(replacement) == 1
                assert replacement[0]["closed_at"] == confirmed[0]["fill"]["timestamp"]
                if case in {"replacement_missing", "replacement_capacity"}:
                    assert not any(event["event"] == "ROTATION_REPLACEMENT_CONFIRMED" for event in events), (case, events)
                    rejection = next(event for event in events if event["event"] == "ROTATION_REPLACEMENT_REJECTED")
                    assert payload["rotation_terminal"][0]["replacement_status"] == "REJECTED"
                    assert payload["rotation_terminal"][0]["replacement_terminal_reason"] == rejection["reason"]
                else:
                    refill = next(event for event in events if event["event"] == "ROTATION_REPLACEMENT_CONFIRMED")
                    assert refill["fill"]["timestamp"] > confirmed[0]["fill"]["timestamp"]
            assert all(fill["dependency_order_id"] in {"", "None"} for fill in fills)
            completed.append(case)
    rotation_policy.RotationTracker = original_tracker
    rotation_policy.fee_hurdle = original_hurdle
    print(json.dumps({"synthetic_native_cases": completed, "neutral_v2_financial_body_identical": True,
        "historical_outcomes_read": False, "holdout_accessed": False}))


class IsolatedNativeControllerTests(unittest.TestCase):
    def test_real_native_controller_synthetic_adversarial_paths(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--native-fixture"],
            cwd=ROOT, text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(receipt["synthetic_native_cases"]), 11)
        self.assertTrue(receipt["neutral_v2_financial_body_identical"])
        self.assertFalse(receipt["historical_outcomes_read"])


if __name__ == "__main__":
    if "--native-fixture" in sys.argv:
        run_native_fixture()
    else:
        unittest.main()
