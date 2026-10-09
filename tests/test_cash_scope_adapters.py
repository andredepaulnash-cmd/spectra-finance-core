from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools import executor_create
from mcp_servers.hummingbot_api.tools.vcap_execution import execute_vcap_decision
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.kraken_authorization import reserve_kraken_capital
from spectra_core.models import (
    BreakerMode,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.vcap_execution import VCAPExecutionDecision, VCAPLoopAction
from spectra_core.vcap_tranches import VCAPTranche

D = Decimal


class Portfolio:
    async def get_state(self, *, account_names, connector_names, refresh):
        assert connector_names == ["kraken"]
        return {
            account_names[0]: {
                "kraken": [
                    {
                        "token": "USD",
                        "units": "500",
                        "available_units": "500",
                        "value": "500",
                    }
                ]
            }
        }


class Connectors:
    async def get_trading_rules(self, *, connector_name, trading_pairs):
        assert connector_name == "kraken"
        return {
            pair: {"min_order_size": "0.001", "min_notional_size": "1"}
            for pair in trading_pairs
        }


class Executors:
    def __init__(self):
        self.calls = []

    async def create_executor(self, executor_config, account_name, controller_id):
        self.calls.append((executor_config, account_name, controller_id))
        return {"executor_id": "scoped-executor-1"}


class Client:
    def __init__(self):
        self.portfolio = Portfolio()
        self.connectors = Connectors()
        self.executors = Executors()


@pytest.fixture
def activated_governor(tmp_path):
    governor = CapitalGovernor(
        tmp_path / "scoped-cash.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("20"),
            max_total_committed_quote=D("500"),
            max_pair_quote=D("200"),
            max_position_quote=D("200"),
            max_recovery_quote=D("200"),
            max_leverage=D("1"),
        ),
    )
    assert governor.activate_cash_authority(
        account_name="funded_account",
        connector_name="kraken",
        quote_asset="USD",
        opening_productive_quote=D("100"),
        opening_parked_quote=D("400"),
        activation_id="reconciled-opening-balance",
        activated_at=1_000,
    )
    return governor


def configure_executor_gate(monkeypatch, governor):
    executor_create.trading_rules_cache.clear()
    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")
    monkeypatch.setattr(executor_create, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: governor)


def authorize(client, governor, account="funded_account", amount="50"):
    return asyncio.run(
        reserve_kraken_capital(
            client,
            account_name=account,
            trading_pair="BTC-USD",
            intent="ENTRY",
            amount_quote=amount,
            governor=governor,
        )
    )


def create_grid(client, proposal_id, account):
    return asyncio.run(
        executor_create.create_grid_executor(
            client,
            connector_name="kraken",
            trading_pair="BTC-USD",
            side=1,
            start_price=90,
            end_price=100,
            limit_price=80,
            total_amount_quote=50,
            leverage=1,
            account_name=account,
            spectra_proposal_id=proposal_id,
            spectra_intent="ENTRY",
        )
    )


def test_actual_kraken_account_reaches_snapshot_and_authorization(activated_governor):
    authorization = authorize(Client(), activated_governor)
    assert authorization.decision.status == DecisionStatus.RESERVED
    assert authorization.snapshot.account_name == "funded_account"
    assert authorization.proposal.account_name == "funded_account"
    assert (
        authorization.snapshot.connector_name
        == authorization.proposal.connector_name
        == "kraken"
    )
    assert (
        authorization.snapshot.quote_asset
        == authorization.proposal.quote_asset
        == "USD"
    )


def test_other_real_account_balance_cannot_authorize_bound_cash(activated_governor):
    authorization = authorize(Client(), activated_governor, "another_account")
    assert authorization.decision.status == DecisionStatus.REJECTED
    assert authorization.snapshot.account_name == "another_account"
    assert authorization.proposal.account_name == "another_account"
    assert activated_governor.cash_authority_snapshot().productive_quote == D("100")


