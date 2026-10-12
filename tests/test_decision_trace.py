import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_spectra_core import base_snapshot, governor, proposal

import spectra_core.capital_governor as governor_module
from spectra_core.decision_trace import (
    append_decision_trace,
    initialize_decision_trace,
    read_decision_traces,
)
from spectra_core.models import DecisionStatus
from spectra_core.vcap_runtime import VCAPRuntimePhase, VCAPRuntimeStore


def test_rejected_capital_carries_exact_observed_limits_and_no_authority(tmp_path):
    g = governor(tmp_path)
    snapshot = base_snapshot(free_quote=D("250"))
    decision = g.reserve(proposal("blocked", "60"), snapshot)
    assert decision.status == DecisionStatus.REJECTED
    (trace,) = read_decision_traces(g.db_path, subject_id="blocked")
    assert trace.disposition == "REJECTED"
    assert trace.reasons == (decision.reason,)
    assert trace.facts["free_quote"] == "250"
    assert trace.facts["requested_quote"] == "60"
    assert trace.facts["available_after_quote"] == "190"
    assert g.active_reserved_quote() == 0


def test_reservation_retry_does_not_rewrite_original_decision_evidence(tmp_path):
    g = governor(tmp_path)
    first = g.reserve(proposal("retry", "50"), base_snapshot())
    before = read_decision_traces(g.db_path, subject_id="retry")
    assert g.reserve(proposal("retry", "5000"), base_snapshot()) == first
    assert read_decision_traces(g.db_path, subject_id="retry") == before
    assert len(before) == 1


def test_trace_does_not_normalize_distinct_capital_identity_strings(tmp_path):
    g = governor(tmp_path)
    first = g.reserve(proposal("owner ", "10"), base_snapshot())
    second = g.reserve(proposal("owner", "10"), base_snapshot())
    assert first.approved and second.approved
    (first_trace,) = read_decision_traces(g.db_path, subject_id="owner ")
    (second_trace,) = read_decision_traces(g.db_path, subject_id="owner")
    assert first_trace.trace_id != second_trace.trace_id
    assert first_trace.subject_id == "owner "
    assert second_trace.subject_id == "owner"


def test_tracing_preserves_baseline_economic_decisions(tmp_path, monkeypatch):
    def run(root):
        root.mkdir()
        g = governor(root)
        results = [g.reserve(proposal("first", "50"), base_snapshot())]
        results.append(g.reserve(proposal("second", "60"), base_snapshot()))
        results.append(g.release("first"))
        results.append(g.reserve(proposal("third", "30"), base_snapshot()))
        results.append(g.commit("third"))
        return results, g.active_reserved_quote(), g.get_decision("third")

    traced = run(tmp_path / "traced")
    monkeypatch.setattr(
        governor_module, "append_decision_trace", lambda *args, **kwargs: None
    )
    assert run(tmp_path / "unobserved") == traced


def test_rejected_nonfinite_input_remains_rejected_and_serializable(tmp_path):
    g = governor(tmp_path)
    decision = g.reserve(
        proposal("nan", "10"), replace(base_snapshot(), pair_exposure_quote=D("NaN"))
    )
    assert decision.status == DecisionStatus.REJECTED
    (trace,) = read_decision_traces(g.db_path, subject_id="nan")
    assert trace.facts["pair_exposure_quote"] == "NaN"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "bad", object()])
def test_tracing_cannot_turn_invalid_input_rejection_into_an_exception(tmp_path, value):
    g = governor(tmp_path)
    decision = g.reserve(
        proposal("invalid", "10"), replace(base_snapshot(), pair_exposure_quote=value)
    )
    assert decision.status == DecisionStatus.REJECTED
    assert "must be Decimal" in decision.reason
    (trace,) = read_decision_traces(g.db_path, subject_id="invalid")
    assert trace.disposition == "REJECTED"


def test_legacy_empty_runtime_reason_remains_observable_without_behavior_change(
    tmp_path,
):
    path = tmp_path / "runtime.db"
    store = VCAPRuntimeStore(path)
    cycle = store.begin_or_resume(position_id="p", envelope_proposal_id="e", now=1000)
    result = store.transition(
        cycle.cycle_id, phase=VCAPRuntimePhase.HOLD, now=1001, decision_reasons=("",)
    )
    assert result.decision_reasons == ("",)
    assert read_decision_traces(path, subject_id=cycle.cycle_id)[-1].reasons == ("",)


def test_trace_write_is_atomic_with_capital_decision(tmp_path, monkeypatch):
    g = governor(tmp_path)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("test storage failure")

    monkeypatch.setattr(governor_module, "append_decision_trace", broken)
    with pytest.raises(sqlite3.OperationalError):
        g.reserve(proposal("atomic", "10"), base_snapshot())
    assert g.get_decision("atomic") is None
    assert g.active_reserved_quote() == 0


def test_runtime_history_survives_phase_replacement_and_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = VCAPRuntimeStore(path)
    cycle = store.begin_or_resume(position_id="p", envelope_proposal_id="e", now=1000)
    store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MARKET_REFRESHED,
        now=1001,
        decision_reasons=("fresh",),
    )
    store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.HOLD,
        now=1002,
        decision_reasons=("pending_order",),
    )
    restarted = VCAPRuntimeStore(path)
    assert restarted.load(cycle.cycle_id).phase == VCAPRuntimePhase.HOLD
    rows = read_decision_traces(path, subject_id=cycle.cycle_id)
    assert [row.disposition for row in rows] == ["STARTED", "MARKET_REFRESHED", "HOLD"]
    assert rows[-1].reasons == ("pending_order",)
    assert rows[-1].facts["previous_phase"] == "MARKET_REFRESHED"


def test_exact_trace_replay_and_conflict(tmp_path):
    path = tmp_path / "trace.db"
    with sqlite3.connect(path) as conn:
        initialize_decision_trace(conn)
        args = dict(
            subject_id="s",
            stage="TEST",
            disposition="BLOCKED",
            reasons=("r",),
            observed_at=1000,
            facts={"amount": D("1.00")},
            trace_id="fixed",
        )
        first = append_decision_trace(conn, **args)
        assert append_decision_trace(conn, **args) == first
        with pytest.raises(RuntimeError, match="conflicts"):
            append_decision_trace(conn, **{**args, "facts": {"amount": D(2)}})
    assert len(read_decision_traces(path, subject_id="s")) == 1


def test_read_does_not_create_missing_file_or_migrate_legacy_database(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        read_decision_traces(missing, subject_id="s")
    assert not missing.exists()
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE legacy (value TEXT)")
    before = path.read_bytes()
    assert read_decision_traces(path, subject_id="s") == ()
    assert path.read_bytes() == before
