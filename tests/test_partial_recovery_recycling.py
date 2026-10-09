import asyncio
from decimal import Decimal

import pytest

import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.fill_reconciliation import TradeFillEvidence
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    BreakerMode,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.partial_recycle import (
    PartialRecycleAction,
    PartialRecycleStore,
    assess_partial_recycle_manifest,
    plan_partial_recycle,
)

D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "partial-recycle-governor.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("1000"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def prepare_execution(
    g,
    *,
    proposal_id,
    executor_id,
    intent,
    amount,
    pair="XRP-USD",
):
    proposal = CapitalProposal(
        proposal_id=proposal_id,
        pair=pair,
        intent=intent,
        amount_quote=D(amount),
        leverage=D("1"),
    )
    decision = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("5000"),
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


def rule(*, min_base="1", min_quote="1"):
    return KrakenTradingRule(
        trading_pair="XRP-USD",
        min_order_size=D(min_base),
        min_notional_size=D(min_quote),
        min_order_value=D("0"),
        min_price_increment=D("0.0001"),
        min_base_amount_increment=D("0.1"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def ledger(tmp_path):
    result = EconomicPositionLedger(tmp_path / "partial-recycle.sqlite3")
    result.open_position(
        position_id="xrp-position-1",
        pair="XRP-USD",
        created_at=1_000,
    )
    return result


def add_recovery(
    g,
    book,
    *,
    proposal_id,
    executor_id,
    lot_id,
    base,
    basis,
    occurred_at,
):
    prepare_execution(
        g,
        proposal_id=proposal_id,
        executor_id=executor_id,
        intent=IntentKind.RECOVERY,
        amount=basis,
    )
    book.record_vcap_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id=lot_id,
        proposal_id=proposal_id,
        base_amount=D(base),
        cost_basis_quote=D(basis),
        occurred_at=occurred_at,
    )


def test_one_active_position_per_normalized_base_asset_and_reopen_after_close(
    tmp_path,
):
    g = governor(tmp_path)
    book = EconomicPositionLedger(tmp_path / "single-base.sqlite3")
    book.open_position(
        position_id="btc-1",
        pair="BTC-USD",
        created_at=1_000,
    )

    with pytest.raises(ValueError, match="already has ACTIVE economic position"):
        book.open_position(
            position_id="btc-2",
            pair="XBT-EUR",
            created_at=1_001,
        )

    prepare_execution(
        g,
        proposal_id="btc-entry",
        executor_id="btc-entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
        pair="BTC-USD",
    )
    book.record_entry_fill(
        governor=g,
        position_id="btc-1",
        lot_id="btc-entry-lot",
        proposal_id="btc-entry",
        base_amount=D("0.001"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    closed = book.record_partial_exit(
        event_id="btc-close",
        position_id="btc-1",
        base_sold=D("0.001"),
        gross_proceeds_quote=D("10.50"),
        occurred_at=1_020,
    )
    assert closed.position_closed is True

    book.open_position(
        position_id="btc-2",
        pair="XBT-EUR",
        created_at=1_030,
    )
    assert set(book.list_active_position_ids()) == {"btc-2"}


def test_partial_recycle_selects_profitable_recovery_only(tmp_path):
    g = governor(tmp_path)
    book = ledger(tmp_path)

    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-lot",
        proposal_id="entry",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery-profit",
        executor_id="recovery-profit-exec",
        lot_id="recovery-profit-lot",
        base="10",
        basis="4",
        occurred_at=1_020,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery-loss",
        executor_id="recovery-loss-exec",
        lot_id="recovery-loss-lot",
        base="10",
        basis="6",
        occurred_at=1_030,
    )
    book.record_strategic_retained(
        lot_id="retained",
        pair="XRP-USD",
        base_amount=D("100"),
        cost_basis_quote=D("40"),
        occurred_at=1_040,
    )
    book.record_unattributed(
        lot_id="unattributed",
        pair="XRP-USD",
        base_amount=D("50"),
        occurred_at=1_050,
    )

    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("0"),
        slippage_allowance_bps=D("0"),
        now=2_000,
    )

    assert decision.action == PartialRecycleAction.RECYCLE
    assert decision.manifest is not None
    assert decision.manifest.proposal_ids == ("recovery-profit",)
    assert decision.manifest.lot_ids == ("recovery-profit-lot",)
    assert "entry-lot" not in decision.manifest.lot_ids
    assert "retained" not in decision.manifest.lot_ids
    assert "unattributed" not in decision.manifest.lot_ids


def test_subminimum_profitable_chunks_are_consolidated_for_repair(tmp_path):
    g = governor(tmp_path)
    book = ledger(tmp_path)
    add_recovery(
        g,
        book,
        proposal_id="recovery-a",
        executor_id="recovery-a-exec",
        lot_id="recovery-a-lot",
        base="6",
        basis="2",
        occurred_at=1_010,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery-b",
        executor_id="recovery-b-exec",
        lot_id="recovery-b-lot",
        base="6",
        basis="2",
        occurred_at=1_020,
    )

    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(min_base="1", min_quote="5"),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("0"),
        slippage_allowance_bps=D("0"),
        now=2_000,
    )

    assert decision.action == PartialRecycleAction.RECYCLE
    assert decision.manifest is not None
    assert decision.manifest.target_base == D("12")
    assert set(decision.manifest.lot_ids) == {
        "recovery-a-lot",
        "recovery-b-lot",
    }
    assert set(decision.repair_lot_ids) == {
        "recovery-a-lot",
        "recovery-b-lot",
    }


