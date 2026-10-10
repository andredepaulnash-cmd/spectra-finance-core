"""Source-bound, research-only continuous profitability campaign.

This module adapts the preserved native rotating-fleet research bridge. It does
not substitute a generic strategy for the product, activate a live module, or
assert exchange fills. Controls retain parked partial proceeds and the original
finite shared-cash reserve. All optional policy changes must be declared in a
frozen external protocol before historical outcomes are evaluated.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, getcontext
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from typing import Mapping

D = Decimal
ZERO = D(0)
ONE = D(1)
BPS = D(10000)
REFERENCE_ARRAY_SHA256 = "bf9ad858e2b696d189a42cb7f5d084f5312611bdc4b841f8a10219110ce552b1"
REFERENCE_METADATA_SHA256 = "0e8753e7436e8bff4a2065b9abdaddb8846a2546f1a107cefa4e7baaa3f00732"
ADAPTER_SHA256 = "45249e0273bfa4b15cfc54db2bede37b81c0488239fab1503c8911584127583e"
BRIDGE_SHA256 = "87247fd166775c3230445ed06e29f5a4ba2ad0e9253a9a52e3ff5e085f7db6c7"
GENERATED_NATIVE_SHA256 = "9260c013c5c85088f0098d76d39f754b456d581d93e8dda98c5bbf0711d6b838"
NATIVE_MANIFEST_SHA256 = "01fc7540c03c72113aeefea5187cbaae5d14f559e8b6dc90da8cc7aa9d6b4d08"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decimal(value, name: str, *, positive=False) -> D:
    out = D(str(value))
    if not out.is_finite() or out < ZERO or (positive and out <= ZERO):
        raise ValueError(f"{name} must be finite and nonnegative")
    return out


@dataclass(frozen=True)
class RecoveryRung:
    """Explicit hypothesis anchored to the original confirmed entry price."""
    drop_fraction: D
    seed_multiple: D

    def __post_init__(self):
        object.__setattr__(self, "drop_fraction", decimal(self.drop_fraction, "drop", positive=True))
        object.__setattr__(self, "seed_multiple", decimal(self.seed_multiple, "multiple", positive=True))
        if self.drop_fraction >= ONE:
            raise ValueError("a drawdown threshold must be below 100%")


@dataclass(frozen=True)
class RecoveryLadder:
    name: str
    rungs: tuple[RecoveryRung, ...]
    hypothesis: bool = True

    def __post_init__(self):
        if not self.name or not self.rungs or not self.hypothesis:
            raise ValueError("only explicitly named research hypotheses are admitted")
        drops = tuple(r.drop_fraction for r in self.rungs)
        if tuple(sorted(set(drops))) != drops:
            raise ValueError("ladder thresholds must be distinct and increasing")


class LadderBook:
    """Immutable parent anchors; rejected orders never consume a rung."""
    def __init__(self, profiles: Mapping[str, RecoveryLadder]):
        self.profiles = dict(profiles)
        self.parents: dict[int, dict] = {}
        self.reservations: dict[int, dict] = {}

    def confirmed_entry(self, position_id: int, pair: str, price: D, all_in: D):
        if position_id in self.parents:
            raise ValueError("parent identities cannot be reused")
        self.parents[position_id] = {"pair": pair, "anchor": decimal(price, "price", positive=True),
                                     "seed": decimal(all_in, "entry cash", positive=True), "consumed": 0,
                                     "profile": self.profiles.get(pair)}

    def requested_budget(self, position_id: int, pair: str, completed_close: D):
        parent = self.parents.get(position_id)
        profile = None if parent is None else parent["profile"]
        if parent is None or parent["pair"] != pair or profile is None:
            return None, "UNKNOWN_LADDER_PROFILE"
        index = parent["consumed"]
        if index >= len(profile.rungs):
            return None, "LADDER_EXHAUSTED"
        rung = profile.rungs[index]
        close = decimal(completed_close, "close", positive=True)
        if close > parent["anchor"] * (ONE - rung.drop_fraction):
            return None, "LADDER_THRESHOLD_NOT_REACHED"
        return parent["seed"] * rung.seed_multiple, "LADDER_RUNG_ELIGIBLE"

    def reserve_recovery(self, order_id: int, position_id: int, pair: str, budget: D):
        parent = self.parents.get(position_id)
        if (order_id in self.reservations or parent is None or parent["pair"] != pair
                or parent["profile"] is None):
            raise ValueError("recovery reservation identity differs")
        if any(r["parent_id"] == position_id for r in self.reservations.values()):
            raise ValueError("one pending rung per parent is permitted")
        rung = parent["consumed"]
        if rung >= len(parent["profile"].rungs):
            raise ValueError("no unconsumed rung remains")
        requested = decimal(budget, "reservation", positive=True)
        maximum = parent["seed"] * parent["profile"].rungs[rung].seed_multiple
        if requested > maximum + D("1e-18"):
            raise ValueError("reservation exceeds the declared rung maximum")
        self.reservations[order_id] = {"parent_id": position_id, "pair": pair,
                                       "rung": rung, "maximum_cash": requested}

    def rejected_order(self, order_id: int):
        """Release an exact FOK rejection; consumption is unchanged."""
        self.reservations.pop(order_id, None)

    def confirmed_recovery(self, position_id: int, pair: str, quantity: D, cash_consumed: D,
                           *, order_id: int):
        if decimal(quantity, "quantity", positive=True) <= ZERO or decimal(cash_consumed, "cash", positive=True) <= ZERO:
            raise ValueError("positive confirmed recovery is required")
        parent = self.parents.get(position_id)
        if parent is None or parent["pair"] != pair or parent["profile"] is None:
            raise ValueError("recovery has no immutable ladder parent")
        reservation = self.reservations.get(order_id)
        if (reservation is None or reservation["parent_id"] != position_id
                or reservation["pair"] != pair or reservation["rung"] != parent["consumed"]
                or cash_consumed > reservation["maximum_cash"] + D("1e-18")):
            raise ValueError("confirmed recovery differs from its accepted rung reservation")
        if parent["consumed"] >= len(parent["profile"].rungs):
            raise ValueError("no unconsumed rung remains")
        parent["consumed"] += 1
        del self.reservations[order_id]


def replace_once(source: str, before: str, after: str):
    count = source.count(before)
    if count != 1:
        raise ValueError(f"research adapter boundary must occur exactly once: {count}")
    return source.replace(before, after), {"before": before, "after": after}


def build_campaign_body(native_source: str, *, selection=False, directional=False,
                        recovery_quality=False, ladder=False, entry_gate=False):
    """Apply auditable boundaries; retain the native accounting/exit controller."""
    source = native_source
    changes = []
    def change(before, after):
        nonlocal source
        source, receipt = replace_once(source, before, after)
        changes.append(receipt)
    # A passive end-of-frame observer does not alter native decisions.
    change("        pending = exit_orders + partial_orders + recovery_orders + entry_orders",
           "        pending = exit_orders + partial_orders + recovery_orders + entry_orders\n"
           "        dataset.observe_account(locals())")
    if selection:
        change("        candidates.sort(key=movement_rank)",
               "        candidates = dataset.rank_candidates(candidates, timestamp)")
    if directional:
        change("            and pair not in closed_this_frame\n",
               "            and pair not in closed_this_frame\n"
               "            and dataset.directional_entry_allowed(pair, index)\n")
    if entry_gate:
        change("            and pair not in pending_pairs\n",
               "            and pair not in pending_pairs\n"
               "            and dataset.entry_gate_allowed(index)\n")
    if recovery_quality:
        change("            proposal = CapitalProposal(\n",
               "            if not dataset.recovery_quality_allowed(pair, index):\n"
               "                continue\n"
               "            proposal = CapitalProposal(\n")
    if ladder:
        change("            if not dataset.recovery_enabled or bar.close >= average * (ONE - cfg.recovery_trigger) or spendable <= ZERO:\n",
               "            ladder_budget = dataset.ladder_budget(position, bar.close)\n"
               "            if not dataset.recovery_enabled or ladder_budget is None or spendable <= ZERO:\n")
        change("                recovery.planned_amount_quote,\n",
               "                recovery.planned_amount_quote,\n"
               "                ladder_budget / (ONE + fee_rate),\n")
        change("            recovery_orders.append(\n"
               "                next_order(\n"
               "                    timestamp,\n"
               "                    pair,\n"
               '                    "RECOVERY",\n'
               "                    budget=budget,\n"
               "                    position_id=position.position_id,\n"
               "                )\n"
               "            )",
               "            recovery_order = next_order(timestamp, pair, 'RECOVERY',\n"
               "                budget=budget, position_id=position.position_id)\n"
               "            dataset.note_recovery_order(recovery_order, position)\n"
               "            recovery_orders.append(recovery_order)")
        change("        rejection_counts[reason] += 1\n",
               "        rejection_counts[reason] += 1\n"
               "        dataset.note_rejection(order, reason)\n")
    return source, changes


def verify_reference(prepared: Path):
    """Verify the entire original six-month tape before loading outcomes."""
    import numpy as np
    prepared = Path(prepared)
    identities = {"metadata_sha256": sha256(prepared / "metadata.json"),
                  "array_sha256": sha256(prepared / "ohlcv.npy")}
    if identities != {"metadata_sha256": REFERENCE_METADATA_SHA256,
                      "array_sha256": REFERENCE_ARRAY_SHA256}:
        raise ValueError("the original six-month input identity differs")
    meta = json.loads((prepared / "metadata.json").read_text())
    rows = np.load(prepared / "ohlcv.npy", mmap_mode="r")
    if rows.shape != (52128, 619, 6) or rows.dtype != np.dtype("float64"):
        raise ValueError("the original fixed-universe geometry differs")
    if (meta["start"], meta["end"], meta["interval_seconds"]) != (1767225600, 1782864000, 300):
        raise ValueError("unexpected replay period")
    return {**identities, "shape": list(rows.shape), "observed_only": True, "holdout_accessed": False}


def verify_native(core: Path, manifest: Path, adapter: Path, bridge: Path):
    if sha256(manifest) != NATIVE_MANIFEST_SHA256:
        raise ValueError("archived prerequisite manifest identity differs")
    inventory = json.loads(Path(manifest).read_text())["external_native_core"]["actually_imported_modules"]
    records = []
    for record in inventory:
        path = Path(core) / Path(record["path"]).name
        actual = sha256(path)
        if actual != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise ValueError(f"preserved native source differs: {path.name}")
        records.append({"file": path.name, "sha256": actual})
    if sha256(adapter) != ADAPTER_SHA256 or sha256(bridge) != BRIDGE_SHA256:
        raise ValueError("preserved research bridge differs")
    if len(records) != 23:
        raise ValueError("all 23 imported preserved native modules are required")
    return records


def load_native(core: Path, adapter: Path, bridge: Path, native_inventory=None):
    """Import only after verify_native; each CLI invocation is a fresh process."""
    if "spectra_core" in sys.modules or "sparse_adapter" in sys.modules:
        raise RuntimeError("native replay requires a fresh isolated interpreter")
    sys.path.insert(0, str(Path(core).parent.resolve()))
    spec = importlib.util.spec_from_file_location("sparse_adapter", Path(adapter))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    expected = {record["file"]: record["sha256"] for record in native_inventory or ()}
    for name, imported in tuple(sys.modules.items()):
        if name == "spectra_core" or name.startswith("spectra_core."):
            actual_file = Path(imported.__file__).resolve()
            if actual_file.parent != Path(core).resolve():
                raise ValueError(f"native module imported outside the verified source tree: {name}")
            if expected and (actual_file.name not in expected
                             or sha256(actual_file) != expected[actual_file.name]):
                raise ValueError(f"loaded native module differs from its verified bytes: {name}")
    # The import uses the explicit parent path first. Later source references
    # are bound to these same verified files, rather than the obsolete workspace.
    module.ARCHIVE_SOURCE = Path(core).parent.resolve()
    spec = importlib.util.spec_from_file_location("spectra_profitability_native_bridge", Path(bridge))
    bridge_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge_module
    spec.loader.exec_module(bridge_module)
    if hashlib.sha256(bridge_module.GENERATED_SOURCE.encode()).hexdigest() != GENERATED_NATIVE_SHA256:
        raise ValueError("generated preserved native financial body differs")
    bridge_module.CASES["hard_174_park"] = dict(whole_target_bps="174", partial_target_bps="174",
        hard_target=True, execution_floor_bps="174", park_proceeds=True)
    return bridge_module


def causal_anchor_allowed(rows, pair_index: int, index: int):
    """Entry evidence at this completed close; no execution-bar information."""
    import numpy as np
    if index < 288:
        return False
    values = rows[[index, index - 12, index - 288], pair_index, :]
    return bool(np.isfinite(values).all() and (values[:, :4] > 0).all()
                and values[0, 3] > values[1, 3] and values[0, 3] > values[2, 3])


def causal_recovery_allowed(rows, pair_index: int, index: int):
    """A frozen, exploratory six-bar improvement gate; no profitability claim."""
    import numpy as np
    if index < 5:
        return False
    values = rows[index - 5:index + 1, pair_index, :]
    return bool(np.isfinite(values).all() and (values[:, :4] > 0).all()
                and values[-1, 3] > values[-4, 3]
                and values[-3:, 2].min() > values[:3, 2].min())


def validate_declaration(protocol, *, expected_sha256: str, protocol_path: Path, arm: str):
    if sha256(protocol_path) != expected_sha256:
        raise ValueError("protocol differs from the root freeze acknowledgment")
    if protocol.get("research_only") is not True or protocol.get("production_activation") is not False:
        raise ValueError("research-only authority is required")
    if protocol.get("holdout_access") is not False:
        raise ValueError("sealed holdout access must be disabled")
    arms = {a["name"]: a for a in protocol["arms"]}
    if len(arms) != len(protocol["arms"]) or arm not in arms:
        raise ValueError("arm is not uniquely predeclared")
    return arms[arm]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--native-core", type=Path, required=True)
    parser.add_argument("--native-manifest", type=Path, required=True)
    parser.add_argument("--native-adapter", type=Path, required=True)
    parser.add_argument("--native-bridge", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_campaign(args)


def run_campaign(args):
    protocol = json.loads(Path(args.protocol).read_text())
    arm = validate_declaration(protocol, expected_sha256=args.protocol_sha256,
        protocol_path=Path(args.protocol), arm=args.arm)
    blocked = arm.get("blocked")
    if arm.get("age_rotation") or arm.get("fee_rotation"):
        blocked = arm["implementation_gate"]
    if arm.get("redeploy_confirmed_partial"):
        blocked = "Exact productive/VCAP cash-owner integration is unavailable; native shared-cash reuse is not permitted as an owner-preserving substitute."
    if arm.get("ladder_assignment"):
        blocked = "The new ladder's exact productive/VCAP cash-owner authorization and protected pending locks require a separately validated owner adapter. Native shared-cash authorization is insufficient."
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("refusing to overwrite earlier campaign evidence")
    output.mkdir(parents=True, exist_ok=True)
    if blocked:
        (output / "blocked.json").write_text(json.dumps({"arm": args.arm, "status": "BLOCKED",
            "completed_strategy_evaluation": False, "reason": blocked,
            "protocol_sha256": args.protocol_sha256}, indent=2) + "\n")
        raise RuntimeError(blocked)
    admitted_engine_sha256 = sha256(Path(__file__))
    admission = {"arm": args.arm, "protocol_sha256": args.protocol_sha256,
        "research_adapter_sha256": admitted_engine_sha256,
        "native_manifest_sha256": sha256(Path(args.native_manifest)),
        "native_adapter_sha256": sha256(Path(args.native_adapter)),
        "native_bridge_sha256": sha256(Path(args.native_bridge)),
        "expected_reference_array_sha256": REFERENCE_ARRAY_SHA256,
        "expected_reference_metadata_sha256": REFERENCE_METADATA_SHA256,
        "research_only": True, "production_activation": False, "holdout_accessed": False}
    from datetime import datetime, timezone
    admission["recorded_before_strategy_imports_at"] = datetime.now(timezone.utc).isoformat()
    (output / "run_admission.receipt.json").write_text(json.dumps(admission, indent=2) + "\n")
    # Every source/input check precedes strategy imports and outcomes.
    native_sources = verify_native(Path(args.native_core), Path(args.native_manifest),
        Path(args.native_adapter), Path(args.native_bridge))
    input_identity = verify_reference(Path(args.prepared))
    if getcontext().prec != 28 or getcontext().rounding != "ROUND_HALF_EVEN":
        raise ValueError("preserved native Decimal context must remain 28/HALF_EVEN")
    native = load_native(Path(args.native_core), Path(args.native_adapter), Path(args.native_bridge), native_sources)
    selection = bool(arm.get("selection"))
    ladder = arm.get("ladder_assignment") == "prior_completed_drawdown"
    use_selector = selection or ladder
    source, changes = build_campaign_body(native.GENERATED_SOURCE,
        selection=selection, directional=bool(arm.get("directional")),
        recovery_quality=bool(arm.get("recovery_quality")),
        ladder=ladder, entry_gate=bool(arm.get("warmup_days")) or bool(arm.get("regime_gate")))
    selector_module = None
    if use_selector:
        selector_path = Path(__file__).with_name("opportunity_selector.py")
        spec = importlib.util.spec_from_file_location("spectra_campaign_selector", selector_path)
        selector_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = selector_module
        spec.loader.exec_module(selector_module)
    regime_module = None
    if arm.get("regime_gate"):
        regime_path = Path(__file__).with_name("market_regime.py")
        spec = importlib.util.spec_from_file_location("spectra_campaign_regime", regime_path)
        regime_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = regime_module
        spec.loader.exec_module(regime_module)
    account_stream = gzip.open(output / "account_5m.csv.gz", "wt", newline="", compresslevel=1)
    account_writer = csv.DictWriter(account_stream, fieldnames=["timestamp", "cash", "committed_basis",
        "marked_value", "equity", "active_positions", "unique_base_assets", "parked_cash",
        "deployable_cash", "cumulative_realized", "cumulative_fees", "pending_orders"])
    account_writer.writeheader()
    pending_stream = gzip.open(output / "pending_orders.jsonl.gz", "wt", compresslevel=1)
    watch_stream = gzip.open(output / "watchlist_updates.jsonl.gz", "wt", compresslevel=1)
    OriginalDataset = native.Dataset
    profiles = {name: RecoveryLadder(name, tuple(RecoveryRung(drop, multiple)
        for drop, multiple in zip(profile["drop_fractions"], profile["seed_multiples"])))
        for name, profile in protocol["ladder_hypotheses"]["profiles"].items()}

    class CampaignDataset(OriginalDataset):
        def __init__(self):
            original_init = native.a.SparseDataset.__init__
            def redirected(instance, ignored_path, **kwargs):
                return original_init(instance, Path(args.prepared), **kwargs)
            native.a.SparseDataset.__init__ = redirected
            try:
                super().__init__(case=arm["case"], enabled=True, frames=None, trace_path=None)
            finally:
                native.a.SparseDataset.__init__ = original_init
            self.pair_indices = {pair: i for i, pair in enumerate(self.pairs)}
            self.ladders = LadderBook({})
            self.research_counts = Counter()
            self.selector = (selector_module.MatrixOpportunitySelector(self.rows, self.pairs,
                self.start, config=selector_module.SelectorConfig(**protocol["selector_config"]))
                if use_selector else None)
            self.weekly_members = None
            self.next_weekly_refresh = None
            self.last_watched_day = None
            self.decision_digest = None
            self.final_pending = []
            self.manifest.update(campaign_protocol_sha256=args.protocol_sha256,
                research_adapter_sha256=admitted_engine_sha256, experimental_arm=args.arm,
                continuous_month_boundary_resets=False,
                cash_owner_model="original shared fungible cash with source-colored parked recovery proceeds; no complete production cash-owner buckets",
                selector_screening_proxy_only=selection, recovered_original_coin_ladders=False)

        def observe_account(self, state):
            sample = state["samples"][-1]
            row = asdict(sample)
            parked = self.parked_cash()
            row.update(parked_cash=parked, deployable_cash=sample.cash - parked,
                cumulative_realized=state["realized"], cumulative_fees=state["fees"],
                pending_orders=len(state["pending"]))
            account_writer.writerow(row)
            pending_stream.write(json.dumps({"timestamp": sample.timestamp,
                "orders": [asdict(o) for o in state["pending"]]}, default=str) + "\n")
            self.decision_digest = state["decision_digest"].hexdigest()
            self.final_pending = [asdict(o) for o in state["pending"]]

        def rank_candidates(self, candidates, timestamp):
            ordered, diagnostics = self.selector.rank_at_index(self.current_index + 1)
            if arm.get("list_refresh_days"):
                if self.next_weekly_refresh is None or timestamp >= self.next_weekly_refresh:
                    self.weekly_members = tuple(ordered)
                    self.next_weekly_refresh = timestamp + int(arm["list_refresh_days"]) * 86400
                ordered = [pair for pair in self.weekly_members
                    if diagnostics[pair].get("eligible")]
            allowed = set(candidates)
            selected = [pair for pair in ordered if pair in allowed]
            self.research_counts["selector_denied_candidate_steps"] += len(allowed) - len(selected)
            day = timestamp // 86400
            if day != self.last_watched_day:
                watch_stream.write(json.dumps({"timestamp": timestamp,
                    "evidence_cutoff": timestamp, "ordered_pairs": ordered,
                    "diagnostics": diagnostics}, default=str) + "\n")
                self.last_watched_day = day
            return selected

        def directional_entry_allowed(self, pair, index):
            allowed = causal_anchor_allowed(self.rows, self.pair_indices[pair], index)
            self.research_counts["directional_allowed" if allowed else "directional_denied"] += 1
            return allowed

        def entry_gate_allowed(self, index):
            if index + 1 < int(arm.get("warmup_days", 0)) * 288:
                self.research_counts["warmup_denied"] += 1
                return False
            if arm.get("regime_gate"):
                allowed, diagnostic = regime_module.causal_market_regime(self.rows, index)
                self.research_counts["regime_allowed" if allowed else "regime_denied"] += 1
                return allowed
            return True

        def recovery_quality_allowed(self, pair, index):
            allowed = causal_recovery_allowed(self.rows, self.pair_indices[pair], index)
            self.research_counts["recovery_quality_allowed" if allowed else "recovery_quality_denied"] += 1
            return allowed

        def ladder_budget(self, position, close):
            budget, reason = self.ladders.requested_budget(position.position_id, position.pair, close)
            self.research_counts[reason] += 1
            return budget

        def note_recovery_order(self, order, position):
            self.ladders.reserve_recovery(order.order_id, position.position_id, position.pair, order.budget)

        def note_rejection(self, order, reason):
            self.ladders.rejected_order(order.order_id)

        def record_buy(self, fill, position, cash_before):
            super().record_buy(fill, position, cash_before)
            if not ladder:
                return
            if fill.kind == "ENTRY":
                # Fill happens before the execution candle is ingested. Only
                # rows strictly before this actual next-open fill enter the
                # category, never the execution bar's high/low/close.
                _, diagnostics = self.selector.rank_at_index(self.current_index)
                evidence = diagnostics[fill.pair]
                if (evidence.get("observed_completed_days", 0) >= protocol["selector_config"]["minimum_history_days"]
                        and evidence.get("recent_coverage", 0) >= protocol["selector_config"]["minimum_recent_coverage"]):
                    drawdown = evidence["observed_close_drawdown_fraction"]
                    name = "shallow" if drawdown <= 0.1 else "medium" if drawdown <= 0.2 else "deep"
                    self.ladders.profiles[fill.pair] = profiles[name]
                else:
                    name = "UNKNOWN"
                    self.ladders.profiles.pop(fill.pair, None)
                self.research_counts[f"ladder_parent_category_{name}"] += 1
                self.ladders.confirmed_entry(fill.position_id, fill.pair, fill.price,
                    fill.gross_quote + fill.fee_quote)
                self.events.append({"event": "LADDER_ASSIGNMENT", "position_id": fill.position_id,
                    "pair": fill.pair, "category": name, "entry_anchor": fill.price,
                    "entry_seed_all_in": fill.gross_quote + fill.fee_quote,
                    "evidence_cutoff": fill.timestamp - self.interval_seconds,
                    "source": "new predeclared causal research hypothesis"})
            elif fill.kind == "RECOVERY":
                self.ladders.confirmed_recovery(fill.position_id, fill.pair, fill.quantity,
                    fill.gross_quote + fill.fee_quote, order_id=fill.order_id)

    data = CampaignDataset()
    namespace = dict(native.a.original.__dict__)
    namespace["FleetOrder"] = native.ResearchOrder
    exec(compile(source, str(output / "generated_campaign_body.py"), "exec"), namespace)
    class CampaignSimulator(native.a.original.RotatingFleetSimulator):
        run = namespace["run"]
    cfg = replace(native.a.original.FleetConfig(), initial_cash=D(arm["capital"]),
        fee_bps=D(arm["fee_bps"]), slippage_bps=D(arm["slippage_bps"]))
    simulator = CampaignSimulator(cfg,
        exit_policy=replace(native.a.VCAPProfitExitPolicy(), target_net_profit_bps=data.whole_target_bps),
        exit_strategy=replace(native.a.original.FleetExitStrategy(), mode=native.a.original.FleetExitMode.HARD_TARGET))
    started = time.monotonic()
    try:
        result = simulator.run(data)
    finally:
        account_stream.close()
        pending_stream.close()
        watch_stream.close()
    elapsed = time.monotonic() - started
    if sha256(Path(__file__)) != admitted_engine_sha256:
        raise ValueError("campaign adapter changed while the admitted evaluation was running")
    # Every fill and surviving lot is serialized for an independent replay of
    # cash/quantity/basis arithmetic; this is not just a summary test.
    with (output / "fills.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(result.fills[0])) if result.fills else
            ["timestamp", "order_id", "pair", "kind", "position_id", "quantity", "price", "gross_quote",
             "fee_quote", "basis_released", "realized_pnl", "dependency_order_id"])
        writer.writeheader()
        writer.writerows(asdict(fill) for fill in result.fills)
    with gzip.open(output / "ownership_events.json.gz", "wt", compresslevel=1) as stream:
        json.dump(data.events, stream, default=str)
    final_at = data.timestamp(data.frames - 1) + data.interval_seconds
    terminal = [{**asdict(position), "mark": data.marks[position.pair],
        "mark_timestamp": data.last_seen[position.pair],
        "mark_age_seconds": final_at - data.last_seen[position.pair],
        "position_age_seconds": final_at - position.opened_at}
        for position in result.positions]
    (output / "terminal.json").write_text(json.dumps(terminal, default=str, indent=2) + "\n")
    (output / "terminal_lots.json").write_text(json.dumps([asdict(lot) for lot in data.ledger.lots.values()],
        default=str, indent=2) + "\n")
    months = {}
    from datetime import datetime, timezone
    previous_equity = cfg.initial_cash
    for sample in result.samples:
        month = datetime.fromtimestamp(sample.timestamp - data.interval_seconds, timezone.utc).strftime("%Y-%m")
        if month not in months:
            months[month] = {"starting_equity": previous_equity, "ending_equity": sample.equity,
                "realized_pnl": ZERO, "fees": ZERO, "frames": 0}
        months[month]["ending_equity"] = sample.equity
        months[month]["frames"] += 1
        previous_equity = sample.equity
    for fill in result.fills:
        month = datetime.fromtimestamp(fill.timestamp - data.interval_seconds, timezone.utc).strftime("%Y-%m")
        months[month]["realized_pnl"] += fill.realized_pnl
        months[month]["fees"] += fill.fee_quote
    for values in months.values():
        values["marked_pnl"] = values["ending_equity"] - values["starting_equity"]
    payload = {"arm": args.arm, "status": "COMPLETED", "completed_strategy_evaluation": True,
        "research_only": True, "production_activation": False, "holdout_accessed": False,
        "protocol_sha256": args.protocol_sha256, "input_identity": input_identity,
        "native_sources": native_sources, "native_manifest_sha256": sha256(Path(args.native_manifest)),
        "native_generated_body_sha256": GENERATED_NATIVE_SHA256,
        "research_generated_body_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "research_adapter_sha256": admitted_engine_sha256, "declared_changes": changes,
        "financial_metrics": result.metrics, "elapsed_seconds": elapsed,
        "decision_digest_sha256": data.decision_digest, "rejection_counts": result.rejections,
        "research_counts": dict(data.research_counts), "ending_parked_cash": data.parked_cash(),
        "ending_deployable_cash": result.cash - data.parked_cash(),
        "partial_fills": sum(fill.kind == "PARTIAL_RECYCLE" for fill in result.fills),
        "partial_principal_reused": data.principal_reused,
        "capital_dollar_days": sum((sample.committed_basis for sample in result.samples), ZERO) / D(288),
        "pending_terminal": data.final_pending, "monthly": months,
        "coverage": {"native_spot_selection": True, "native_recovery_planners": True,
            "native_partial_lot_recycling": True, "native_protected_net_exit_surrogate": True,
            "continuous_finite_cash_and_inventory": True, "recorded_volume_fok_surrogate": True,
            "productive_VCAP_cash_owner_integration": False, "full_API_lifecycle": False,
            "live_orderbook_or_pair_specific_historical_minima": False, "actual_hedge_route": False,
            "DTSS_enabled": False, "DTSSR_enabled": False, "PSSM_enabled": False},
        "interpretation": "Previously inspected development history; recorded OHLC, estimated next-open FOK execution and historical marks. Positive development PnL does not establish independent or live profitability."}
    (output / "results.json").write_text(json.dumps(payload, default=str, indent=2) + "\n")
    (output / "generated_campaign_body.py").write_text(source)
    artifacts = [{"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(output.iterdir()) if path.is_file()]
    (output / "artifact_manifest.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    print(json.dumps({"arm": args.arm, "status": "COMPLETED", "elapsed_seconds": elapsed,
        "ending_equity": str(result.metrics["ending_equity"]),
        "total_pnl": str(result.metrics["total_pnl"]),
        "drawdown": str(result.metrics["max_drawdown_fraction"]),
        "source_owner_integration": False}), flush=True)
    return payload


if __name__ == "__main__":
    main()