def test_account_swap_between_reserve_and_create_never_reaches_backend(
    monkeypatch, activated_governor
):
    client = Client()
    authorization = authorize(client, activated_governor)
    configure_executor_gate(monkeypatch, activated_governor)
    result = create_grid(client, authorization.proposal.proposal_id, "another_account")
    assert "error" in result
    assert not client.executors.calls
    assert (
        activated_governor.get_decision(authorization.proposal.proposal_id).status
        == DecisionStatus.RESERVED
    )
    assert activated_governor.cash_authority_snapshot().productive_quote == D("100")


def test_matching_account_dispatch_uses_productive_cash_and_preserves_park(
    monkeypatch, activated_governor
):
    client = Client()
    authorization = authorize(client, activated_governor)
    configure_executor_gate(monkeypatch, activated_governor)
    result = create_grid(client, authorization.proposal.proposal_id, "funded_account")
    assert result["executor_id"] == "scoped-executor-1"
    assert len(client.executors.calls) == 1
    state = activated_governor.cash_authority_snapshot()
    assert state.productive_quote == D("50")
    assert state.parked_quote == D("400")


def recovery_decision(governor):
    envelope = CapitalProposal(
        proposal_id="scoped-envelope",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("80"),
        account_name="funded_account",
        connector_name="kraken",
        quote_asset="USD",
    )
    snapshot = CapitalSnapshot(
        free_quote=D("500"),
        account_name="funded_account",
        connector_name="kraken",
        quote_asset="USD",
    )
    assert governor.reserve(envelope, snapshot).status == DecisionStatus.RESERVED
    return VCAPExecutionDecision(
        envelope_proposal_id=envelope.proposal_id,
        sequence=1,
        action=VCAPLoopAction.EXECUTE,
        remaining_authorized_quote=D("80"),
        current_price=D("100"),
        recovery_plan=None,
        tranche_plan=None,
        next_tranche=VCAPTranche(
            sequence=1,
            quote_amount=D("20"),
            base_amount=D("0.2"),
            weight_ratio=D("0.25"),
        ),
        next_proposal=CapitalProposal(
            proposal_id="scoped-envelope:vcap:0001",
            pair="BTC-USD",
            intent=IntentKind.RECOVERY,
            amount_quote=D("20"),
        ),
        reasons=("one-recovery-tranche",),
    )


def test_vcap_wrong_actual_caller_cannot_carve_correct_account_envelope(
    activated_governor,
):
    client = Client()
    decision = recovery_decision(activated_governor)
    result = asyncio.run(
        execute_vcap_decision(
            client,
            decision=decision,
            governor=activated_governor,
            account_name="another_account",
        )
    )
    assert not result["executed"]
    assert not client.executors.calls
    assert activated_governor.get_decision("scoped-envelope").reserved_quote == D("80")


def test_vcap_actual_scope_survives_carve_commit_and_restart_replay(
    monkeypatch, activated_governor
):
    client = Client()
    decision = recovery_decision(activated_governor)
    configure_executor_gate(monkeypatch, activated_governor)
    first = asyncio.run(
        execute_vcap_decision(
            client,
            decision=decision,
            governor=activated_governor,
            account_name="funded_account",
        )
    )
    assert first["executed"]
    restarted = CapitalGovernor(activated_governor.db_path, activated_governor.policy)
    replay = asyncio.run(
        execute_vcap_decision(
            client,
            decision=decision,
            governor=restarted,
            account_name="funded_account",
        )
    )
    assert replay["idempotent_replay"]
    assert len(client.executors.calls) == 1
    with pytest.raises(RuntimeError, match="provenance scope changed"):
        asyncio.run(
            execute_vcap_decision(
                client,
                decision=decision,
                governor=restarted,
                account_name="another_account",
            )
        )
    state = restarted.cash_authority_snapshot()
    assert state.productive_quote == D("80")
    assert state.parked_quote == D("400")
