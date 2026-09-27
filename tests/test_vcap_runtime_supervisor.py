import asyncio
from decimal import Decimal

import pytest

import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime_tool
from spectra_core.accounting_health import (
    AccountingHealthDecision,
    AccountingHealthState,
    AccountingHealthSupervisor,
)
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import CapitalPolicy, CapitalProposal, IntentKind
from spectra_core.vcap_execution import VCAPExecutionDecision, VCAPLoopAction
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    VCAPLifecycleDecision,
)
from spectra_core.vcap_runtime import (
    VCAPRuntimePhase,
    VCAPRuntimePolicy,
    VCAPRuntimeStore,
)
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)
from spectra_core.vcap_tranches import VCAPTranche


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "runtime-governor.sqlite3",
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


def economic_ledger(tmp_path):
    ledger = EconomicPositionLedger(tmp_path / "runtime-economic.sqlite3")
    ledger.open_position(
        position_id="position-runtime-1",
        pair="XRP-USD",
        created_at=1_000,
    )
    return ledger


def lifecycle_state():
    return VCAPLifecycleState(
        envelope_proposal_id="runtime-envelope-1",
        pair="XRP-USD",
        next_sequence=1,
        remaining_authorized_quote=D("100"),
        recovery_proposal_ids=(),
        position_base=D("20"),
        cost_basis_quote=D("10"),
        last_known_price=D("0.5"),
        profit_state=None,
        status=VCAPLifecycleStatus.ACTIVE,
        updated_at=1_900,
    )


def stores(tmp_path):
    lifecycle = VCAPStateStore(tmp_path / "runtime-vcap-state.sqlite3")
    lifecycle.save(lifecycle_state())
    runtime = VCAPRuntimeStore(tmp_path / "runtime-cycle.sqlite3")
    ledger = economic_ledger(tmp_path)
    health = AccountingHealthSupervisor(ledger.path)
    return lifecycle, runtime, ledger, health


