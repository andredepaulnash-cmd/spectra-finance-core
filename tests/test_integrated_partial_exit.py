"""Offline composition of funded LIMIT BUY, protected partial SELL and PARK cash."""

import asyncio
import sqlite3
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest
from aiohttp import ClientResponseError

import mcp_servers.hummingbot_api.tools.partial_recycle as tool
import spectra_core.partial_recycle as partial_core
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_accounting import executor_accounting_observation_from_raw
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
    synchronize_executor_evidence,
)
from spectra_core.fill_reconciliation import LiveFillLedgerReconciler, TradeFillEvidence
from spectra_core.models import (
    BreakerMode,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.partial_recycle import (
    PartialRecycleAction,
    PartialRecycleStore,
    plan_partial_recycle,
)
from tests.test_partial_recovery_recycling import governor, ledger, rule


def funded_case(
    tmp_path,
    *,
    filled_base="10.04",
    buy_fee="0.04",
    buy_status="FILLED",
    buy_close_type="COMPLETED"
):
    g, book = governor(tmp_path), ledger(tmp_path)
    scope = dict(
        account_name="master_account", connector_name="kraken", controller_id="spectra"
    )
    entry = CapitalProposal(
        proposal_id="entry-owner",
        pair="XRP-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("1"),
    )
    assert (
        g.reserve(entry, CapitalSnapshot(free_quote=D("5000"))).status
        == DecisionStatus.RESERVED
    )
    assert g.commit_entry_submission_if_matches(
        entry, position_id="xrp-position-1", ledger_path=book.path, **scope
    )
    assert g.reconcile_execution(
        proposal_id=entry.proposal_id, executor_id="entry-exec", **scope
    )
    assert g.activate_cash_authority(
        account_name=scope["account_name"],
        connector_name="kraken",
        quote_asset="USD",
        opening_productive_quote=D("100"),
        opening_parked_quote=D("400"),
        activation_id="reviewed-opening-cash",
        activated_at=1000,
    )
    recovery = CapitalProposal(
        proposal_id="funded-recovery",
        pair="XRP-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("5"),
        account_name=scope["account_name"],
        connector_name="kraken",
        quote_asset="USD",
    )
    assert (
        g.reserve(
            recovery,
            CapitalSnapshot(
                free_quote=D("5000"),
                account_name=scope["account_name"],
                connector_name="kraken",
                quote_asset="USD",
            ),
        ).status
        == DecisionStatus.RESERVED
    )
    config = {
        "id": "funded-buy",
        "type": "order_executor",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "side": 1,
        "amount": "10.04",
        "price": "0.4",
        "execution_strategy": "LIMIT",
        "leverage": 1,
        "position_action": "OPEN",
        "controller_id": "spectra",
    }
    assert g.commit_recovery_submission_if_matches(
        recovery,
        position_id="xrp-position-1",
        ledger_path=book.path,
        protected_buy_config=config,
        protected_buy_policy={
            "fee_allowance_bps": "100",
            "max_price_slippage_bps": "0",
            "reference_price": "0.4",
            "price_cap": "0.4",
            "amount_base": "10.04",
            "max_principal_quote": "4.016",
            "max_fee_quote": "0.04016",
            "max_total_quote": "4.05616",
            "authorized_quote_budget": "5",
            "venue_book": {
                "pair": "XRP-USD",
                "exchange_pair": "XRPUSD",
                "best_bid": "0.3999",
                "best_ask": "0.4",
                "requested_at": time.time(),
                "received_at": time.time(),
                "source": "offline-exact-venue-fixture",
                "response_sha256": "a" * 64,
            },
        },
        **scope,
    )
    assert g.reconcile_execution(
        proposal_id=recovery.proposal_id, executor_id="funded-buy", **scope
    )
    order = ExecutorOrderEvidence(
        "funded-buy",
        "funded-buy-order",
        "LIMIT",
        "BUY",
        D("10.04"),
        D("0.4"),
        buy_status,
        D(filled_base),
        D("0.4"),
        1010,
        1011,
    )
    trade = TradeFillEvidence(
        "funded-buy-fill",
        order.order_id,
        scope["account_name"],
        "kraken",
        "XRP-USD",
        "BUY",
        D(filled_base),
        D("0.4"),
        D(buy_fee),
        "USD",
        1011,
    )
    orders, trades = ExecutorOrderSnapshot(
        "funded-buy", (order,)
    ), ExecutorTradeSnapshot("funded-buy", (trade,))
    report = synchronize_executor_evidence(
        reconciler=LiveFillLedgerReconciler(ledger=book, governor=g),
        position_id="xrp-position-1",
        proposal_id=recovery.proposal_id,
        order_snapshot=orders,
        trade_snapshot=trades,
        now=time.time(),
    )
    assert report.fully_reconciled
    raw = {
        "id": "funded-buy",
        "executor_type": "order_executor",
        "status": "TERMINATED",
        "close_type": buy_close_type,
        "filled_amount_quote": str(D(filled_base) * D("0.4")),
        "cum_fees_quote": buy_fee,
        "net_pnl_quote": "0",
        "custom_info": {"realized_pnl_quote": "0"},
        "config": config,
    }
    g.reconcile_executor_accounting(
        executor_accounting_observation_from_raw(raw, expected_executor_id="funded-buy")
    )
    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=time.time(),
    )
    assert decision.action == PartialRecycleAction.RECYCLE
    return g, book, decision, raw, orders, trades


