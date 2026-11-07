"""Whole sale settlement and strict retirement must compose across restart."""

import asyncio
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import mcp_servers.hummingbot_api.tools.economic_exit as exit_tool
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_exit import (
    EconomicExitManifestStatus,
    EconomicExitManifestStore,
    create_or_load_position_exit_manifest,
    economic_exit_terminal_certificate,
)
from spectra_core.economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
    TerminalExecutorEvidence,
)
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
    TradeFillEvidence,
)
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    ExecutorAccountingObservation,
    IntentKind,
)
from tests.test_residual_position_lifecycle import fixture as residual_fixture
from tests.test_unified_economic_exit import trading_rule

D = Decimal


class MissingExecutor(RuntimeError):
    status = 404


def record_trade(reconciler, trade, now):
    return reconciler.reconcile_trade_fill(
        dict(
            trade_id=trade.trade_id,
            order_id=trade.order_id,
            account_name=trade.account_name,
            connector_name=trade.connector_name,
            trading_pair=trade.pair,
            trade_type=trade.side,
            amount=str(trade.amount_base),
            price=str(trade.price),
            fee_paid=str(trade.fee_paid),
            fee_currency=trade.fee_currency,
            timestamp=datetime.fromtimestamp(
                trade.occurred_at, timezone.utc
            ).isoformat(),
        ),
        reconciled_at=now,
    )


