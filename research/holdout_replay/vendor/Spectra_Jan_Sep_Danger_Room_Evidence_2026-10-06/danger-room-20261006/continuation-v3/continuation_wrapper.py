"""Research-only exact-prefix continuous replay and passive boundary capture.

The preserved one-percent bridge remains unchanged. This wrapper changes its
input location and adds one observational hook after a completed frame's next
intents have been scheduled. A January-to-later uninterrupted tape carries all
live Python strategy state across June without reconstructing or resetting it.
The JSON checkpoint is a durable audit record, not a standalone restart API.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, fields, is_dataclass, replace
from decimal import Decimal, getcontext
from enum import Enum
import gzip
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile

import numpy as np

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parent / "restored/evidence"
BRIDGE = EVIDENCE / "one-percent-research-20261006/one_percent_test.py"
REFERENCE = EVIDENCE / "baseline-recovery-20261001/adapter/prepared"
JUNE_FRAMES = 52128
PARENT_WRAPPER = HERE.parent / "continuation-v2/continuation_wrapper.py"

# These are research arms, not edits to the original frozen bridge/protocol.
EXTRA_CASES = {
    "hard_174_park": dict(whole_target_bps="174", partial_target_bps="174",
                          hard_target=True, execution_floor_bps="174",
                          park_proceeds=True),
    "hard_100_no_partial": dict(whole_target_bps="100", partial_target_bps="100",
                                hard_target=True, execution_floor_bps="100",
                                park_proceeds=True),
}

# Persistent engine locals: each survives the frame boundary and may affect
# subsequent economic decisions, counters, or reproducibility witnesses.
ENGINE_STATE_NAMES = (
    "cash positions pending fills samples rejection_counts rejection_examples "
    "fingerprints profit_states qualifying_wins earned_dttp_until "
    "dttp_extended_positions ever_dttp_extended_positions earned_dttp_assets "
    "exit_signal_reasons volume_24h next_order_id next_position_id realized "
    "fees turnover peak_committed initialized_index successful_dependencies "
    "replacement_recycle_fills planner_exit_signals planner_recovery_signals "
    "hard_target_exit_signals earned_dttp_activation_events "
    "earned_dttp_deactivation_events earned_dttp_extension_decisions "
    "qualifying_hard_wins entry_basis_observed fee_rate slip exit_costs"
).split()
DATA_STATE_NAMES = (
    "enabled case case_name hard_target whole_target_bps partial_target_bps "
    "execution_floor_bps park_proceeds counts plan_context principal_cash "
    "profit_cash principal_reused profit_reused principal_released "
    "gross_basis_released partial_profit principal_returned orders marks "
    "last_seen current_index recovery_enabled conservative_capacity "
    "volume_history events"
).split()


def load_bridge():
    spec = importlib.util.spec_from_file_location("spectra_preserved_one_percent", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for case, settings in EXTRA_CASES.items():
        if case in module.CASES:
            raise AssertionError(f"Unexpected preserved case collision: {case}")
        module.CASES[case] = settings.copy()
    return module


def encode(value):
    """Unambiguous audit encoding, preserving exact decimal values and types."""
    if value is None or isinstance(value, (bool, int, str)) and not isinstance(value, Enum):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, Enum):
        return {"$enum": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": value.value}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite floating strategy state")
        return value
    if is_dataclass(value):
        return {"$dataclass": f"{type(value).__module__}.{type(value).__qualname__}",
                "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, Counter):
        return {"$counter": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, dict):
        return {"$mapping": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, deque):
        return {"$deque": [encode(x) for x in value], "maxlen": value.maxlen}
    if isinstance(value, tuple):
        return {"$tuple": [encode(x) for x in value]}
    if isinstance(value, set):
        return {"$set": sorted((encode(x) for x in value), key=lambda x: json.dumps(x, sort_keys=True))}
    if isinstance(value, list):
        return [encode(x) for x in value]
    raise TypeError(f"Unsupported checkpoint object {type(value)}")


def validate_tape(prepared, frames, boundary_frames):
    prepared = Path(prepared).resolve()
    meta = json.loads((prepared / "metadata.json").read_text())
    reference_meta = json.loads((REFERENCE / "metadata.json").read_text())
    tape = np.load(prepared / "ohlcv.npy", mmap_mode="r")
    reference = np.load(REFERENCE / "ohlcv.npy", mmap_mode="r")
    for key in ("start", "interval_seconds", "pairs", "symbols"):
        if meta[key] != reference_meta[key]:
            raise AssertionError(f"Changed reference universe/timing field: {key}")
    if tape.ndim != 3 or tuple(tape.shape[1:]) != tuple(reference.shape[1:]):
        raise AssertionError("Changed reference array geometry")
    actual_frames = min(frames, len(tape)) if frames else len(tape)
    if actual_frames < 3:
        raise AssertionError("At least three replay frames are required")
    if meta["end"] != meta["start"] + len(tape) * meta["interval_seconds"]:
        raise AssertionError("Metadata end does not match explicit tape shape")
    prefix_frames = min(actual_frames, len(reference))
    left_hash, right_hash = hashlib.sha256(), hashlib.sha256()
    # Check raw float bytes, including NaN encodings: stronger than equal_nan.
    for start in range(0, prefix_frames, 256):
        end = min(prefix_frames, start + 256)
        left = np.ascontiguousarray(tape[start:end]).tobytes()
        right = np.ascontiguousarray(reference[start:end]).tobytes()
        if left != right:
            raise AssertionError(f"January prefix differs at frames [{start},{end})")
        left_hash.update(left)
        right_hash.update(right)
    if actual_frames > len(reference) and prefix_frames != JUNE_FRAMES:
        raise AssertionError("Later continuation lacks the complete January–June prefix")
    return {"prepared": str(prepared), "reference_prepared": str(REFERENCE),
            "replayed_frames": actual_frames, "boundary_frames": boundary_frames,
            "identical_reference_prefix_frames": prefix_frames,
            "identical_reference_prefix_raw_float_sha256": left_hash.hexdigest(),
            "reference_prefix_raw_float_sha256": right_hash.hexdigest(),
            "array_sha256": hash_file(prepared / "ohlcv.npy"),
            "prepared_metadata_sha256": hash_file(prepared / "metadata.json"),
            "shape": list(tape.shape), "start": meta["start"], "end": meta["end"],
            "continuous_future_data": len(tape) > JUNE_FRAMES,
            "frozen_universe": True,
            "new_post_june_listings_excluded": True}


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_serialized_bytes(path, expected_sha256, *, compressed=False):
    """Bounded-memory complete gzip CRC and producer-byte identity verification."""
    opener = gzip.open if compressed else open
    readback_hash = hashlib.sha256()
    with opener(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            readback_hash.update(chunk)
    if readback_hash.hexdigest() != expected_sha256:
        raise IOError("Checkpoint serialized-content hash differs after full readback")
    return readback_hash.hexdigest()


def durable_json(path, payload, *, compressed=False):
    """Publish valid producer JSON after streaming full byte/CRC verification.

    Standard json.dump(...allow_nan=False) validates serialization. Matching
    its complete SHA256 on independent full readback proves identical valid JSON
    without constructing another decoded graph alongside the live simulation.
    """
    if not isinstance(payload, dict):
        raise TypeError("Checkpoint producer must supply a top-level dictionary")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    serialized_hash = hashlib.sha256()
    class TextHashSink:
        def __init__(self, stream):
            self.stream = stream
        def write(self, value):
            serialized_hash.update(value.encode("utf-8"))
            return self.stream.write(value)
    try:
        with os.fdopen(descriptor, "wb") as durable:
            if compressed:
                with gzip.GzipFile(filename=path.name, fileobj=durable, mode="wb", compresslevel=1, mtime=0) as zipped:
                    with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                        sink = TextHashSink(text)
                        json.dump(payload, sink, default=str, separators=(",", ":"), allow_nan=False)
                        sink.write("\n")
            else:
                encoder = json.JSONEncoder(default=str, indent=2, allow_nan=False)
                for chunk in encoder.iterencode(payload):
                    encoded = chunk.encode("utf-8")
                    serialized_hash.update(encoded)
                    durable.write(encoded)
                serialized_hash.update(b"\n")
                durable.write(b"\n")
            durable.flush()
            os.fsync(durable.fileno())
        verify_serialized_bytes(temporary, serialized_hash.hexdigest(), compressed=compressed)
        compressed_sha = hash_file(temporary)
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if hash_file(path) != compressed_sha:
            raise IOError("Published checkpoint bytes differ from validated temporary artifact")
        return {"sha256": compressed_sha, "uncompressed_json_sha256": serialized_hash.hexdigest(),
                "storage_revision": 3,
                "gzip_crc_and_content_hash_readback": compressed,
                "producer_valid_json": True, "producer_top_level_dict_verified": True,
                "full_serialized_bytes_readback": True, "second_decoded_graph_constructed": False,
                "atomic_publish": True, "file_and_directory_fsynced": True}
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def instrument(module):
    old = "        pending = exit_orders + partial_orders + recovery_orders + entry_orders"
    source = module.GENERATED_SOURCE
    if source.count(old) != 1:
        raise AssertionError("Unexpected research scheduling boundary")
    new = old + "\n        if index + 1 == dataset.boundary_frames:\n            dataset.capture_boundary(locals())"
    source = source.replace(old, new)
    namespace = dict(module.a.original.__dict__)
    namespace["FleetOrder"] = module.ResearchOrder
    exec(compile(source, str(HERE / "generated_continuous_body.py"), "exec"), namespace)
    class ContinuousSimulator(module.a.original.RotatingFleetSimulator):
        run = namespace["run"]
    return ContinuousSimulator, source, [{"before": old, "after": new,
                                          "purpose": "passive complete-frame boundary observation only"}]


def dataset_class(module, prepared, output, boundary_frames, witness):
    OriginalDataset = module.Dataset
    class ContinuousDataset(OriginalDataset):
        def __init__(self, *, case, enabled=True, frames=None, trace_path=None):
            # Redirect only the original explicit prepared-input boundary.
            # Each wrapper is a separate process; no restored file is edited.
            original_init = module.a.SparseDataset.__init__
            def redirected(instance, _original_path, **kwargs):
                return original_init(instance, prepared, **kwargs)
            module.a.SparseDataset.__init__ = redirected
            try:
                super().__init__(case=case, enabled=enabled and case != "hard_100_no_partial",
                                 frames=frames, trace_path=trace_path)
            finally:
                module.a.SparseDataset.__init__ = original_init
            self.boundary_frames = boundary_frames
            self.boundary_written = False
            self.manifest.update(continuation_wrapper_source_sha256=hash_file(__file__),
                                 continuous_tape_prefix_witness=witness,
                                 continuation_method="uninterrupted January-to-later replay; all causal engine locals retained",
                                 matched_arm_scope="each policy creates its own January–June state; no cross-arm state transplant",
                                 checkpoint_kind="complete economic/strategy audit state; standalone JSON restart is not implemented")

        def capture_boundary(self, state):
            if self.boundary_written:
                raise AssertionError("Boundary checkpoint would be duplicated")
            for name in ENGINE_STATE_NAMES:
                if name not in state:
                    raise AssertionError(f"Missing persistent engine state: {name}")
            for name in DATA_STATE_NAMES:
                if not hasattr(self, name):
                    raise AssertionError(f"Missing persistent dataset state: {name}")
            positions = state["positions"]
            committed = sum((p.basis for p in positions.values()), Decimal(0))
            marked = sum((p.quantity * self.marks[p.pair] for p in positions.values()), Decimal(0))
            cash, realized = state["cash"], state["realized"]
            if abs(cash + committed - state["cfg"].initial_cash - realized) > Decimal("1e-18"):
                raise ArithmeticError("Boundary account identity failed")
            if self.parked_cash() > cash + Decimal("1e-18"):
                raise ArithmeticError("Boundary parked cash exceeds total cash")
            for position in positions.values():
                self.check_parent(position)
            def bars_snapshot(bars):
                return {pair: {"timestamp": bar.timestamp,
                               "raw_ohlcvt": [float(x) for x in bar.raw],
                               "execution": bar.execution}
                        for pair, bar in bars.items()}
            context = getcontext()
            decision_open = self.timestamp(state["index"])
            if state["timestamp"] != decision_open + self.interval_seconds:
                raise AssertionError("Unexpected completed-close timestamp contract")
            checkpoint = {
                "schema": 1, "research_only": True,
                "method": "observational boundary inside uninterrupted strategy function",
                "warning": "Complete economic/causal state audit record. Standard hashlib exposes only a digest witness, not resumable internal state; this file is not a standalone restart API. Exact continuation is by unchanged full-tape replay.",
                "frame_index": state["index"], "completed_frames": state["index"] + 1,
                "timestamp": state["timestamp"], "decision_bar_open_timestamp": decision_open,
                "completed_close_timestamp": state["timestamp"],
                "next_execution_open_timestamp": state["timestamp"],
                "next_execution_recorded_fill_timestamp": state["timestamp"] + self.interval_seconds,
                "case": self.case_name, "case_settings": self.case,
                "config": asdict(state["cfg"]), "exit_policy": asdict(state["self"].exit_policy),
                "exit_strategy": asdict(state["self"].exit_strategy),
                "fingerprint_policy": asdict(state["self"].fingerprint_policy),
                "recovery_policy": asdict(state["self"].recovery_policy),
                "tranche_policy": asdict(state["self"].tranche_policy),
                "input_witness": witness,
                "decimal_context": {"prec": context.prec, "rounding": context.rounding,
                                    "Emin": context.Emin, "Emax": context.Emax},
                "summary": {"cash": cash, "committed_basis": committed,
                            "marked_inventory": marked, "equity": cash + marked,
                            "realized_pnl": realized, "unrealized_pnl": marked - committed,
                            "fees": state["fees"], "turnover": state["turnover"],
                            "parents": len(positions), "pending_orders": len(state["pending"]),
                            "parked_cash": self.parked_cash(),
                            "deployable_cash": cash - self.parked_cash(),
                            "open_manifests": len(self.store.active)},
                "engine_state": {name: encode(state[name]) for name in ENGINE_STATE_NAMES},
                "decision_digest_sha256": state["decision_digest"].hexdigest(),
                "dataset_state": {name: encode(getattr(self, name)) for name in DATA_STATE_NAMES},
                "ledger": {"lots": encode(self.ledger.lots),
                           "by_position": encode(self.ledger.by_position),
                           "parents": encode(self.ledger.parents)},
                "governor_provenance": encode(self.governor.provenance),
                "store": {"active": encode(self.store.active),
                          "manifests": encode(self.store.manifests),
                          "completions": encode(self.store.completions)},
                "current_bars": bars_snapshot(self.current_bars),
                "previous_bars": bars_snapshot(self.previous_bars),
            }
            path = Path(output) / "boundary_state.json.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            storage_validation = durable_json(path, checkpoint, compressed=True)
            summary = {"checkpoint": path.name, "checkpoint_sha256": storage_validation["sha256"],
                       "storage_validation": storage_validation,
                       "timestamp": checkpoint["timestamp"], "summary": checkpoint["summary"],
                       "decision_digest_sha256": checkpoint["decision_digest_sha256"]}
            durable_json(Path(output) / "boundary_summary.json", summary)
            plain_inventory = {"timestamp": state["timestamp"], "case": self.case_name,
                               "summary": checkpoint["summary"],
                               "parents": [asdict(p) for p in positions.values()],
                               "active_lots": [asdict(lot) for lot in self.ledger.lots.values()
                                               if lot.base_remaining > Decimal(0)]}
            durable_json(Path(output) / "boundary_inventory_plain.json", plain_inventory)
            self.boundary_written = True
    return ContinuousDataset


def freeze_protocol(module, allow_change=False):
    protocol = json.loads((EVIDENCE / "one-percent-research-20261006/protocol.json").read_text())
    protocol["parent_protocol_sha256"] = hash_file(EVIDENCE / "one-percent-research-20261006/protocol.json")
    protocol["research_control_additions"] = list(EXTRA_CASES)
    for case, settings in EXTRA_CASES.items():
        protocol["cases"][case] = {
            "case_settings": settings,
            "profit_policy": asdict(replace(module.a.VCAPProfitExitPolicy(),
                                             target_net_profit_bps=Decimal(settings["whole_target_bps"]))),
            "exit_strategy": asdict(replace(module.a.original.FleetExitStrategy(),
                                             mode=module.a.original.FleetExitMode.HARD_TARGET)),
            "recycle_enabled": case != "hard_100_no_partial",
        }
    protocol["source_hashes"]["continuation_wrapper"] = hash_file(__file__)
    protocol["checkpoint_storage_revision"] = {
        "revision": 3,
        "parent_wrapper_sha256": hash_file(PARENT_WRAPPER),
        "parent_protocol_sha256": hash_file(PARENT_WRAPPER.parent / "protocol.json"),
        "economic_strategy_change": False,
        "reason": "Full reuse continuation was killed at June checkpoint under the memory limit while v2 built a second decoded JSON graph. Failed v2 evidence remains unchanged; v3 changes only checkpoint verification memory use.",
        "write_contract": "Standard json.dump producer with allow_nan=False and known top-level dictionary; temporary gzip file; close compression; flush/fsync; full streaming gzip CRC and serialized-content SHA256 equality; no second decoded graph; atomic rename; fsync directory; recheck published bytes. Summary and plain inventory use the same producer-byte identity contract.",
        "validation": "Short actual original-v2/v3 strategy equivalence and valid/truncated/altered checkpoint fault checks required before full continuation. June original economic/ownership/intent/decision equality and full independent audits remain mandatory. No economic strategy change.",
    }
    _, observer_source, observer_changes = instrument(module)
    protocol["continuation_observation_contract"] = {
        "changes": observer_changes,
        "generated_continuous_body_sha256": hashlib.sha256(observer_source.encode()).hexdigest(),
        "strategy_change": False,
    }
    protocol["comparison_scope"] = {
        "primary": "Every arm starts with original $2000 on January1, builds its own June terminal state, then continues on the same verified continuous tape.",
        "secondary_same_state": "Not implemented. Any future policy switch applied to hard100park June state must be labeled as a same-state continuation intervention and not compared as an end-to-end arm.",
        "DR-A": "hard_100_park", "DR-B": "hard_174_park", "DR-C": "hard_100_reuse",
        "DR-D": "hard_100_no_partial: strict100 whole exits and recovery buys retained, exact recovery recycling disabled; parking has no effect because no partial proceeds exist.",
        "historical_reference": "Original archived no-partial DTTP+174 result is a historical reference, not DR-D.",
        "DR-E": "Not authorized or implemented; NaturalizedBTD rules remain unfrozen.",
    }
    path = HERE / "protocol.json"
    content = json.dumps(protocol, default=str, indent=2) + "\n"
    if path.exists():
        if path.read_text() == content:
            return path
        if not allow_change:
            raise AssertionError("Preregistered protocol changed; use explicit --freeze-only before any runs")
    temporary = HERE / "protocol.json.tmp"
    temporary.write_text(content)
    temporary.replace(path)
    return path


def smoke_equivalence(module, prepared, output, frames, boundary_frames, witness):
    baseline_data = module.Dataset(case="hard_100_park", frames=frames)
    cfg = replace(module.a.original.FleetConfig(), initial_cash=Decimal(2000))
    kwargs = dict(exit_policy=replace(module.a.VCAPProfitExitPolicy(), target_net_profit_bps=Decimal(100)),
                  exit_strategy=replace(module.a.original.FleetExitStrategy(), mode=module.a.original.FleetExitMode.HARD_TARGET))
    baseline = module.Simulator(cfg, **kwargs).run(baseline_data)
    Continuous, source, changes = instrument(module)
    Dataset = dataset_class(module, prepared, output, boundary_frames, witness)
    continued_data = Dataset(case="hard_100_park", frames=frames)
    continued = Continuous(cfg, **kwargs).run(continued_data)
    for name in ("cash", "positions", "fills", "samples", "rejections", "rejection_examples", "metrics", "fingerprint"):
        if getattr(baseline, name) != getattr(continued, name):
            raise AssertionError(f"Observational wrapper changed baseline {name}")
    if baseline_data.events != continued_data.events:
        raise AssertionError("Observational wrapper changed exact ownership events")
    if baseline_data.counts != continued_data.counts:
        raise AssertionError("Observational wrapper changed planner counts")
    return {"passed": True, "frames": frames,
            "boundary_frames": boundary_frames, "checkpoint_written": continued_data.boundary_written,
            "equal_fields": ["cash", "positions", "fills", "samples", "rejections", "rejection_examples", "metrics", "fingerprint", "events", "planner_counts"],
            "generated_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "passive_changes": changes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, default=REFERENCE)
    parser.add_argument("--frames", type=int)
    parser.add_argument("--case", default="hard_100_park",
                        choices=("hard_100_park", "hard_174_park", "hard_100_reuse", "hard_100_no_partial"))
    parser.add_argument("--name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--boundary-frames", type=int, default=JUNE_FRAMES)
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--smoke-equivalence", action="store_true")
    args = parser.parse_args()
    module = load_bridge()
    protocol_path = freeze_protocol(module, allow_change=args.freeze_only)
    if args.freeze_only:
        print(json.dumps({"protocol": str(protocol_path), "sha256": hash_file(protocol_path)}))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    witness = validate_tape(args.prepared, args.frames, args.boundary_frames)
    (args.output / "input_prefix_validation.json").write_text(json.dumps(witness, indent=2) + "\n")
    if args.smoke_equivalence:
        if not args.frames or args.frames > 288:
            raise AssertionError("Smoke equivalence is limited to at most one day")
        check = smoke_equivalence(module, args.prepared, args.output, args.frames, args.boundary_frames, witness)
        (args.output / "smoke_equivalence.json").write_text(json.dumps(check, indent=2) + "\n")
        print(json.dumps(check))
        return
    Simulator, source, changes = instrument(module)
    module.Dataset = dataset_class(module, args.prepared, args.output, args.boundary_frames, witness)
    module.Simulator = Simulator
    run_args = argparse.Namespace(output=str(args.output), case=args.case, disabled=False,
                                  frames=args.frames, name=args.name or args.case)
    module.run(run_args)
    (args.output / "generated_continuous_body.py").write_text(source)
    (args.output / "continuation_source_changes.json").write_text(json.dumps(changes, indent=2) + "\n")
    result_path = args.output / f"{run_args.name}.json"
    payload = json.loads(result_path.read_text())
    payload.update(continuation_wrapper_source_sha256=hash_file(__file__),
                   continuation_generated_source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                   continuation_protocol_sha256=hash_file(protocol_path),
                   continuation_protocol_path=str(protocol_path),
                   continuous_frame_prefix_validation=witness,
                   matched_control_scope="each arm builds its own January–June state; no reset or cross-arm transplant",
                   naturalized_btd_enabled=False,
                   boundary_checkpoint_available=(args.output / "boundary_summary.json").exists())
    result_path.write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    main()
