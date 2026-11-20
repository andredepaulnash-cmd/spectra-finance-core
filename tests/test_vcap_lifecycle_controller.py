from decimal import Decimal
from dataclasses import replace
from types import SimpleNamespace

from mcp_servers.hummingbot_api.tools.vcap_lifecycle import (
    execute_vcap_lifecycle_decision,
)
from spectra_core.accounting_health import (
    AccountingHealthDecision,
    AccountingHealthState,
)
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.economic_position import EconomicPositionStatus
from spectra_core.models import (
    EntryStrategySource,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.recovery_fingerprint import VCAPPositionState
from spectra_core.vcap_execution import (
    VCAPExecutionDecision,
    VCAPLoopAction,
    carve_next_vcap_tranche,
)
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    checkpoint_vcap_tranche_execution,
    plan_vcap_lifecycle,
    plan_vcap_lifecycle_from_ledger,
)
from spectra_core.vcap_profit_exit import (
    VCAPExitCostModel,
    VCAPProfitState,
    plan_top_loser_adaptive_exit,
)
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)


D = Decimal


def healthy_accounting(position_id, now):
    return AccountingHealthDecision(
        position_id=position_id,
        state=AccountingHealthState.HEALTHY,
        assessed_at=float(now),
        last_attempt_at=float(now),
        last_success_at=float(now),
        max_age_seconds=30.0,
        proposal_count=1,
        fully_reconciled_proposals=1,
        known_orders=1,
        bound_orders=1,
        confirmed_fills=1,
        ledgered_fills=1,
        unresolved_order_ids=(),
        unresolved_trade_ids=(),
        reasons=("all_executor_accounting_fully_reconciled",),
    )


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


def test_exit_dispatcher_refuses_without_unified_economic_exit_context(
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
            position_id="position-1",
            accounting_health=healthy_accounting(
                "position-1",
                fp.last_seen_at + 1,
            ),
        )
    )

    assert result["exit_requested"] is False
    assert result["lifecycle_status"] == VCAPLifecycleStatus.HOLD.value
    assert result["executed"] is False
    assert "unified economic exit requires" in result["error"]



def test_adaptive_exit_state_survives_vcap_store_restart(tmp_path):
    fingerprint = reversal_fingerprint()
    decision = plan_top_loser_adaptive_exit(
        VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1000"),
            current_price=D("96"),
        ),
        costs=costs(),
        now=fingerprint.last_seen_at,
        fingerprint=fingerprint,
    )
    state = lifecycle_state(profit_state=decision.state)
    path = tmp_path / "vcap-adaptive-state.sqlite3"
    VCAPStateStore(path).save(state)

    restored = VCAPStateStore(path).load("vcap-envelope-1")
    assert restored is not None
    assert restored.profit_state is not None
    assert restored.profit_state.peak_price == decision.state.peak_price
    assert restored.profit_state.trailing_stop_price == decision.state.trailing_stop_price
    assert restored.profit_state.stop_distance_bps == decision.state.stop_distance_bps
    assert restored.profit_state.take_profit_target_bps == decision.state.take_profit_target_bps



def test_ledger_backed_vcap_uses_adaptive_exits_only_for_top_loser_positions(tmp_path):
    fingerprint = reversal_fingerprint()
    now = fingerprint.last_seen_at
    store = VCAPStateStore(tmp_path / "source-gated-vcap.sqlite3")
    state = lifecycle_state(position_base="10", cost_basis="1000", price="96", remaining="0")

    def evaluate(source):
        snapshot = SimpleNamespace(
            pair="BTC-USD",
            status=EconomicPositionStatus.ACTIVE,
            strategy_source=source,
            active_base=D("10"),
            active_cost_basis_quote=D("1000"),
            active_entry_fees_quote=D("0"),
            to_vcap_position=lambda *, current_price: VCAPPositionState(
                pair="BTC-USD",
                position_base=D("10"),
                cost_basis_quote=D("1000"),
                current_price=current_price,
            ),
        )
        ledger = SimpleNamespace(snapshot=lambda position_id: snapshot)
        return plan_vcap_lifecycle_from_ledger(
            position_id="position-source-test",
            ledger=ledger,
            state=state,
            store=store,
            fingerprint=fingerprint,
            trading_rule=rule(),
            current_price=D("96"),
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=now,
            accounting_health=healthy_accounting("position-source-test", now),
        )

    top_loser = evaluate(EntryStrategySource.TOP_LOSER_REVERSAL)
    ordinary = evaluate(EntryStrategySource.MEAN_REVERSION)

    assert top_loser.profit_decision is not None
    assert top_loser.profit_decision.exit_mode == "ADAPTIVE_TRAILING"
    assert top_loser.action == VCAPLifecycleAction.EXIT
    assert ordinary.profit_decision is not None
    assert ordinary.profit_decision.exit_mode == "PROFIT_PROTECTED"



def test_ledger_vcap_preserves_adaptive_decision_and_state_before_recovery(monkeypatch, tmp_path):
    from spectra_core import vcap_lifecycle as lifecycle_module

    fingerprint = reversal_fingerprint()
    now = fingerprint.last_seen_at
    position = VCAPPositionState(
        pair="BTC-USD",
        position_base=D("10"),
        cost_basis_quote=D("1000"),
        current_price=D("100"),
    )
    snapshot = SimpleNamespace(
        pair="BTC-USD",
        status=EconomicPositionStatus.ACTIVE,
        strategy_source=EntryStrategySource.TOP_LOSER_REVERSAL,
        active_base=D("10"),
        active_cost_basis_quote=D("1000"),
        active_entry_fees_quote=D("0"),
        to_vcap_position=lambda *, current_price: replace(position, current_price=current_price),
    )
    ledger = SimpleNamespace(snapshot=lambda position_id: snapshot)
    store = VCAPStateStore(tmp_path / "adaptive-ledger-state.sqlite3")
    state = lifecycle_state(cost_basis="1000", price="100")

    def stop_recovery(**kwargs):
        assert kwargs["strategy_source"] == EntryStrategySource.TOP_LOSER_REVERSAL
        return VCAPExecutionDecision(
            envelope_proposal_id="vcap-envelope-1",
            sequence=1,
            action=VCAPLoopAction.STOP,
            remaining_authorized_quote=D("100"),
            current_price=D("100"),
            recovery_plan=None,
            tranche_plan=None,
            next_tranche=None,
            next_proposal=None,
            reasons=("test_recovery_stop",),
        )

    monkeypatch.setattr(lifecycle_module, "replan_vcap_sequence", stop_recovery)
    decision = plan_vcap_lifecycle_from_ledger(
        position_id="top-loser-position",
        ledger=ledger,
        state=state,
        store=store,
        fingerprint=fingerprint,
        trading_rule=rule(),
        current_price=D("100"),
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
        now=now,
        accounting_health=healthy_accounting("top-loser-position", now),
    )

    assert decision.action == VCAPLifecycleAction.HOLD
    assert decision.profit_decision is not None
    assert decision.profit_decision.exit_mode == "ADAPTIVE_TRAILING"
    assert decision.state.profit_state is not None
    assert decision.state.profit_state.trailing_stop_price is not None
    restored = store.load("vcap-envelope-1")
    assert restored is not None
    assert restored.profit_state == decision.state.profit_state
