import asyncio
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.accounting_health import (
    synchronize_all_active_position_accounting,
    synchronize_position_accounting_health,
)
from mcp_servers.hummingbot_api.tools.vcap_lifecycle import (
    execute_vcap_lifecycle_decision,
)
from spectra_core.accounting_health import (
    AccountingHealthPolicy,
    AccountingHealthState,
    AccountingHealthSupervisor,
)
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import ExecutorLedgerSyncReport
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    VCAPLifecycleDecision,
    plan_vcap_lifecycle_from_ledger,
)
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "health-governor.sqlite3",
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


def prepare_execution(
    g,
    *,
    proposal_id="entry-health-1",
    executor_id="exec-health-1",
):
    proposal = CapitalProposal(
        proposal_id=proposal_id,
        pair="XRP-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("20"),
        leverage=D("1"),
    )
    decision = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert decision.status == DecisionStatus.RESERVED
    assert g.commit(proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal_id,
        executor_id=executor_id,
        account_name="master_account",
        connector_name="kraken",
        controller_id="spectra",
    )
    return proposal


def ledger(tmp_path):
    book = EconomicPositionLedger(tmp_path / "health-economic.sqlite3")
    book.open_position(
        position_id="position-health-1",
        pair="XRP-USD",
        created_at=1_000,
    )
    return book


def order_payload(*, executor_id="exec-health-1"):
    return {
        "executor_id": executor_id,
        "count": 1,
        "orders": [
            {
                "executor_id": executor_id,
                "client_order_id": "buy-health-1",
                "exchange_order_id": "kraken-buy-health-1",
                "order_type": "open",
                "trade_type": "BUY",
                "amount": "20",
                "price": "0.5",
                "status": "FILLED",
                "filled_amount": "20",
                "average_fill_price": "0.5",
                "created_at": "2026-09-27T19:00:00+00:00",
                "updated_at": "2026-09-27T19:00:01+00:00",
            }
        ],
    }


