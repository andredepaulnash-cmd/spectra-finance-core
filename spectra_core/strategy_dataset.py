"""Strict input provenance and fixed-split offline comparison campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .strategy_lab_validation import ResearchStage, ResearchTrial, StrategyLabStore
from .strategy_replay import BPS, ONE, Candidate, ReplayConfig, load_csv, replay
from .strategy_replay_vcap import ProductionPlannerAdapter


def digest(value):
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def audit_dataset(csv_path, manifest_path):
    path = Path(csv_path)
    manifest = json.loads(Path(manifest_path).read_text())
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    for field in (
        "source",
        "venue",
        "rules_source",
        "volume_source",
        "depth_definition",
        "protocol_id",
    ):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ValueError(f"manifest requires {field}")
    if manifest.get("kind") not in ("recorded", "synthetic", "estimated"):
        raise ValueError("kind must be recorded, synthetic or estimated")
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if manifest.get("sha256") != actual_hash:
        raise ValueError("dataset hash mismatch")
    if (
        type(manifest.get("max_gap_seconds")) is not int
        or manifest["max_gap_seconds"] <= 0
    ):
        raise ValueError("max_gap_seconds must be positive integer")
    if manifest.get("point_in_time_rules") is not True:
        raise ValueError("explicit point-in-time rules declaration required")
    frames = load_csv(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != actual_hash:
        raise ValueError("dataset changed during audit")
    if frames[0][0].timestamp <= 0 or any(
        q.quote_volume_24h is None for f in frames for q in f
    ):
        raise ValueError(
            "positive Unix timestamps and volume required for production fingerprint replay"
        )
    gaps = [b[0].timestamp - a[0].timestamp for a, b in zip(frames, frames[1:])]
    if max(gaps) > manifest["max_gap_seconds"]:
        raise ValueError("dataset contains a gap beyond declared maximum")
    if (
        manifest.get("start_timestamp") != frames[0][0].timestamp
        or manifest.get("end_timestamp") != frames[-1][0].timestamp
    ):
        raise ValueError("manifest coverage mismatch")
    return frames, {
        **manifest,
        "frames": len(frames),
        "pairs": [q.pair for q in frames[0]],
        "observed_max_gap_seconds": max(gaps),
        "provenance_status": "caller-declared, hash-checked; source authenticity not independently verified",
    }


def engine_fingerprint():
    root = Path(__file__).parent
    files = (
        "strategy_dataset.py",
        "strategy_replay.py",
        "strategy_replay_vcap.py",
        "strategy_lab.py",
        "strategy_lab_validation.py",
        "asset_fingerprint.py",
        "recovery_fingerprint.py",
        "vcap_tranches.py",
        "vcap_profit_exit.py",
        "models.py",
        "kraken_market_fabric.py",
    )
    return digest(
        {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    )


def campaign(csv_path, manifest_path, *, config, train_end, test_start, ledger_path):
    """Compare fixed policies on disjoint intervals. No tuning or live promotion."""
    frames, audit = audit_dataset(csv_path, manifest_path)
    if (
        type(train_end) is not int
        or type(test_start) is not int
        or train_end >= test_start
    ):
        raise ValueError("integer train_end must precede test_start")
    segments = {
        "development": tuple(f for f in frames if f[0].timestamp <= train_end),
        "evaluation": tuple(f for f in frames if f[0].timestamp >= test_start),
    }
    if any(len(segment) < 2 for segment in segments.values()):
        raise ValueError("each split needs at least two frames")
    store = StrategyLabStore(ledger_path)
    engine = engine_fingerprint()
    report = {
        "schema": 1,
        "audit": audit,
        "config": asdict(config),
        "engine_sha256": engine,
        "split": {"train_end": train_end, "test_start": test_start},
        "trials": [],
        "limitations": [
            "fixed policies; no parameter training",
            "splits cold-start fingerprints and portfolios independently",
            "evaluation is not proof that data was never examined before",
            "production pure planners only; no production governor/lifecycle dispatch parity",
            "no live authority or profitability conclusion",
        ],
    }
    cost_bps = (
        ONE - (ONE - config.slippage_bps / BPS) * (ONE - config.fee_bps / BPS)
    ) * BPS
    for label, segment in segments.items():
        for name in (
            "cash",
            "dca",
            "recovery",
            "recycling",
            "capital_time",
            "production_planners",
        ):
            policy = (
                ProductionPlannerAdapter(config)
                if name == "production_planners"
                else Candidate(name, exit_cost_bps=cost_bps)
            )
            params = (
                policy.parameters()
                if isinstance(policy, ProductionPlannerAdapter)
                else asdict(policy)
            )
            params = {
                **params,
                "replay_config": asdict(config),
                "engine_sha256": engine,
                "split": label,
                "train_end": train_end,
                "test_start": test_start,
                "manifest_fingerprint": digest(audit),
            }
            stage = (
                (
                    ResearchStage.HISTORICAL
                    if label == "development"
                    else ResearchStage.OUT_OF_SAMPLE
                )
                if audit["kind"] == "recorded"
                else ResearchStage.ADVERSARIAL
            )
            trial_id = digest(
                {"parameters": params, "strategy": name, "dataset": audit["sha256"]}
            )
            try:
                result = replay(segment, policy, config)
                metrics = {
                    **result.metrics,
                    "status": "completed",
                    "ending_cash": result.cash,
                    "expired_orders": result.expired_orders,
                    "rejections": len(result.rejected),
                }
            except Exception as exc:
                # An attempted failing candidate remains in the durable trial count.
                store.record(
                    ResearchTrial(
                        trial_id,
                        name,
                        stage,
                        audit["sha256"],
                        params,
                        {"status": "failed", "error": str(exc)},
                        segment[-1][0].timestamp,
                    )
                )
                raise
            store.record(
                ResearchTrial(
                    trial_id,
                    name,
                    stage,
                    audit["sha256"],
                    params,
                    metrics,
                    segment[-1][0].timestamp,
                )
            )
            report["trials"].append(
                {
                    "trial_id": trial_id,
                    "strategy": name,
                    "split": label,
                    "stage": stage.value,
                    "parameters": params,
                    "metrics": metrics,
                    "planner_decisions": getattr(policy, "decisions", []),
                }
            )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--train-end", type=int, required=True)
    parser.add_argument("--test-start", type=int, required=True)
    parser.add_argument("--fee-bps", required=True)
    parser.add_argument("--slippage-bps", default="5")
    parser.add_argument("--cash", default="200")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = ReplayConfig(
        initial_cash=Decimal(args.cash),
        fee_bps=Decimal(args.fee_bps),
        slippage_bps=Decimal(args.slippage_bps),
    )
    report = campaign(
        args.csv,
        args.manifest,
        config=config,
        train_end=args.train_end,
        test_start=args.test_start,
        ledger_path=args.ledger,
    )
    args.output.write_text(json.dumps(report, default=str, indent=2) + "\n")


if __name__ == "__main__":
    main()