def healthy(now=2_000):
    return AccountingHealthDecision(
        position_id="position-runtime-1",
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


def fingerprint(now=2_000):
    return AssetFingerprint.cold_start(
        AssetObservation(
            trading_pair="XRP-USD",
            timestamp=now,
            price=0.5,
            quote_volume_24h=1_000_000,
            spread_bps=5.0,
            depth_quote=50_000.0,
            depth_imbalance=0.1,
            microprice_edge_bps=0.5,
        )
    )


def rule():
    return KrakenTradingRule(
        trading_pair="XRP-USD",
        min_order_size=D("1"),
        min_notional_size=D("5"),
        min_order_value=D("0"),
        min_price_increment=D("0.0001"),
        min_base_amount_increment=D("0.1"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def tranche_decision(state):
    proposal = CapitalProposal(
        proposal_id="runtime-envelope-1:vcap:0001",
        pair="XRP-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("20"),
        leverage=D("1"),
    )
    execution = VCAPExecutionDecision(
        envelope_proposal_id="runtime-envelope-1",
        sequence=1,
        action=VCAPLoopAction.EXECUTE,
        remaining_authorized_quote=D("100"),
        current_price=D("0.5"),
        recovery_plan=None,
        tranche_plan=None,
        next_tranche=VCAPTranche(
            sequence=1,
            quote_amount=D("20"),
            base_amount=D("40"),
            weight_ratio=D("1"),
        ),
        next_proposal=proposal,
        reasons=("runtime_test_tranche",),
    )
    return VCAPLifecycleDecision(
        action=VCAPLifecycleAction.EXECUTE_TRANCHE,
        state=state,
        profit_decision=None,
        execution_decision=execution,
        reasons=("runtime_test_tranche",),
    )


def test_runtime_store_resumes_nonterminal_cycle_and_advances_after_terminal(
    tmp_path,
):
    store = VCAPRuntimeStore(tmp_path / "runtime.sqlite3")

    first = store.begin_or_resume(
        position_id="position-1",
        envelope_proposal_id="envelope-1",
        now=1_000,
    )
    waiting = store.transition(
        first.cycle_id,
        phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
        now=1_010,
        expected_phases=(VCAPRuntimePhase.STARTED,),
        planned_action=VCAPLifecycleAction.EXECUTE_TRANCHE.value,
        mutation_key="TRANCHE:p1",
        mutation_proposal_id="p1",
    )
    resumed = store.begin_or_resume(
        position_id="position-1",
        envelope_proposal_id="envelope-1",
        now=1_020,
    )

    assert resumed.cycle_id == waiting.cycle_id
    assert resumed.sequence == 1
    assert resumed.heartbeat_at == 1_020

    store.transition(
        resumed.cycle_id,
        phase=VCAPRuntimePhase.COMPLETED,
        now=1_030,
        expected_phases=(VCAPRuntimePhase.AWAITING_RECONCILIATION,),
    )
    second = store.begin_or_resume(
        position_id="position-1",
        envelope_proposal_id="envelope-1",
        now=1_040,
    )
    assert second.sequence == 2
    assert second.cycle_id != first.cycle_id


def test_runtime_fingerprint_survives_store_restart(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    store = VCAPRuntimeStore(path)
    fp = fingerprint(2_000)
    store.save_fingerprint(fp, now=2_000)

    restarted = VCAPRuntimeStore(path)
    restored = restarted.load_fingerprint("XRP-USD")

    assert restored == fp
    assert restored.observation_count == 1


class FakeMarketData:
    def __init__(self, timestamp=2_000.0, price="0.5"):
        self.timestamp = timestamp
        self.price = price

    async def get_tickers(self, connector):
        assert connector == "kraken"
        return {
            "tickers": {
                "kraken": {
                    "XRP-USD": {
                        "price": self.price,
                        "base_volume": "100000",
                        "quote_volume": "50000",
                        "timestamp": self.timestamp,
                    }
                }
            },
            "updated_at": {"kraken": self.timestamp},
        }

    async def get_order_book(self, **kwargs):
        assert kwargs["connector_name"] == "kraken"
        return {
            "bids": [["0.4999", "1000"]],
            "asks": [["0.5001", "1000"]],
            "timestamp": self.timestamp,
        }


class FakeConnectors:
    async def get_trading_rules(self, connector):
        assert connector == "kraken"
        return {
            "XRP-USD": {
                "min_order_size": "1",
                "min_notional_size": "5",
                "min_order_value": "0",
                "min_price_increment": "0.0001",
                "min_base_amount_increment": "0.1",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
                "supports_market_orders": True,
            }
        }


class MarketClient:
    def __init__(self, timestamp=2_000.0, price="0.5"):
        self.market_data = FakeMarketData(timestamp, price)
        self.connectors = FakeConnectors()


def test_market_refresh_persists_and_updates_behavioral_memory(tmp_path):
    store = VCAPRuntimeStore(tmp_path / "runtime.sqlite3")
    client = MarketClient(2_000.0, "0.5")

    first, first_rule, first_price = asyncio.run(
        runtime_tool._refresh_market(
            client,
            pair="XRP-USD",
            runtime_store=store,
            policy=VCAPRuntimePolicy(market_max_age_seconds=30),
            now=2_000,
        )
    )

    assert first.observation_count == 1
    assert first_rule.trading_pair == "XRP-USD"
    assert first_price == D("0.5")

    client.market_data.timestamp = 2_010.0
    client.market_data.price = "0.51"
    second, _, second_price = asyncio.run(
        runtime_tool._refresh_market(
            client,
            pair="XRP-USD",
            runtime_store=store,
            policy=VCAPRuntimePolicy(market_max_age_seconds=30),
            now=2_010,
        )
    )

    assert second.observation_count == 2
    assert second.last_seen_at == 2_010
    assert second_price == D("0.51")
    assert store.load_fingerprint("XRP-USD") == second


def test_market_refresh_rejects_stale_snapshot(tmp_path):
    store = VCAPRuntimeStore(tmp_path / "runtime.sqlite3")
    client = MarketClient(2_000.0, "0.5")

    with pytest.raises(RuntimeError, match="market observation is stale"):
        asyncio.run(
            runtime_tool._refresh_market(
                client,
                pair="XRP-USD",
                runtime_store=store,
                policy=VCAPRuntimePolicy(market_max_age_seconds=30),
                now=2_031,
            )
        )


def test_runtime_waits_for_confirmed_fill_and_restart_does_not_redispatch(
    tmp_path,
    monkeypatch,
):
    lifecycle, runtime, ledger, supervisor = stores(tmp_path)
    g = governor(tmp_path)
    execute_calls = []
    fill_visible = {"value": False}

    async def fake_health(*args, **kwargs):
        return healthy(kwargs["now"])

    async def fake_market(*args, **kwargs):
        return fingerprint(kwargs["now"]), rule(), D("0.5")

    def fake_plan(**kwargs):
        return tranche_decision(kwargs["state"])

    async def fake_execute(*args, **kwargs):
        execute_calls.append(kwargs["decision"].action.value)
        return {
            "action": VCAPLifecycleAction.EXECUTE_TRANCHE.value,
            "result": {
                "executed": True,
                "proposal_id": "runtime-envelope-1:vcap:0001",
            },
            "lifecycle_status": VCAPLifecycleStatus.ACTIVE.value,
        }

    monkeypatch.setattr(runtime_tool, "_refresh_accounting", fake_health)
    monkeypatch.setattr(runtime_tool, "_refresh_market", fake_market)
    monkeypatch.setattr(
        runtime_tool,
        "plan_vcap_lifecycle_from_ledger",
        fake_plan,
    )
    monkeypatch.setattr(
        runtime_tool,
        "execute_vcap_lifecycle_decision",
        fake_execute,
    )
    monkeypatch.setattr(
        runtime_tool,
        "_tranche_fill_confirmed",
        lambda **kwargs: fill_visible["value"],
    )

    first = asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            object(),
            position_id="position-runtime-1",
            envelope_proposal_id="runtime-envelope-1",
            governor=g,
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            accounting_supervisor=supervisor,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_000,
        )
    )

    assert first["phase"] == VCAPRuntimePhase.AWAITING_RECONCILIATION.value
    assert first["terminal"] is False
    assert first["cycle_sequence"] == 1
    assert execute_calls == [VCAPLifecycleAction.EXECUTE_TRANCHE.value]

    fill_visible["value"] = True
    second = asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            object(),
            position_id="position-runtime-1",
            envelope_proposal_id="runtime-envelope-1",
            governor=g,
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            accounting_supervisor=supervisor,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_010,
        )
    )

    assert second["cycle_id"] == first["cycle_id"]
    assert second["phase"] == VCAPRuntimePhase.COMPLETED.value
    assert execute_calls == [VCAPLifecycleAction.EXECUTE_TRANCHE.value]


