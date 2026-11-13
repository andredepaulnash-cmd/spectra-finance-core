import asyncio
import base64
import json
import time
from decimal import Decimal
from pathlib import Path

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
        self.order_amount = "0.5"
        self.order_price = "94"

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
                            "amount": self.order_amount,
                            "price": self.order_price,
                            "status": "FILLED",
                            "filled_amount": self.order_amount,
                            "average_fill_price": self.order_price,
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
                            "amount": self.order_amount,
                            "price": self.order_price,
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
        self.session.order_amount = str(executor_config["amount"])
        self.session.order_price = str(executor_config["price"])
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
        "SPECTRA_APPROVED_ASSETS_FILE",
        str(Path(__file__).parent / "fixtures" / "approved_assets.json"),
    )
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
            position_id="e2e-entry",
        )
    )

    proposal = authorization.proposal

    assert authorization.decision.status.value == "RESERVED"
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
            spectra_fee_allowance_bps=10,
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
    assert ledger.snapshot("e2e-entry").active_base == D(call["executor_config"]["amount"])
    assert ledger.snapshot("e2e-entry").active_cost_basis_quote == D(call["executor_config"]["amount"]) * D("94")
    assert ledger.snapshot("e2e-entry").active_entry_fees_quote == D("0.01")

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


@pytest.mark.asyncio
async def test_live_api_paper_execution_reconciles_confirmed_fill(
    monkeypatch,
    tmp_path,
):
    """CI-only real API smoke: Core signs, Hummingbot paper-fills, Core reconciles."""
    import json
    import os
    import time
    from uuid import uuid4

    if os.getenv("SPECTRA_CI_PAPER_EXECUTION") != "1":
        pytest.skip("requires the isolated CI Kraken paper API container")

    from hummingbot_api_client import HummingbotAPIClient
    from spectra_core.accounting_health import AccountingHealthState
    from spectra_core.kraken_authorization import reserve_kraken_capital
    from spectra_core.runtime import (
        get_accounting_health_supervisor,
        get_capital_governor,
        get_economic_position_ledger,
        reset_runtime_cache,
    )
    from mcp_servers.hummingbot_api.tools.accounting_health import (
        synchronize_position_accounting_health,
    )
    from mcp_servers.hummingbot_api.tools.executor_create import create_grid_executor

    paper_state = tmp_path / "paper-state"
    live_state = tmp_path / "live-state"
    paper_state.mkdir()
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(paper_state))
    monkeypatch.setenv("SPECTRA_STATE_DIR", str(live_state))
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    (paper_state / "capital_policy.json").write_text(
        json.dumps(
            {
                "reserve_floor_quote": "100",
                # The isolated paper connector starts with a simulated portfolio,
                # not just free USD. The Governor correctly counts all non-quote
                # holdings toward the global envelope; use the project paper cap
                # here while keeping the per-pair and per-position test caps at $50.
                "max_total_committed_quote": "25000",
                "max_pair_quote": "50",
                "max_position_quote": "50",
                "max_recovery_quote": "25",
                "max_leverage": "1",
                "reservation_ttl_seconds": 180,
                "position_hold_snapshot_ttl_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (paper_state / "breaker_state.json").write_text(
        json.dumps({"mode": "NORMAL"}),
        encoding="utf-8",
    )
    (paper_state / "approved_assets.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "isolated CI paper execution fixture",
                "connector": "kraken_paper_trade",
                "approved_pairs": ["BTC-USD"],
            }
        ),
        encoding="utf-8",
    )
    from spectra_core.runtime import spectra_state_dir

    assert spectra_state_dir().resolve() == paper_state.resolve()
    assert "max_total_committed_quote" in json.loads(
        (paper_state / "capital_policy.json").read_text(encoding="utf-8")
    )
    reset_runtime_cache()

    client = HummingbotAPIClient(
        base_url=os.environ["HUMMINGBOT_API_URL"],
        username=os.environ["HUMMINGBOT_USERNAME"],
        password=os.environ["HUMMINGBOT_PASSWORD"],
    )
    try:
        await client.init()
        assert await client.accounts.list_accounts()

        governor = get_capital_governor()
        ledger = get_economic_position_ledger()
        supervisor = get_accounting_health_supervisor()
        position_id = "ci-paper-" + uuid4().hex
        ledger.open_position(
            position_id=position_id,
            pair="BTC-USD",
            created_at=time.time(),
        )

        authorization = await reserve_kraken_capital(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            intent="ENTRY",
            amount_quote="25",
            governor=governor,
            position_id=position_id,
            refresh=True,
        )
        assert authorization.decision.status == DecisionStatus.RESERVED, (
            "paper capital reservation rejected: "
            f"reason={authorization.decision.reason!r}; "
            f"free_quote={authorization.snapshot.free_quote}; "
            f"committed_quote={authorization.snapshot.committed_quote}; "
            f"pair_exposure_quote={authorization.snapshot.pair_exposure_quote}; "
            f"position_exposure_quote={authorization.snapshot.position_exposure_quote}; "
            f"recovery_exposure_quote={authorization.snapshot.recovery_exposure_quote}"
        )

        result = await create_grid_executor(
            client,
            connector_name="kraken_paper_trade",
            trading_pair="BTC-USD",
            side=1,
            start_price=90,
            end_price=100,
            limit_price=80,
            total_amount_quote=25,
            leverage=1,
            account_name="master_account",
            controller_id="spectra-ci",
            spectra_proposal_id=authorization.proposal.proposal_id,
            spectra_intent="ENTRY",
            spectra_position_id=position_id,
            max_open_orders=1,
            max_orders_per_batch=1,
            min_order_amount_quote=25,
            spectra_max_price_slippage_bps=10,
            spectra_fee_allowance_bps=60,
        )
        assert not result.get("error"), result
        executor_id = result.get("executor_id")
        assert executor_id, result

        last_health = None
        for _ in range(30):
            last_health = await synchronize_position_accounting_health(
                client,
                position_id=position_id,
                proposal_ids=(authorization.proposal.proposal_id,),
                governor=governor,
                ledger=ledger,
                supervisor=supervisor,
                now=time.time(),
            )
            snapshot = ledger.snapshot(position_id)
            if (
                last_health.state == AccountingHealthState.HEALTHY
                and last_health.fully_reconciled
                and last_health.confirmed_fills >= 1
                and last_health.ledgered_fills == last_health.confirmed_fills
                and snapshot.active_base > 0
            ):
                # Prove the paper ledger and capital receipt survive a genuine
                # Core runtime-cache restart, then replay the same venue evidence.
                # The replay must be idempotent: no duplicate inventory, basis,
                # fees, or fill rows may be created.
                before_restart = ledger.snapshot(position_id)
                proposal_id = authorization.proposal.proposal_id
                assert governor.get_execution_receipt(proposal_id) == executor_id

                reset_runtime_cache()
                restored_governor = get_capital_governor()
                restored_ledger = get_economic_position_ledger()
                restored_supervisor = get_accounting_health_supervisor()
                after_restart = restored_ledger.snapshot(position_id)

                assert after_restart.active_base == before_restart.active_base
                assert after_restart.active_cost_basis_quote == before_restart.active_cost_basis_quote
                assert after_restart.active_entry_fees_quote == before_restart.active_entry_fees_quote
                assert restored_governor.get_execution_receipt(proposal_id) == executor_id
                restored_decision = restored_governor.get_decision(proposal_id)
                assert restored_decision is not None
                assert restored_decision.status == DecisionStatus.RECONCILED

                replay_health = await synchronize_position_accounting_health(
                    client,
                    position_id=position_id,
                    proposal_ids=(proposal_id,),
                    governor=restored_governor,
                    ledger=restored_ledger,
                    supervisor=restored_supervisor,
                    now=time.time(),
                )
                after_replay = restored_ledger.snapshot(position_id)
                assert replay_health.state == AccountingHealthState.HEALTHY
                assert replay_health.fully_reconciled
                assert replay_health.confirmed_fills == last_health.confirmed_fills
                assert replay_health.ledgered_fills == last_health.ledgered_fills
                assert after_replay.active_base == before_restart.active_base
                assert after_replay.active_cost_basis_quote == before_restart.active_cost_basis_quote
                assert after_replay.active_entry_fees_quote == before_restart.active_entry_fees_quote

                print(
                    "PASS: Core-authorized Kraken paper execution produced a confirmed fill; "
                    "accounting survived restart and duplicate reconciliation was idempotent "
                    f"(executor={executor_id}, fills={replay_health.ledgered_fills}, "
                    f"base={after_replay.active_base})."
                )
                return
            await asyncio.sleep(2)

        raise AssertionError(
            "No fully reconciled confirmed paper fill within 60 seconds: "
            f"executor={executor_id}, state={getattr(last_health, 'state', None)}, "
            f"confirmed={getattr(last_health, 'confirmed_fills', 0)}, "
            f"ledgered={getattr(last_health, 'ledgered_fills', 0)}"
        )
    finally:
        await client.close()
