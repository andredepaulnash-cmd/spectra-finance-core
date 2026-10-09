"""Independent purpose-budget and raw-tape quality audit of saved outputs.

Reads saved actual journals only; no checkpoint hydration or strategy replay.
The prior independent financial/ownership/tape auditor is retained unchanged.
"""
from collections import Counter
from decimal import Decimal as D, getcontext
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json

import numpy as np

getcontext().prec = 60
Z, TOL = D(0), D("1e-18")
HERE = Path(__file__).resolve().parent
DEFAULT_BASE_ADAPTER = HERE.parent.parent / "parked_cash/release_adapter.py"
DEFAULT_FINANCIAL_AUDITOR = HERE / "financial_source_auditor.py"
SPEC = importlib.util.spec_from_file_location("prior_independent_financial_audit",
    DEFAULT_FINANCIAL_AUDITOR)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)


def load(path): return json.loads(Path(path).read_text())
def close(a,b,label):
    if abs(D(a)-D(b)) > TOL:
        raise ArithmeticError(f"{label}: {a} differs from {b}")


def scalar_gate(tape, metadata, pair_index, pair, purpose, timestamp):
    """Independent scalar oracle, using only exact completed-time raw rows."""
    step, start = int(metadata["interval_seconds"]), int(metadata["start"])
    elapsed = timestamp-start
    i = elapsed//step-1
    record = dict(pair=pair,kind=purpose,decision_timestamp=timestamp,
        decision_index=i,maximum_feature_index=None,eligible=False,
        reason="missing_history",anchor_indices=[],features={})
    if elapsed % step or i < 0 or i >= len(tape):
        record["reason"] = "outside_exact_close_grid"
        return record
    if pair not in pair_index:
        record["reason"] = "unknown_pair"
        return record
    if purpose not in {"ENTRY","RECOVERY"}:
        record["reason"] = "unsupported_purpose"
        return record
    anchors = [i,i-12,i-288] if purpose == "ENTRY" else list(range(i-5,i+1))
    record["anchor_indices"] = anchors
    if min(anchors) < 0: return record
    record["maximum_feature_index"] = i
    asset = pair_index[pair]
    for index in anchors:
        row = tape[index,asset]
        if not all(np.isfinite(value) for value in row) or not all(value > 0 for value in row[:4]):
            record["reason"] = "missing_actual_anchor_or_candle"
            return record
    current = float(tape[i,asset,3])
    if purpose == "ENTRY":
        hour, day = float(tape[i-12,asset,3]),float(tape[i-288,asset,3])
        record["features"] = dict(close=current,close_1h=hour,close_24h=day,
            return_1h=current/hour-1,return_24h=current/day-1)
        good = current > hour and current > day
        reason = "positive_1h_and_24h" if good else "nonpositive_entry_trend"
    else:
        before = float(tape[i-3,asset,3])
        early = min(float(tape[j,asset,2]) for j in range(i-5,i-2))
        late = min(float(tape[j,asset,2]) for j in range(i-2,i+1))
        record["features"] = dict(close=current,close_15m=before,
            return_15m=current/before-1,earlier_15m_low=early,recent_15m_low=late)
        good = current > before and late > early
        reason = "rebound_and_higher_low" if good else "unconfirmed_recovery"
    record.update(eligible=bool(good),reason=reason)
    return record