def test_ambiguous_dispatch_never_starts_a_new_cycle_or_retries_mutation(
    tmp_path,
    monkeypatch,
):
    lifecycle, runtime, ledger, supervisor = stores(tmp_path)
    g = governor(tmp_path)
    execute_calls = []

    async def fake_health(*args, **kwargs):
        return healthy(kwargs["now"])

    async def fake_market(*args, **kwargs):
        return fingerprint(kwargs["now"]), rule(), D("0.5")

    def fake_plan(**kwargs):
        return tranche_decision(kwargs["state"])

    async def exploding_execute(*args, **kwargs):
        execute_calls.append("attempt")
        raise RuntimeError("transport lost after dispatch")

    monkeypatch.setattr(runtime_tool, "_refresh_accounting", fake_health)
    monkeypatch.setattr(runtime_tool, "_refresh_market", fake_market)
    monkeypatch.setattr(
        runtime_tool,
        "plan_vcap_lifecycle_from_ledger",
        fake_plan,
    )
    monkeypatch.setattr(
        runtime_tool,
        "execute_vcap_lifecycle_decision",
        exploding_execute,
    )
    monkeypatch.setattr(
        runtime_tool,
        "_tranche_fill_confirmed",
        lambda **kwargs: False,
    )

    first = asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            object(),
            position_id="position-runtime-1",
            envelope_proposal_id="runtime-envelope-1",
            governor=g,
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            accounting_supervisor=supervisor,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_000,
        )
    )
    assert first["phase"] == VCAPRuntimePhase.AWAITING_RECONCILIATION.value
    assert execute_calls == ["attempt"]

    second = asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            object(),
            position_id="position-runtime-1",
            envelope_proposal_id="runtime-envelope-1",
            governor=g,
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            accounting_supervisor=supervisor,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_010,
        )
    )

    assert second["cycle_id"] == first["cycle_id"]
    assert second["phase"] == VCAPRuntimePhase.AWAITING_RECONCILIATION.value
    assert execute_calls == ["attempt"]


def test_unhealthy_accounting_stops_cycle_before_market_or_planning(
    tmp_path,
    monkeypatch,
):
    lifecycle, runtime, ledger, supervisor = stores(tmp_path)
    g = governor(tmp_path)
    calls = {"market": 0, "plan": 0, "execute": 0}

    async def held_health(*args, **kwargs):
        decision = healthy(kwargs["now"])
        return AccountingHealthDecision(
            **{
                **decision.__dict__,
                "state": AccountingHealthState.HOLD,
                "reasons": ("executor_accounting_requires_reconciliation",),
            }
        )

    async def fake_market(*args, **kwargs):
        calls["market"] += 1
        return fingerprint(kwargs["now"]), rule(), D("0.5")

    def fake_plan(**kwargs):
        calls["plan"] += 1
        return tranche_decision(kwargs["state"])

    async def fake_execute(*args, **kwargs):
        calls["execute"] += 1
        return {}

    monkeypatch.setattr(runtime_tool, "_refresh_accounting", held_health)
    monkeypatch.setattr(runtime_tool, "_refresh_market", fake_market)
    monkeypatch.setattr(
        runtime_tool,
        "plan_vcap_lifecycle_from_ledger",
        fake_plan,
    )
    monkeypatch.setattr(
        runtime_tool,
        "execute_vcap_lifecycle_decision",
        fake_execute,
    )

    result = asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            object(),
            position_id="position-runtime-1",
            envelope_proposal_id="runtime-envelope-1",
            governor=g,
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            accounting_supervisor=supervisor,
            seed_quote=D("20"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("10"),
            now=2_000,
        )
    )

    assert result["phase"] == VCAPRuntimePhase.HOLD.value
    assert result["accounting_state"] == AccountingHealthState.HOLD.value
    assert calls == {"market": 0, "plan": 0, "execute": 0}