class Venue:
    def __init__(
        self,
        case,
        *,
        filled="10",
        fee="0.01",
        status="FILLED",
        sale_price="0.5",
        amount="10"
    ):
        (
            self.g,
            self.book,
            self.decision,
            self.source,
            self.source_orders,
            self.source_trades,
        ) = case
        self.manifest = self.decision.manifest
        self.amount = D(amount)
        self.created = []
        self.known = False
        self.filled, self.fee, self.status, self.sale_price = (
            D(filled),
            D(fee),
            status,
            D(sale_price),
        )
        self.executors = self

    async def get_executor(self, executor_id):
        if executor_id == "funded-buy":
            return self.source
        assert executor_id == self.manifest.sell_executor_id
        if not self.known:
            raise ClientResponseError(None, (), status=404, message="absent")
        return {
            "id": executor_id,
            "executor_type": "order_executor",
            "status": "TERMINATED",
            "close_type": "COMPLETED",
            "filled_amount_quote": str(self.filled * self.sale_price),
            "cum_fees_quote": str(self.fee if self.filled else D(0)),
            "net_pnl_quote": "0",
            "custom_info": {"realized_pnl_quote": "0"},
        }

    async def create_executor(self, *, executor_config, account_name, controller_id):
        self.created.append((executor_config, account_name, controller_id))
        self.known = True
        return {"executor_id": executor_config["id"]}

    async def orders(self, client, *, executor_id):
        if executor_id == "funded-buy":
            return self.source_orders
        assert executor_id == self.manifest.sell_executor_id
        order = ExecutorOrderEvidence(
            executor_id,
            "sell-order",
            "LIMIT",
            "SELL",
            self.amount,
            self.manifest.protected_limit_price,
            self.status,
            self.filled,
            None if not self.filled else self.sale_price,
            2010,
            2011,
        )
        return ExecutorOrderSnapshot(executor_id, (order,))

    async def trades(self, client, *, executor_id):
        if executor_id == "funded-buy":
            return self.source_trades
        assert executor_id == self.manifest.sell_executor_id
        rows = (
            ()
            if not self.filled
            else (
                TradeFillEvidence(
                    "sell-fill",
                    "sell-order",
                    "master_account",
                    "kraken",
                    "XRP-USD",
                    "SELL",
                    self.filled,
                    self.sale_price,
                    self.fee,
                    "USD",
                    2011,
                ),
            )
        )
        return ExecutorTradeSnapshot(executor_id, rows)


