"""Prepare saved historical PARK observations without rerunning the strategy.

The source CSV was published with an exact research pin. Only its January-June
prefix is interpreted. The terminal June observation uses the separately pinned
boundary summary and inventory, never as a January initial condition.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from decimal import Decimal, localcontext
from pathlib import Path

START = 1767225600
END = 1782864000
TOLERANCE = Decimal("1e-18")
SOURCE_PINS = {
    "hard_100_park_equity.csv": ("d6c39e2c6843aabc57949856f462ab397e77b33ee38505644994c01ef7f283f7", 904294),
    "boundary_inventory_plain.json": ("95ef1d9eb5064ec5c5f17984c7bd6eddd92fb712a4f6b0e8d9169a80fcb36fe7", 47077),
    "boundary_summary.json": ("daf34ca0621bf2ac4d44a9b0c3af2afe440cb8c03684173ac0df30dd599baacc", 1385),
}
FIELDNAMES = ("timestamp", "cash", "committed_basis", "marked_value", "equity", "active_positions", "unique_base_assets")


def prepare(source_directory: Path, output_directory: Path) -> dict:
    verified = {}
    for name, (expected_hash, expected_size) in SOURCE_PINS.items():
        path = source_directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Nonregular or symlink PARK source: {name}")
        raw = path.read_bytes()
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError(f"Unverified saved PARK source: {name}")
        verified[name] = raw
    boundary_inventory = json.loads(verified["boundary_inventory_plain.json"])
    boundary_summary = json.loads(verified["boundary_summary.json"])
    if boundary_inventory["timestamp"] != END or boundary_summary["timestamp"] != END:
        raise ValueError("Wrong endpoint")
    if boundary_inventory["case"] != "hard_100_park":
        raise ValueError("Wrong PARK control")
    if boundary_inventory["summary"] != boundary_summary["summary"]:
        raise ValueError("Boundary sources disagree")
    summary = boundary_summary["summary"]
    pairs = {lot["pair"] for lot in boundary_inventory["active_lots"]}
    # Only interpret the immutable CSV snapshot that matched the source pin.
    with io.StringIO(verified["hard_100_park_equity.csv"].decode("utf-8"), newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDNAMES:
            raise ValueError("Unexpected saved account schema")
        rows = []
        for row in reader:
            timestamp = int(row["timestamp"])
            if timestamp >= END:
                break  # Later source observations are not interpreted.
            if timestamp < START:
                raise ValueError("Unexpected pre-January row")
            rows.append({**row, "observation_source": "SAVED_HOURLY_ACCOUNT"})
    expected_times = [START + 300, *range(START + 3600, END, 3600)]
    if [int(row["timestamp"]) for row in rows] != expected_times:
        raise ValueError("Saved hourly prefix differs from the exact January-June calendar")
    if rows[0]["cash"] != "2000" or rows[0]["equity"] != "2000" or rows[0]["marked_value"] != "0":
        raise ValueError("Saved beginning differs from the declared $2000 zero-position control")
    rows.append({
        "timestamp": str(END),
        "cash": summary["cash"],
        "committed_basis": summary["committed_basis"],
        "marked_value": summary["marked_inventory"],
        "equity": summary["equity"],
        "active_positions": str(summary["parents"]),
        "unique_base_assets": str(len(pairs)),
        "observation_source": "EXACT_JUNE_BOUNDARY",
    })
    with localcontext() as context:
        context.prec = 80
        residuals = []
        for row in rows:
            cash, basis, value, equity = (Decimal(row[key]) for key in ("cash", "committed_basis", "marked_value", "equity"))
            if not all(x.is_finite() for x in (cash, basis, value, equity)) or min(cash, basis, value) < 0:
                raise ValueError("Invalid account observation")
            residual = abs(cash + value - equity)
            if residual > TOLERANCE:
                raise ValueError("Cash plus marked inventory fails saved equity reconciliation")
            residuals.append(residual)
        starting_equity = Decimal("2000")
        ending_equity = Decimal(summary["equity"])
        saved_profit = ending_equity - starting_equity
        saved_return_pct = saved_profit / starting_equity * 100
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / "PARK_2026_H1_Hourly_Observed.csv"
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(*FIELDNAMES, "observation_source"), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    receipt = {
        "schema": "spectra-saved-park-h1-observations-v1",
        "scope": "SAVED_HISTORICAL_MODEL_OBSERVATIONS_ONLY",
        "sources": {name: {"sha256": digest, "size_bytes": size, "matches_prior_research_pin": True} for name, (digest, size) in SOURCE_PINS.items()},
        "source_research_commit": "b6015c504a3bd24576f000d72dddd7208f55815c",
        "source_research_provenance_path": "docs/research-results/crash-defense-danger-room-20261010/data/Data_Provenance_Verification.json",
        "saved_hourly_rows": len(rows) - 1,
        "appended_exact_boundary_rows": 1,
        "total_observations": len(rows),
        "first_observation_timestamp": int(rows[0]["timestamp"]),
        "terminal_observation_timestamp": END,
        "initial_declared_equity": "2000",
        "initial_declared_owned_units": "0",
        "ending_equity": summary["equity"],
        "baseline_saved_profit_usd": str(saved_profit),
        "baseline_saved_return_percent": str(saved_return_pct),
        "max_cash_inventory_equity_residual_usd": str(max(residuals)),
        "reconciliation_tolerance_usd": str(TOLERANCE),
        "ending_distinct_asset_count_from_owned_lots": len(pairs),
        "timestamp_semantics": "Saved producer observes completed bars: dataset.timestamp(index) + 300. First account snapshot is January 1 00:05 UTC, then hourly completed snapshots, plus the separately pinned July 1 00:00 UTC terminal June boundary.",
        "spot_marked_exposure": "marked_value from the actual saved PARK account, not basis, future endpoint holdings or another arm.",
        "unobserved_spot_between_snapshots": True,
        "all_intrabar_spot_commitments_proven": False,
        "pending_spot_cash_replay_performed": False,
        "cash_owner_partitions_omitted_from_hourly_csv": True,
        "costs_embedded_in_baseline_equity_counted_once": True,
        "baseline_outcome_is_prior_development_evidence": True,
        "new_spot_strategy_replay_performed": False,
        "live_balances_or_fills_verified": False,
        "sealed_holdout_read": False,
        "future_boundary_used_as_initial_condition": False,
        "output_filename": output.name,
        "output_size_bytes": output.stat().st_size,
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "approved_for_live": False,
    }
    (output_directory / "park-observations.receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.source_directory, args.output_directory)
    print(json.dumps({key: result[key] for key in ("saved_hourly_rows", "total_observations", "ending_equity", "output_sha256")}))


if __name__ == "__main__":
    main()