def whole_fixture(
    tmp_path, monkeypatch, *, remaining="0.01", intent=IntentKind.ENTRY, cash=True
):
    stamp = float(int(time.time()))
    g = CapitalGovernor(
        tmp_path / "capital.db",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("1000"),
        ),
    )
    ledger = EconomicPositionLedger(tmp_path / "ledger.db")
    ledger.open_position(position_id="position", pair="XRP-USD", created_at=stamp - 15)
    proposal = CapitalProposal("owner", "XRP-USD", intent, D("100"))
    g.reserve(proposal, CapitalSnapshot(free_quote=D("5000")))
    assert g.commit("owner")
    assert g.reconcile_execution(
        proposal_id="owner",
        executor_id="buyer",
        account_name="account",
        connector_name="kraken",
        controller_id="spectra",
    )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    reconciler.bind_executor_order(
        order_id="buy",
        position_id="position",
        role=(
            EconomicFillRole.BASE_ENTRY
            if intent == IntentKind.ENTRY
            else EconomicFillRole.VCAP_RECOVERY
        ),
        proposal_id="owner",
        account_name="account",
        connector_name="kraken",
        pair="XRP-USD",
        created_at=stamp - 14,
    )
    buy = TradeFillEvidence(
        "buy-fill",
        "buy",
        "account",
        "kraken",
        "XRP-USD",
        "BUY",
        D("100"),
        D("0.5"),
        D("0.1"),
        "USD",
        stamp - 12,
    )
    record_trade(reconciler, buy, stamp - 11)
    buy_order = ExecutorOrderEvidence(
        "buyer",
        "buy",
        "LIMIT",
        "BUY",
        D("100"),
        D("0.5"),
        "FILLED",
        D("100"),
        D("0.5"),
        stamp - 14,
        stamp - 12,
    )
    source = ExecutorAccountingObservation(
        "buyer",
        "TERMINATED",
        D("50"),
        D("0.1"),
        position_size_quote=D("50"),
        close_type="POSITION_HOLD",
        executor_type="dca_executor",
    )
    g.reconcile_executor_accounting(source)
    ledger.record_strategic_retained(
        lot_id="retained",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("17"),
        occurred_at=stamp - 10,
    )
    ledger.record_unattributed(
        lot_id="unattributed",
        pair="XRP-USD",
        base_amount=D("20"),
        occurred_at=stamp - 10,
    )
    if cash:
        assert g.activate_cash_authority(
            account_name="account",
            connector_name="kraken",
            quote_asset="USD",
            opening_productive_quote=D("100"),
            activation_id="opening",
            activated_at=stamp - 9,
        )
    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="position",
        trading_rule=trading_rule(),
        current_price=D("1"),
        now=stamp - 8,
        target_net_profit_bps=D("174"),
        expected_exit_cost_bps=D("20"),
    )
    store = EconomicExitManifestStore(ledger.path)
    assert store.claim_dispatch(manifest_id=manifest.manifest_id, now=stamp - 7)
    sold = D("100") - D(remaining)
    sell = TradeFillEvidence(
        "whole-fill",
        "sell",
        "account",
        "kraken",
        "XRP-USD",
        "SELL",
        sold,
        D("1"),
        D("0.1"),
        "USD",
        stamp - 5,
    )
    sell_order = ExecutorOrderEvidence(
        manifest.sell_executor_id,
        "sell",
        "LIMIT",
        "SELL",
        D("100"),
        D("1"),
        "FILLED" if remaining == "0" else "CANCELED",
        sold,
        D("1"),
        stamp - 7,
        stamp - 5,
    )

    class Executors:
        known = True
        created = []
        volume = sell.quote_amount
        status = "TERMINATED"
        fills = (sell,)
        orders = (sell_order,)

        async def get_executor(self, executor_id):
            if executor_id == "buyer":
                return dict(
                    id="buyer",
                    executor_type="dca_executor",
                    status="TERMINATED",
                    close_type="POSITION_HOLD",
                    filled_amount_quote="50",
                    cum_fees_quote="0.1",
                    position_size_quote="50",
                    net_pnl_quote="0",
                    custom_info={"realized_pnl_quote": "0"},
                )
            if not self.known:
                raise MissingExecutor("unknown")
            return dict(
                id=manifest.sell_executor_id,
                executor_type="order_executor",
                status=self.status,
                close_type="COMPLETED",
                filled_amount_quote=str(self.volume),
                cum_fees_quote="0.1",
                net_pnl_quote="0",
                custom_info={"realized_pnl_quote": "0"},
            )

        async def create_executor(self, **kwargs):
            self.created.append(kwargs)
            pytest.fail("reconciliation submitted a second SELL")

    client = SimpleNamespace(executors=Executors())

    async def orders(client, *, executor_id):
        return ExecutorOrderSnapshot(
            executor_id,
            (buy_order,) if executor_id == "buyer" else client.executors.orders,
        )

    async def trades(client, *, executor_id):
        return ExecutorTradeSnapshot(
            executor_id, (buy,) if executor_id == "buyer" else client.executors.fills
        )

    monkeypatch.setattr(exit_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(exit_tool, "fetch_executor_trade_snapshot", trades)
    kwargs = dict(
        client=client,
        manifest=manifest,
        governor=g,
        ledger=ledger,
        store=store,
        now=time.time(),
    )
    return g, ledger, manifest, client, kwargs


def sync(kwargs):
    return asyncio.run(
        exit_tool.synchronize_economic_position_exit(**dict(kwargs, now=time.time()))
    )


def retire(g, ledger, client):
    now = time.time()
    return asyncio.run(
        exit_tool.retire_terminal_economic_position(
            client,
            position_id="position",
            proposal_ids=("owner",),
            governor=g,
            ledger=ledger,
            trading_rule=trading_rule(),
            current_price=D("1"),
            market_observed_at=now,
            now=now,
        )
    )


@pytest.mark.parametrize("intent", [IntentKind.ENTRY, IntentKind.RECOVERY])
def test_whole_sale_settles_then_terminal_residue_keeps_basis_fees_and_cash(
    tmp_path, monkeypatch, intent
):
    g, ledger, manifest, client, kwargs = whole_fixture(
        tmp_path, monkeypatch, intent=intent
    )
    result = sync(kwargs)
    assert not result["completed"]
    before = ledger.get_lot("trade-fill:buy-fill")
    cash_before = g.cash_authority_snapshot()
    exposure_before = g.current_recovery_exposure_quote()
    certificate = retire(g, ledger, client)
    assert certificate["kind"] == "RESIDUAL"
    assert certificate["capital_released_quote"] == "0"
    assert ledger.get_lot(before.lot_id) == before
    assert ledger.snapshot("position").status == EconomicPositionStatus.RESIDUAL
    assert g.cash_authority_snapshot() == cash_before
    assert cash_before.productive_quote == D("199.89")
    assert cash_before.parked_quote == D("0")
    assert g.current_recovery_exposure_quote() == exposure_before
    assert ledger.get_lot("retained").base_remaining == D("43")
    assert ledger.get_lot("unattributed").base_remaining == D("20")
    assert (
        certificate["proof"]["whole_terminal_certificates"][0]["executor_filled_quote"]
        == "99.99"
    )
    assert client.executors.created == []


@pytest.mark.parametrize("remaining", ["0", "0.01"])
def test_crash_after_inventory_commit_keeps_cash_and_identity_locked_until_restart_settlement(
    tmp_path, monkeypatch, remaining
):
    g, ledger, manifest, client, kwargs = whole_fixture(
        tmp_path, monkeypatch, remaining=remaining
    )
    original = exit_tool.finalize_sale_cash_fill

    def crash(**fields):
        raise RuntimeError("crash before cash settlement")

    monkeypatch.setattr(exit_tool, "finalize_sale_cash_fill", crash)
    with pytest.raises(RuntimeError, match="crash"):
        sync(kwargs)
    assert ledger.snapshot("position").active_base == D(remaining)
    assert (
        EconomicExitManifestStore(ledger.path).load(manifest.manifest_id).status
        == EconomicExitManifestStatus.OPEN
    )
    assert g.cash_authority_snapshot().blocked
    with pytest.raises(ValueError, match="cash settlement"):
        retire(g, ledger, client)
    assert ledger.get_retirement_certificate("position") is None
    monkeypatch.setattr(exit_tool, "finalize_sale_cash_fill", original)
    restarted = EconomicPositionLedger(ledger.path)
    sync(
        dict(kwargs, ledger=restarted, store=EconomicExitManifestStore(restarted.path))
    )
    assert not g.cash_authority_snapshot().blocked
    assert retire(g, restarted, client)["kind"] == (
        "FLAT_TERMINAL" if remaining == "0" else "RESIDUAL"
    )
    assert client.executors.created == []


def test_lagging_terminal_sell_volume_never_creates_retirement_certificate(
    tmp_path, monkeypatch
):
    g, ledger, manifest, client, kwargs = whole_fixture(
        tmp_path, monkeypatch, cash=False
    )
    client.executors.fills = ()
    client.executors.orders = (
        replace(client.executors.orders[0], filled_amount=D("0")),
    )
    with pytest.raises(RuntimeError, match="executor volume"):
        sync(kwargs)
    with pytest.raises(ValueError, match="whole exit dispatch"):
        retire(g, ledger, client)
    assert ledger.get_retirement_certificate("position") is None
    assert ledger.snapshot("position").active_base == D("100")


@pytest.mark.parametrize("hold", ["price", "market", "fee", "reconciliation_only"])
def test_confirmed_whole_sell_truth_keeps_cash_pending_on_protection_hold(
    tmp_path, monkeypatch, hold
):
    g, ledger, manifest, client, kwargs = whole_fixture(tmp_path, monkeypatch)
    fill, order = client.executors.fills[0], client.executors.orders[0]
    if hold == "price":
        fill = replace(fill, price=manifest.protected_limit_price - D("0.001"))
        order = replace(order, price=fill.price, average_fill_price=fill.price)
    elif hold == "market":
        order = replace(order, order_type="MARKET")
    elif hold == "fee":
        fill = replace(fill, fee_paid=D("50"))
    else:
        kwargs["reconciliation_only"] = True
    client.executors.fills = (fill,)
    client.executors.orders = (order,)
    client.executors.volume = fill.quote_amount
    for _ in range(2):
        with pytest.raises(RuntimeError, match="protection conflict|HOLD"):
            sync(kwargs)
        assert ledger.snapshot("position").active_base == D("0.01")
        assert kwargs["store"].fill_recorded(fill.trade_id)
        authority = g.cash_authority_snapshot()
        assert authority.blocked
        assert authority.productive_quote == D("100")
        assert authority.parked_quote == D("0")
        assert not kwargs["store"].finality_confirmed(manifest.manifest_id)
        assert ledger.get_retirement_certificate("position") is None
    assert client.executors.created == []


@pytest.mark.parametrize("claimed", [False, True])
def test_whole_sell_reconciles_original_parcel_after_exact_owned_inventory_growth(
    tmp_path, monkeypatch, claimed
):
    g, ledger, manifest, client, kwargs = whole_fixture(tmp_path, monkeypatch)
    extra_lot = ledger.record_entry_fill(
        governor=g,
        position_id="position",
        lot_id="late-buy-fill",
        proposal_id="owner",
        base_amount=D("1"),
        cost_basis_quote=D("0.5"),
        occurred_at=time.time(),
    )
    if not claimed:
        with sqlite3.connect(ledger.path) as conn:
            conn.execute(
                "DELETE FROM economic_exit_dispatch_claims WHERE manifest_id = ?",
                (manifest.manifest_id,),
            )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="parcel growth|new ACTIVE_TRADE"):
            sync(kwargs)
        assert ledger.snapshot("position").active_base == (
            D("1.01") if claimed else D("101")
        )
        assert ledger.get_lot(extra_lot.lot_id) == extra_lot
        assert kwargs["store"].fill_recorded("whole-fill") == claimed
        assert g.cash_authority_snapshot().blocked == claimed
        assert g.cash_authority_snapshot().productive_quote == D("100")
        assert not kwargs["store"].finality_confirmed(manifest.manifest_id)
        assert ledger.get_retirement_certificate("position") is None
    assert client.executors.created == []


