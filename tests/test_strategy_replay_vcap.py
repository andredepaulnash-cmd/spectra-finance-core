from dataclasses import asdict, replace
from decimal import Decimal as D
import hashlib
import json

import pytest

from spectra_core.models import CapitalProposal, IntentKind
from spectra_core.recovery_fingerprint import VCAPPositionState, plan_vcap_recovery
from spectra_core.strategy_dataset import audit_dataset, campaign
from spectra_core.strategy_lab_validation import StrategyLabStore
from spectra_core.strategy_replay import Lot, Quote, ReplayConfig, View, replay
from spectra_core.strategy_replay_vcap import ProductionPlannerAdapter
from spectra_core.vcap_profit_exit import plan_vcap_profit_exit


def data(prices=(100, 80, 82, 110)):
    return tuple(
        (
            Quote(
                1000 + i * 60,
                "BTC-USD",
                D(p),
                D(p),
                D(100),
                D(".001"),
                D(1),
                D(".001"),
                D(100000),
            ),
        )
        for i, p in enumerate(prices)
    )


def config():
    return ReplayConfig(
        initial_cash=D(1000),
        fee_bps=D(40),
        slippage_bps=D(5),
        reserve_fraction=D(0),
        pair_cap_fraction=D(1),
    )


def test_calls_production_recovery_and_tranche_geometry_exactly():
    cfg = config()
    adapter = ProductionPlannerAdapter(cfg, ticket=D(40))
    frames = data()
    adapter(View(frames[:1], D(1000), (), ()))
    lot = Lot(1, "BTC-USD", D(2), D(240), False, 1000)
    orders = adapter(View(frames[:2], D(760), (lot,), ()))
    record = adapter.decisions[-1]
    expected = plan_vcap_recovery(
        CapitalProposal("replay:1060:BTC-USD", "BTC-USD", IntentKind.RECOVERY, D(40)),
        adapter.fingerprints["BTC-USD"],
        VCAPPositionState("BTC-USD", D(2), D(240), D(80)),
        now=1060,
        fingerprint_policy=adapter.fingerprint_policy,
        vcap_policy=adapter.recovery_policy,
    )
    assert record["recovery"] == asdict(expected)
    assert orders[0].recovery is True
    assert orders[0].amount == record["tranches"]["tranches"][0]["quote_amount"] * D(
        "1.004"
    )
    assert record["tranches"]["authorized_quote"] <= expected.planned_amount_quote


def test_profit_plan_does_not_double_count_entry_fees():
    adapter = ProductionPlannerAdapter(config())
    frames = data((100, 150))
    adapter(View(frames[:1], D(1000), (), ()))
    lot = Lot(1, "BTC-USD", D(1), D("100.4"), False, 1000)
    adapter(View(frames, D("899.6"), (lot,), ()))
    expected = plan_vcap_profit_exit(
        VCAPPositionState("BTC-USD", D(1), D("100.4"), D(150)),
        costs=adapter.costs,
        now=1060,
        fingerprint=adapter.fingerprints["BTC-USD"],
        policy=adapter.exit_policy,
    )
    assert adapter.costs.accrued_fees_quote == 0
    assert adapter.decisions[-1]["exit"] == asdict(expected)


def test_adapter_is_fresh_for_each_run_and_volume_is_not_invented():
    adapter = ProductionPlannerAdapter(config())
    replay(data(), adapter, config())
    with pytest.raises(ValueError, match="fresh adapter"):
        replay(data(), adapter, config())
    missing = tuple((replace(f[0], quote_volume_24h=None),) for f in data())
    with pytest.raises(ValueError, match="volume"):
        replay(missing, ProductionPlannerAdapter(config()), config())


