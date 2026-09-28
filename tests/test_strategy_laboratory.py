from decimal import Decimal

import pytest

from spectra_core.strategy_lab import (
    CapitalAuctionPolicy,
    CapitalOpportunity,
    EquitySample,
    ExecutionPolicy,
    ExecutionStyle,
    LabAction,
    MarketMicrostructure,
    calculate_performance_metrics,
    dynamic_fleet_capacity,
    effective_exposure_count,
    estimate_execution,
    run_capital_auction,
    score_opportunity,
)
from spectra_core.strategy_lab_scenarios import (
    CompoundingResearchAccount,
    LIQUIDITY_CRUNCH,
    TOKEN_COLLAPSE,
    stress_execution_policy,
    stress_market,
    stress_return_path,
)
from spectra_core.strategy_lab_validation import (
    PromotionDecision,
    PromotionEvidence,
    ResearchStage,
    ResearchTrial,
    StrategyLabStore,
    block_bootstrap_stress,
    dataset_fingerprint,
    deflated_sharpe_ratio,
    evaluate_canary_promotion,
    parameter_neighborhood_stability,
    probability_of_backtest_overfitting,
    purged_walk_forward_splits,
)


D = Decimal


def policy(**overrides):
    values = {
        "reserve_fraction": D("0.50"),
        "minimum_recovery_reserve_quote": D("0"),
        "target_fleet_min": 10,
        "target_fleet_normal": 20,
        "target_fleet_max": 40,
        "minimum_viable_position_quote": D("10"),
    }
    values.update(overrides)
    return CapitalAuctionPolicy(**values)


def opportunity(
    opportunity_id,
    pair,
    *,
    action=LabAction.ENTRY,
    request="10",
    edge="100",
    hours="24",
    liquidity="0.8",
    reversal="0.8",
    mre="0",
    urgency="0.2",
    correlation="0",
    dead_money="0",
    execution_cost="5",
    position_id=None,
):
    return CapitalOpportunity(
        opportunity_id=opportunity_id,
        pair=pair,
        action=action,
        requested_quote=D(request),
        expected_net_edge_bps=D(edge),
        expected_holding_hours=D(hours),
        liquidity_score=D(liquidity),
        reversal_score=D(reversal),
        marginal_recovery_efficiency=D(mre),
        urgency_score=D(urgency),
        correlation_penalty=D(correlation),
        dead_money_penalty=D(dead_money),
        expected_execution_cost_bps=D(execution_cost),
        position_id=position_id,
    )


def test_dynamic_fleet_is_target_not_forced_count():
    small = dynamic_fleet_capacity(D("100"), policy=policy())
    assert small.feasible_positions == 5
    assert small.target_positions == 5
    assert small.target_min_constrained is True

    starting = dynamic_fleet_capacity(D("200"), policy=policy())
    assert starting.protected_reserve_quote == D("100")
    assert starting.deployable_quote == D("100")
    assert starting.target_positions == 10
    assert starting.target_min_constrained is False

    grown = dynamic_fleet_capacity(D("400"), policy=policy())
    assert grown.target_positions == 20


def test_capital_auction_prices_time_recovery_quality_and_dead_money():
    fast = opportunity(
        "fast",
        "SOL-USD",
        edge="80",
        hours="4",
        liquidity="0.9",
        reversal="0.8",
    )
    slow = opportunity(
        "slow",
        "ADA-USD",
        edge="120",
        hours="240",
        liquidity="0.9",
        reversal="0.8",
        dead_money="0.8",
    )
    assert score_opportunity(fast, policy=policy()).score > score_opportunity(
        slow,
        policy=policy(),
    ).score

    recovery = opportunity(
        "recovery",
        "XRP-USD",
        action=LabAction.RECOVERY,
        request="20",
        edge="50",
        hours="12",
        mre="0.95",
        position_id="xrp-position",
    )
    weak_recovery = opportunity(
        "weak-recovery",
        "ETH-USD",
        action=LabAction.RECOVERY,
        request="20",
        edge="50",
        hours="12",
        mre="0.05",
        position_id="eth-position",
    )
    result = run_capital_auction(
        [weak_recovery, recovery],
        equity_quote=D("200"),
        active_pairs_by_position={
            "xrp-position": "XRP-USD",
            "eth-position": "ETH-USD",
        },
        policy=policy(),
    )
    assert result.allocations[0].opportunity_id == "recovery"


