"""Research-only fresh-cash replay of the immutable prospective financial rules.

The fixture entrypoint accepts constructed tapes ending before the October 9
freeze. Prospective capture assembly/scoring is deliberately unavailable while
the holdout is collecting. Every arm runs in its own Python process.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from pins import ARMS, NATIVE_STRATEGY_SHA256, digest, verify_freeze

ZERO = D(0)
TOL = D("1e-18")


def dump(path, value):
    Path(path).write_text(json.dumps(value, default=str, indent=2, allow_nan=False) + "\n")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_native_sources(receipt_path, freeze):
    """Independently bind recovered bytes to the frozen member allowlist."""
    receipt = json.loads(Path(receipt_path).read_text())
    if receipt.get("manifest_sha256") != freeze["verified_candidates"]["native-manifest"]["sha256"]:
        raise ValueError("Native recovery receipt is not bound to immutable source manifest")
    pins = {(x["archive"], x["member"]): x
            for x in freeze["native_manifest"]["archived_python_members"]}
    members = receipt.get("members", receipt.get("extracted_sources", []))
    if not members:
        raise ValueError("Native recovery receipt has no source members")
    verified = {}
    payloads = {}
    for row in members:
        key = (row["archive"], row.get("member", row.get("original_member")))
        expected = pins.get(key)
        if expected is None:
            raise ValueError(f"Recovered source is outside frozen allowlist: {key}")
        if key in verified:
            raise ValueError("Duplicate recovered native source member")
        expected_relative = Path(key[0]).stem + "/" + key[1]
        if row.get("relative_path") != expected_relative:
            raise ValueError("Recovered source path differs from immutable archive/member identity")
        source_root = Path(receipt["source_root"])
        if not source_root.is_absolute():
            source_root = Path(receipt_path).resolve().parent / source_root
        source_root = source_root.resolve()
        original_path = source_root / row["relative_path"]
        if original_path.is_symlink() or not original_path.resolve().is_relative_to(source_root):
            raise ValueError("Recovered native source escaped source root")
        path = original_path.resolve()
        if path.suffix != ".py" or path.stat().st_size != expected["bytes"]:
            raise ValueError(f"Native source size/type differs: {key}")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected["sha256"]:
            raise ValueError(f"Native source hash differs: {key}")
        verified[key] = path
        payloads[key] = payload
    archive_rows = receipt["archives"]
    if len({row["filename"] for row in archive_rows}) != len(archive_rows):
        raise ValueError("Ambiguous recovered native archive provenance")
    frozen_archives = {row["filename"] for row in freeze["native_manifest"]["external_archives"]}
    if any(row.get("whole_archive_pin_verified") and row["filename"] not in frozen_archives for row in archive_rows):
        raise ValueError("Auxiliary source archive has no frozen whole-archive pin")
    for expected in freeze["native_manifest"]["external_archives"]:
        actual = next((x for x in receipt["archives"]
                       if x["filename"] == expected["filename"]), None)
        if actual is None or not actual.get("whole_archive_pin_verified"):
            raise ValueError("Required whole native archive was not verified")
        if any(actual[k] != expected[k] for k in ("bytes", "sha256")):
            raise ValueError("Required native archive identity differs")
    def one(ending):
        matches = [p for (_, member), p in verified.items() if member.endswith(ending)]
        if len(matches) != 1:
            raise ValueError(f"Ambiguous/missing pinned native dependency: {ending}")
        return matches[0]
    return dict(verified=verified,
                payloads=payloads,
                bridge=one("one-percent-research-20261006/one_percent_test.py"),
                sparse=one("baseline-recovery-20261001/adapter/sparse_adapter.py"),
                generated=one("runs/hard_100_park/generated_recycle_body.py"),
                core=one("spectra_core/rotating_fleet.py").parent.parent,
                receipt_sha256=digest(Path(receipt_path)))


def isolated_source_snapshot(binding, freeze):
    """Stage only verified bytes before imports; extra mutable files cannot load."""
    temporary = tempfile.TemporaryDirectory(prefix="spectra-fresh-native-")
    root = Path(temporary.name)
    source_root = root / "native"
    staged = {}
    for key, payload in binding["payloads"].items():
        path = source_root / Path(key[0]).stem / key[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        staged[key] = path
    cloned = dict(binding, verified=staged, _snapshot_guard=temporary)
    for name in ("bridge", "sparse", "generated"):
        key = next(k for k,p in binding["verified"].items() if p == binding[name])
        cloned[name] = staged[key]
    key = next(k for k,p in binding["verified"].items() if p == binding["core"] / "spectra_core/rotating_fleet.py")
    cloned["core"] = staged[key].parent.parent
    repo = root / "immutable_repo"
    for name, payload in freeze["verified_payloads"].items():
        path = repo / freeze["verified_candidates"][name]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return cloned, dict(freeze, repo_root=repo)


def load_native(binding):
    if any(name == "spectra_core" or name.startswith("spectra_core.")
           or name == "sparse_adapter" for name in sys.modules):
        raise RuntimeError("Native engine requires a fresh interpreter per arm")
    sys.path.insert(0, str(binding["core"]))
    sparse = load_module(binding["sparse"], "sparse_adapter")
    bridge = load_module(binding["bridge"], "spectra_fresh_pinned_bridge")
    if hashlib.sha256(bridge.GENERATED_SOURCE.encode()).hexdigest() != NATIVE_STRATEGY_SHA256:
        raise ArithmeticError("Rebuilt native kernel differs from frozen strategy")
    if bridge.GENERATED_SOURCE != binding["generated"].read_text():
        raise ArithmeticError("Rebuilt native kernel differs from recovered body")
    allowed = set(binding["verified"].values())
    for name, module in tuple(sys.modules.items()):
        if name == "spectra_core" or name.startswith("spectra_core.") or name == "sparse_adapter":
            location = getattr(module, "__file__", None)
            if location is None or Path(location).resolve() not in allowed:
                raise ArithmeticError(f"Mutable/unverified native import: {name}")
    return bridge, sparse


def validate_fixture(prepared, freeze):
    """Read only caller-created fixture bytes, never a prospective capture."""
    prepared = Path(prepared).resolve()
    receipt = json.loads((prepared / "fixture-receipt.json").read_text())
    if receipt.get("input_class") != "constructed_fixture" or receipt.get("uses_market_observations") is not False:
        raise ValueError("This entrypoint requires explicitly constructed fixture inputs")
    metadata_bytes = (prepared / "metadata.json").read_bytes()
    if hashlib.sha256(metadata_bytes).hexdigest() != receipt["sha256"]["metadata.json"]:
        raise ValueError("Constructed fixture metadata changed")
    meta = json.loads(metadata_bytes, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite metadata constant")))
    if any(type(meta.get(k)) is not int for k in ("start", "end", "interval_seconds")):
        raise ValueError("Fixture timestamps and interval must be exact integers")
    if meta["end"] <= meta["start"] or meta["start"] % 300 or meta["end"] % 300:
        raise ValueError("Fixture requires a positive exact five-minute timeline")
    frozen_start = datetime.fromisoformat(freeze["protocol"]["start_utc"].replace("Z", "+00:00")).timestamp()
    if meta["end"] > frozen_start:
        raise ValueError("Constructed fixture overlaps the sealed prospective window")
    if meta["interval_seconds"] != 300 or meta["pairs"] != freeze["universe"]["pairs"]:
        raise ValueError("Fixture timing or fixed universe differs from financial freeze")
    warmup = receipt["warmup_frames"]
    if type(warmup) is not int or warmup != 288:
        raise ValueError("Financial freeze requires exactly 24 hours of 5m warmup")
    # Embargo/time declaration is checked before opening even a fixture tape.
    if digest(prepared / "ohlcv.npy") != receipt["sha256"]["ohlcv.npy"]:
        raise ValueError("Constructed fixture tape bytes changed")
    import numpy as np
    tape = np.load(prepared / "ohlcv.npy", mmap_mode="r")
    if tape.ndim != 3 or tape.shape[1:] != (619, 6) or len(tape) < warmup + 3:
        raise ValueError("Constructed tape geometry is invalid")
    if meta["end"] != meta["start"] + len(tape) * 300:
        raise ValueError("Fixture metadata end differs from actual array length")
    # Each pair/candle is either wholly absent or a complete observed-style row.
    finite = np.isfinite(tape)
    complete = finite.all(axis=2)
    absent = np.isnan(tape).all(axis=2)
    if not (complete | absent).all():
        raise ValueError("Partial/nonfinite fixture candle")
    rows = tape[complete]
    if (rows[:, :4] <= 0).any() or (rows[:, 4:] < 0).any():
        raise ValueError("Invalid fixture candle prices/volume/trades")
    if (rows[:, 1] < np.maximum(rows[:, 0], rows[:, 3])).any() or (rows[:, 2] > np.minimum(rows[:, 0], rows[:, 3])).any():
        raise ValueError("Invalid fixture candle price geometry")
    return meta, receipt, len(tape)


def instrument_fresh(source):
    changes = []
    def change(before, after, purpose):
        nonlocal source
        if source.count(before) != 1:
            raise ArithmeticError(f"Fresh adapter source boundary changed: {purpose}")
        source = source.replace(before, after)
        changes.append(dict(before=before, after=after, purpose=purpose))
    # Warmup uses the native rolling-volume/fingerprint updates above this
    # boundary. It creates no orders, samples, fills or economic book history.
    marker = "        committed = sum((position.basis for position in positions.values()), ZERO)"
    change(marker, "        if index < dataset.fresh_warmup_frames:\n            dataset.observe_warmup(locals())\n            continue\n        dataset.observe_initial_seed(locals())\n" + marker,
           "passive causal warmup; exact fresh financial seed before evaluation")
    marker = "        pending = exit_orders + partial_orders + recovery_orders + entry_orders"
    change(marker, marker + "\n        dataset.observe_frame(locals())",
           "passive accounting/ownership observer after native intent scheduling")
    change("            initialized_index = index",
           "            initialized_index = index - dataset.fresh_warmup_frames",
           "observation index addresses evaluation-only samples after excluded warmup")
    return source, changes


def require_closed_holdout(freeze):
    end = datetime.fromisoformat(freeze["protocol"]["end_utc"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) < end:
        raise PermissionError("Prospective holdout replay remains embargoed until 2026-11-09T00:00:00Z")


def verify_source_archives(directory, freeze):
    if directory is None:
        raise ValueError("Prospective scoring requires original native archive bytes")
    for row in freeze["native_manifest"]["external_archives"]:
        path = Path(directory) / row["filename"]
        if path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError("Original native whole archive pin differs")


def fixture_arm(repo_root, native_receipt, prepared, arm, output, seconds=120,
                *, prospective=False, source_archives=None):
    if arm not in ARMS or not 0 < seconds <= 900:
        raise ValueError("Invalid fixed arm or bounded execution budget")
    freeze = verify_freeze(Path(repo_root))
    if prospective:
        # No prepared/capture/market file opens are permitted before this check.
        require_closed_holdout(freeze)
        verify_source_archives(source_archives, freeze)
        from assemble_captures import validate_prepared
        meta, fixture, frames = validate_prepared(Path(repo_root), Path(prepared))
    else:
        meta, fixture, frames = validate_fixture(prepared, freeze)
    binding = verify_native_sources(native_receipt, freeze)
    binding, freeze = isolated_source_snapshot(binding, freeze)
    bridge, sparse = load_native(binding)
    quality = load_module(freeze["repo_root"] / "research/capital_quality/quality_gate.py", "spectra_fresh_pinned_quality")
    adapter = load_module(freeze["repo_root"] / "research/capital_quality/release_adapter.py", "spectra_fresh_pinned_release")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    old_init = sparse.SparseDataset.__init__
    def redirected(instance, _old_prepared, **kwargs):
        return old_init(instance, Path(prepared), **kwargs)
    sparse.SparseDataset.__init__ = redirected
    try:
        dataset = bridge.Dataset(case="hard_100_park", enabled=True, frames=frames)
    finally:
        sparse.SparseDataset.__init__ = old_init
    dataset.fresh_warmup_frames = fixture["warmup_frames"]
    financial = freeze["financial"]
    economics = financial["economics"]
    cfg = replace(bridge.a.original.FleetConfig(),
        initial_cash=D(financial["initial_state"]["initial_cash_quote"]),
        target_positions=economics["maximum_distinct_assets"],
        entry_budget=D(economics["entry_all_in_quote"]),
        reserve_fraction=D(economics["physical_cash_reserve_fraction"]),
        pair_cap_fraction=D(economics["pair_basis_cap_fraction"]),
        fee_bps=D(economics["fee_bps_per_side"]),
        slippage_bps=D(economics["adverse_slippage_bps"]),
        simulated_min_order_quote=D(economics["generic_minimum_quote"]),
        volume_participation=D(economics["maximum_volume_participation_fraction"]),
        recovery_trigger=D(economics["recovery_trigger_fraction"]),
        recovery_seed_quote=D(economics["recovery_seed_quote"]))
    if dataset.parked_cash() != ZERO or dataset.ledger.lots or dataset.orders:
        raise ArithmeticError("Native constructor contaminated fresh initial financial state")
    if any(dataset.case[key] != economics[field] for key, field in (
        ("whole_target_bps", "whole_profit_target_bps"),
        ("partial_target_bps", "partial_profit_target_bps"),
        ("execution_floor_bps", "hard_execution_floor_net_bps"))):
        raise ArithmeticError("Native case differs from frozen prospective floor")
    source, changes = instrument_fresh(bridge.GENERATED_SOURCE)
    dataset.hydration_generated_source = source  # pinned overlay API name only; no checkpoint hydration
    namespace = dict(bridge.a.original.__dict__)
    namespace["FleetOrder"] = bridge.ResearchOrder
    exec(compile(source, str(output / "fresh_native_body.py"), "exec"), namespace)
    Simulator = type("FreshCashNativeSimulator", (bridge.a.original.RotatingFleetSimulator,), {"run": namespace["run"]})
    simulator = Simulator(cfg,
        exit_policy=replace(bridge.a.VCAPProfitExitPolicy(), target_net_profit_bps=dataset.whole_target_bps),
        exit_strategy=replace(bridge.a.original.FleetExitStrategy(), mode=bridge.a.original.FleetExitMode.HARD_TARGET))
    gate = quality.CausalGate(prepared, json.loads((freeze["repo_root"] / "research/capital_quality/protocol.json").read_text())["quality_policy"])
    adapter.attach(dataset, arm, gate=gate)
    simulator = adapter.compile_hydrated(bridge, dataset, simulator)
    accounts = []
    warmup_count = 0
    initial_seed = None
    last_state = None
    started = time.monotonic()
    def observe_warmup(state):
        nonlocal warmup_count
        if state["cash"] != D(2000) or state["positions"] or state["fills"] or state["pending"] or state["samples"] or dataset.parked_cash() != ZERO:
            raise ArithmeticError("Warmup mutated fresh financial seed")
        warmup_count += 1
    def observe_initial_seed(state):
        nonlocal initial_seed
        if initial_seed is None:
            if state["cash"] != D(2000) or state["positions"] or state["fills"] or state["pending"]:
                raise ArithmeticError("Evaluation did not start with exact fresh $2,000")
            initial_seed = dict(cash="2000", ordinary="2000", parked="0", positions=0,
                                fills=0, pending=0, initialized_from_checkpoint=False,
                                timestamp=state["timestamp"], warmup_frames=warmup_count)
    def observe_frame(state):
        nonlocal last_state
        last_state = state.copy()
        cash = state["cash"]
        basis = sum((p.basis for p in state["positions"].values()), ZERO)
        marked = sum((p.quantity * dataset.marks[pair] for pair, p in state["positions"].items()), ZERO)
        parked = dataset.parked_cash()
        if abs(cash + basis - D(2000) - state["realized"]) > TOL or min(cash, parked, cash - parked) < -TOL:
            raise ArithmeticError("Fresh replay cash/source conservation failed")
        for position in state["positions"].values():
            dataset.check_parent(position)
        dataset.release.check(cash=cash)
        accounts.append(dict(timestamp=state["timestamp"], cash=cash, parked=parked,
            ordinary=cash-parked, basis=basis, marked=marked, equity=cash+marked,
            realized=state["realized"], positions=len(state["positions"])))
        if time.monotonic() - started > seconds:
            raise TimeoutError("Fresh replay bounded fixture execution budget reached")
    dataset.observe_warmup = observe_warmup
    dataset.observe_initial_seed = observe_initial_seed
    dataset.observe_frame = observe_frame
    dump(output / "source_changes.json", changes + dataset.release_source_changes)
    (output / "executed_native_body.py").write_text(dataset.release_generated_source)
    try:
        result = simulator.run(dataset)
        if warmup_count != 288 or len(accounts) != frames - 288 or initial_seed is None:
            raise ArithmeticError("Incomplete warmup/evaluation window")
        dataset.release.check(cash=result.cash)
        dataset.release.finalize_window(last_state["pending"])
        basis = sum((p.basis for p in result.positions), ZERO)
        marked = sum((p.quantity * dataset.marks[p.pair] for p in result.positions), ZERO)
        realized = sum((f.realized_pnl for f in result.fills), ZERO)
        fees = sum((f.fee_quote for f in result.fills), ZERO)
        slippage = ZERO
        for fill in result.fills:
            fill_index = (fill.timestamp - meta["start"]) // 300 - 1
            pair_index = meta["pairs"].index(fill.pair)
            actual_open = D(str(float(dataset.rows[fill_index, pair_index, 0])))
            slippage += abs(fill.price - actual_open) * fill.quantity
        terminal_timestamp = accounts[-1]["timestamp"]
        inventory = [dict(pair=p.pair, position_id=p.position_id,
            quantity=p.quantity, basis=p.basis, mark=dataset.marks[p.pair],
            marked_value=p.quantity*dataset.marks[p.pair],
            last_mark_close_timestamp=dataset.last_seen[p.pair],
            mark_age_seconds=terminal_timestamp-dataset.last_seen[p.pair])
            for p in result.positions]
        import numpy as np
        observed_evaluation_rows = int(np.isfinite(dataset.rows[288:, :, 0]).sum())
        if abs(result.cash + marked - D(2000) - realized - (marked - basis)) > TOL:
            raise ArithmeticError("Fresh replay marked-equity identity failed")
        if any(f.realized_pnl < f.basis_released * D(".01") for f in result.fills if f.kind in {"EXIT", "PARTIAL_RECYCLE"}):
            raise ArithmeticError("Frozen sale net profit floor violated")
        peak = D(2000)
        drawdown = ZERO
        for row in accounts:
            peak = max(peak, row["equity"])
            drawdown = max(drawdown, (peak-row["equity"])/peak)
        receipt = dict(status="PASS", research_only=True, input_class="prospective_capture" if prospective else "constructed_fixture",
            performance_claim=bool(prospective and observed_evaluation_rows), live_authority=False, arm=arm, initial_seed=initial_seed,
            validation_scope="mechanical replay integrity; economic criteria are a separate comparison",
            parked_supplement_activated=dataset.release.parked_spent > ZERO,
            inactive_no_fills=not bool(result.fills),
            observed_evaluation_rows=observed_evaluation_rows,
            evaluation_missing_grid_slots=(frames-288)*619-observed_evaluation_rows,
            completed_evaluation_frames=len(accounts), ending_cash=result.cash,
            ending_marked_inventory=marked, ending_equity=result.cash+marked,
            realized_pnl=realized, open_pnl=marked-basis, fees=fees,
            modeled_adverse_slippage_quote=slippage,
            stale_inventory_marks=sum(row["mark_age_seconds"] > 300 for row in inventory),
            maximum_inventory_mark_age_seconds=max((row["mark_age_seconds"] for row in inventory), default=0),
            drawdown_fraction=drawdown, fill_counts=dict(Counter(f.kind for f in result.fills)),
            source_release=dataset.release.audit_snapshot(), native_generated_sha256=NATIVE_STRATEGY_SHA256,
            executed_native_sha256=hashlib.sha256(dataset.release_generated_source.encode()).hexdigest(),
            freeze_sha256="ef3148ec5b896aded5e812b71d687ea64eddc931c5e86533bfca6f0d08c37134",
            native_recovery_receipt_sha256=binding["receipt_sha256"],
            prepared_input_sha256=fixture.get("sha256", fixture.get("prepared_sha256")),
            input_coverage=dict(observed_rows=meta.get("observed_rows"),
                missing_grid_slots=fixture.get("missing_grid_slots"),
                census_policy=fixture.get("first_observation_policy"),
                assembly_counts=fixture.get("counts"),
                source_hashes=meta.get("source_hashes")),
            immutable_candidate_hashes={k:v["sha256"] for k,v in freeze["verified_candidates"].items()},
            source_change_count=len(changes)+len(dataset.release_source_changes),
            conservation_checks=len(accounts), exact_parent_lot_checks=True,
            process_id=__import__("os").getpid(), simulation_seconds=time.monotonic()-started)
        dump(output / "result.json", receipt)
        dump(output / "accounts.json", accounts)
        dump(output / "fills.json", [asdict(x) for x in result.fills])
        dump(output / "lots.json", [asdict(x) for x in dataset.ledger.lots.values()])
        dump(output / "terminal_inventory.json", inventory)
        dump(output / "quality_decisions.json", gate.records)
        dump(output / "source_events.json", dataset.release.events)
        return receipt
    except Exception as error:
        dump(output / "failure.json", dict(status="FAIL", arm=arm, error=type(error).__name__,
            reason=str(error), completed_frames=len(accounts), performance_claim=False))
        raise


def fixture_matrix(repo_root, native_receipt, prepared, output, seconds=120,
                   *, prospective=False, source_archives=None):
    if prospective:
        freeze = verify_freeze(Path(repo_root))
        require_closed_holdout(freeze)
        verify_source_archives(source_archives, freeze)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    matrix = dict(status="INCOMPLETE", arms=list(ARMS),
        input_class="prospective_capture" if prospective else "constructed_fixture",
        performance_claim=False, live_authority=False, completed_arms=[],
        arm_states={arm:"NOT_RUN" for arm in ARMS}, arm_receipt_sha256={},
        same_fresh_initial_state=True, separate_interpreters=True,
        holdout_payloads_read=bool(prospective), checkpoint_hydration_used=False)
    def persist():
        temporary = output / "matrix.receipt.pending"
        dump(temporary, matrix)
        temporary.replace(output / "matrix.receipt.json")
    persist()
    for arm in ARMS:
        command = [sys.executable, str(Path(__file__).resolve()), "evaluate-arm" if prospective else "fixture-arm",
            "--repo-root", str(repo_root), "--native-receipt", str(native_receipt),
            "--prepared", str(prepared), "--arm", arm, "--output", str(output / arm),
            "--seconds", str(seconds)]
        if prospective:
            command += ["--source-archives", str(source_archives)]
        matrix["arm_states"][arm] = "RUNNING"
        persist()
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=seconds+120)
            (output / (arm + ".log")).write_text(completed.stdout + completed.stderr)
            if completed.returncode:
                raise RuntimeError(f"Fresh replay arm failed: {arm}; inspect saved log")
            result_path = output / arm / "result.json"
            result = json.loads(result_path.read_text())
            if result.get("status") != "PASS" or result.get("arm") != arm:
                raise ArithmeticError("Completed arm receipt has wrong status/identity")
            rows.append(result)
            matrix["arm_states"][arm] = "PASS"
            matrix["completed_arms"].append(arm)
            matrix["arm_receipt_sha256"][arm] = digest(result_path)
            persist()
        except Exception as error:
            if isinstance(error, subprocess.TimeoutExpired):
                def decoded(value):
                    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                (output / (arm + ".log")).write_text(decoded(error.stdout)+decoded(error.stderr))
            matrix.update(status="FAIL", failing_arm=arm, error=type(error).__name__, reason=str(error))
            matrix["arm_states"][arm] = "FAIL"
            persist()
            raise
    if len({x["process_id"] for x in rows}) != len(ARMS):
        matrix.update(status="FAIL", error="ArithmeticError", reason="Fixture arms shared a process identity")
        persist()
        raise ArithmeticError("Fixture arms did not use independent processes")
    matrix.update(status="PASS", performance_claim=bool(prospective and all(row.get("performance_claim") for row in rows)),
        parked_supplement_activated={row["arm"]:row.get("parked_supplement_activated", False) for row in rows},
        inactive_arms=[row["arm"] for row in rows if row.get("inactive_no_fills")],
        economic_winner_selected=False, production_promotion_allowed=False)
    persist()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["readiness", "fixture-arm", "fixture-matrix", "evaluate-arm", "evaluate-matrix"])
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--native-receipt", type=Path, required=True)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--source-archives", type=Path)
    args = parser.parse_args()
    if args.mode == "readiness":
        freeze = verify_freeze(args.repo_root)
        binding = verify_native_sources(args.native_receipt, freeze)
        print(json.dumps(dict(status="SOURCE_BINDING_READY", research_only=True,
            approved_for_live=False, holdout_payloads_read=False,
            prospective_capture_assembly_implemented=(Path(__file__).parent / "assemble_captures.py").is_file(),
            verified_native_members=len(binding["verified"]), arms=list(ARMS))))
    else:
        if args.prepared is None or args.output is None or (args.mode.endswith("-arm") and args.arm is None):
            parser.error("Fixture replay requires prepared inputs, output and an arm where applicable")
        if args.mode.endswith("-arm"):
            result = fixture_arm(args.repo_root, args.native_receipt, args.prepared, args.arm, args.output, args.seconds,
                prospective=args.mode == "evaluate-arm", source_archives=args.source_archives)
            print(json.dumps(dict(status=result["status"], arm=args.arm, performance_claim=result["performance_claim"])))
        else:
            fixture_matrix(args.repo_root, args.native_receipt, args.prepared, args.output, args.seconds,
                prospective=args.mode == "evaluate-matrix", source_archives=args.source_archives)
            receipt = json.loads((args.output / "matrix.receipt.json").read_text())
            print(json.dumps(dict(status="PASS", arms=list(ARMS), performance_claim=receipt["performance_claim"])))


if __name__ == "__main__":
    main()
