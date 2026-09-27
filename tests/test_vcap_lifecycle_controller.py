from decimal import Decimal

from mcp_servers.hummingbot_api.tools.vcap_lifecycle import (
    execute_vcap_lifecycle_decision,
)
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.recovery_fingerprint import VCAPPositionState
from spectra_core.vcap_execution import carve_next_vcap_tranche
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    checkpoint_vcap_tranche_execution,
    plan_vcap_lifecycle,
)
from spectra_core.vcap_profit_exit import (
    VCAPExitCostModel,
    VCAPProfitState,
)
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)


D = Decimal


def observation(timestamp, price):
    return AssetObservation(
        trading_pair="BTC-USD",
        timestamp=timestamp,
        price=price,
        quote_volume_24h=1_000_000,
        spread_bps=5.0,
        depth_quote=50_000.0,
        depth_imbalance=0.1,
        microprice_edge_bps=0.5,
    )


def reversal_fingerprint():
    price = 100.0
    fp = AssetFingerprint.cold_start(observation(1_000, price))
    for i in range(1, 41):
        price = 101.0 if i % 2 else 100.0
        fp = fp.update(observation(1_000 + i * 60, price))
    return fp


def rule():
    return KrakenTradingRule(
        trading_pair="BTC-USD",
        min_order_size=D("0.01"),
        min_notional_size=D("10"),
        min_order_value=D("0"),
        min_price_increment=D("0.01"),
        min_base_amount_increment=D("0.001"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def lifecycle_state(
    *,
    position_base="10",
    cost_basis="1100",
    price="100",
    remaining="100",
    sequence=1,
    proposal_ids=(),
    profit_state=None,
    status=VCAPLifecycleStatus.ACTIVE,
):
    return VCAPLifecycleState(
        envelope_proposal_id="vcap-envelope-1",
        pair="BTC-USD",
        next_sequence=sequence,
        remaining_authorized_quote=D(remaining),
        recovery_proposal_ids=tuple(proposal_ids),
        position_base=D(position_base),
        cost_basis_quote=D(cost_basis),
        last_known_price=D(price),
        profit_state=profit_state,
        status=status,
        updated_at=3_400,
    )


def costs():
    return VCAPExitCostModel(
        accrued_fees_quote=D("0"),
        expected_exit_fee_bps=D("0"),
        slippage_allowance_bps=D("0"),
    )


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "vcap-lifecycle-governor.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("1000"),
            max_pair_quote=D("1000"),
            max_position_quote=D("1000"),
            max_recovery_quote=D("500"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def reserve_envelope(g, amount="100"):
    proposal = CapitalProposal(
        proposal_id="vcap-envelope-1",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D(amount),
        leverage=D("1"),
    )
    result = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert result.status == DecisionStatus.RESERVED
    return proposal


def test_unified_lifecycle_checkpoints_next_tranche_before_external_mutation(
    tmp_path,
):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state()
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLifecycleAction.EXECUTE_TRANCHE
    assert decision.execution_decision is not None
    proposal = decision.execution_decision.next_proposal
    assert proposal is not None

    persisted = store.load("vcap-envelope-1")
    assert persisted is not None
    assert proposal.proposal_id in persisted.recovery_proposal_ids
    assert persisted.next_sequence == 1
    assert persisted.remaining_authorized_quote == D("100")
    assert "external_mutation_checkpointed" in decision.reasons


def test_profit_floor_disables_additional_recovery_patching(tmp_path):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state(
        cost_basis="1000",
        price="102",
    )
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1000"),
            current_price=D("102"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLifecycleAction.PROTECT_PROFIT
    assert decision.execution_decision is None
    assert decision.profit_decision is not None
    assert decision.profit_decision.profit_floor_reached
    assert "recovery_patching_disabled_after_profit_floor" in decision.reasons


def test_previously_reached_profit_floor_exit_outranks_recovery(tmp_path):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    prior = VCAPProfitState(
        updated_at=fp.last_seen_at - 60,
        profit_floor_reached_at=fp.last_seen_at - 120,
        peak_net_profit_bps=D("220"),
        peak_at=fp.last_seen_at - 60,
    )
    state = lifecycle_state(
        cost_basis="1000",
        price="101",
        profit_state=prior,
    )
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1000"),
            current_price=D("101"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLifecycleAction.EXIT
    assert decision.execution_decision is None
    assert "protected_profit_floor_breached" in decision.reasons
    assert "profit_exit_has_priority" in decision.reasons


def test_recovery_stop_maps_to_hold_until_profit_is_realized(tmp_path):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state(
        cost_basis="1040",
        price="100",
    )
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1040"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLifecycleAction.HOLD
    assert decision.execution_decision is not None
    assert "recovery_target_satisfied" in decision.reasons
    assert "awaiting_profitable_exit" in decision.reasons

    persisted = store.load("vcap-envelope-1")
    assert persisted is not None
    assert persisted.status == VCAPLifecycleStatus.HOLD


def test_successful_tranche_receipt_advances_sequence_exactly_once(tmp_path):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state()
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )
    assert decision.execution_decision is not None

    g = governor(tmp_path)
    reserve_envelope(g)
    assert carve_next_vcap_tranche(g, decision.execution_decision)

    proposal = decision.execution_decision.next_proposal
    assert proposal is not None
    assert g.commit_if_matches(proposal)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id="exec-unified-1",
    )

    first = checkpoint_vcap_tranche_execution(
        decision=decision,
        governor=g,
        store=store,
        now=fp.last_seen_at + 1,
    )
    assert first.next_sequence == 2
    assert first.remaining_authorized_quote == (
        D("100") - proposal.amount_quote
    )

    replay = checkpoint_vcap_tranche_execution(
        decision=decision,
        governor=g,
        store=store,
        now=fp.last_seen_at + 2,
    )
    assert replay.next_sequence == 2
    assert replay.remaining_authorized_quote == first.remaining_authorized_quote


def test_missing_receipt_holds_without_consuming_persisted_envelope(tmp_path):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state()
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )

    g = governor(tmp_path)
    checkpoint = checkpoint_vcap_tranche_execution(
        decision=decision,
        governor=g,
        store=store,
        now=fp.last_seen_at + 1,
    )

    assert checkpoint.status == VCAPLifecycleStatus.HOLD
    assert checkpoint.next_sequence == 1
    assert checkpoint.remaining_authorized_quote == D("100")


def test_retired_lifecycle_is_terminal_and_does_not_replan(tmp_path):
    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state(status=VCAPLifecycleStatus.RETIRED)
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLifecycleAction.COMPLETE
    assert decision.execution_decision is None
    assert decision.profit_decision is None


def test_exit_dispatcher_refuses_when_checkpointed_ownership_lacks_provenance(
    tmp_path,
):
    import asyncio

    fp = reversal_fingerprint()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    prior = VCAPProfitState(
        updated_at=fp.last_seen_at - 60,
        profit_floor_reached_at=fp.last_seen_at - 120,
        peak_net_profit_bps=D("220"),
        peak_at=fp.last_seen_at - 60,
    )
    state = lifecycle_state(
        cost_basis="1000",
        price="101",
        proposal_ids=("vcap-envelope-1:vcap:0001",),
        profit_state=prior,
    )
    store.save(state)

    decision = plan_vcap_lifecycle(
        state=state,
        store=store,
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1000"),
            current_price=D("101"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        seed_quote=D("20"),
        costs=costs(),
        now=fp.last_seen_at,
    )
    assert decision.action == VCAPLifecycleAction.EXIT

    class Client:
        pass

    result = asyncio.run(
        execute_vcap_lifecycle_decision(
            Client(),
            decision=decision,
            governor=governor(tmp_path),
            store=store,
            now=fp.last_seen_at + 1,
        )
    )

    assert result["exit_requested"] is False
    assert result["lifecycle_status"] == VCAPLifecycleStatus.HOLD.value
    assert result["missing_proposal_provenance"] == [
        "vcap-envelope-1:vcap:0001"
    ]
