"""Bounded offline parked-cash comparison on a pinned native checkpoint.

Requires the preserved research bridge and external canonical tape. This CLI
has no exchange client, credentials, order endpoint, or production entry point.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import csv
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

D = Decimal
ZERO = D(0)
TOL = D("1e-18")
HERE = Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def dump(path, value):
    path.write_text(json.dumps(value, default=str, indent=2) + "\n")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def preserve_failure_evidence(output, error, *, arm, completed_frames,
                              simulation_seconds, latest, dataset, receipt,
                              sample_file):
    """Save the original failure before optional, fallible evidence exports.

    This helper does not expire claims, validate a full result or change money.
    Export failures are diagnostics beside the original simulation failure.
    The caller must re-raise its original exception with a bare ``raise``.
    """
    failure_path = output / "failure_receipt.json"
    payload = dict(status="FAIL", arm=arm, exception=type(error).__name__,
        reason=str(error), completed_frames=completed_frames,
        simulation_seconds=simulation_seconds, no_performance_claim=True,
        evidence_export_errors=[])

    def write_receipt():
        # An interrupted amendment must not truncate the first valid receipt.
        staging = failure_path.with_suffix(".pending")
        dump(staging, payload)
        staging.replace(failure_path)

    def capture(label, operation):
        try:
            operation()
            return True
        except Exception as export_error:
            payload["evidence_export_errors"].append(dict(
                export=label, exception=type(export_error).__name__,
                reason=str(export_error)))
            return False

    # The original failure is durable before CSV closure or ownership checks.
    receipt_written = capture("failure_receipt_initial", write_receipt)
    capture("account_csv_close", sample_file.close)
    if receipt_written and latest is not None:
        capture("partial_new_fills", lambda: dump(output / "partial_new_fills.json",
            [asdict(fill) for fill in latest["fills"][receipt["original_fill_count"]:]]))
        capture("partial_release_events", lambda: dump(
            output / "partial_release_events.json", dataset.release.events))
        capture("partial_active_lots", lambda: dump(output / "partial_active_lots.json",
            [asdict(lot) for lot in dataset.ledger.lots.values() if lot.base_remaining > ZERO]))
        capture("partial_positions", lambda: dump(output / "partial_positions.json",
            [asdict(position) for position in latest["positions"].values()]))
        capture("partial_release", lambda: dump(output / "partial_release.json",
            dataset.release.audit_snapshot()))
    capture("failure_receipt_update", write_receipt)
    if payload["evidence_export_errors"]:
        # A filesystem error can prevent saving the amended receipt. Report it
        # without replacing the original exception, even if stderr also fails.
        try:
            print(json.dumps(dict(event="FAILURE_EVIDENCE_EXPORT_ERRORS",
                original_exception=payload["exception"],
                errors=payload["evidence_export_errors"])), file=sys.stderr)
        except Exception:
            pass
    return payload


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arm", choices=["OFF", "ENTRY_ONLY", "RECOVERY_ONLY", "BOTH", "BOTH_TIGHT"], required=True)
    p.add_argument("--wrapper", type=Path, required=True)
    p.add_argument("--hydration", type=Path, required=True)
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--protocol", type=Path, default=HERE / "protocol.json")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--seconds", type=int, default=420)
    args = p.parse_args()
    protocol = json.loads(args.protocol.read_text())
    if args.seconds > protocol["execution"]["maximum_simulation_seconds_per_arm"]:
        raise ValueError("Execution budget exceeds frozen protocol")
    args.output.mkdir(parents=True, exist_ok=False)
    # Pin the executed observation code before any long restore. These bytes
    # are evidence, rather than a later copy of possibly edited source files.
    for name in ("run_comparison.py", "release_adapter.py"):
        (args.output / ("executed_" + name)).write_bytes((HERE / name).read_bytes())
    start_clock = time.monotonic()
    adapter = load(HERE / "release_adapter.py", "parked_release_adapter")
    wrapper = load(args.wrapper, "parked_native_wrapper")
    native = wrapper.load_bridge()
    hydration = load(args.hydration, "parked_hydration")
    if hashlib.sha256(native.GENERATED_SOURCE.encode()).hexdigest() != protocol["native_strategy_sha256"]:
        raise ArithmeticError("Native source pin changed")
    end_timestamp = int(datetime.fromisoformat(protocol["end_exclusive_utc"]).timestamp())
    metadata = json.loads((args.prepared / "metadata.json").read_text())
    end_frame = (end_timestamp - metadata["start"]) // metadata["interval_seconds"]
    execution_start = None
    observed = Counter()
    last_day = None
    latest = None
    peak = D(protocol["restored_equity"])
    window_drawdown = ZERO
    first_release_receipt = None
    last_audited_fill_count = None
    last_release_event_count = 0
    sample_path = args.output / "window_accounts.csv"
    sample_file = sample_path.open("w", newline="")
    writer = csv.DictWriter(sample_file, fieldnames=["timestamp", "cash", "parked", "ordinary", "committed_basis", "marked_inventory", "equity", "realized", "active_positions"])
    writer.writeheader()

    def observer(state, dataset):
        nonlocal last_day, latest, peak, window_drawdown, first_release_receipt
        nonlocal last_audited_fill_count, last_release_event_count
        latest = state.copy()
        cash = state["cash"]
        positions = state["positions"]
        basis = sum((position.basis for position in positions.values()), ZERO)
        marked = sum((position.quantity * dataset.marks[pair] for pair, position in positions.items()), ZERO)
        equity = cash + marked
        parked = dataset.parked_cash()
        if abs(cash + basis - D(protocol["initial_capital"]) - state["realized"]) > TOL:
            raise ArithmeticError("Account identity failed")
        if min(cash, parked, cash - parked) < -TOL:
            raise ArithmeticError("Negative or overspent source cash")
        day = datetime.fromtimestamp(state["timestamp"], timezone.utc).date().isoformat()
        # Source/lot ownership changes only on native fills. Audit each change
        # and each day; do not rebuild thousands of immutable source records
        # on every no-fill candle. Financial identities remain checked below
        # on EVERY candle, and the independent auditor folds EVERY actual fill.
        fill_count = len(state["fills"])
        if fill_count != last_audited_fill_count or day != last_day:
            for position in positions.values():
                dataset.check_parent(position)
            dataset.release.check(cash=cash)
            observed["exact_ownership_audits"] += 1
            last_audited_fill_count = fill_count
        controller = dataset.release
        if controller.owned_parked_basis + controller.reserved_parked > controller.cap + TOL:
            raise ArithmeticError("Outstanding parked source cap exceeded")
        peak = max(peak, equity)
        window_drawdown = max(window_drawdown, (peak - equity) / peak)
        observed["frames"] += 1
        observed["below_target_frames"] += len(positions) < 50
        observed["position_sum"] += len(positions)
        observed["maximum_positions"] = max(observed["maximum_positions"], len(positions))
        writer.writerow(dict(timestamp=state["timestamp"], cash=cash, parked=parked,
            ordinary=cash-parked, committed_basis=basis, marked_inventory=marked,
            equity=equity, realized=state["realized"], active_positions=len(positions)))
        if day != last_day:
            print(json.dumps(dict(event="DAY_CHECKPOINT", arm=args.arm, day=day,
                frames=observed["frames"], equity=str(equity), cash=str(cash),
                parked=str(parked), release=dict(owned_parked_basis=controller.owned_parked_basis,
                    reserved_parked=controller.reserved_parked, parked_spent=controller.parked_spent,
                    counts=dict(controller.counts))), default=str), flush=True)
            sample_file.flush()
            last_day = day
        if args.arm != "OFF" and first_release_receipt is None:
            actual = [event for event in controller.events[last_release_event_count:]
                if event.get("event") == "BUY_SOURCE_COMMIT" and event.get("parked_used", ZERO) > ZERO]
            if actual:
                first_release_receipt = dict(actual[0], frame_account_identity_passed=True,
                    exact_parent_lot_checks_passed=True, cash=str(cash), parked=str(parked))
                dump(args.output / "activation_receipt.json", first_release_receipt)
                print(json.dumps(dict(event="ACTIVATION_PROVEN", arm=args.arm,
                    receipt=first_release_receipt), default=str), flush=True)
        last_release_event_count = len(controller.events)
        if time.monotonic() - execution_start > args.seconds:
            raise TimeoutError("Frozen execution budget reached")

    print(json.dumps(dict(event="HYDRATION_START", arm=args.arm)), flush=True)
    simulator, dataset, receipt = hydration.hydrate_native(native, args.prepared,
        args.checkpoint, end_frame=end_frame, frame_observer=observer,
        expected_checkpoint_sha256=protocol["boundary_checkpoint_sha256"])
    if receipt["end_frame"] - receipt["start_frame"] != protocol["frames"]:
        raise ArithmeticError("Wrong experimental frame window")
    if sha(args.prepared / "ohlcv.npy") != protocol["tape_sha256"]:
        raise ArithmeticError("Canonical tape hash changed")
    starting_events = len(dataset.events)
    starting_sample_count = receipt["original_sample_count"]
    starting_rejections = dict(dataset.hydration_engine_state["rejection_counts"])
    starting = receipt["initial_boundary_audit"]["observed"]
    starting_lots = [asdict(lot) for lot in dataset.ledger.lots.values() if lot.base_remaining > ZERO]
    dump(args.output / "initial_active_lots.json", starting_lots)
    adapter.attach(dataset, args.arm)
    simulator = adapter.compile_hydrated(native, dataset, simulator)
    dump(args.output / "hydration_receipt.json", receipt)
    dump(args.output / "source_receipt.json", dict(
        protocol_sha256=sha(args.protocol), adapter_sha256=sha(HERE / "release_adapter.py"),
        runner_sha256=sha(args.output / "executed_run_comparison.py"), hydration_sha256=sha(args.hydration),
        native_strategy_sha256=receipt["native_strategy_source_sha256"],
        compiled_source_sha256=hashlib.sha256(dataset.release_generated_source.encode()).hexdigest()))
    (args.output / "executed_native_body.py").write_text(dataset.release_generated_source)
    dump(args.output / "source_changes.json", dataset.release_source_changes)
    execution_start = time.monotonic()
    try:
        result = simulator.run(dataset)
    except Exception as error:
        preserve_failure_evidence(args.output, error, arm=args.arm,
            completed_frames=observed["frames"],
            simulation_seconds=time.monotonic()-execution_start,
            latest=latest, dataset=dataset, receipt=receipt, sample_file=sample_file)
        raise
    sample_file.close()
    # Save the native result independently before formatting any report.
    dump(args.output / "native_metrics.json", dict(metrics=result.metrics,
        rejections=result.rejections, simulation_seconds=time.monotonic()-execution_start,
        total_seconds=time.monotonic()-start_clock))
    if latest is None or observed["frames"] != protocol["frames"]:
        raise ArithmeticError("Incomplete window")
    for position in latest["positions"].values():
        dataset.check_parent(position)
    dataset.release.check(cash=result.cash)
    dataset.release.finalize_window(latest["pending"])
    release = dataset.release.audit_snapshot()
    new_fills = result.fills[receipt["original_fill_count"]:]
    dump(args.output / "new_fills.json", [asdict(fill) for fill in new_fills])
    dump(args.output / "release_events.json", dataset.release.events)
    with gzip.open(args.output / "native_new_events.json.gz", "wt") as f:
        json.dump(dataset.events[starting_events:], f, default=str)
    final_lots = [asdict(lot) for lot in dataset.ledger.lots.values() if lot.base_remaining > ZERO]
    dump(args.output / "terminal_active_lots.json", final_lots)
    dump(args.output / "terminal_positions.json", [asdict(position) for position in result.positions])
    dump(args.output / "terminal_release.json", release)
    if args.arm == "OFF":
        directory = args.checkpoint.parent
        audit = hydration.validate_archived_window(result, dataset, receipt,
            directory / "hard_100_park_fills.csv", directory / "hard_100_park_equity.csv")
        dump(args.output / "control_archive_audit.json", audit)
        if not audit["passed"]:
            raise ArithmeticError("OFF does not match archived control")
    basis = sum((p.basis for p in result.positions), ZERO)
    marked = sum((p.quantity * dataset.marks[p.pair] for p in result.positions), ZERO)
    equity = result.cash + marked
    realized = sum((fill.realized_pnl for fill in new_fills), ZERO)
    fees = sum((fill.fee_quote for fill in new_fills), ZERO)
    new_open = marked - basis
    original_open = D(starting["unrealized_pnl"])
    if abs(equity - D(starting["equity"]) - realized - (new_open - original_open)) > TOL:
        raise ArithmeticError("Window P&L identity failed")
    exit_fills = [f for f in new_fills if f.kind in {"EXIT", "PARTIAL_RECYCLE"}]
    if any(f.realized_pnl < f.basis_released * D("0.01") for f in exit_fills):
        raise ArithmeticError("Net profit floor failed")
    payload = dict(status="PASS", arm=args.arm, research_only=True,
        window=protocol["start_utc"]+" / "+protocol["end_exclusive_utc"],
        complete_frames=observed["frames"], initial_equity=starting["equity"],
        ending_equity=equity, marked_gain=equity-D(starting["equity"]),
        ending_cash=result.cash, parked_cash=dataset.parked_cash(),
        ordinary_cash=result.cash-dataset.parked_cash(), marked_inventory=marked,
        open_basis=basis, remaining_open_pnl=new_open, change_in_open_pnl=new_open-original_open,
        new_realized_profit=realized, fees=fees, fill_counts=dict(Counter(f.kind for f in new_fills)),
        ending_positions=len(result.positions), maximum_positions=observed["maximum_positions"],
        mean_positions=observed["position_sum"]/observed["frames"],
        window_max_drawdown_fraction=window_drawdown,
        cumulative_max_drawdown_fraction=result.metrics["max_drawdown_fraction"],
        new_rejection_counts={k: v-starting_rejections.get(k,0) for k,v in result.rejections.items()
            if starting_rejections.get(k,0) != v},
        release=release, activated=first_release_receipt is not None,
        window_accounting_checks=observed["frames"],
        exact_ownership_audits=observed["exact_ownership_audits"],
        simulation_seconds=time.monotonic()-execution_start, total_seconds=time.monotonic()-start_clock,
        native_selector_not_predictive=True, live_readiness_demonstrated=False,
        later_validation_required=True)
    dump(args.output / "result.json", payload)
    print(json.dumps(dict(event="ARM_COMPLETE", **payload), default=str), flush=True)


if __name__ == "__main__":
    main()