def audit_purpose_journal(directory, arm):
    directory = Path(directory)
    fills = load(directory/"new_fills.json")
    by_order = {row["order_id"]:row for row in fills}
    final = load(directory/"terminal_release.json")
    events = load(directory/"release_events.json")
    caps = {k:D(v) for k,v in arm["purpose_caps_quote"].items()}
    global_cap = D(arm["outstanding_cap_quote"])
    purpose_by_lot = {row["lot_id"]:BASE.native_lot(row)["kind"]
                      for row in load(directory/"initial_active_lots.json")}
    owned = {kind:Z for kind in ("ENTRY","RECOVERY")}
    spent, retired = owned.copy(), owned.copy()
    peak = owned.copy()
    active = {key:Z for key in purpose_by_lot}
    claims = {}
    checks = Counter()
    for event in events:
        name,oid = event["event"],event["order_id"]
        if name == "CLAIM":
            purpose = event["kind"]
            claims[oid] = (purpose,D(event["parked"]))
            close(event["purpose_cap"] or global_cap,caps.get(purpose,global_cap),"Claim purpose cap")
            close(event["total_outstanding_cap"],global_cap,"Claim shared cap")
        elif name == "CLAIM_ZERO_FILL_RELEASE":
            claims.pop(oid,None)
        elif name == "BUY_SOURCE_COMMIT":
            fill = by_order[oid]
            purpose = fill["kind"]
            lot_id = event["lot_id"]
            if lot_id in purpose_by_lot: raise ArithmeticError("Acquisition rewrote immutable lot purpose")
            purpose_by_lot[lot_id] = purpose
            amount = D(event["parked_used"])
            old_purpose,old_amount = claims.pop(oid)
            if old_purpose != purpose: raise ArithmeticError("Claim/buy purpose changed")
            close(old_amount,amount,"Purpose claim/actual spend")
            active[lot_id] = amount
            owned[purpose] += amount
            spent[purpose] += amount
        elif name == "SOURCE_SALE_FINAL":
            for row in event["lots"]:
                lot_id = row["lot_id"]
                purpose = purpose_by_lot[lot_id]
                if row["purpose"] != purpose: raise ArithmeticError("Disposal rewrote original acquisition purpose")
                if event["kind"] == "PARTIAL_RECYCLE" and purpose != "RECOVERY":
                    raise ArithmeticError("Recovery partial freed ENTRY purpose budget")
                amount = active.pop(lot_id)
                close(row["parked_basis"],amount,"Per-purpose exact acquisition basis retirement")
                owned[purpose] -= amount
                retired[purpose] += amount
                checks["sale_lot_original_purpose"] += 1
        else:
            raise ArithmeticError(f"Unknown source event: {name}")
        reserved = {kind:sum((v for k,v in claims.values() if k==kind),Z) for kind in owned}
        committed = {kind:owned[kind]+reserved[kind] for kind in owned}
        for kind,value in committed.items():
            if value < -TOL or value > caps.get(kind,global_cap)+TOL:
                raise ArithmeticError(f"Strict purpose capacity exceeded: {kind}")
            close(spent[kind],retired[kind]+owned[kind],"Per-purpose basis conservation")
            peak[kind] = max(peak[kind],value)
        if sum(committed.values(),Z) > global_cap+TOL:
            raise ArithmeticError("Shared source budget exceeded")
        if name == "CLAIM":
            close(event["purpose_committed"],committed[event["kind"]],"Claim purpose occupancy")
        checks["every_event_strict_purpose_and_shared_caps"] += 1
    if claims: raise ArithmeticError("Completed arm left pending purpose claims")
    if final["purpose_by_lot"] != purpose_by_lot or final["immutable_lot_purposes"] != purpose_by_lot:
        raise ArithmeticError("Final immutable purpose history differs from independent reconstruction")
    actual_caps = {k:D(v) for k,v in final["purpose_budget_caps"].items()}
    if actual_caps != caps or final["purpose_cross_borrowing"] is not False:
        raise ArithmeticError("Final purpose budget policy differs")
    for key,values in (
        ("owned_parked_basis_by_kind",owned), ("committed_parked_by_kind",owned),
        ("reserved_parked_by_kind",{k:Z for k in owned}),
        ("parked_spent_by_kind",spent), ("parked_basis_retired_by_kind",retired),
        ("peak_committed_by_kind",peak), ("per_kind_owned_basis",owned),
        ("per_kind_reserved",{k:Z for k in owned}), ("per_kind_peak",peak)):
        for kind,value in values.items(): close(final[key][kind],value,"Final purpose snapshot "+key+":"+kind)
    final_active = {row["lot_id"]:row for row in final["active_source_lots"]}
    if set(final_active) != set(active): raise ArithmeticError("Active purpose lot sets differ")
    for lot_id,amount in active.items():
        close(final_active[lot_id]["parked_basis"],amount,"Final source purpose basis")
        if final_active[lot_id]["purpose"] != purpose_by_lot[lot_id]:
            raise ArithmeticError("Final source purpose changed")
    return dict(passed=True,checks=dict(checks),strict_caps={k:str(v) for k,v in caps.items()},
        independently_reconstructed_peak_by_purpose={k:str(v) for k,v in peak.items()},
        spent_by_purpose={k:str(v) for k,v in spent.items()},
        retired_by_purpose={k:str(v) for k,v in retired.items()},
        surviving_basis_by_purpose={k:str(v) for k,v in owned.items()})


