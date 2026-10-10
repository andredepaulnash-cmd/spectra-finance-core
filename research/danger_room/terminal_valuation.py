"""Read-only, universally reported terminal-mark valuation sensitivities.

This module neither sells positions nor admits a strategy finalist. Marks can
be stale or commercially unobtainable. Fees and slippage are declared research
assumptions, not an authenticated historical account tier or executable quote.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence


D = Decimal
RECONCILIATION_TOLERANCE = D("1e-18")
TERMINAL_PROTOCOL_SHA256 = "a0de80a3022c3e918f69d4565e7463a032f408dcf551ac598e876862f184e909"
DISCLOSURE = (
    "Valuation sensitivities only: no orders, fills, released cash or realizable "
    "liquidation are established. Pair minima, quantity increments, volume, "
    "order-book availability and historical listing/delisting completeness "
    "remain unverified. Stale-zero cases are hypothetical markdowns. The "
    "original frozen strategy-selection gate is unchanged."
)


def _decimal(value, name: str, *, positive=False) -> D:
    if isinstance(value, bool) or not isinstance(value, (str, int, D)):
        raise ValueError(f"{name} must be an exact decimal string, integer or Decimal")
    try:
        result = D(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} is malformed") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _integer(value, name: str, *, positive=False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise ValueError(f"{name} must be a {'positive' if positive else 'nonnegative'} integer")
    return value


def _same(a: D, b: D, name: str) -> None:
    if abs(a - b) > RECONCILIATION_TOLERANCE:
        raise ValueError(f"{name} does not reconcile within {RECONCILIATION_TOLERANCE}")


def evaluate_terminal(results: Mapping, terminal: Sequence[Mapping], *, fee_bps,
                      slippage_bps, expected_terminal_timestamp: int | None = None) -> dict:
    """Calculate the frozen sensitivities for a fully completed research arm."""
    if not isinstance(results, Mapping) or not isinstance(terminal, (list, tuple)):
        raise ValueError("results must be an object and terminal an array")
    if results.get("status") != "COMPLETED" or results.get("completed_strategy_evaluation") is not True:
        raise ValueError("only completed strategy evaluations may be analyzed")
    if results.get("research_only") is not True or results.get("production_activation") is not False:
        raise ValueError("research-only authority must be explicit")
    if results.get("holdout_accessed") is not False:
        raise ValueError("holdout access must be explicitly false")
    arm = results.get("arm")
    if not isinstance(arm, str) or not arm:
        raise ValueError("arm must be a nonempty string")
    metrics = results.get("financial_metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("financial_metrics are required")
    if expected_terminal_timestamp is not None:
        _integer(expected_terminal_timestamp, "expected_terminal_timestamp")
    with localcontext() as context:
        context.prec = 60
        cash = _decimal(metrics.get("ending_cash"), "ending_cash")
        recorded_equity = _decimal(metrics.get("ending_equity"), "ending_equity")
        starting_cash = _decimal(metrics.get("starting_cash"), "starting_cash", positive=True)
        recorded_basis = _decimal(metrics.get("open_basis"), "open_basis")
        n_positions = _integer(metrics.get("ending_positions"), "ending_positions")
        if n_positions != len(terminal):
            raise ValueError("ending_positions differs from terminal parent count")
        fee = _decimal(fee_bps, "fee_bps")
        slip = _decimal(slippage_bps, "slippage_bps")
        if fee >= 10000 or slip >= 10000:
            raise ValueError("each modeled exit-cost rate must be below proceeds")
        cost_factor = (1 - slip / 10000) * (1 - fee / 10000)
        marked = D(0)
        post_cost = D(0)
        basis = D(0)
        value_24h = D(0)
        value_7d = D(0)
        stale_24h_marked = D(0)
        stale_7d_marked = D(0)
        stale_24h_post_cost = D(0)
        stale_7d_post_cost = D(0)
        counts_24h = counts_7d = zeros = 0
        oldest = 0
        pairs = set()
        position_ids = set()
        parents = []
        terminal_timestamp = expected_terminal_timestamp
        for row in terminal:
            if not isinstance(row, Mapping):
                raise ValueError("each terminal parent must be an object")
            pair = row.get("pair")
            if not isinstance(pair, str) or not pair or pair in pairs:
                raise ValueError("terminal pairs must be nonempty and unique")
            position_id = _integer(row.get("position_id"), "position_id", positive=True)
            if position_id in position_ids:
                raise ValueError("terminal position IDs must be unique")
            pairs.add(pair)
            position_ids.add(position_id)
            quantity = _decimal(row.get("quantity"), "quantity", positive=True)
            mark = _decimal(row.get("mark"), "mark")
            parent_basis = _decimal(row.get("basis"), "basis")
            age = _integer(row.get("mark_age_seconds"), "mark_age_seconds")
            mark_timestamp = _integer(row.get("mark_timestamp"), "mark_timestamp")
            implied_timestamp = mark_timestamp + age
            if terminal_timestamp is None:
                terminal_timestamp = implied_timestamp
            if implied_timestamp != terminal_timestamp:
                raise ValueError("terminal mark timestamps and ages disagree")
            if "opened_at" in row or "position_age_seconds" in row:
                opened = _integer(row.get("opened_at"), "opened_at")
                position_age = _integer(row.get("position_age_seconds"), "position_age_seconds")
                if opened + position_age != terminal_timestamp:
                    raise ValueError("position age does not match terminal timestamp")
            value = quantity * mark
            hypothetical = value * cost_factor
            marked += value
            post_cost += hypothetical
            basis += parent_basis
            old_24h = age > 86400
            old_7d = age > 604800
            counts_24h += old_24h
            counts_7d += old_7d
            zeros += mark == 0
            oldest = max(oldest, age)
            if old_24h:
                stale_24h_marked += value
                stale_24h_post_cost += hypothetical
            else:
                value_24h += hypothetical
            if old_7d:
                stale_7d_marked += value
                stale_7d_post_cost += hypothetical
            else:
                value_7d += hypothetical
            parents.append({"position_id": position_id, "pair": pair,
                "quantity": str(quantity), "recorded_mark": str(mark),
                "mark_timestamp": mark_timestamp, "mark_age_seconds": age,
                "marked_value": str(value), "modeled_exit_cost_value": str(hypothetical),
                "stale_over_24h": old_24h, "stale_over_7d": old_7d})
        equity = cash + marked
        _same(equity, recorded_equity, "terminal marked equity")
        _same(basis, recorded_basis, "terminal open basis")
        raw_pnl = metrics.get("total_pnl")
        if isinstance(raw_pnl, bool) or not isinstance(raw_pnl, (str, int, D)):
            raise ValueError("total_pnl must be an exact decimal")
        try:
            recorded_pnl = D(raw_pnl)
        except InvalidOperation as exc:
            raise ValueError("total_pnl is malformed") from exc
        if not recorded_pnl.is_finite():
            raise ValueError("total_pnl must be finite")
        _same(recorded_equity - starting_cash, recorded_pnl, "recorded total PnL")
        sensitivities = {"marked": equity, "modeled_exit_cost": cash + post_cost,
                         "stale_over_24h_zero_value": cash + value_24h,
                         "stale_over_7d_zero_value": cash + value_7d}
        return {"schema_version": 1, "arm": arm, "status": "ANALYZED_COMPLETED_ARM",
            "research_only": True, "orders_or_fills_created": False,
            "strategy_evaluations_added": 0, "strategy_finalist_gate_changed": False,
            "holdout_accessed": False, "production_activation": False,
            "decimal_precision": 60, "reconciliation_tolerance": str(RECONCILIATION_TOLERANCE),
            "reconciliation_tolerance_basis": "Same absolute1e-18 as source-bound native cash/quantity accounting; Decimal60 recomputation avoids inherited28digit product rounding.",
            "reconciliation": {"status": "PASS", "recorded_equity": str(recorded_equity),
                "independently_marked_equity": str(equity),
                "difference": str(equity - recorded_equity),
                "terminal_basis": str(basis), "recorded_basis": str(recorded_basis)},
            "declared_modeled_exit_costs": {"fee_bps": str(fee), "slippage_bps": str(slip),
                "multiplicative_value_factor": str(cost_factor), "actual_venue_execution_proven": False},
            "ending_cash": str(cash), "starting_cash": str(starting_cash),
            "terminal_timestamp": terminal_timestamp, "terminal_parent_count": len(terminal),
            "zero_mark_parent_count": zeros, "oldest_mark_age_seconds": oldest,
            "terminal_marked_inventory_value": str(marked),
            "stale_marks": {"over_24h": {"count": counts_24h,
                "marked_value": str(stale_24h_marked), "modeled_exit_cost_value": str(stale_24h_post_cost)},
                "over_7d": {"count": counts_7d, "marked_value": str(stale_7d_marked),
                "modeled_exit_cost_value": str(stale_7d_post_cost)}},
            "valuation_sensitivities": {name: {"equity": str(value),
                "pnl": str(value - starting_cash), "return_fraction": str(value / starting_cash - 1)}
                for name, value in sensitivities.items()},
            "parents": parents, "disclosure": DISCLOSURE}


def _read_json(path: Path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(), parse_float=D, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze_files(results_path: Path, terminal_path: Path, terminal_protocol_path: Path,
                  *, strategy_protocol_paths: Sequence[Path] = ()) -> dict:
    """Bind exact source files and fee declarations before evaluating values."""
    results_path, terminal_path, terminal_protocol_path = map(Path,
        (results_path, terminal_path, terminal_protocol_path))
    if _hash(terminal_protocol_path) != TERMINAL_PROTOCOL_SHA256:
        raise ValueError("terminal valuation protocol differs from frozen identity")
    protocol = _read_json(terminal_protocol_path)
    if protocol.get("research_only") is not True or protocol.get("holdout_access") is not False:
        raise ValueError("terminal protocol authority differs")
    if results_path.parent.resolve() != terminal_path.parent.resolve():
        raise ValueError("results and terminal must belong to one completed arm directory")
    completion_manifest = results_path.parent / "artifact_manifest.json"
    manifest = _read_json(completion_manifest)
    if not isinstance(manifest, list):
        raise ValueError("completed artifact manifest must be an array")
    declared_artifacts = {}
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("malformed completed artifact manifest")
        if item["file"] in declared_artifacts:
            raise ValueError("duplicate completed artifact manifest entry")
        declared_artifacts[item["file"]] = item
    for path in (results_path, terminal_path):
        item = declared_artifacts.get(path.name)
        if item is None or item.get("bytes") != path.stat().st_size or item.get("sha256") != _hash(path):
            raise ValueError("completed artifact identity differs")
    results = _read_json(results_path)
    if not isinstance(results, dict):
        raise ValueError("results must be an object")
    if results.get("status") != "COMPLETED" or results.get("completed_strategy_evaluation") is not True:
        raise ValueError("only completed strategy evaluations may be read")
    bound_protocols = {}
    matching = []
    expected_terminal_timestamp = None
    for path in map(Path, strategy_protocol_paths):
        digest = _hash(path)
        strategy = _read_json(path)
        admitted = digest == results.get("protocol_sha256") or digest in (results.get("supplement_sha256s") or {}).values()
        if not admitted:
            raise ValueError("provided strategy protocol lacks result hash binding")
        bound_protocols[str(path)] = digest
        for candidate in strategy.get("arms", []) + strategy.get("additional_arms", []):
            if candidate.get("name") == results.get("arm"):
                matching.append((candidate, path, digest))
        end = strategy.get("window", {}).get("end_exclusive")
        if end is not None:
            if expected_terminal_timestamp is not None and expected_terminal_timestamp != end:
                raise ValueError("strategy terminal timestamp bindings disagree")
            expected_terminal_timestamp = end
    declared = results.get("declared_arm")
    if isinstance(declared, dict) and "fee_bps" in declared and "slippage_bps" in declared:
        if declared.get("name") != results.get("arm"):
            raise ValueError("declared arm identity differs")
        fee, slip = declared["fee_bps"], declared["slippage_bps"]
        binding = {"source": "results.json#/declared_arm", "path": str(results_path),
                   "sha256": _hash(results_path)}
        for candidate, _, _ in matching:
            if D(str(candidate["fee_bps"])) != D(str(fee)) or D(str(candidate["slippage_bps"])) != D(str(slip)):
                raise ValueError("declared costs differ from matching frozen arm")
    else:
        if len(matching) != 1:
            raise ValueError("legacy results require one exact frozen arm cost declaration")
        candidate, path, digest = matching[0]
        fee, slip = candidate.get("fee_bps"), candidate.get("slippage_bps")
        binding = {"source": "frozen_strategy_protocol#/arms", "path": str(path), "sha256": digest,
                   "name": candidate["name"]}
    terminal = _read_json(terminal_path)
    analysis = evaluate_terminal(results, terminal, fee_bps=fee, slippage_bps=slip,
                                 expected_terminal_timestamp=expected_terminal_timestamp)
    analysis["source_files"] = {str(results_path): _hash(results_path),
        str(terminal_path): _hash(terminal_path), str(terminal_protocol_path): _hash(terminal_protocol_path),
        str(completion_manifest): _hash(completion_manifest),
        **bound_protocols}
    analysis["cost_binding"] = binding
    analysis["analyzer_sha256"] = _hash(Path(__file__))
    return analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--terminal", type=Path, required=True)
    parser.add_argument("--terminal-protocol", type=Path, required=True)
    parser.add_argument("--strategy-protocol", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    analysis = analyze_files(args.results, args.terminal, args.terminal_protocol,
                             strategy_protocol_paths=args.strategy_protocol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError("output already exists; preserve prior analysis evidence")
    args.output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"arm": analysis["arm"], "status": analysis["status"],
                      "reconciliation": analysis["reconciliation"]["status"]}))


if __name__ == "__main__":
    main()
