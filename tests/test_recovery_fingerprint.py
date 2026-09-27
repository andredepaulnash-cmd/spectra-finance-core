from decimal import Decimal

import pytest

from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.models import CapitalProposal, IntentKind
from spectra_core.recovery_fingerprint import (
    RecoveryFingerprintMode,
    RecoveryFingerprintPolicy,
    VCAPPositionState,
    VCAPRecoveryPolicy,
    assess_recovery_fingerprint,
    plan_vcap_recovery,
    shape_recovery_proposal,
)


D = Decimal


def observation(
    *,
    timestamp: float,
    price: float,
    pair: str = "BTC-USD",
    with_book: bool = True,
    spread_bps: float = 5.0,
    depth_quote: float = 50_000.0,
) -> AssetObservation:
    return AssetObservation(
        trading_pair=pair,
        timestamp=timestamp,
        price=price,
        quote_volume_24h=1_000_000,
        spread_bps=spread_bps if with_book else None,
        depth_quote=depth_quote if with_book else None,
        depth_imbalance=0.1 if with_book else None,
        microprice_edge_bps=0.5 if with_book else None,
    )


def recovery_proposal(
    amount: str = "100",
    *,
    pair: str = "BTC-USD",
) -> CapitalProposal:
    return CapitalProposal(
        proposal_id="recovery-1",
        pair=pair,
        intent=IntentKind.RECOVERY,
        amount_quote=D(amount),
        leverage=D("1"),
    )


def build_fingerprint(
    *,
    alternating: bool,
    count: int = 41,
    stale_book_after_first: bool = False,
) -> AssetFingerprint:
    price = 100.0
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=price)
    )

    for i in range(1, count):
        if alternating:
            price = 101.0 if i % 2 else 100.0
        else:
            price *= 1.001

        fp = fp.update(
            observation(
                timestamp=1_000 + i * 60,
                price=price,
                with_book=not stale_book_after_first,
            )
        )

    return fp


def test_cold_start_uses_conservative_recovery_size():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )

    shaped, decision = shape_recovery_proposal(
        recovery_proposal("100"),
        fp,
        now=1_000,
    )

    assert decision.mode == RecoveryFingerprintMode.CONSERVATIVE
    assert decision.size_multiplier == D("0.25")
    assert "low_confidence" in decision.reasons
    assert shaped is not None
    assert shaped.amount_quote == D("25")


def test_reversal_behavior_can_restore_more_of_requested_recovery():
    fp = build_fingerprint(alternating=True)

    shaped, decision = shape_recovery_proposal(
        recovery_proposal("100"),
        fp,
        now=fp.last_seen_at,
    )

    assert decision.mode == RecoveryFingerprintMode.ADAPTIVE
    assert decision.mean_reversion_score is not None
    assert decision.mean_reversion_score > 0
    assert decision.size_multiplier > D("0.50")
    assert decision.size_multiplier <= D("1.00")
    assert "reversal_support" in decision.reasons
    assert shaped is not None
    assert D("50") < shaped.amount_quote <= D("100")


def test_persistent_momentum_throttles_recovery():
    fp = build_fingerprint(alternating=False)

    shaped, decision = shape_recovery_proposal(
        recovery_proposal("100"),
        fp,
        now=fp.last_seen_at,
    )

    assert decision.mode == RecoveryFingerprintMode.ADAPTIVE
    assert decision.mean_reversion_score is not None
    assert decision.mean_reversion_score < 0
    assert D("0.25") <= decision.size_multiplier < D("0.50")
    assert "momentum_throttle" in decision.reasons
    assert shaped is not None
    assert D("25") <= shaped.amount_quote < D("50")


def test_stale_fast_and_short_behavior_blocks_recovery():
    fp = build_fingerprint(alternating=True)

    shaped, decision = shape_recovery_proposal(
        recovery_proposal(),
        fp,
        now=fp.last_seen_at + 4 * 60 * 60,
    )

    assert shaped is None
    assert decision.mode == RecoveryFingerprintMode.BLOCKED
    assert decision.size_multiplier == D("0")
    assert "fast_stale" in decision.reasons
    assert "short_stale" in decision.reasons


def test_stale_microstructure_caps_reversal_signal_at_neutral():
    fp = build_fingerprint(
        alternating=True,
        stale_book_after_first=True,
    )

    shaped, decision = shape_recovery_proposal(
        recovery_proposal("100"),
        fp,
        now=fp.last_seen_at,
    )

    assert decision.mean_reversion_score is not None
    assert decision.mean_reversion_score > 0
    assert not decision.microstructure_fresh
    assert decision.size_multiplier == D("0.50")
    assert "microstructure_stale" in decision.reasons
    assert shaped is not None
    assert shaped.amount_quote == D("50")


