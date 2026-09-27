from decimal import Decimal

import pytest

from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import CapitalProposal, IntentKind
from spectra_core.recovery_fingerprint import (
    RecoveryFingerprintDecision,
    RecoveryFingerprintMode,
    VCAPRecoveryPlan,
)
from spectra_core.vcap_tranches import (
    VCAPTranchePolicy,
    plan_vcap_tranches,
)


D = Decimal


def trading_rule(
    *,
    pair: str = "BTC-USD",
    min_order_size: str = "0.1",
    min_notional_size: str = "10",
    min_order_value: str = "0",
    base_increment: str = "0.001",
    quote_increment: str = "0.01",
    limit_orders: bool = True,
    market_orders: bool = True,
) -> KrakenTradingRule:
    return KrakenTradingRule(
        trading_pair=pair,
        min_order_size=D(min_order_size),
        min_notional_size=D(min_notional_size),
        min_order_value=D(min_order_value),
        min_price_increment=D("0.01"),
        min_base_amount_increment=D(base_increment),
        min_quote_amount_increment=D(quote_increment),
        supports_limit_orders=limit_orders,
        supports_market_orders=market_orders,
    )


def recovery_plan(
    *,
    planned: str = "100",
    aggressive: bool = False,
    score: float = 0.0,
    confidence: float = 1.0,
    pair: str = "BTC-USD",
) -> VCAPRecoveryPlan:
    amount = D(planned)
    proposal = CapitalProposal(
        proposal_id="recovery-8",
        pair=pair,
        intent=IntentKind.RECOVERY,
        amount_quote=amount,
        leverage=D("1"),
    )
    behavior = RecoveryFingerprintDecision(
        pair=pair,
        mode=RecoveryFingerprintMode.ADAPTIVE,
        size_multiplier=D("1"),
        effective_confidence=confidence,
        mean_reversion_score=score,
        microstructure_fresh=True,
        reasons=("reversal_support",) if score > 0 else ("behavior_neutral",),
    )
    return VCAPRecoveryPlan(
        pair=pair,
        seed_amount_quote=D("20"),
        planned_amount_quote=amount,
        capital_weight_ratio=D("0.1"),
        before_rebound_bps=D("1000"),
        after_rebound_bps=D("500"),
        rebound_improvement_bps=D("500"),
        target_patch_quote=amount,
        behavior=behavior,
        aggressive=aggressive,
        reasons=(),
        proposal=proposal,
    )


def test_normal_vcap_can_split_100_into_five_sellable_20_parcels():
    result = plan_vcap_tranches(
        recovery_plan(planned="100"),
        authorized_quote=D("100"),
        current_price=D("10"),
        trading_rule=trading_rule(),
        policy=VCAPTranchePolicy(sellability_buffer_bps=D("0")),
    )

    assert result.desired_tranche_count == 5
    assert result.actual_tranche_count == 5
    assert result.deployable_quote == D("100")
    assert result.unallocated_quote == D("0")
    assert [row.quote_amount for row in result.tranches] == [
        D("20"),
        D("20"),
        D("20"),
        D("20"),
        D("20"),
    ]
    assert all(row.independently_sellable for row in result.tranches)


def test_exchange_minimum_reduces_tranche_count_instead_of_creating_dust():
    result = plan_vcap_tranches(
        recovery_plan(planned="100"),
        authorized_quote=D("100"),
        current_price=D("10"),
        trading_rule=trading_rule(min_notional_size="25"),
    )

    assert result.desired_tranche_count == 5
    assert result.actual_tranche_count == 3
    assert "tranche_count_reduced_for_sellability" in result.reasons
    assert all(row.quote_amount >= D("25.25") for row in result.tranches)


def test_sellability_buffer_prevents_exact_minimum_edge_parcel():
    result = plan_vcap_tranches(
        recovery_plan(planned="100"),
        authorized_quote=D("100"),
        current_price=D("10"),
        trading_rule=trading_rule(min_notional_size="20"),
    )

    assert result.actual_tranche_count == 4
    assert all(row.quote_amount >= D("20.20") for row in result.tranches)


def test_governor_authorization_is_hard_envelope():
    result = plan_vcap_tranches(
        recovery_plan(planned="100"),
        authorized_quote=D("60"),
        current_price=D("10"),
        trading_rule=trading_rule(),
    )

    assert result.deployable_quote <= D("60")
    assert sum((row.quote_amount for row in result.tranches), D("0")) <= D("60")
    assert "governor_reduced_recovery_envelope" in result.reasons


def test_aggressive_reversal_uses_fewer_front_loaded_tranches():
    result = plan_vcap_tranches(
        recovery_plan(
            planned="120",
            aggressive=True,
            score=1.0,
            confidence=1.0,
        ),
        authorized_quote=D("120"),
        current_price=D("10"),
        trading_rule=trading_rule(),
        policy=VCAPTranchePolicy(sellability_buffer_bps=D("0")),
    )

    assert result.desired_tranche_count == 2
    assert result.actual_tranche_count == 2
    assert result.tranches[0].quote_amount > result.tranches[1].quote_amount
    assert "aggressive_front_loaded_fracture" in result.reasons


def test_quantization_residual_is_held_not_created_as_dust():
    result = plan_vcap_tranches(
        recovery_plan(planned="100"),
        authorized_quote=D("100"),
        current_price=D("10"),
        trading_rule=trading_rule(
            min_order_size="1",
            min_notional_size="10",
            base_increment="0.3",
            quote_increment="0",
        ),
        policy=VCAPTranchePolicy(sellability_buffer_bps=D("0")),
    )

    assert result.deployable_quote == D("99.0")
    assert result.unallocated_quote == D("1.0")
    assert "rounding_residual_held_unallocated" in result.reasons
    assert all(row.base_amount % D("0.3") == 0 for row in result.tranches)


def test_capital_too_small_for_one_sellable_parcel_stays_unallocated():
    result = plan_vcap_tranches(
        recovery_plan(planned="20"),
        authorized_quote=D("20"),
        current_price=D("10"),
        trading_rule=trading_rule(min_notional_size="25"),
    )

    assert result.actual_tranche_count == 0
    assert result.deployable_quote == D("0")
    assert result.unallocated_quote == D("20")
    assert result.tranches == ()
    assert "below_exchange_sellability_minimum" in result.reasons


def test_base_minimum_can_reduce_fraction_count():
    result = plan_vcap_tranches(
        recovery_plan(planned="100"),
        authorized_quote=D("100"),
        current_price=D("100"),
        trading_rule=trading_rule(
            min_order_size="0.30",
            min_notional_size="1",
            base_increment="0.01",
            quote_increment="0",
        ),
    )

    assert result.actual_tranche_count == 3
    assert all(row.base_amount >= D("0.303") for row in result.tranches)


def test_pair_mismatch_fails_closed():
    with pytest.raises(ValueError, match="does not match trading rule"):
        plan_vcap_tranches(
            recovery_plan(pair="BTC-USD"),
            authorized_quote=D("100"),
            current_price=D("10"),
            trading_rule=trading_rule(pair="ETH-USD"),
        )


def test_no_supported_exit_order_type_fails_closed():
    with pytest.raises(ValueError, match="no supported exit order type"):
        plan_vcap_tranches(
            recovery_plan(),
            authorized_quote=D("100"),
            current_price=D("10"),
            trading_rule=trading_rule(
                limit_orders=False,
                market_orders=False,
            ),
        )