def dispatch(case, venue, monkeypatch, *, observed_at=None):
    monkeypatch.setattr(tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(tool, "fetch_executor_order_snapshot", venue.orders)
    monkeypatch.setattr(tool, "fetch_executor_trade_snapshot", venue.trades)
    return asyncio.run(
        tool.execute_partial_recycle(
            venue,
            decision=case[2],
            governor=case[0],
            ledger=case[1],
            trading_rule=rule(),
            current_price=D("0.50"),
            now=time.time(),
            market_observed_at=time.time() if observed_at is None else observed_at,
        )
    )


def test_funded_terminal_buy_to_fixed_limit_partial_preserves_dust_exposure_and_park(
    tmp_path, monkeypatch
):
    case = funded_case(tmp_path)
    g, book, decision = case[:3]
    before = book.snapshot("xrp-position-1")
    assert before.active_base == D("10.04")
    assert g.current_recovery_exposure_quote() >= D("4.016")
    venue = Venue(case)
    result = dispatch(case, venue, monkeypatch)
    assert result["dispatch_state"] == "NEW_DISPATCH_CONFIRMED"
    assert result["result"]["terminal"] and not result["result"]["completed"]
    config = venue.created[0][0]
    assert config["execution_strategy"] == "LIMIT"
    assert D(config["amount"]) == D("10")
    assert D(config["price"]) == decision.manifest.protected_limit_price
    assert "time_in_force" not in config
    after = book.snapshot("xrp-position-1")
    assert after.active_base == D("0.04")
    assert after.active_cost_basis_quote == D("0.016")
    assert after.active_entry_fees_quote > D(0)
    assert g.current_recovery_exposure_quote() >= D("0.016")
    cash = g.cash_authority_snapshot()
    assert cash.productive_quote == D("95")
    assert cash.parked_quote == D("404.99")
    assert cash.held_quote == D(0) and not cash.blocked
    store = PartialRecycleStore(book.path)
    assert store.load(decision.manifest.manifest_id).status.value == "TERMINAL"
    proof = store.terminal_proof(decision.manifest.manifest_id)
    assert proof["durable_fills"][0]["base_sold"] == "10"
    restarted = CapitalGovernor(g.db_path, g.policy)
    reloaded = EconomicPositionLedger(book.path)
    assert restarted.current_recovery_exposure_quote() >= D("0.016")
    assert reloaded.snapshot("xrp-position-1") == after
    assert dispatch(case, venue, monkeypatch)["dispatch_state"] == "EXISTING_RECONCILED"
    assert len(venue.created) == 1
    assert restarted.cash_authority_snapshot().parked_quote == D("404.99")


@pytest.mark.parametrize(
    "fault",
    [
        "actual_market",
        "missing_trade_journal",
        "cumulative_mismatch",
        "different_owner",
    ],
)
def test_generic_terminal_buy_or_incomplete_ownership_is_never_permission_to_sell(
    tmp_path, monkeypatch, fault
):
    case = funded_case(tmp_path)
    venue = Venue(case)
    if fault == "actual_market":
        venue.source = dict(
            venue.source,
            config=dict(venue.source["config"], execution_strategy="MARKET"),
        )
    elif fault == "missing_trade_journal":
        with sqlite3.connect(case[1].path) as conn:
            conn.execute(
                "DELETE FROM economic_trade_fill_reconciliation WHERE trade_id='funded-buy-fill'"
            )
    elif fault == "cumulative_mismatch":
        venue.source_orders = ExecutorOrderSnapshot(
            "funded-buy",
            (replace(venue.source_orders.orders[0], filled_amount=D("10")),),
        )
    else:
        with sqlite3.connect(case[0].db_path) as conn:
            conn.execute(
                "DELETE FROM protected_buy_submissions WHERE proposal_id='funded-recovery'"
            )
    result = dispatch(case, venue, monkeypatch)
    assert result["executed"] is False
    assert venue.created == []
    assert case[1].snapshot("xrp-position-1").active_base == D("10.04")
    assert not PartialRecycleStore(case[1].path).dispatch_claimed(
        case[2].manifest.manifest_id
    )


@pytest.mark.parametrize("price,fee", [("0.39", "0.01"), ("0.5", "1.1")])
def test_real_below_floor_or_excess_fee_sell_is_owned_loss_and_never_terminal_success(
    tmp_path, monkeypatch, price, fee
):
    case = funded_case(tmp_path)
    venue = Venue(case, sale_price=price, fee=fee)
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        dispatch(case, venue, monkeypatch)
    # Venue evidence is journaled honestly even when execution violated its floor.
    assert case[1].snapshot("xrp-position-1").active_base == D("0.04")
    store = PartialRecycleStore(case[1].path)
    assert store.fill_recorded("sell-fill")
    assert store.protection_conflicts(case[2].manifest.manifest_id)
    assert store.terminal_proof(case[2].manifest.manifest_id) is None
    cash = case[0].cash_authority_snapshot()
    assert cash.blocked and cash.held_quote > D(0)
    assert cash.parked_quote == D("400")
    assert case[0].current_recovery_exposure_quote() >= D("0.016")


def test_proven_zero_fill_cancel_allows_new_protected_generation_without_cash_credit(
    tmp_path, monkeypatch
):
    case = funded_case(tmp_path)
    venue = Venue(case, filled="0", status="CANCELED")
    result = dispatch(case, venue, monkeypatch)
    assert result["result"]["terminal"] and not result["result"]["completed"]
    g, book, decision = case[:3]
    assert g.cash_authority_snapshot().parked_quote == D("400")
    second = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=time.time(),
    )
    assert second.manifest.predecessor_manifest_id == decision.manifest.manifest_id
    assert second.manifest.manifest_id != decision.manifest.manifest_id
    assert (
        second.manifest.protected_limit_price == decision.manifest.protected_limit_price
    )
    assert book.snapshot("xrp-position-1").active_base == D("10.04")
    assert PartialRecycleStore(book.path).dispatch_claimed(
        decision.manifest.manifest_id
    )