def test_retired_residual_cannot_claim_a_preplanned_whole_exit(tmp_path):
    g, ledger, proof = residual_fixture(tmp_path)
    prior_rule = replace(
        proof["trading_rule"],
        min_order_size=D("0.001"),
        min_notional_size=D("0.001"),
        min_base_amount_increment=D("0.001"),
    )
    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="position",
        trading_rule=prior_rule,
        current_price=D("1"),
        now=proof["now"],
        target_net_profit_bps=D("174"),
        expected_exit_cost_bps=D("20"),
    )
    ledger.retire_terminal_position(**proof)
    with pytest.raises(RuntimeError, match="ACTIVE ownership"):
        EconomicExitManifestStore(ledger.path).claim_dispatch(
            manifest_id=manifest.manifest_id, now=time.time()
        )


@pytest.mark.parametrize(
    "guard", ["missing_bid", "stale_bid", "below_floor", "dispatch_disabled"]
)
def test_unclaimed_whole_sale_requires_fresh_marketable_bid_and_dispatch_authority(
    tmp_path, monkeypatch, guard
):
    g, ledger, manifest, client, kwargs = whole_fixture(
        tmp_path, monkeypatch, cash=False
    )
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "DELETE FROM economic_exit_dispatch_claims WHERE manifest_id = ?",
            (manifest.manifest_id,),
        )
    client.executors.known = False
    monkeypatch.setattr(
        exit_tool, "get_breaker_mode", lambda: exit_tool.BreakerMode.NORMAL
    )
    fields = dict(
        client=client,
        manifest=manifest,
        governor=g,
        ledger=ledger,
        now=time.time(),
        trading_rule=trading_rule(),
        account_name="account",
        best_bid=D("1"),
        market_observed_at=time.time() - 1,
    )
    if guard == "missing_bid":
        fields["best_bid"] = None
    elif guard == "stale_bid":
        fields["market_observed_at"] -= 31
    elif guard == "below_floor":
        fields["best_bid"] = D("0.1")
    else:
        fields["allow_new_dispatch"] = False
    if guard in {"stale_bid", "below_floor"}:
        with pytest.raises(ValueError, match="stale|immutable"):
            asyncio.run(exit_tool.dispatch_protected_economic_exit(**fields))
    else:
        result = asyncio.run(exit_tool.dispatch_protected_economic_exit(**fields))
        assert not result["executed"]
        assert (
            "first_submission_requires_fresh_executable_bid"
            if guard == "missing_bid"
            else "new_exit_dispatch_disabled"
        ) in result["reasons"]
    assert client.executors.created == []
    assert not EconomicExitManifestStore(ledger.path).dispatch_claimed(
        manifest.manifest_id
    )


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
def test_exact_protected_buy_order_executor_can_retire_owned_dust_or_empty_prefill(
    tmp_path, empty, conflict
):
    now = float(int(time.time()))
    g = CapitalGovernor(
        tmp_path / "capital.db",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("100"),
            max_pair_quote=D("100"),
            max_position_quote=D("100"),
            max_recovery_quote=D("100"),
        ),
    )
    ledger = EconomicPositionLedger(tmp_path / "ledger.db")
    ledger.open_position(position_id="position", pair="XRP-USD", created_at=now - 10)
    proposal = CapitalProposal("owner", "XRP-USD", IntentKind.ENTRY, D("1"))
    g.reserve(proposal, CapitalSnapshot(free_quote=D("100")))
    config = dict(
        id="buyer",
        type="order_executor",
        connector_name="kraken",
        trading_pair="XRP-USD",
        side=1,
        amount="0.01",
        price="1",
        execution_strategy="LIMIT",
        leverage=1,
    )
    policy = dict(
        fee_allowance_bps="0",
        max_price_slippage_bps="0",
        reference_price="1",
        price_cap="1",
        amount_base="0.01",
        max_principal_quote="0.01",
        max_fee_quote="0",
        max_total_quote="0.01",
        authorized_quote_budget="1",
        venue_book=dict(
            pair="XRP-USD",
            requested_at=now,
            received_at=now,
            venue_min_order_size="0.001",
            venue_min_cost_quote="0.001",
            venue_base_asset="XRP",
            venue_quote_asset="USD",
            venue_rules_observed_at=now,
            venue_rules_sha256="b" * 64,
        ),
    )
    assert g.commit_entry_submission_if_matches(
        proposal,
        position_id="position",
        ledger_path=ledger.path,
        account_name="account",
        connector_name="kraken",
        controller_id="spectra",
        protected_buy_config=config,
        protected_buy_policy=policy,
    )
    g.reconcile_execution(
        proposal_id="owner",
        executor_id="buyer",
        account_name="account",
        connector_name="kraken",
        controller_id="spectra",
    )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    orders, trades = (), ()
    if not empty:
        reconciler.bind_executor_order(
            order_id="buy",
            position_id="position",
            role=EconomicFillRole.BASE_ENTRY,
            proposal_id="owner",
            account_name="account",
            connector_name="kraken",
            pair="XRP-USD",
            created_at=now - 9,
        )
        trade = TradeFillEvidence(
            "buy-fill",
            "buy",
            "account",
            "kraken",
            "XRP-USD",
            "BUY",
            D("0.01"),
            D("1"),
            D("0"),
            None,
            now - 8,
        )
        record_trade(reconciler, trade, now - 7)
        orders = (
            ExecutorOrderEvidence(
                "buyer",
                "buy",
                "LIMIT",
                "BUY",
                D("0.01"),
                D("1"),
                "FILLED",
                D("0.01"),
                D("1"),
                now - 9,
                now - 8,
            ),
        )
        trades = (trade,)
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            "buyer",
            "TERMINATED",
            D("0") if empty else D("0.01"),
            D("0"),
            close_type="FAILED" if empty else "COMPLETED",
            executor_type="order_executor",
        )
    )
    observed = time.time()
    evidence = TerminalExecutorEvidence(
        "owner",
        ExecutorOrderSnapshot("buyer", orders),
        ExecutorTradeSnapshot("buyer", trades),
        observed,
        config,
    )
    proof = dict(
        event_id="terminal",
        position_id="position",
        governor=g,
        executor_evidence=(evidence,),
        trading_rule=trading_rule(),
        current_price=D("1"),
        rules_observed_at=observed,
        price_observed_at=observed,
        now=observed,
    )
    wrong = replace(evidence, executor_config=dict(config, execution_strategy="MARKET"))
    with pytest.raises(ValueError, match="observed config"):
        ledger.retire_terminal_position(**dict(proof, executor_evidence=(wrong,)))
    assert ledger.get_retirement_certificate("position") is None
    if conflict:
        g.record_protected_buy_conflict(
            proposal_id="owner", reason="actual BUY exceeds immutable ceiling"
        )
        with pytest.raises(ValueError, match="protected BUY conflict"):
            ledger.retire_terminal_position(**proof)
        assert ledger.get_retirement_certificate("position") is None
        assert ledger.snapshot("position").status == EconomicPositionStatus.ACTIVE
        return
    certificate = ledger.retire_terminal_position(**proof)
    assert certificate["kind"] == ("EMPTY_TERMINAL" if empty else "RESIDUAL")