def test_production_adapter_prefix_is_causal_and_reproducible():
    a = ProductionPlannerAdapter(config())
    b = ProductionPlannerAdapter(config())
    ra = replay(data((100, 80, 82, 90, 110)), a, config())
    rb = replay(data((100, 80, 82, 90, 1)), b, config())
    assert ra.samples[:4] == rb.samples[:4]
    assert [r for r in a.decisions if r["timestamp"] <= 1180] == [
        r for r in b.decisions if r["timestamp"] <= 1180
    ]
    assert ra == replay(
        data((100, 80, 82, 90, 110)), ProductionPlannerAdapter(config()), config()
    )


def dataset(tmp_path, kind="synthetic"):
    csv = tmp_path / "quotes.csv"
    rows = [
        "timestamp,pair,bid,ask,depth_base,min_base,min_quote,step_base,quote_volume_24h"
    ]
    for frame in data((100, 90, 92, 95, 100, 101, 80, 82, 85, 88, 99, 100)):
        q = frame[0]
        rows.append(f"{q.timestamp},{q.pair},{q.bid},{q.ask},100,.001,1,.001,100000")
    csv.write_text("\n".join(rows) + "\n")
    manifest = tmp_path / "manifest.json"
    values = {
        "kind": kind,
        "sha256": hashlib.sha256(csv.read_bytes()).hexdigest(),
        "source": "unit-test fixture",
        "venue": "simulated",
        "rules_source": "fixture",
        "volume_source": "fixture",
        "depth_definition": "fixture symmetric depth",
        "protocol_id": "fixed-test-v1",
        "point_in_time_rules": True,
        "max_gap_seconds": 60,
        "start_timestamp": 1000,
        "end_timestamp": 1660,
    }
    manifest.write_text(json.dumps(values))
    return csv, manifest, values


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "wrong"),
        ("max_gap_seconds", 30),
        ("point_in_time_rules", False),
        ("start_timestamp", 1),
        ("source", ""),
        ("kind", "historical"),
    ],
)
def test_audit_rejects_bad_provenance_and_gaps(tmp_path, field, value):
    csv, manifest, values = dataset(tmp_path)
    values[field] = value
    manifest.write_text(json.dumps(values))
    with pytest.raises(ValueError):
        audit_dataset(csv, manifest)


def test_fixed_split_campaign_keeps_synthetic_labels_and_trials(tmp_path):
    csv, manifest, _ = dataset(tmp_path)
    ledger = tmp_path / "trials.sqlite3"
    kwargs = dict(config=config(), train_end=1240, test_start=1360, ledger_path=ledger)
    report = campaign(csv, manifest, **kwargs)
    assert report == campaign(csv, manifest, **kwargs)
    assert len(report["trials"]) == 12
    assert {r["stage"] for r in report["trials"]} == {"ADVERSARIAL"}
    assert StrategyLabStore(ledger).trial_count() == 12
    assert "not independently verified" in report["audit"]["provenance_status"]
    assert all(r["metrics"]["status"] == "completed" for r in report["trials"])
    # A different fee assumption is a new experiment, not an overwrite.
    campaign(csv, manifest, **{**kwargs, "config": replace(config(), fee_bps=D(60))})
    assert StrategyLabStore(ledger).trial_count() == 24


def test_split_must_be_disjoint_and_nonempty(tmp_path):
    csv, manifest, _ = dataset(tmp_path)
    for train, test in [(1240, 1240), (1000, 1360), (1240, 1660)]:
        with pytest.raises(ValueError):
            campaign(
                csv,
                manifest,
                config=config(),
                train_end=train,
                test_start=test,
                ledger_path=tmp_path / "ledger.sqlite3",
            )


def test_failed_candidate_is_recorded(tmp_path, monkeypatch):
    from spectra_core import strategy_dataset as module

    csv, manifest, _ = dataset(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(module, "replay", fail)
    ledger = tmp_path / "trials.sqlite3"
    with pytest.raises(RuntimeError, match="test failure"):
        campaign(
            csv,
            manifest,
            config=config(),
            train_end=1240,
            test_start=1360,
            ledger_path=ledger,
        )
    rows = StrategyLabStore(ledger).list_trials()
    assert len(rows) == 1 and rows[0].metrics["status"] == "failed"
