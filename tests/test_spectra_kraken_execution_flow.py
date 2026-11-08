import asyncio
import base64
import json
import time
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools import execution_book, executor_create
from spectra_core import (
    CapitalGovernor,
    CapitalPolicy,
    DecisionStatus,
    EconomicPositionLedger,
)
from spectra_core import kraken_authorization
from spectra_core.kraken_authorization import reserve_kraken_capital
from spectra_core.accounting_health import AccountingHealthState, AccountingHealthSupervisor
from mcp_servers.hummingbot_api.tools.accounting_health import synchronize_position_accounting_health

D = Decimal


class Portfolio:
    def __init__(self, connector_name):
        self.connector_name = connector_name

    async def get_state(
        self,
        *,
        account_names,
        connector_names,
        refresh,
    ):
        assert account_names == ["master_account"]
        assert connector_names == [self.connector_name]
        assert refresh is True

        return {
            "master_account": {
                self.connector_name: [
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


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.executor_id = "spectra-e2e-1"

    async def get(self, url):
        if url.endswith("/orders"):
            return FakeResponse(
                {
                    "executor_id": self.executor_id,
                    "count": 1,
                    "orders": [
                        {
                            "executor_id": self.executor_id,
                            "client_order_id": "kraken-paper-buy-1",
                            "order_type": "LIMIT",
                            "trade_type": "BUY",
                            "amount": "0.5",
                            "price": "94",
                            "status": "FILLED",
                            "filled_amount": "0.5",
                            "average_fill_price": "94",
                            "created_at": "2026-11-08T18:00:00+00:00",
                            "updated_at": "2026-11-08T18:00:05+00:00",
                        }
                    ],
                }
            )
        if url.endswith("/trades"):
            return FakeResponse(
                {
                    "executor_id": self.executor_id,
                    "count": 1,
                    "trades": [
                        {
                            "executor_id": self.executor_id,
                            "order_id": "kraken-paper-buy-1",
                            "trade_id": "kraken-paper-fill-1",
                            "account_name": "master_account",
                            "connector_name": "kraken_paper_trade",
                            "trading_pair": "BTC-USD",
                            "trade_type": "BUY",
                            "amount": "0.5",
                            "price": "94",
                            "fee_paid": "0.01",
                            "fee_currency": "USD",
                            "timestamp": "2026-11-08T18:00:05+00:00",
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected provenance URL: {url}")


class Executors:
    def __init__(self, governor, proposal_id, session):
        self.governor = governor
        self.proposal_id = proposal_id
        self.session = session
        self.base_url = "http://hummingbot.test"
        self.calls = []
        self.receipt_token = None

    async def create_executor(
        self,
        executor_config,
        account_name,
        controller_id,
    ):
        # The irreversible boundary:
        # capital must already be durably COMMITTED
        # before Hummingbot receives the mutation.
        self.receipt_token = self.session.headers.get("X-Spectra-Dispatch-Receipt")
        assert self.receipt_token
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
            "executor_id": "spectra-e2e-1",
        }


class Client:
    def __init__(self, connector_name):
        self.portfolio = Portfolio(connector_name)
        self.connectors = Connectors()
        self.executors = None
        self._session = FakeSession()


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


@pytest.mark.parametrize(
    "paper_mode,connector_name",
    [
        (True, "kraken_paper_trade"),
    ],
)
def test_kraken_snapshot_to_authorization_to_hummingbot(
    monkeypatch, tmp_path, paper_mode, connector_name
):
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv(
        "SPECTRA_DISPATCH_PRIVATE_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    monkeypatch.setattr(executor_create.settings, "paper_only", paper_mode)
    g = governor(tmp_path)
    client = Client(connector_name)
    ledger = EconomicPositionLedger(tmp_path / "economic-e2e.sqlite3")
    ledger.open_position(position_id="e2e-entry", pair="BTC-USD", created_at=1000)
    health = AccountingHealthSupervisor(ledger.path)
    monkeypatch.setattr(executor_create, "get_economic_position_ledger", lambda: ledger)
    monkeypatch.setattr(kraken_authorization, "get_economic_position_ledger", lambda: ledger)
    monkeypatch.setattr(kraken_authorization, "get_accounting_health_supervisor", lambda: health)

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
        session=client._session,
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
            connector_name=connector_name,
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

    assert result.get("executor_id") == "spectra-e2e-1", result

    assert len(client.connectors.calls) == 1
    assert client.connectors.calls[0] == {
        "connector_name": connector_name,
        "trading_pairs": ["BTC-USD"],
    }

    assert len(client.executors.calls) == 1

    call = client.executors.calls[0]

    assert call["account_name"] == "master_account"
    assert call["executor_config"]["connector_name"] == connector_name
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
    assert g.get_execution_receipt(proposal.proposal_id) == "spectra-e2e-1"

    provenance = g.get_execution_provenance(proposal.proposal_id)
    assert provenance is not None
    assert provenance.account_name == "master_account"
    assert provenance.connector_name == connector_name
    assert provenance.controller_id == "spectra"

    # Reconcile the exact executor's authoritative order + confirmed fill
    # evidence back into the Core economic ledger after the API receipt.
    accounting = asyncio.run(
        synchronize_position_accounting_health(
            client,
            position_id="e2e-entry",
            proposal_ids=(proposal.proposal_id,),
            governor=g,
            ledger=ledger,
            supervisor=health,
            now=2_000,
        )
    )

    assert accounting.state == AccountingHealthState.HEALTHY
    assert accounting.fully_reconciled
    assert accounting.ledgered_fills == 1
    assert ledger.snapshot("e2e-entry").active_base == D("0.5")
    assert ledger.snapshot("e2e-entry").cost_basis_quote == D("47.01")

    # The exact same position becomes stale after its health watermark expires.
    # A second buy-side authorization must fail before any new capital reservation.
    monkeypatch.setattr(kraken_authorization.time, "time", lambda: 2_031)
    with pytest.raises(ValueError, match="accounting health gate blocks new exposure"):
        asyncio.run(
            reserve_kraken_capital(
                client,
                account_name="master_account",
                trading_pair="BTC-USD",
                intent="ENTRY",
                amount_quote="10",
                governor=g,
            )
        )
