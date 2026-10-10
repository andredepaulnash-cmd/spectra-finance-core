"""Descriptive, audit-gated diagnostics; never a strategy or selection policy.

Only completed serialized evidence whose independent PASS receipt binds every
manifest member is read. This module neither runs strategies nor imports their
adapters, reads market/holdout tapes, ranks coins, or changes frozen assumptions.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from decimal import Decimal, Inexact, MAX_EMAX, MIN_EMIN, Rounded, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import gzip
import hashlib
from itertools import zip_longest
import json
from pathlib import Path


TOLERANCE = Fraction(1, 10**18)
OWNERS = ("PRODUCTIVE", "VCAP")
BASE = "BASE_ENTRY"
RECOVERY = "VCAP_RECOVERY"


def _number(value) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("boolean is not serialized monetary evidence")
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("nonfinite serialized monetary evidence")
    return Fraction(decimal)


def _text(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 60
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        context.rounding = ROUND_HALF_EVEN
        context.traps[Inexact] = context.traps[Rounded] = False
        context.clear_flags()
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path):
    with path.open() as handle:
        return json.load(handle)


def _json_lines(path: Path):
    with gzip.open(path, "rt") as handle:
        for line in handle:
            yield json.loads(line)


def verified_completed_inputs(run_directory: Path, accounting_receipt: Path) -> tuple[dict, dict, dict]:
    """Verify admission before parsing any run financial artifact.

    Returns the completed results, independent receipt and source-binding receipt.
    The independent proof is reused; this is not a duplicate full-path audit.
    """
    run_directory, accounting_receipt = Path(run_directory), Path(accounting_receipt)
    receipt = _json(accounting_receipt)
    if receipt.get("passed") is not True or receipt.get("status") != "PASS":
        raise ValueError("independent finalized accounting PASS is required")
    if receipt.get("arm") != run_directory.name:
        raise ValueError("audit/run identity differs")
    if (receipt.get("owner_proof") is not None or receipt.get("full_cash_owner_adapter_verified") is True) and (
            receipt.get("full_cash_owner_adapter_verified") is not True
            or (receipt.get("owner_proof") or {}).get("passed") is not True):
        raise ValueError("independent owner proof is incomplete")
    manifest_path = run_directory / "artifact_manifest.json"
    manifest = _json(manifest_path)
    if not isinstance(manifest, list) or not manifest or manifest != receipt.get("artifacts"):
        raise ValueError("manifest does not equal the independently audited member set")
    names = set()
    for member in manifest:
        name = member.get("file")
        if not isinstance(name, str) or not name or Path(name).name != name or name in names:
            raise ValueError("manifest member must have a unique local filename")
        names.add(name)
        path = run_directory / name
        if path.is_symlink() or path.resolve().parent != run_directory.resolve():
            raise ValueError("manifest member cannot escape the finalized run")
        if path.stat().st_size != member.get("bytes") or _sha(path) != member.get("sha256"):
            raise ValueError(f"finalized manifest member changed: {name}")
    if "results.json" not in names or _sha(run_directory / "results.json") != receipt.get("results_sha256"):
        raise ValueError("independent results digest differs")
    results = _json(run_directory / "results.json")
    if (results.get("status") != "COMPLETED" or results.get("completed_strategy_evaluation") is not True
            or results.get("holdout_accessed") is not False or results.get("arm") != receipt["arm"]):
        raise ValueError("only finalized development results are admissible")
    if results.get("declared_arm", {}).get("owned_cash"):
        if receipt.get("full_cash_owner_adapter_verified") is not True or (receipt.get("owner_proof") or {}).get("passed") is not True:
            raise ValueError("independent full owner proof is required")
    binding = {"arm": receipt["arm"], "run_directory": str(run_directory.resolve()),
               "accounting_receipt": str(accounting_receipt.resolve()),
               "accounting_receipt_sha256": _sha(accounting_receipt),
               "artifact_manifest_sha256": _sha(manifest_path),
               "results_sha256": receipt["results_sha256"], "artifacts": manifest,
               "input_array_sha256": receipt.get("input_array_sha256"),
               "input_metadata_sha256": receipt.get("input_metadata_sha256"),
               "research_adapter_sha256": receipt.get("research_adapter_sha256"),
               "policy_sources": receipt.get("policy_sources", {})}
    return results, receipt, binding


def source_attribution(sales: list[dict], lots: list[dict], terminal: list[dict]) -> dict:
    """Attribute exact disposed allocations and terminal marks to lot sources."""
    lot_map = {lot["lot_id"]: lot for lot in lots}
    if len(lot_map) != len(lots):
        raise ValueError("duplicate immutable lot identity")
    positions = {(str(row["position_id"]), row["pair"]): row for row in terminal}
    totals = {source: defaultdict(Fraction) for source in (BASE, RECOVERY)}
    seen_sales = set()
    for event in sales:
        identity = event["receipt_id"]
        if identity in seen_sales:
            raise ValueError("duplicate disposal receipt")
        seen_sales.add(identity)
        for allocation in event["allocations"]:
            lot = lot_map[allocation["lot_id"]]
            source = lot["source"]
            if source not in totals:
                raise ValueError("unrecognized immutable lot source")
            gross, exit_fee = _number(allocation["gross_proceeds"]), _number(allocation["exit_fee"])
            basis, entry_fee = _number(allocation["gross_basis_release"]), _number(allocation["entry_fee_release"])
            totals[source]["realized_net_pnl"] += gross - exit_fee - basis - entry_fee
            totals[source]["disposed_gross_proceeds"] += gross
            totals[source]["disposed_gross_basis"] += basis
            totals[source]["disposed_entry_fees"] += entry_fee
            totals[source]["exit_fees"] += exit_fee
    for lot in lots:
        source = lot["source"]
        if source not in totals:
            raise ValueError("unrecognized immutable lot source")
        quantity = _number(lot["base_remaining"])
        basis = _number(lot["cost_basis_remaining_quote"]) + _number(lot["entry_fees_remaining_quote"])
        totals[source]["open_basis"] += basis
        if quantity:
            position = positions[(str(lot["position_id"]), lot["pair"])]
            if position.get("mark") is None:
                raise ValueError("terminal lot lacks an authenticated mark")
            totals[source]["open_marked_value"] += quantity * _number(position["mark"])
    output = {}
    for source, values in totals.items():
        values["open_unrealized_pnl"] = values["open_marked_value"] - values["open_basis"]
        values["marked_total_pnl"] = values["realized_net_pnl"] + values["open_unrealized_pnl"]
        values["disposed_fees"] = values["disposed_entry_fees"] + values["exit_fees"]
        values["gross_realized_edge_before_disposed_fees"] = values["realized_net_pnl"] + values["disposed_fees"]
        output[source] = {key: _text(value) for key, value in values.items()}
    return output


def _stats(values: list[Fraction]) -> dict:
    if not values:
        raise ValueError("completed owner path is empty")
    return {"minimum": _text(min(values)), "mean": _text(sum(values, Fraction()) / len(values)),
            "maximum": _text(max(values)), "ending": _text(values[-1])}


def owner_cash_diagnostics(account_rows, owner_rows, *, fee_bps) -> dict:
    """Use already audited owner/account snapshots to describe usable cash."""
    usable = {owner: [] for owner in OWNERS}
    owner_cash = {owner: [] for owner in OWNERS}
    physical = []
    counts = Counter()
    entry_ticket = Fraction(1025, 100)
    recovery_minimum = 5 * (1 + _number(fee_bps) / 10000)
    for account, owned in zip_longest(account_rows, owner_rows):
        if account is None or owned is None or int(account["timestamp"]) != owned["timestamp"]:
            raise ValueError("owner/account sample identity differs")
        cash = {owner: _number(owned["cash_by_owner"][owner]) for owner in OWNERS}
        holds = {owner: _number(owned["held_by_owner"][owner]) for owner in OWNERS}
        reserve = max(_number(account["equity"]) / 4, Fraction(40))
        spendable = max(Fraction(), _number(owned["cash_total"]) - sum(holds.values()) - reserve)
        physical.append(spendable)
        for owner in OWNERS:
            owner_cash[owner].append(cash[owner])
            usable[owner].append(max(Fraction(), min(cash[owner] - holds[owner], spendable)))
        counts["samples"] += 1
        counts["productive_cash_can_fund_fixed_10_25_ticket"] += usable["PRODUCTIVE"][-1] >= entry_ticket
        counts["VCAP_cash_can_fund_modeled_minimum_gross_5_plus_fee"] += usable["VCAP"][-1] >= recovery_minimum
        counts["VCAP_cash_can_fund_native_40_ticket"] += usable["VCAP"][-1] >= 40
    return {"cash_by_owner": {owner: _stats(owner_cash[owner]) for owner in OWNERS},
            "usable_after_owner_holds_and_physical_reserve": {owner: _stats(usable[owner]) for owner in OWNERS},
            "physical_spendable_after_all_holds_and_max_25pct_equity_40_reserve": _stats(physical),
            "cash_only_readiness_counts": dict(counts),
            "cash_only_readiness_excludes_pair_route_identity_and_selection_admission": True,
            "modeled_recovery_minimum_all_in": _text(recovery_minimum)}


def rung_funding_diagnostics(assignments: list[dict], orders: list[dict], fills: list[dict],
                             profiles: dict, terminal: list[dict], research_counts: dict) -> dict:
    """Reconstruct only serialized submitted rungs, using confirmed-fill counts.

    Gate counters are repeated evaluations, not unique funding denials. No
    unexported rejected request or cap-specific decision is invented here.
    """
    if not assignments:
        return {"applicable": False, "reason": "no_ladder_assignment_events"}
    parents = {str(event["position_id"]): event for event in assignments}
    if len(parents) != len(assignments):
        raise ValueError("immutable parent received duplicate ladder assignments")
    confirmed = defaultdict(list)
    fill_by_order = {}
    for fill in fills:
        if fill["kind"] == "RECOVERY":
            confirmed[str(fill["position_id"])].append(int(fill["timestamp"]))
            fill_by_order[int(fill["order_id"])] = fill
    for times in confirmed.values():
        times.sort()
    unique_orders = {}
    for order in orders:
        if order["kind"] != "RECOVERY":
            continue
        identifier = int(order["order_id"])
        if identifier in unique_orders and unique_orders[identifier] != order:
            raise ValueError("serialized order identity changed")
        unique_orders[identifier] = order
    details = {}
    clipped_proposals = 0
    cent_shortfall_proposals = 0
    proposed_sum = Fraction()
    for order in sorted(unique_orders.values(), key=lambda row: (int(row["submitted_at"]), int(row["order_id"]))):
        parent_id = str(order["position_id"])
        parent = parents[parent_id]
        category = parent["category"]
        if category not in profiles:
            raise ValueError("submitted recovery has no authenticated frozen ladder profile")
        rung = bisect_right(confirmed[parent_id], int(order["submitted_at"]))
        multiples = profiles[category]["seed_multiples"]
        if rung >= len(multiples):
            raise ValueError("submitted recovery exceeds confirmed frozen ladder rungs")
        requested = _number(parent["entry_seed_all_in"]) * _number(multiples[rung])
        proposed = _number(order["budget"])
        clipped = proposed + TOLERANCE < requested
        clipped_proposals += clipped
        cent_shortfall_proposals += requested - proposed >= Fraction(1, 100)
        proposed_sum += proposed
        key = (parent_id, rung)
        entry = details.setdefault(key, {"parent_id": parent_id, "pair": parent["pair"],
                    "category": category, "rung_index": rung, "requested_all_in": requested,
                    "proposals": 0, "clipped_proposals": 0, "confirmed_all_in": Fraction(),
                    "confirmed_fills": 0, "proposed_budgets": []})
        entry["proposals"] += 1
        entry["clipped_proposals"] += clipped
        entry["proposed_budgets"].append(proposed)
        fill = fill_by_order.get(int(order["order_id"]))
        if fill:
            entry["confirmed_fills"] += 1
            entry["confirmed_all_in"] += _number(fill["gross_quote"]) + _number(fill["fee_quote"])
    if set(fill_by_order) - set(unique_orders):
        raise ValueError("confirmed recovery lacks a serialized submitted order")
    terminal_ids = {str(row["position_id"]) for row in terminal}
    exhausted = [identity for identity, parent in parents.items() if parent["category"] in profiles
                 and len(confirmed[identity]) >= len(profiles[parent["category"]]["seed_multiples"])]
    rows = []
    for entry in details.values():
        proposed = entry.pop("proposed_budgets")
        entry["minimum_proposed_all_in"] = _text(min(proposed))
        entry["maximum_proposed_all_in"] = _text(max(proposed))
        entry["confirmed_below_full_requested_rung"] = (entry["confirmed_fills"] > 0
                   and entry["confirmed_all_in"] + TOLERANCE < entry["requested_all_in"])
        entry["confirmed_shortfall_at_least_one_cent"] = (entry["confirmed_fills"] > 0
                   and entry["requested_all_in"] - entry["confirmed_all_in"] >= Fraction(1, 100))
        entry["requested_minus_confirmed_all_in"] = _text(entry["requested_all_in"] - entry["confirmed_all_in"])
        entry["requested_all_in"] = _text(entry["requested_all_in"])
        entry["confirmed_all_in"] = _text(entry["confirmed_all_in"])
        rows.append(entry)
    categories = Counter(parent["category"] for parent in parents.values())
    return {"applicable": True, "original_coin_map_recovered": False,
            "assignment_is_predeclared_new_research_hypothesis": True,
            "parent_category_counts": dict(categories), "assigned_parents": len(parents),
            "UNKNOWN_parents": categories["UNKNOWN"],
            "ending_UNKNOWN_parents": sum(parents[identity]["category"] == "UNKNOWN" for identity in terminal_ids),
            "submitted_recovery_orders": len(unique_orders), "clipped_submitted_proposals": clipped_proposals,
            "clipping_numerical_tolerance_USD": _text(TOLERANCE),
            "submitted_shortfalls_at_least_one_cent": cent_shortfall_proposals,
            "submitted_all_in_sum_including_retries": _text(proposed_sum),
            "unique_observed_parent_rungs": len(rows),
            "unique_observed_rung_requested_all_in": _text(sum((_number(row["requested_all_in"]) for row in rows), Fraction())),
            "confirmed_rung_all_in": _text(sum((_number(row["confirmed_all_in"]) for row in rows), Fraction())),
            "confirmed_rungs_below_full_requested_amount": sum(row["confirmed_below_full_requested_rung"] for row in rows),
            "confirmed_rung_shortfalls_at_least_one_cent": sum(row["confirmed_shortfall_at_least_one_cent"] for row in rows),
            "cent_threshold_is_descriptive_reporting_only_not_policy": True,
            "parents_reaching_all_rungs_consumed": len(exhausted),
            "ending_parents_with_all_rungs_consumed": sum(identity in terminal_ids for identity in exhausted),
            "gate_evaluations_not_unique_orders": {key: value for key, value in research_counts.items()
                                                   if key.startswith("LADDER_") or key == "UNKNOWN_LADDER_PROFILE"},
            "parent_rungs": rows,
            "unserialized_cap_specific_denial_amounts_or_rung_reset_events": "UNAVAILABLE"}


def describe_completed_run(run_directory: Path, accounting_receipt: Path, *, protocol_path: Path) -> dict:
    results, receipt, binding = verified_completed_inputs(run_directory, accounting_receipt)
    run_directory, protocol_path = Path(run_directory), Path(protocol_path)
    required = {"fills.csv", "ownership_events.json.gz", "terminal.json", "terminal_lots.json",
                "owner_transactions.jsonl.gz", "pending_orders.jsonl.gz", "account_5m.csv.gz",
                "owner_account_5m.jsonl.gz"}
    if not required.issubset({member["file"] for member in binding["artifacts"]}):
        raise ValueError("descriptive owner inputs must all be independently manifest-bound")
    if _sha(protocol_path) != results["protocol_sha256"]:
        raise ValueError("diagnostic ladder protocol differs from finalized results")
    protocol = _json(protocol_path)
    with (run_directory / "fills.csv").open() as handle:
        fills = list(csv.DictReader(handle))
    with gzip.open(run_directory / "ownership_events.json.gz", "rt") as handle:
        events = json.load(handle)
    terminal, lots = _json(run_directory / "terminal.json"), _json(run_directory / "terminal_lots.json")
    journal = list(_json_lines(run_directory / "owner_transactions.jsonl.gz"))
    sales = [event for event in journal if event["event"] == "SALE"]
    attribution = source_attribution(sales, lots, terminal)
    realized = sum((_number(value["realized_net_pnl"]) for value in attribution.values()), Fraction())
    unrealized = sum((_number(value["open_unrealized_pnl"]) for value in attribution.values()), Fraction())
    if (abs(realized - _number(receipt["independent_realized_pnl"])) > TOLERANCE
            or abs(unrealized - _number(receipt["independent_unrealized_pnl"])) > TOLERANCE):
        raise ValueError("source attribution differs from reused independent proof")
    buy_gross = sum((_number(fill["gross_quote"]) for fill in fills if fill["kind"] in {"ENTRY", "RECOVERY"}), Fraction())
    sell_gross = sum((_number(fill["gross_quote"]) for fill in fills if fill["kind"] in {"EXIT", "PARTIAL_RECYCLE"}), Fraction())
    fees = sum((_number(fill["fee_quote"]) for fill in fills), Fraction())
    if abs(fees - _number(receipt["independent_fees"])) > TOLERANCE:
        raise ValueError("serialized fill fees differ from independent proof")
    orders = {}
    for sample in _json_lines(run_directory / "pending_orders.jsonl.gz"):
        for order in sample["orders"]:
            if order["kind"] == "RECOVERY":
                if int(order["order_id"]) in orders and orders[int(order["order_id"])] != order:
                    raise ValueError("serialized recovery order identity changed")
                orders[int(order["order_id"])] = order
    denied = [event for event in journal if event["event"] == "DENIED"]
    for event in denied:
        if event["order"]["kind"] == "RECOVERY":
            orders[int(event["order"]["order_id"])] = event["order"]
    assignments = [event for event in events if event["event"] == "LADDER_ASSIGNMENT"]
    with gzip.open(run_directory / "account_5m.csv.gz", "rt") as handle:
        owner_cash = owner_cash_diagnostics(csv.DictReader(handle),
            _json_lines(run_directory / "owner_account_5m.jsonl.gz"), fee_bps=receipt["execution_fee_bps"])
    fee_edge = sum((_number(value["gross_realized_edge_before_disposed_fees"]) for value in attribution.values()), Fraction())
    disposed_fees = sum((_number(value["disposed_fees"]) for value in attribution.values()), Fraction())
    start = _number(results["financial_metrics"]["starting_cash"])
    turnover = buy_gross + sell_gross
    return {"schema_version": 1, "scope": "Completed audit-PASS development description; no policy tuning or coin selection",
            "independent_full_path_audit_reused": True, "holdout_read": False,
            "diagnostic_source_sha256": _sha(Path(__file__)), "source_binding": binding,
            "whole_account": {"marked_total_pnl": _text(realized + unrealized),
                "realized_net_pnl": _text(realized), "open_unrealized_pnl": _text(unrealized),
                "ending_equity": receipt["independent_equity"], "native_5m_drawdown": receipt["native_5m_drawdown"]},
            "immutable_lot_source_attribution": attribution,
            "turnover_fee_drag": {"buy_gross_quote": _text(buy_gross), "sell_gross_quote": _text(sell_gross),
                "total_gross_turnover": _text(turnover), "total_paid_fees": _text(fees),
                "fees_fraction_of_starting_capital": _text(fees / start),
                "turnover_over_starting_capital": _text(turnover / start),
                "paid_fee_bps_of_gross_turnover": _text(fees / turnover * 10000) if turnover else None,
                "disposed_fee_drag": _text(disposed_fees), "gross_realized_edge_before_disposed_fees": _text(fee_edge),
                "disposed_fees_fraction_of_positive_gross_realized_edge": _text(disposed_fees / fee_edge) if fee_edge > 0 else None,
                "gross_edge_is_accounting_addback_not_a_zero_fee_strategy_counterfactual": True},
            "owner_cash": owner_cash,
            "ladder_funding": rung_funding_diagnostics(assignments, list(orders.values()), fills,
                protocol["ladder_hypotheses"]["profiles"], terminal, results.get("research_counts", {})),
            "serialized_owner_funding_denials": {"orders": len(denied),
                "reasons": dict(Counter(event["reason"] for event in denied)),
                "requested_all_in_sum_including_retries": _text(sum((_number(event["requested_all_in"]) for event in denied), Fraction())),
                "aggregate_unjournaled_counts": results.get("owner_model", {}).get("funding_counts", {})},
            "aggregate_native_rejections_not_ladder_specific": results.get("rejection_counts", {}),
            "occupancy_reused_independent_proof": {"occupied_slot_fraction": receipt["occupied_slot_fraction"],
                "all_minutes_below50_including_initialization": receipt["all_minutes_below50_including_initialization"],
                "ending_positions": len(terminal), "target_positions": 50,
                "maximum_funded_active_or_pending_asset_claims": receipt.get("owner_proof", {}).get("maximum_funded_active_or_pending_asset_claims")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--accounting-receipt", required=True, type=Path)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = describe_completed_run(args.run, args.accounting_receipt, protocol_path=args.protocol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"arm": report["source_binding"]["arm"], "output": str(args.output),
                      "output_sha256": _sha(args.output)}))


if __name__ == "__main__":
    main()
