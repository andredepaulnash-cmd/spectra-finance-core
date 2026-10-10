import asyncio
import time
from decimal import Decimal

from mcp_servers.hummingbot_api.tools import execution_book, executor_create
from spectra_core import (
    CapitalGovernor,
    CapitalPolicy,
    DecisionStatus,
    EconomicPositionLedger,
)
from spectra_core.kraken_authorization import reserve_kraken_capital

D = Decimal


class Portfolio:
    async def get_state(
        self,
        *,
        account_names,
        connector_names,
        refresh,
    ):
        assert account_names == ["master_account"]
        assert connector_names == ["kraken"]
        assert refresh is True

        return {
            "master_account": {
                "kraken": [
                    {
                        "token": "USD",
                        "units": 500,
                        "available_units": 500,
                        "value": 500,
                    }
                ]
            }
        }


class Connectors:
    def __init__(self):
        self.calls = []

    async def get_trading_rules(
        self,
        *,
        connector_name,
        trading_pairs,
    ):
        self.calls.append(
            {
                "connector_name": connector_name,
                "trading_pairs": trading_pairs,
            }
        )

        return {
            "BTC-USD": {
                "min_order_size": "0.00001",
                "min_notional_size": "1",
                "min_base_amount_increment": "0.00001",
                "min_price_increment": "0.01",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
            }
        }


class Executors:
    def __init__(self, governor, proposal_id):
        self.governor = governor
        self.proposal_id = proposal_id
        self.calls = []

    async def create_executor(
        self,
        executor_config,
        account_name,
        controller_id,
    ):
        # The irreversible boundary:
        # capital must already be durably COMMITTED
        # before Hummingbot receives the mutation.
        decision = self.governor.get_decision(self.proposal_id)

        assert decision is not None
        assert decision.status == DecisionStatus.COMMITTED

        self.calls.append(
            {
                "executor_config": executor_config,
                "account_name": account_name,
                "controller_id": controller_id,
            }
        )

        return {
            "executor_id": executor_config["id"],
        }


class Client:
    def __init__(self):
        self.portfolio = Portfolio()
        self.connectors = Connectors()
        self.executors = None


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "spectra-e2e.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("100"),
            max_total_committed_quote=D("400"),
            max_pair_quote=D("150"),
            max_position_quote=D("150"),
            max_recovery_quote=D("100"),
            max_leverage=D("1"),
        ),
    )


def test_kraken_snapshot_to_authorization_to_hummingbot(monkeypatch, tmp_path):
    g = governor(tmp_path)
    client = Client()
    ledger = EconomicPositionLedger(tmp_path / "economic-e2e.sqlite3")
    ledger.open_position(position_id="e2e-entry", pair="BTC-USD", created_at=1000)
    monkeypatch.setattr(executor_create, "get_economic_position_ledger", lambda: ledger)

    async def fresh(rule):
        observed = time.time()
        return execution_book.FreshVenueBook(
            "BTC-USD",
            "XBTUSD",
            D("93"),
            D("94"),
            ((D("93"), D("100")),),
            ((D("94"), D("100")),),
            observed,
            observed,
            "test-fresh-Kraken-Depth",
            "a" * 64,
            venue_min_order_size=D("0.00001"),
            venue_min_cost_quote=D("1"),
            venue_base_asset="BTC",
            venue_quote_asset="USD",
            venue_rules_observed_at=observed,
            venue_rules_sha256="b" * 64,
        )

    monkeypatch.setattr(execution_book, "fetch_protected_kraken_book", fresh)

    # Prevent any cached answer from another test from affecting
    # this integration path.
    executor_create.trading_rules_cache.clear()

    authorization = asyncio.run(
        reserve_kraken_capital(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            intent="ENTRY",
            amount_quote="50",
            governor=g,
        )
    )

    proposal = authorization.proposal

    assert authorization.decision.status == DecisionStatus.RESERVED
    assert proposal.amount_quote == D("50")

    client.executors = Executors(
        governor=g,
        proposal_id=proposal.proposal_id,
    )

    monkeypatch.setattr(
        executor_create.settings,
        "agent_slug",
        "spectra",
    )
    monkeypatch.setattr(
        executor_create,
        "get_breaker_mode",
        lambda: executor_create.BreakerMode.NORMAL,
    )
    monkeypatch.setattr(
        executor_create,
        "get_capital_governor",
        lambda: g,
    )

    result = asyncio.run(
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
            account_name="master_account",
            spectra_proposal_id=proposal.proposal_id,
            spectra_intent="ENTRY",
            spectra_position_id="e2e-entry",
            max_open_orders=1,
            max_orders_per_batch=1,
            min_order_amount_quote=50,
            spectra_max_price_slippage_bps=0,
            spectra_fee_allowance_bps=0,
        )
    )

    assert result["executor_id"] == client.executors.calls[0]["executor_config"]["id"]

    assert len(client.connectors.calls) == 1
    assert client.connectors.calls[0] == {
        "connector_name": "kraken",
        "trading_pairs": ["BTC-USD"],
    }

    assert len(client.executors.calls) == 1

    call = client.executors.calls[0]

    assert call["account_name"] == "master_account"
    assert call["executor_config"]["connector_name"] == "kraken"
    assert call["executor_config"]["trading_pair"] == "BTC-USD"
    assert call["executor_config"]["type"] == "order_executor"
    assert call["executor_config"]["execution_strategy"] == "LIMIT"
    assert D(call["executor_config"]["price"]) == D("94")
    assert D(call["executor_config"]["amount"]) * D("94") <= D("50")
    assert D(
        g.get_protected_buy_submission(proposal.proposal_id)["authorized_quote_budget"]
    ) == D("50")

    final_decision = g.get_decision(proposal.proposal_id)

    assert final_decision is not None
    assert final_decision.status == DecisionStatus.RECONCILED
    assert (
        g.get_execution_receipt(proposal.proposal_id) == call["executor_config"]["id"]
    )

    provenance = g.get_execution_provenance(proposal.proposal_id)
    assert provenance is not None
    assert provenance.executor_id == call["executor_config"]["id"]
    assert provenance.account_name == "master_account"
    assert provenance.connector_name == "kraken"
    assert provenance.controller_id == "spectra"