def test_durable_buy_conflict_blocks_new_sell_but_later_sell_truth_is_recorded(
    tmp_path, monkeypatch
):
    case = funded_case(tmp_path)
    g, book, decision = case[:3]
    venue = Venue(case)
    g.record_protected_buy_conflict(
        proposal_id="funded-recovery", reason="funded BUY exceeded its saved ceiling"
    )
    with pytest.raises(RuntimeError, match="cash authority requires reconciliation"):
        dispatch(case, venue, monkeypatch)
    assert not venue.created
    new_plan = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=time.time(),
    )
    assert (
        new_plan.manifest == decision.manifest
    )  # Exact old mutation remains available only for reconciliation.
    # A prior dispatched SELL can still fill while a separate BUY conflict is unresolved.
    venue.known = True
    with pytest.raises(RuntimeError, match="BUY source conflict"):
        dispatch(case, venue, monkeypatch)
    assert book.snapshot("xrp-position-1").active_base == D("0.04")
    assert PartialRecycleStore(book.path).fill_recorded("sell-fill")
    assert (
        PartialRecycleStore(book.path).terminal_proof(decision.manifest.manifest_id)
        is None
    )
    assert g.cash_authority_snapshot().parked_quote == D("400")
    assert g.cash_authority_snapshot().blocked


@pytest.mark.parametrize("close_type", ["FAILED", "EARLY_STOP", "POSITION_HOLD"])
def test_exact_terminal_funded_buy_recognized_close_types_remain_passive(
    tmp_path, monkeypatch, close_type
):
    case = funded_case(tmp_path)
    venue = Venue(case)
    venue.source = dict(venue.source, close_type=close_type)
    result = dispatch(case, venue, monkeypatch)
    assert result["dispatch_state"] == "NEW_DISPATCH_CONFIRMED"
    assert result["result"]["terminal"] is True
    assert len(venue.created) == 1
    assert venue.created[0][0]["execution_strategy"] == "LIMIT"
    assert case[1].snapshot("xrp-position-1").active_base == D("0.04")


@pytest.mark.parametrize("close_type", ["FAILED", "EARLY_STOP", "POSITION_HOLD"])
def test_canceled_partially_filled_funded_buy_can_recycle_only_confirmed_owned_parcel(
    tmp_path, monkeypatch, close_type
):
    case = funded_case(
        tmp_path,
        filled_base="5.04",
        buy_fee="0.02",
        buy_status="CANCELED",
        buy_close_type=close_type,
    )
    venue = Venue(case, filled="5", amount="5")
    assert case[1].snapshot("xrp-position-1").active_base == D("5.04")
    result = dispatch(case, venue, monkeypatch)
    assert result["result"]["terminal"] is True
    assert D(venue.created[0][0]["amount"]) == D("5")
    assert venue.created[0][0]["execution_strategy"] == "LIMIT"
    assert D(venue.created[0][0]["price"]) == case[2].manifest.protected_limit_price
    assert case[1].snapshot("xrp-position-1").active_base == D("0.04")
    assert case[1].snapshot("xrp-position-1").active_cost_basis_quote == D("0.016")
    assert case[0].cash_authority_snapshot().parked_quote == D("402.49")
    assert case[0].cash_authority_snapshot().productive_quote == D("95")
