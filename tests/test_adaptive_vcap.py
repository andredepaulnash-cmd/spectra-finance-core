from decimal import Decimal

import pytest

from spectra_core.adaptive_vcap import (
    AdaptiveVCAPPhase,
    AdaptiveVCAPPolicy,
    VCAPFragmentCandidate,
    VCAPFragmentStatus,
    adaptive_vcap_ladder_amounts,
    adaptive_vcap_priority_score,
    classify_adaptive_vcap_phase,
    drawdown_bps_from_prices,
    next_adaptive_vcap_step,
    next_pre_vcap_dca_trigger,
    select_active_vcap_fragments,
)


D = Decimal


@pytest.mark.parametrize(
    ("drawdown_bps", "expected"),
    [
        (0, AdaptiveVCAPPhase.RECOVERED),
        (776, AdaptiveVCAPPhase.PRE_VCAP),
        (777, AdaptiveVCAPPhase.FAT_1),
        (1173, AdaptiveVCAPPhase.FAT_1),
        (1174, AdaptiveVCAPPhase.SKINNY),
        (1776, AdaptiveVCAPPhase.SKINNY),
        (1777, AdaptiveVCAPPhase.FAT_2),
        (2400, AdaptiveVCAPPhase.FAT_2),
        (2401, AdaptiveVCAPPhase.DEEP_GOVERNOR),
    ],
)
def test_adaptive_vcap_phase_boundaries_are_exact(drawdown_bps, expected):
    assert classify_adaptive_vcap_phase(drawdown_bps) is expected


def test_drawdown_is_positive_basis_point_magnitude():
    assert drawdown_bps_from_prices(D("100"), D("92.23")) == 777
    assert drawdown_bps_from_prices(D("100"), D("105")) == 0


def test_conventional_dca_ladder_precedes_vcap_activation():
    assert next_pre_vcap_dca_trigger(274, 0) == 274
    assert next_pre_vcap_dca_trigger(500, 1) == 444
    assert next_pre_vcap_dca_trigger(700, 2) == 674
    assert next_pre_vcap_dca_trigger(777, 3) is None
    assert next_pre_vcap_dca_trigger(273, 0) is None


def test_phase_ladders_match_adaptive_fat_skinny_fat_design():
    assert adaptive_vcap_ladder_amounts(AdaptiveVCAPPhase.FAT_1) == (
        D("20.00"),
        D("40.00"),
        D("80.00"),
    )
    assert adaptive_vcap_ladder_amounts(AdaptiveVCAPPhase.SKINNY) == (
        D("5.00"),
        D("7.50"),
        D("10.00"),
        D("12.50"),
    )
    assert adaptive_vcap_ladder_amounts(AdaptiveVCAPPhase.FAT_2) == (
        D("160.00"),
        D("320.00"),
        D("640.00"),
    )
    assert adaptive_vcap_ladder_amounts(AdaptiveVCAPPhase.DEEP_GOVERNOR) == ()


def test_ladder_step_returns_none_after_phase_steps_are_exhausted():
    assert next_adaptive_vcap_step(AdaptiveVCAPPhase.FAT_1, 0) == D("20.00")
    assert next_adaptive_vcap_step(AdaptiveVCAPPhase.FAT_1, 2) == D("80.00")
    assert next_adaptive_vcap_step(AdaptiveVCAPPhase.FAT_1, 3) is None


def test_fragment_priority_is_not_just_deepest_drawdown():
    strong_recovery = adaptive_vcap_priority_score(
        mean_reversion_score=D("0.9"),
        confidence=D("0.9"),
        liquidity_score=D("0.9"),
        volatility_score=D("0.2"),
        recovery_efficiency=D("0.9"),
        drawdown_bps=1000,
    )
    weak_recovery = adaptive_vcap_priority_score(
        mean_reversion_score=D("0.1"),
        confidence=D("0.2"),
        liquidity_score=D("0.2"),
        volatility_score=D("0.9"),
        recovery_efficiency=D("0.1"),
        drawdown_bps=2300,
    )
    assert strong_recovery > weak_recovery


def test_two_active_fragment_limit_and_priority_queue():
    candidates = (
        VCAPFragmentCandidate("f0", 0, VCAPFragmentStatus.WAITING, D("0.1")),
        VCAPFragmentCandidate("f1", 1, VCAPFragmentStatus.WAITING, D("0.9")),
        VCAPFragmentCandidate("f2", 2, VCAPFragmentStatus.WAITING, D("0.8")),
        VCAPFragmentCandidate("f3", 3, VCAPFragmentStatus.WAITING, D("0.7")),
        VCAPFragmentCandidate("f4", 4, VCAPFragmentStatus.WAITING, D("0.6")),
    )
    active = select_active_vcap_fragments(
        candidates,
        phase=AdaptiveVCAPPhase.FAT_1,
    )
    assert [fragment.fragment_id for fragment in active] == ["f1", "f2"]
    assert len(active) == 2


def test_pending_fragments_keep_slots_and_are_never_abandoned():
    candidates = (
        VCAPFragmentCandidate(
            "pending", 0, VCAPFragmentStatus.RECONCILING, D("0.1"), pending_executor=True
        ),
        VCAPFragmentCandidate("f1", 1, VCAPFragmentStatus.WAITING, D("0.9")),
        VCAPFragmentCandidate("f2", 2, VCAPFragmentStatus.WAITING, D("0.8")),
    )
    active = select_active_vcap_fragments(
        candidates,
        phase=AdaptiveVCAPPhase.FAT_1,
    )
    assert [fragment.fragment_id for fragment in active] == ["pending", "f1"]


def test_deep_drawdown_does_not_activate_new_fragments():
    candidates = (
        VCAPFragmentCandidate("pending", 0, VCAPFragmentStatus.RECONCILING, pending_executor=True),
        VCAPFragmentCandidate("waiting", 1, VCAPFragmentStatus.WAITING, D("1")),
    )
    active = select_active_vcap_fragments(
        candidates,
        phase=AdaptiveVCAPPhase.DEEP_GOVERNOR,
    )
    assert [fragment.fragment_id for fragment in active] == ["pending"]


def test_fragment_policy_rejects_more_than_two_active_by_default():
    policy = AdaptiveVCAPPolicy(fragment_count=5, max_active_fragments_per_position=2)
    assert policy.fragment_count == 5
    assert policy.max_active_fragments_per_position == 2
    with pytest.raises(ValueError):
        AdaptiveVCAPPolicy(fragment_count=5, max_active_fragments_per_position=3)



def test_adaptive_vcap_policy_loads_validated_environment(monkeypatch):
    monkeypatch.setenv("SPECTRA_VCAP_FRAGMENT_COUNT", "4")
    monkeypatch.setenv("SPECTRA_VCAP_MAX_ACTIVE_FRAGMENTS_PER_POSITION", "2")
    monkeypatch.setenv("SPECTRA_VCAP_BASE_QUOTE", "25")
    policy = AdaptiveVCAPPolicy.from_env()
    assert policy.fragment_count == 4
    assert policy.max_active_fragments_per_position == 2
    assert policy.base_quote == D("25")


def test_adaptive_vcap_policy_rejects_more_than_two_active_fragments(monkeypatch):
    monkeypatch.setenv("SPECTRA_VCAP_MAX_ACTIVE_FRAGMENTS_PER_POSITION", "3")
    with pytest.raises(ValueError):
        AdaptiveVCAPPolicy.from_env()