def test_auction_preserves_single_asset_and_one_mutation_per_position():
    rows = [
        opportunity("new-btc", "XBT-EUR", edge="500", hours="1"),
        opportunity(
            "xrp-recovery",
            "XRP-USD",
            action=LabAction.RECOVERY,
            edge="200",
            hours="2",
            mre="1",
            position_id="xrp-position",
        ),
        opportunity(
            "xrp-recycle",
            "XRP-USD",
            action=LabAction.PARTIAL_RECYCLE,
            edge="190",
            hours="2",
            mre="1",
            position_id="xrp-position",
        ),
    ]
    result = run_capital_auction(
        rows,
        equity_quote=D("300"),
        active_pairs_by_position={
            "btc-position": "BTC-USD",
            "xrp-position": "XRP-USD",
        },
        policy=policy(),
    )
    assert result.skipped["new-btc"] == "base_asset_already_active"
    xrp = [
        allocation
        for allocation in result.allocations
        if allocation.position_id == "xrp-position"
    ]
    assert len(xrp) == 1
    assert result.proposed_quote <= result.deployable_quote


def test_realized_profit_is_immediately_available_but_not_forced_to_spend():
    account = CompoundingResearchAccount(D("190"))
    before = account.auction(
        [opportunity(f"entry-{i}", f"A{i}-USD") for i in range(20)],
        policy=policy(),
    )
    assert before.target_positions == 9

    account.book_realized_pnl(D("10"))
    after = account.auction(
        [opportunity(f"entry-{i}", f"A{i}-USD") for i in range(20)],
        policy=policy(),
    )
    assert account.equity_quote == D("200")
    assert after.target_positions == 10
    assert after.protected_reserve_quote == D("100")


def test_execution_model_prices_maker_taker_and_liquidity_stress():
    market = MarketMicrostructure(
        pair="XRP-USD",
        price=D("0.50"),
        spread_bps=D("8"),
        depth_quote=D("1000"),
        liquidity_score=D("0.9"),
        volatility_bps=D("50"),
    )
    fees = ExecutionPolicy(
        maker_fee_bps=D("40"),
        taker_fee_bps=D("80"),
        base_latency_bps=D("1"),
        impact_at_full_depth_bps=D("30"),
    )
    maker = estimate_execution(
        requested_quote=D("100"),
        urgency_score=D("0.1"),
        market=market,
        policy=fees,
        style=ExecutionStyle.PATIENT_MAKER,
    )
    taker = estimate_execution(
        requested_quote=D("100"),
        urgency_score=D("1"),
        market=market,
        policy=fees,
        style=ExecutionStyle.TAKER,
    )
    assert maker.fill_ratio < D("1")
    assert taker.fill_ratio == D("1")
    assert taker.total_cost_bps > maker.total_cost_bps

    stressed_market = stress_market(market, LIQUIDITY_CRUNCH)
    stressed_fees = stress_execution_policy(fees, LIQUIDITY_CRUNCH)
    stressed = estimate_execution(
        requested_quote=D("100"),
        urgency_score=D("1"),
        market=stressed_market,
        policy=stressed_fees,
        style=ExecutionStyle.TAKER,
    )
    assert stressed.total_cost_bps > taker.total_cost_bps


def test_performance_metrics_measure_capital_time_drawdown_and_fee_drag():
    samples = [
        EquitySample(0, D("100"), D("50")),
        EquitySample(3600, D("110"), D("50")),
        EquitySample(7200, D("99"), D("25")),
        EquitySample(10800, D("120"), D("25")),
    ]
    metrics = calculate_performance_metrics(
        samples,
        realized_net_profit_quote=D("20"),
        turnover_quote=D("300"),
        fee_drag_quote=D("3"),
    )
    assert metrics.net_profit_quote == D("20")
    assert metrics.total_return == D("0.2")
    assert metrics.max_drawdown == D("0.1")
    assert metrics.capital_hours == D("125")
    assert metrics.capital_time_yield_quote_per_dollar_hour == D("0.16")
    assert metrics.fee_drag_quote == D("3")
    assert metrics.time_underwater_hours == D("1")


def test_effective_exposure_detects_fake_diversification():
    weights = {"BTC": D("0.5"), "ETH": D("0.5")}
    independent = effective_exposure_count(weights, {})
    correlated = effective_exposure_count(
        weights,
        {("BTC", "ETH"): D("1")},
    )
    assert independent == D("2")
    assert correlated == D("1")


def test_walk_forward_splits_have_purge_and_embargo():
    folds = purged_walk_forward_splits(
        100,
        train_size=20,
        test_size=10,
        purge_size=3,
        embargo_size=2,
    )
    assert folds[0].train_start == 0
    assert folds[0].train_end == 20
    assert folds[0].test_start == 23
    assert folds[0].test_end == 33
    assert folds[1].train_start == 35
    assert set(folds[0].train_range).isdisjoint(folds[0].test_range)


