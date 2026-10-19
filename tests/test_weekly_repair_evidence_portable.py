"""Portable immutable repair evidence; synthetic inputs only, no campaign import."""
import builtins
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields
from decimal import Decimal as D

import pytest

from research.weekly_reversal.owned_spot.adapter import OwnedSpotAdapter
from research.weekly_reversal.owned_spot.model import CompletedBar, RepairEvidence
from test_weekly_owned_adapter import config, seed


@pytest.fixture(autouse=True)
def forbid_external_campaign_import(monkeypatch):
    """Fail even on machines where the old research checkout happens to exist."""
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "spot_campaign" or name.startswith("spot_campaign."):
            raise AssertionError("Repair evidence must not import an external campaign")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.mark.parametrize("reason,expected", [
    ("OWNER_CASH", "REPAIR_OWNER_CASH"),
    ("RESERVE", "PROTECTED_CASH_FLOOR"),
    ("PAIR_CAP", "PAIR_BASIS_CAP"),
    ("TOTAL_CAP", "TOTAL_BASIS_CAP"),
    ("RECOVERY_CAP", "RECOVERY_BUDGET_CAP"),
    ("ADMISSIBLE", "ADMISSIBLE"),
    ("ACCOUNTING", "ACCOUNTING"),
])
def test_local_evidence_preserves_every_field_and_reason(reason, expected):
    adapter = object.__new__(OwnedSpotAdapter)
    adapter.state = {"marks": {"BTC-USD": {"close": "9.25"}}}
    source = {
        "at": 36000.0, "completed_bar": 36000, "cohort_id": "BTC-USD:seed:01",
        "next_rung": 2, "trigger": "9.5", "target": "16.6", "reason_code": reason,
        "guards": {"health": True, "pending_clear": False, "freshness": True,
                   "liquidity_valid": False, "profit_clear": True, "seed_complete": False},
    }
    source_before, state_before = deepcopy(source), deepcopy(adapter.state)
    calls = []

    def feasibility(pair, *, now, rung):
        calls.append((pair, now, rung))
        return source

    adapter.repair_feasibility = feasibility
    evidence = adapter.policy_repair_evidence("BTC-USD", now=36000, rung=2)
    assert type(evidence) is RepairEvidence
    assert asdict(evidence) == {
        "pair": "BTC-USD", "decision_at": 36000, "completed_bar_at": 36000,
        "cohort_id": "BTC-USD:seed:01", "next_rung": 2, "trigger_price": D("9.5"),
        "completed_close": D("9.25"), "requested_target": D("16.6"), "reason": expected,
        "accounting_healthy": True, "no_pending_action": False, "mark_fresh": True,
        "prior_bar_liquidity_valid": False, "cohort_profit_allows_fallback": True,
        "seed_barrier_complete": False,
    }
    assert calls == [("BTC-USD", 36000, 2)]
    assert source == source_before and adapter.state == state_before
    assert tuple(field.name for field in fields(evidence)) == tuple(asdict(evidence))
    with pytest.raises(FrozenInstanceError):
        evidence.reason = "ADMISSIBLE"


def test_unavailable_evidence_remains_explicit_and_blocked():
    adapter = object.__new__(OwnedSpotAdapter)
    adapter.state = {"marks": {}}
    adapter.repair_feasibility = lambda *args, **kwargs: {
        "at": 1000, "completed_bar": None, "cohort_id": None, "next_rung": None,
        "trigger": None, "target": None, "reason_code": "ACCOUNTING",
        "guards": dict.fromkeys(("health", "pending_clear", "freshness",
                                 "liquidity_valid", "profit_clear", "seed_complete"), False),
    }
    evidence = adapter.policy_repair_evidence("BTC-USD", now=1000)
    assert (evidence.completed_bar_at, evidence.cohort_id, evidence.next_rung) == (0, "", 0)
    assert evidence.trigger_price == evidence.completed_close == evidence.requested_target == D(0)
    assert evidence.reason == "ACCOUNTING"
    assert not any((evidence.accounting_healthy, evidence.no_pending_action, evidence.mark_fresh,
                    evidence.prior_bar_liquidity_valid, evidence.cohort_profit_allows_fallback,
                    evidence.seed_barrier_complete))


def test_native_repair_evidence_preserves_cash_state_and_accounting(tmp_path):
    """Exercise the real feasibility implementation with an owned synthetic account."""
    with OwnedSpotAdapter(tmp_path, config(("BTC",)), now=1000) as adapter:
        seed(adapter)
        adapter.observe(CompletedBar("BTC-USD", 3300, 3600, D("9"), D("1000000"), 3600), now=3600)
        adapter.refresh_accounting_all(now=3600)
        expected = adapter.repair_feasibility("BTC-USD", now=3600, rung=1)
        assert expected["reason_code"] == "ADMISSIBLE"
        before = deepcopy(adapter.state)
        with adapter._db() as db:
            paper_before = list(db.iterdump())
        import sqlite3
        with sqlite3.connect(adapter.root / "native.sqlite") as db:
            native_before = list(db.iterdump())
        evidence = adapter.policy_repair_evidence("BTC-USD", now=3600, rung=1)
        assert evidence.reason == "ADMISSIBLE"
        assert evidence.requested_target == D(expected["target"])
        assert evidence.trigger_price == D(expected["trigger"])
        assert evidence.cohort_id == expected["cohort_id"]
        assert all((evidence.accounting_healthy, evidence.no_pending_action, evidence.mark_fresh,
                    evidence.prior_bar_liquidity_valid, evidence.cohort_profit_allows_fallback,
                    evidence.seed_barrier_complete))
        assert adapter.state == before
        with adapter._db() as db:
            assert list(db.iterdump()) == paper_before
        with sqlite3.connect(adapter.root / "native.sqlite") as db:
            assert list(db.iterdump()) == native_before
        assert adapter.cash() + adapter.deployed_basis() == D("2000")