def test_fingerprint_layer_never_increases_requested_capital():
    fp = build_fingerprint(alternating=True)

    shaped, decision = shape_recovery_proposal(
        recovery_proposal("123.45"),
        fp,
        now=fp.last_seen_at,
    )

    assert decision.size_multiplier <= D("1")
    assert shaped is not None
    assert shaped.amount_quote <= D("123.45")


def test_pair_mismatch_fails_closed():
    fp = build_fingerprint(alternating=True)

    with pytest.raises(ValueError, match="does not match fingerprint"):
        shape_recovery_proposal(
            recovery_proposal(pair="ETH-USD"),
            fp,
            now=fp.last_seen_at,
        )


def test_non_recovery_intent_is_rejected():
    fp = build_fingerprint(alternating=True)
    proposal = CapitalProposal(
        proposal_id="entry-1",
        pair="BTC-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("100"),
        leverage=D("1"),
    )

    with pytest.raises(ValueError, match="requires RECOVERY intent"):
        shape_recovery_proposal(
            proposal,
            fp,
            now=fp.last_seen_at,
        )


def test_policy_rejects_multiplier_that_could_expand_capital():
    with pytest.raises(ValueError, match="multipliers"):
        RecoveryFingerprintPolicy(max_multiplier=D("1.01"))


def test_future_fingerprint_evidence_is_rejected():
    fp = build_fingerprint(alternating=True)

    with pytest.raises(ValueError, match="cannot precede"):
        assess_recovery_fingerprint(
            fp,
            now=fp.last_seen_at - 1,
        )



def position(
    *,
    base: str = "10",
    cost: str = "1100",
    price: str = "100",
    pair: str = "BTC-USD",
) -> VCAPPositionState:
    return VCAPPositionState(
        pair=pair,
        position_base=D(base),
        cost_basis_quote=D(cost),
        current_price=D(price),
    )


def test_vcap_strong_reversal_can_expand_seed_request():
    fp = build_fingerprint(alternating=True)

    plan = plan_vcap_recovery(
        recovery_proposal("100"),
        fp,
        position(),
        now=fp.last_seen_at,
    )

    assert plan.behavior.mean_reversion_score is not None
    assert plan.behavior.mean_reversion_score > 0
    assert plan.aggressive
    assert plan.expanded
    assert D("100") < plan.planned_amount_quote <= D("400")
    assert plan.proposal is not None
    assert plan.proposal.amount_quote == plan.planned_amount_quote
    assert plan.after_rebound_bps < plan.before_rebound_bps
    assert plan.rebound_improvement_bps > 0
    assert "aggressive_reversal_weight" in plan.reasons
    assert "capital_request_expanded" in plan.reasons


def test_vcap_persistent_momentum_does_not_expand_seed():
    fp = build_fingerprint(alternating=False)

    plan = plan_vcap_recovery(
        recovery_proposal("100"),
        fp,
        position(),
        now=fp.last_seen_at,
    )

    assert not plan.aggressive
    assert not plan.expanded
    assert plan.planned_amount_quote <= D("100")
    assert "aggressive_signal_unavailable" in plan.reasons


def test_vcap_geometry_measures_break_even_weight():
    fp = build_fingerprint(alternating=True)

    plan = plan_vcap_recovery(
        recovery_proposal("100"),
        fp,
        position(),
        now=fp.last_seen_at,
    )

    assert plan.before_rebound_bps == D("1000")
    assert plan.target_patch_quote == D("1000")
    assert plan.after_rebound_bps < D("1000")
    assert (
        plan.capital_weight_ratio
        == plan.planned_amount_quote / D("1100")
    )


def test_vcap_does_not_expand_when_position_is_inside_target():
    fp = build_fingerprint(alternating=True)

    plan = plan_vcap_recovery(
        recovery_proposal("100"),
        fp,
        position(cost="1040"),
        now=fp.last_seen_at,
    )

    assert plan.before_rebound_bps == D("400")
    assert plan.target_patch_quote == D("0")
    assert not plan.expanded
    assert plan.planned_amount_quote <= D("100")
    assert "already_inside_recovery_target" in plan.reasons


def test_vcap_position_weight_cap_limits_aggression():
    fp = build_fingerprint(alternating=True)

    plan = plan_vcap_recovery(
        recovery_proposal("100"),
        fp,
        position(),
        now=fp.last_seen_at,
        vcap_policy=VCAPRecoveryPolicy(
            max_patch_to_position_ratio=D("0.10"),
        ),
    )

    assert plan.planned_amount_quote <= D("110")
    assert plan.capital_weight_ratio <= D("0.10")


def test_vcap_pair_mismatch_fails_closed():
    fp = build_fingerprint(alternating=True)

    with pytest.raises(ValueError, match="does not match position"):
        plan_vcap_recovery(
            recovery_proposal("100"),
            fp,
            position(pair="ETH-USD"),
            now=fp.last_seen_at,
        )