def test_deflated_sharpe_penalizes_large_strategy_search():
    returns = [
        0.01,
        0.02,
        -0.005,
        0.015,
        0.01,
        -0.002,
        0.012,
        0.008,
        0.011,
        -0.001,
    ]
    one = deflated_sharpe_ratio(
        returns,
        trial_sharpes=[0.5],
    )
    many = deflated_sharpe_ratio(
        returns,
        trial_sharpes=[
            -0.4,
            -0.2,
            0.0,
            0.1,
            0.3,
            0.5,
            0.7,
            0.9,
        ],
    )
    assert many.expected_maximum_sharpe > one.expected_maximum_sharpe
    assert many.probability < one.probability


def test_pbo_flags_strategy_that_wins_train_and_fails_holdout():
    pbo = probability_of_backtest_overfitting(
        {
            "fragile": [10.0, 10.0, -10.0, -10.0],
            "steady": [0.0, 0.0, 0.0, 0.0],
        }
    )
    assert pbo > 0


def test_block_bootstrap_is_seeded_and_stresses_drawdown():
    returns = [
        D("0.02"),
        D("-0.01"),
        D("0.03"),
        D("-0.04"),
        D("0.01"),
        D("0.02"),
    ]
    first = block_bootstrap_stress(
        returns,
        starting_equity=D("100"),
        path_count=50,
        block_size=2,
        seed=7,
    )
    second = block_bootstrap_stress(
        returns,
        starting_equity=D("100"),
        path_count=50,
        block_size=2,
        seed=7,
    )
    assert first == second
    assert first.worst_max_drawdown >= first.median_max_drawdown


def test_parameter_stability_rejects_single_point_miracle():
    stable = parameter_neighborhood_stability(
        D("100"),
        [D("90"), D("95"), D("105"), D("80")],
    )
    brittle = parameter_neighborhood_stability(
        D("100"),
        [D("10"), D("20"), D("110"), D("5")],
    )
    assert stable == D("1")
    assert brittle == D("0.25")


def test_promotion_gate_can_only_nominate_canary():
    blocked = evaluate_canary_promotion(
        PromotionEvidence(
            adversarial_passed=True,
            out_of_sample_passed=True,
            shadow_passed=False,
            deflated_sharpe_probability=D("0.99"),
            pbo=D("0.10"),
            parameter_stability=D("0.90"),
            max_drawdown=D("0.20"),
        )
    )
    assert blocked.decision == PromotionDecision.BLOCKED
    assert "shadow_mode_not_passed" in blocked.reasons

    eligible = evaluate_canary_promotion(
        PromotionEvidence(
            adversarial_passed=True,
            out_of_sample_passed=True,
            shadow_passed=True,
            deflated_sharpe_probability=D("0.99"),
            pbo=D("0.10"),
            parameter_stability=D("0.90"),
            max_drawdown=D("0.20"),
        )
    )
    assert eligible.decision == PromotionDecision.CANARY_ELIGIBLE
    assert eligible.reasons == ()


def test_trial_ledger_counts_every_search_and_replay_is_exact(tmp_path):
    store = StrategyLabStore(tmp_path / "strategy-lab.sqlite3")
    trial = ResearchTrial(
        trial_id="trial-1",
        strategy_name="dynamic-vcap",
        stage=ResearchStage.HISTORICAL,
        dataset_fingerprint=dataset_fingerprint(b"dataset-v1"),
        parameters={"rsi": 30, "fleet": 10},
        metrics={"net_profit": "12.34"},
        created_at=1000,
    )
    store.record(trial)
    store.record(trial)
    assert store.trial_count() == 1
    assert store.list_trials()[0] == trial

    with pytest.raises(RuntimeError, match="replay conflicts"):
        store.record(
            ResearchTrial(
                trial_id="trial-1",
                strategy_name="dynamic-vcap",
                stage=ResearchStage.HISTORICAL,
                dataset_fingerprint=trial.dataset_fingerprint,
                parameters={"rsi": 31, "fleet": 10},
                metrics={"net_profit": "12.34"},
                created_at=1000,
            )
        )


def test_adversarial_token_collapse_is_explicit_not_hidden():
    returns = (D("0.01"), D("0.02"), D("0.01"))
    stressed = stress_return_path(returns, TOKEN_COLLAPSE)
    assert len(stressed) == len(returns)
    assert stressed[1] < D("-0.60")