class FakeExecutors:
    def __init__(self):
        self.created = []
        self.recycle_executor_id = None

    async def get_executor(self, executor_id):
        if executor_id == self.recycle_executor_id:
            return {
                "id": executor_id,
                "executor_type": "order_executor",
                "status": "RUNNING",
                "close_type": None,
                "filled_amount_quote": "0",
                "cum_fees_quote": "0",
                "net_pnl_quote": "0",
                "custom_info": {"realized_pnl_quote": "0"},
            }
        if executor_id == "recovery-profit-exec":
            return {
                "id": executor_id,
                "executor_type": "dca_executor",
                "status": "TERMINATED",
                "close_type": "POSITION_HOLD",
                "filled_amount_quote": "4",
                "cum_fees_quote": "0",
                "position_size_quote": "4",
                "net_pnl_quote": "0",
                "custom_info": {"realized_pnl_quote": "0"},
            }
        error = RuntimeError("executor not found")
        error.status = 404
        raise error

    async def create_executor(
        self,
        *,
        executor_config,
        account_name,
        controller_id,
    ):
        self.created.append((executor_config, account_name, controller_id))
        self.recycle_executor_id = executor_config["id"]
        return {
            "executor_id": executor_config["id"],
            "status": "RUNNING",
        }


class FakeClient:
    def __init__(self):
        self.executors = FakeExecutors()


def test_confirmed_partial_recycle_realizes_profit_releases_basis_and_keeps_position(
    tmp_path,
    monkeypatch,
):
    g = governor(tmp_path)
    book = ledger(tmp_path)

    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-lot",
        proposal_id="entry",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery-profit",
        executor_id="recovery-profit-exec",
        lot_id="recovery-profit-lot",
        base="10",
        basis="4",
        occurred_at=1_020,
    )

    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("0"),
        slippage_allowance_bps=D("0"),
        now=2_000,
    )
    manifest = decision.manifest
    assert manifest is not None
    assert g.current_recovery_exposure_quote() == D("4")

    order = ExecutorOrderEvidence(
        executor_id=manifest.sell_executor_id,
        order_id="recycle-sell-order",
        order_type="LIMIT",
        side="SELL",
        amount=D("10"),
        price=manifest.protected_limit_price,
        status="FILLED",
        filled_amount=D("10"),
        average_fill_price=D("0.50"),
        created_at=2_001,
        updated_at=2_002,
    )
    trade = TradeFillEvidence(
        trade_id="recycle-trade-1",
        order_id="recycle-sell-order",
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        side="SELL",
        amount_base=D("10"),
        price=D("0.50"),
        fee_paid=D("0.10"),
        fee_currency="USD",
        occurred_at=2_002,
    )

    async def fake_orders(client, *, executor_id):
        assert executor_id == manifest.sell_executor_id
        return ExecutorOrderSnapshot(
            executor_id=executor_id,
            orders=(order,),
        )

    async def fake_trades(client, *, executor_id):
        assert executor_id == manifest.sell_executor_id
        return ExecutorTradeSnapshot(
            executor_id=executor_id,
            trades=(trade,),
        )

    monkeypatch.setattr(
        partial_tool,
        "fetch_executor_order_snapshot",
        fake_orders,
    )
    monkeypatch.setattr(
        partial_tool,
        "fetch_executor_trade_snapshot",
        fake_trades,
    )
    monkeypatch.setattr(
        partial_tool,
        "get_breaker_mode",
        lambda: BreakerMode.NORMAL,
    )

    client = FakeClient()
    result = asyncio.run(
        partial_tool.execute_partial_recycle(
            client,
            decision=decision,
            governor=g,
            ledger=book,
            trading_rule=rule(),
            current_price=D("0.50"),
            now=2_010,
        )
    )

    assert result["executed"] is True
    assert result["result"]["completed"] is True
    assert result["result"]["realized_net_pnl_quote"] == "0.90"
    assert len(client.executors.created) == 1
    assert book.get_lot("recovery-profit-lot").base_remaining == D("0")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    snapshot = book.snapshot("xrp-position-1")
    assert snapshot.status.value == "ACTIVE"
    assert snapshot.active_base == D("20")
    assert g.current_recovery_exposure_quote() == D("0")

    store = PartialRecycleStore(book.path)
    progress = assess_partial_recycle_manifest(
        manifest,
        ledger=book,
        governor=g,
        store=store,
        now=2_020,
    )
    assert progress.completed is True

    # Exact replay cannot sell the recovery lot twice or release capital twice.
    replay = asyncio.run(
        partial_tool.synchronize_partial_recycle_executor(
            client,
            manifest=manifest,
            governor=g,
            ledger=book,
            store=store,
            now=2_030,
        )
    )
    assert replay["completed"] is True
    assert g.current_recovery_exposure_quote() == D("0")
