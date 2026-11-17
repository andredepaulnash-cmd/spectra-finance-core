from decimal import Decimal
from dataclasses import replace

import pytest

from spectra_core.adaptive_vcap import AdaptiveVCAPPhase, VCAPFragmentStatus
from spectra_core.vcap_fragments import VCAPFragmentManager
from spectra_core.vcap_runtime import VCAPRuntimePhase
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
)


D = Decimal


def base_state():
    return VCAPLifecycleState(
        envelope_proposal_id="entry-proposal-1",
        pair="BTC-USD",
        next_sequence=1,
        remaining_authorized_quote=D("18750"),
        recovery_proposal_ids=(),
        position_base=D("0.0002"),
        cost_basis_quote=D("20"),
        last_known_price=D("100000"),
        profit_state=None,
        status=VCAPLifecycleStatus.ACTIVE,
        updated_at=1000.0,
    )


def test_fragment_manager_persists_five_independent_budget_stores(tmp_path):
    manager = VCAPFragmentManager(tmp_path / "fragments")
    records = manager.ensure_fragments(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        pair="BTC-USD",
        base_state=base_state(),
        max_position_quote=D("25000"),
        now=1000.0,
    )
    assert len(records) == 5
    assert [record.ordinal for record in records] == [0, 1, 2, 3, 4]
    assert all(record.status is VCAPFragmentStatus.WAITING for record in records)
    assert all(record.budget_quote == D("4996.00") for record in records)

    stores = [manager.stores(record.fragment_id) for record in records]
    assert len({str(state.path) for state, _ in stores}) == 5
    assert len({str(runtime.path) for _, runtime in stores}) == 5
    for record, (state_store, _) in zip(records, stores):
        state = state_store.load("entry-proposal-1")
        assert state is not None
        assert state.remaining_authorized_quote == record.budget_quote
        assert state.position_base == D("0.0002")


def test_fragment_manager_activates_only_two_and_respects_priority(tmp_path):
    manager = VCAPFragmentManager(tmp_path / "fragments")
    manager.ensure_fragments(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        pair="BTC-USD",
        base_state=base_state(),
        max_position_quote=D("25000"),
        now=1000.0,
    )
    records = manager.list_fragments("position-btc")
    scores = (D("0.1"), D("0.9"), D("0.8"), D("0.7"), D("0.6"))
    for record, score in zip(records, scores):
        manager._update(record.fragment_id, now=1001.0, priority_score=score)

    active = manager.select_active(
        "position-btc",
        AdaptiveVCAPPhase.FAT_1,
        now=1002.0,
    )
    assert [record.ordinal for record in active] == [1, 2]
    assert len(active) == 2


def test_fragment_ladder_advances_only_after_completed_fill_cycle(tmp_path):
    manager = VCAPFragmentManager(tmp_path / "fragments")
    records = manager.ensure_fragments(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        pair="BTC-USD",
        base_state=base_state(),
        max_position_quote=D("25000"),
        now=1000.0,
    )
    fragment = records[0]
    assert manager.next_step_quote(fragment.fragment_id, AdaptiveVCAPPhase.FAT_1) == D("20.00")
    manager.select_active("position-btc", AdaptiveVCAPPhase.FAT_1, now=1000.5)
    fragment = manager.get_fragment(fragment.fragment_id)

    _, runtime = manager.stores(fragment.fragment_id)
    cycle = runtime.begin_or_resume(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        now=1001.0,
    )
    pending = runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
        now=1002.0,
        expected_phases=(VCAPRuntimePhase.STARTED,),
        planned_action="EXECUTE_TRANCHE",
        mutation_proposal_id="recovery-proposal-1",
    )
    manager.record_cycle_result(
        fragment.fragment_id,
        {"reasons": ["awaiting fill"]},
        now=1002.0,
    )
    waiting = manager.get_fragment(fragment.fragment_id)
    assert waiting.status is VCAPFragmentStatus.RECONCILING
    assert waiting.fat1_step == 0
    assert waiting.active_mutation_proposal_id == "recovery-proposal-1"

    runtime.transition(
        pending.cycle_id,
        phase=VCAPRuntimePhase.COMPLETED,
        now=1003.0,
        expected_phases=(VCAPRuntimePhase.AWAITING_RECONCILIATION,),
    )
    manager.record_cycle_result(
        fragment.fragment_id,
        {"reasons": ["fill reconciled"]},
        now=1003.0,
    )
    completed = manager.get_fragment(fragment.fragment_id)
    assert completed.status is VCAPFragmentStatus.WAITING
    assert completed.fat1_step == 1
    assert completed.active_mutation_proposal_id is None
    assert manager.next_step_quote(fragment.fragment_id, AdaptiveVCAPPhase.FAT_1) == D("40.00")

    # Re-reading the same completed cycle must not consume another ladder step.
    manager.record_cycle_result(
        fragment.fragment_id,
        {"reasons": ["idempotent reread"]},
        now=1004.0,
    )
    assert manager.get_fragment(fragment.fragment_id).fat1_step == 1


def test_deep_drawdown_pauses_new_fragments_but_keeps_pending_reconciliation(tmp_path):
    manager = VCAPFragmentManager(tmp_path / "fragments")
    records = manager.ensure_fragments(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        pair="BTC-USD",
        base_state=base_state(),
        max_position_quote=D("25000"),
        now=1000.0,
    )
    _, runtime = manager.stores(records[0].fragment_id)
    cycle = runtime.begin_or_resume(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        now=1001.0,
    )
    runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
        now=1002.0,
        expected_phases=(VCAPRuntimePhase.STARTED,),
        planned_action="EXECUTE_TRANCHE",
        mutation_proposal_id="recovery-proposal-1",
    )
    manager.record_cycle_result(records[0].fragment_id, {"reasons": []}, now=1002.0)

    selected = manager.select_active(
        "position-btc",
        AdaptiveVCAPPhase.DEEP_GOVERNOR,
        now=1003.0,
    )
    assert [record.fragment_id for record in selected] == [records[0].fragment_id]
    statuses = {record.ordinal: record.status for record in manager.list_fragments("position-btc")}
    assert statuses[0] is VCAPFragmentStatus.RECONCILING
    assert statuses[1] is VCAPFragmentStatus.PAUSED


def test_fragment_manager_refuses_envelope_rebinding(tmp_path):
    manager = VCAPFragmentManager(tmp_path / "fragments")
    manager.ensure_fragments(
        position_id="position-btc",
        envelope_proposal_id="entry-proposal-1",
        pair="BTC-USD",
        base_state=base_state(),
        max_position_quote=D("25000"),
        now=1000.0,
    )
    with pytest.raises(RuntimeError):
        manager.ensure_fragments(
            position_id="position-btc",
            envelope_proposal_id="different-envelope",
            pair="BTC-USD",
            base_state=replace(base_state(), envelope_proposal_id="different-envelope"),
            max_position_quote=D("25000"),
            now=1001.0,
        )