def trade_payload(*, executor_id="exec-health-1"):
    return {
        "executor_id": executor_id,
        "count": 1,
        "trades": [
            {
                "executor_id": executor_id,
                "order_id": "buy-health-1",
                "trade_id": "trade-health-1",
                "account_name": "master_account",
                "connector_name": "kraken",
                "trading_pair": "XRP-USD",
                "trade_type": "BUY",
                "amount": "20",
                "price": "0.5",
                "fee_paid": "0",
                "fee_currency": None,
                "timestamp": "2026-09-27T19:00:01+00:00",
            }
        ],
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(
        self,
        *,
        orders=None,
        trades=None,
        fail_orders=False,
        fail_trades=False,
    ):
        self.orders = order_payload() if orders is None else orders
        self.trades = trade_payload() if trades is None else trades
        self.fail_orders = fail_orders
        self.fail_trades = fail_trades
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        if url.endswith("/trades"):
            if self.fail_trades:
                raise OSError("trade provenance backend unavailable")
            return FakeResponse(self.trades)
        if url.endswith("/orders"):
            if self.fail_orders:
                raise OSError("order provenance backend unavailable")
            return FakeResponse(self.orders)
        raise AssertionError(f"unexpected URL: {url}")


class FakeExecutors:
    def __init__(self, session):
        self.session = session
        self.base_url = "http://hummingbot.test"


class FakeClient:
    def __init__(self, session):
        self.executors = FakeExecutors(session)


def report(*, fully=True):
    return ExecutorLedgerSyncReport(
        proposal_id="entry-health-1",
        executor_id="exec-health-1",
        position_id="position-health-1",
        known_orders=1,
        bound_orders=1 if fully else 0,
        confirmed_fills=1,
        ledgered_fills=1 if fully else 0,
        applied_fills=1 if fully else 0,
        replayed_fills=0,
        unresolved_order_ids=() if fully else ("buy-health-1",),
        unresolved_trade_ids=() if fully else ("trade-health-1",),
        fully_reconciled=fully,
        reasons=(
            ("executor_accounting_fully_reconciled",)
            if fully
            else ("executor_accounting_requires_reconciliation",)
        ),
    )


def test_health_watermark_survives_restart_and_becomes_stale(tmp_path):
    path = tmp_path / "health.sqlite3"
    supervisor = AccountingHealthSupervisor(
        path,
        policy=AccountingHealthPolicy(max_age_seconds=30),
    )
    healthy = supervisor.record_reports(
        position_id="position-health-1",
        reports=(report(),),
        now=1_000,
    )

    assert healthy.state == AccountingHealthState.HEALTHY
    assert healthy.last_success_at == 1_000
    assert healthy.allows_live_mutation(now=1_030)
    assert not healthy.allows_live_mutation(now=1_031)

    restarted = AccountingHealthSupervisor(
        path,
        policy=AccountingHealthPolicy(max_age_seconds=30),
    )
    stale = restarted.assess(
        position_id="position-health-1",
        now=1_031,
    )
    assert stale.state == AccountingHealthState.STALE
    assert stale.last_success_at == 1_000
    assert "accounting_reconciliation_stale" in stale.reasons


def test_unresolved_report_holds_and_preserves_prior_success_watermark(tmp_path):
    supervisor = AccountingHealthSupervisor(tmp_path / "health.sqlite3")
    supervisor.record_reports(
        position_id="position-health-1",
        reports=(report(),),
        now=1_000,
    )
    hold = supervisor.record_reports(
        position_id="position-health-1",
        reports=(report(fully=False),),
        now=1_010,
    )

    assert hold.state == AccountingHealthState.HOLD
    assert hold.last_success_at == 1_000
    assert hold.unresolved_order_ids == ("buy-health-1",)
    assert hold.unresolved_trade_ids == ("trade-health-1",)
    assert not hold.allows_live_mutation(now=1_010)


def test_backend_outage_records_hold_without_economic_mutation(tmp_path):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    supervisor = AccountingHealthSupervisor(book.path)

    decision = asyncio.run(
        synchronize_position_accounting_health(
            FakeClient(FakeSession(fail_trades=True)),
            position_id="position-health-1",
            proposal_ids=("entry-health-1",),
            governor=g,
            ledger=book,
            supervisor=supervisor,
            now=2_000,
        )
    )

    assert decision.state == AccountingHealthState.HOLD
    assert "unavailable" in decision.reasons[0]
    assert book.snapshot("position-health-1").active_base == D("0")


def test_malformed_authoritative_evidence_records_conflict(tmp_path):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    supervisor = AccountingHealthSupervisor(book.path)
    malformed = trade_payload(executor_id="exec-other")

    decision = asyncio.run(
        synchronize_position_accounting_health(
            FakeClient(FakeSession(trades=malformed)),
            position_id="position-health-1",
            proposal_ids=("entry-health-1",),
            governor=g,
            ledger=book,
            supervisor=supervisor,
            now=2_000,
        )
    )

    assert decision.state == AccountingHealthState.CONFLICT
    assert "conflicts" in decision.reasons[0]
    assert book.snapshot("position-health-1").active_base == D("0")


def test_active_position_sweep_reconciles_then_replays_exact_provenance(tmp_path):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    supervisor = AccountingHealthSupervisor(book.path)
    client = FakeClient(FakeSession())

    first = asyncio.run(
        synchronize_position_accounting_health(
            client,
            position_id="position-health-1",
            proposal_ids=("entry-health-1",),
            governor=g,
            ledger=book,
            supervisor=supervisor,
            now=2_000,
        )
    )
    assert first.state == AccountingHealthState.HEALTHY
    assert first.fully_reconciled
    assert book.active_proposal_ids("position-health-1") == (
        "entry-health-1",
    )

    swept = asyncio.run(
        synchronize_all_active_position_accounting(
            client,
            governor=g,
            ledger=book,
            supervisor=supervisor,
            now=2_010,
        )
    )
    second = swept["position-health-1"]
    assert second.state == AccountingHealthState.HEALTHY
    assert second.fully_reconciled
    assert second.ledgered_fills == 1
    assert book.snapshot("position-health-1").active_base == D("20")


def fingerprint(now=2_000):
    return AssetFingerprint.cold_start(
        AssetObservation(
            trading_pair="XRP-USD",
            timestamp=now,
            price=0.5,
            quote_volume_24h=1_000_000,
            spread_bps=5.0,
            depth_quote=50_000.0,
            depth_imbalance=0.0,
            microprice_edge_bps=0.0,
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


def lifecycle_state():
    return VCAPLifecycleState(
        envelope_proposal_id="envelope-health-1",
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


def test_stale_health_blocks_ledger_backed_planning_before_recovery_logic(
    tmp_path,
):
    book = ledger(tmp_path)
    book.record_base_fill(
        position_id="position-health-1",
        lot_id="legacy-base",
        pair="XRP-USD",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_500,
    )
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = lifecycle_state()
    store.save(state)

    supervisor = AccountingHealthSupervisor(
        book.path,
        policy=AccountingHealthPolicy(max_age_seconds=30),
    )
    supervisor.record_reports(
        position_id="position-health-1",
        reports=(report(),),
        now=1_900,
    )
    stale = supervisor.assess(
        position_id="position-health-1",
        now=2_000,
    )
    assert stale.state == AccountingHealthState.STALE

    decision = plan_vcap_lifecycle_from_ledger(
        position_id="position-health-1",
        ledger=book,
        state=state,
        store=store,
        fingerprint=fingerprint(),
        trading_rule=rule(),
        current_price=D("0.5"),
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
        now=2_000,
        accounting_health=stale,
    )

    assert decision.action == VCAPLifecycleAction.HOLD
    assert decision.profit_decision is None
    assert decision.execution_decision is None
    assert "accounting_health_gate_blocked" in decision.reasons
    assert "accounting_health_stale" in decision.reasons


def test_dispatcher_rechecks_health_and_blocks_external_mutation(tmp_path):
    state = lifecycle_state()
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    store.save(state)
    decision = VCAPLifecycleDecision(
        action=VCAPLifecycleAction.EXIT,
        state=state,
        profit_decision=None,
        execution_decision=None,
        reasons=("synthetic_exit_for_gate_test",),
    )

    supervisor = AccountingHealthSupervisor(
        tmp_path / "dispatch-health.sqlite3",
        policy=AccountingHealthPolicy(max_age_seconds=30),
    )
    supervisor.record_reports(
        position_id="position-health-1",
        reports=(report(),),
        now=1_900,
    )
    stale = supervisor.assess(
        position_id="position-health-1",
        now=2_000,
    )

    result = asyncio.run(
        execute_vcap_lifecycle_decision(
            object(),
            decision=decision,
            governor=governor(tmp_path),
            store=store,
            now=2_000,
            position_id="position-health-1",
            accounting_health=stale,
        )
    )

    assert result["executed"] is False
    assert result["lifecycle_status"] == VCAPLifecycleStatus.HOLD.value
    assert result["reasons"][0] == "accounting_health_gate_blocked"
    assert result["reasons"][1] == "accounting_health_stale"