def audit_quality_receipts(directory, arm, prepared):
    directory,prepared = Path(directory),Path(prepared)
    provenance = load(directory/"source_receipt.json")
    executed = directory/"executed_quality_gate.py"
    if hashlib.sha256(executed.read_bytes()).hexdigest() != provenance["quality_gate_sha256"]:
        raise ArithmeticError("Executed feature source pin changed")
    metadata = load(prepared/"metadata.json")
    if metadata["interval_seconds"] != 300: raise ArithmeticError("Gate actual timeframe changed")
    tape = np.load(prepared/"ohlcv.npy",mmap_mode="r")
    pair_index = {pair:i for i,pair in enumerate(metadata["pairs"])}
    records = load(directory/"quality_decisions.json")
    recorded = {}
    counts = Counter()
    for row in records:
        key = (row["pair"],row["kind"],row["decision_timestamp"])
        if key in recorded: raise ArithmeticError("Duplicate unique gate receipt")
        expected = scalar_gate(tape,metadata,pair_index,*key)
        if row != expected:
            raise ArithmeticError(f"Raw-tape causal gate receipt differs: {key}: {row} vs {expected}")
        recorded[key] = row
        counts[(row["kind"],row["reason"])] += 1
    if not arm["quality_gate"] and records: raise ArithmeticError("Raw control evaluated an undeclared quality gate")
    fills = {f["order_id"]:f for f in load(directory/"new_fills.json")}
    source_checks=0
    for event in load(directory/"release_events.json"):
        if event["event"] != "CLAIM": continue
        required = arm["quality_gate"] and D(event["parked"])>Z
        if event["quality_required"] != required: raise ArithmeticError("Gate scope differs from P shortfall")
        if not required: continue
        key=(event["pair"],event["kind"],event["decision_timestamp"])
        row=recorded[key]
        if not row["eligible"] or event["qualified"] is not True:
            raise ArithmeticError("Parked claim passed a rejected or missing gate")
        if event["order_id"] in fills and key[2] > int(fills[event["order_id"]]["timestamp"])-300:
            raise ArithmeticError("Parked claim used execution candle completed data")
        source_checks+=1
    summary = load(directory/"quality_summary.json")
    if summary["unique_evaluations"] != len(records): raise ArithmeticError("Summary gate coverage differs")
    expected_counts={f"{k}:{reason}":n for (k,reason),n in sorted(counts.items())}
    if summary["counts"] != expected_counts: raise ArithmeticError("Summary gate reason counts differ")
    if summary["maximum_feature_index_never_after_decision"] is not True:
        raise ArithmeticError("Gate summary reports future-data use")
    return dict(passed=True,independently_checked_unique_raw_tape_decisions=len(records),
        actual_parked_claim_gate_links=source_checks,
        reason_counts=expected_counts,
        scope="Completed decision OHLC only. Actual fills remain estimated next-open fills; no venue execution or predictive-edge claim.")


def audit(directory, protocol, prepared, base_adapter=DEFAULT_BASE_ADAPTER):
    directory=Path(directory)
    executed_protocol=directory/"executed_protocol.json"
    provenance=load(directory/"source_receipt.json")
    if load(executed_protocol) != protocol:
        raise ArithmeticError("Independent requested protocol differs from executed frozen protocol")
    if hashlib.sha256(executed_protocol.read_bytes()).hexdigest() != provenance["protocol_sha256"]:
        raise ArithmeticError("Executed protocol source pin changed")
    base_path=Path(base_adapter)
    if hashlib.sha256(base_path.read_bytes()).hexdigest() != provenance["base_adapter_sha256"]:
        raise ArithmeticError("Frozen base economic adapter source pin changed")
    arm_name=load(directory/"result.json")["arm"]
    arm=next(row for row in protocol["arms"] if row["name"]==arm_name)
    return dict(passed=True,arm=arm_name,
        base_financial_ownership_and_authentic_fill_audit=BASE.audit(directory,protocol,prepared),
        independent_purpose_budget_audit=audit_purpose_journal(directory,arm),
        independent_actual_raw_tape_gate_audit=audit_quality_receipts(directory,arm,prepared))


if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--protocol",type=Path,required=True)
    p.add_argument("--prepared",type=Path,required=True)
    p.add_argument("--run",type=Path,action="append",required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--base-adapter",type=Path,default=DEFAULT_BASE_ADAPTER)
    p.add_argument("--financial-auditor",type=Path,default=DEFAULT_FINANCIAL_AUDITOR)
    args=p.parse_args()
    if args.financial_auditor.resolve() != DEFAULT_FINANCIAL_AUDITOR.resolve():
        spec=importlib.util.spec_from_file_location("selected_independent_financial_audit",args.financial_auditor)
        BASE=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(BASE)
    audits=[audit(path,load(args.protocol),args.prepared,args.base_adapter) for path in args.run]
    args.output.write_text(json.dumps(dict(passed=True,arms=audits),indent=2)+"\n")
    print(json.dumps(dict(passed=True,arms=[row["arm"] for row in audits],
        rows=sum(row["base_financial_ownership_and_authentic_fill_audit"]["checks"]["independent_account_rows"] for row in audits),
        quality_decisions=sum(row["independent_actual_raw_tape_gate_audit"]["independently_checked_unique_raw_tape_decisions"] for row in audits))))
