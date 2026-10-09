"""Descriptive saved-output capital-quality attribution. No strategy replay."""
from collections import defaultdict
from decimal import Decimal as D,getcontext
from datetime import datetime
from pathlib import Path
import argparse
import csv
import importlib.util
import json

import numpy as np

getcontext().prec=60
Z,TOL=D(0),D("1e-18")
HERE=Path(__file__).resolve().parent


def imported(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_ATTR=imported("prior_saved_source_attribution",HERE/"attribute_source_capital.py")
OLD_MARKS=imported("prior_independent_terminal_mark_audit",HERE/"audit_terminal_marks.py")
def load(path):return json.loads(Path(path).read_text())
def close(a,b,label):
    if abs(D(a)-D(b))>TOL:raise ArithmeticError(label)


def independently_score(directory,protocol):
    directory=Path(directory)
    result=load(directory/"result.json")
    fills=load(directory/"new_fills.json")
    actual_fees=sum((D(fill["fee_quote"]) for fill in fills),Z)
    close(actual_fees,result["fees"],"Reported fees differ from actual modeled fills")
    peak=D(protocol["restored_equity"])
    drawdown=Z
    rows=0
    for row in csv.DictReader((directory/"window_accounts.csv").open()):
        equity=D(row["equity"])
        peak=max(peak,equity)
        drawdown=max(drawdown,(peak-equity)/peak)
        rows+=1
    if rows!=protocol["frames"]:raise ArithmeticError("Incomplete scored financial path")
    close(drawdown,result["window_max_drawdown_fraction"],"Reported window drawdown differs from actual every-row equity")
    activated=any(event["event"]=="BUY_SOURCE_COMMIT" and D(event["parked_used"])>Z
                  for event in load(directory/"release_events.json"))
    if activated!=result["activated"]:raise ArithmeticError("Actual source activation and reported activation differ")
    return dict(actual_fees=actual_fees,actual_window_drawdown_fraction=drawdown,
        actual_source_activation=activated,actual_account_rows=rows)


def per_purpose(directory,metadata,tape,protocol,cache):
    directory=Path(directory)
    final=load(directory/"terminal_release.json")
    events=load(directory/"release_events.json")
    buy={e["lot_id"]:e for e in events if e["event"]=="BUY_SOURCE_COMMIT"}
    records=defaultdict(lambda:dict(parked_spent=Z,parked_basis_retired=Z,
        parked_sale_net=Z,parked_realized_pnl=Z,remaining_parked_basis=Z,
        remaining_parked_quantity=Z,remaining_parked_market_value=Z,
        remaining_parked_open_pnl=Z))
    for row in buy.values():
        records[(row["kind"],row["pair"])]["parked_spent"]+=D(row["parked_used"])
    for event in events:
        if event["event"]!="SOURCE_SALE_FINAL":continue
        for row in event["lots"]:
            basis=D(row["parked_basis"])
            if basis==Z:continue
            acquired=buy[row["lot_id"]]
            if row["purpose"]!=acquired["kind"]:raise ArithmeticError("Source attribution purpose changed")
            record=records[(acquired["kind"],acquired["pair"])]
            net=D(row["parked_net"])
            record["parked_basis_retired"]+=basis
            record["parked_sale_net"]+=net
            record["parked_realized_pnl"]+=net-basis
    end=int(datetime.fromisoformat(protocol["end_exclusive_utc"]).timestamp())
    end_frame=(end-metadata["start"])//metadata["interval_seconds"]
    pair_index={pair:i for i,pair in enumerate(metadata["pairs"])}
    for lot in final["active_source_lots"]:
        basis=D(lot["parked_basis"])
        if basis==Z:continue
        acquired=buy[lot["lot_id"]]
        purpose,pair=acquired["kind"],acquired["pair"]
        if lot["purpose"]!=purpose:raise ArithmeticError("Terminal source attribution purpose changed")
        if pair not in cache:
            observed=np.flatnonzero(np.isfinite(tape[:end_frame,pair_index[pair],0]))
            if not len(observed):raise ArithmeticError("Owned source asset lacks observed canonical mark")
            index=int(observed[-1])
            price=D(str(float(tape[index,pair_index[pair],3])))
            cache[pair]=(price,metadata["start"]+(index+1)*metadata["interval_seconds"])
        price,at=cache[pair]
        quantity=D(lot["quantity"])*basis/D(lot["total_basis"])
        value=quantity*price
        record=records[(purpose,pair)]
        record["remaining_parked_basis"]+=basis
        record["remaining_parked_quantity"]+=quantity
        record["remaining_parked_market_value"]+=value
        record["remaining_parked_open_pnl"]+=value-basis
        record["last_actual_close_price"]=price
        record["last_actual_close_timestamp"]=at
        record["mark_age_seconds_at_end"]=end-at
    rows=[]
    by_kind={kind:dict(parked_spent=Z,parked_basis_retired=Z,parked_sale_net=Z,
        parked_realized_pnl=Z,remaining_parked_basis=Z,remaining_parked_market_value=Z,
        remaining_parked_open_pnl=Z,total_P_source_marked_pnl=Z) for kind in ("ENTRY","RECOVERY")}
    for (kind,pair),record in records.items():
        record.update(kind=kind,pair=pair,
            total_P_source_marked_pnl=record["parked_realized_pnl"]+record["remaining_parked_open_pnl"])
        close(record["parked_spent"],record["parked_basis_retired"]+record["remaining_parked_basis"],"Asset-purpose source basis conservation")
        for key in by_kind[kind]:by_kind[kind][key]+=record[key]
        rows.append(record)
    for kind,record in by_kind.items():
        close(record["parked_spent"],final["parked_spent_by_kind"][kind],"Purpose source cumulative spend")
        close(record["parked_basis_retired"],final["parked_basis_retired_by_kind"][kind],"Purpose source retired basis")
        close(record["remaining_parked_basis"],final["owned_parked_basis_by_kind"][kind],"Purpose source owned basis")
    return dict(by_acquisition_purpose=by_kind,all_asset_purpose_rows=rows,
        distinction="Exact source allocation by original lot acquisition purpose; not the causal account effect of gating that purpose.")


def audit_and_attribute(directories,control,prepared,protocol):
    metadata=load(Path(prepared)/"metadata.json")
    tape=np.load(Path(prepared)/"ohlcv.npy",mmap_mode="r")
    cache={}
    baseline=load(Path(control)/"result.json")
    baseline_path_score=independently_score(control,protocol)
    arms=[]
    for directory in directories:
        result=load(Path(directory)/"result.json")
        if result["status"]!="PASS" or result["complete_frames"]!=protocol["frames"]:
            raise ArithmeticError("Only completed policy paths can be scored")
        marks=OLD_MARKS.audit(directory,metadata,tape,protocol,cache)
        attribution=OLD_ATTR.attribute(directory,control,prepared,protocol)
        source_purpose=per_purpose(directory,metadata,tape,protocol,cache)
        path_score=independently_score(directory,protocol)
        close(sum((r["total_P_source_marked_pnl"] for r in source_purpose["by_acquisition_purpose"].values()),Z),
            attribution["P_source_total_marked_pnl"],"Per-kind and aggregate source marked P&L")
        criteria=dict(complete_window=True,actual_parked_fills_required=path_score["actual_source_activation"],
            higher_marked_equity_than_PARK=D(result["ending_equity"])>D(baseline["ending_equity"]),
            no_higher_window_drawdown_than_PARK=path_score["actual_window_drawdown_fraction"]<=baseline_path_score["actual_window_drawdown_fraction"],
            no_worse_terminal_open_pnl_than_PARK=D(result["remaining_open_pnl"])>=D(baseline["remaining_open_pnl"]))
        scored={key:result[key] for key in ("ending_equity","remaining_open_pnl","new_realized_profit",
            "fees","window_max_drawdown_fraction","ending_cash","parked_cash","ordinary_cash","fill_counts")}
        arms.append(dict(arm=result["arm"],independent_terminal_marks=marks,
            independent_every_row_drawdown_and_actual_fill_fees=path_score,
            aggregate_source_and_control_attribution=attribution,
            exact_source_purpose_attribution=source_purpose,
            predeclared_candidate_criteria=criteria,meets_all_candidate_criteria=all(criteria.values()),
            comparison_metrics=scored))
    return dict(passed=True,arms=arms,independent_PARK_path_metrics=baseline_path_score,
        scope="Independent actual historical-close marks and descriptive saved-output allocation. No new strategy replay, hindsight coin exclusion or profitability guarantee.")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--run",type=Path,action="append",required=True)
    p.add_argument("--control",type=Path,required=True)
    p.add_argument("--protocol",type=Path,required=True)
    p.add_argument("--prepared",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--source-attribution-auditor",type=Path,default=HERE/"attribute_source_capital.py")
    p.add_argument("--terminal-mark-auditor",type=Path,default=HERE/"audit_terminal_marks.py")
    args=p.parse_args()
    OLD_ATTR=imported("selected_source_attribution",args.source_attribution_auditor)
    OLD_MARKS=imported("selected_terminal_mark_audit",args.terminal_mark_auditor)
    report=audit_and_attribute(args.run,args.control,args.prepared,load(args.protocol))
    args.output.write_text(json.dumps(report,default=str,indent=2)+"\n")
    print(json.dumps([dict(arm=row["arm"],meets_all_candidate_criteria=row["meets_all_candidate_criteria"],
        stale_inventory=row["independent_terminal_marks"]["stale_marked_inventory_over_300s"],
        delta_equity=str(row["aggregate_source_and_control_attribution"]["incremental_equity_vs_OFF"])) for row in report["arms"]]))
