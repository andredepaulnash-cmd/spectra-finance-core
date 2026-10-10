from dataclasses import replace
from decimal import Decimal as D
from decimal import localcontext
from time import time

import pytest

from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
    synchronize_executor_evidence,
)
from spectra_core.fill_reconciliation import LiveFillLedgerReconciler, TradeFillEvidence
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)


def setup_buy(tmp_path, *, with_policy=True):
    governor = CapitalGovernor(
        tmp_path / "governor.sqlite",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("1000"),
            max_pair_quote=D("1000"),
            max_position_quote=D("1000"),
            max_recovery_quote=D("1000"),
            max_leverage=D("1"),
        ),
    )
    governor.activate_cash_authority(
        account_name="offline",
        connector_name="kraken",
        quote_asset="USD",
        opening_productive_quote=D("100"),
        opening_parked_quote=D("20"),
        activation_id="reviewed",
        activated_at=1000,
    )
    ledger = EconomicPositionLedger(tmp_path / "economic.sqlite")
    ledger.open_position(position_id="position", pair="XRP-USD", created_at=1000)
    proposal = CapitalProposal(
        "buy",
        "XRP-USD",
        IntentKind.ENTRY,
        D("10"),
        account_name="offline",
        connector_name="kraken",
        quote_asset="USD",
    )
    assert (
        governor.reserve(
            proposal,
            CapitalSnapshot(
                free_quote=D("120"),
                account_name="offline",
                connector_name="kraken",
                quote_asset="USD",
            ),
        ).status
        == DecisionStatus.RESERVED
    )
    config = {
        "id": "executor",
        "type": "order_executor",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "side": 1,
        "amount": "9",
        "price": "1",
        "execution_strategy": "LIMIT",
        "leverage": 1,
        "position_action": "OPEN",
    }
    policy = {
        "fee_allowance_bps": "25",
        "max_price_slippage_bps": "0",
        "reference_price": "1",
        "price_cap": "1",
        "amount_base": "9",
        "max_principal_quote": "9",
        "max_fee_quote": ".0225",
        "max_total_quote": "9.0225",
        "authorized_quote_budget": "10",
        "venue_book": {
            "pair": "XRP-USD",
            "requested_at": time() - 0.01,
            "received_at": time(),
            "best_bid": ".999",
            "best_ask": "1",
            "source": "kraken_public_depth",
            "response_sha256": "a" * 64,
        },
    }
    assert governor.commit_entry_submission_if_matches(
        proposal,
        position_id="position",
        ledger_path=ledger.path,
        account_name="offline",
        connector_name="kraken",
        controller_id="spectra",
        protected_buy_config=config,
        protected_buy_policy=policy if with_policy else None,
    )
    assert governor.reconcile_execution(
        proposal_id="buy",
        executor_id="executor",
        account_name="offline",
        connector_name="kraken",
        controller_id="spectra",
    )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    order = ExecutorOrderEvidence(
        "executor",
        "order",
        "LIMIT",
        "BUY",
        D("9"),
        D("1"),
        "OPEN",
        D("2"),
        D("1"),
        1001,
        1100,
    )
    trade = TradeFillEvidence(
        "fill",
        "order",
        "offline",
        "kraken",
        "XRP-USD",
        "BUY",
        D("2"),
        D("1"),
        D("0"),
        None,
        1100,
    )
    return governor, ledger, reconciler, order, trade


def sync(reconciler, order, trades):
    return synchronize_executor_evidence(
        reconciler=reconciler,
        position_id="position",
        proposal_id="buy",
        order_snapshot=ExecutorOrderSnapshot("executor", (order,)),
        trade_snapshot=ExecutorTradeSnapshot("executor", tuple(trades)),
        now=1101,
    )


@pytest.mark.parametrize(
    "breach",
    [
        "market",
        "order_price",
        "fill_price",
        "amount",
        "quote_fee",
        "base_fee",
        "missing_policy",
    ],
)
def test_confirmed_money_survives_durable_policy_breach_and_restart(tmp_path, breach):
    governor, ledger, reconciler, order, trade = setup_buy(
        tmp_path, with_policy=breach != "missing_policy"
    )
    if breach == "market":
        order = replace(order, order_type="MARKET")
    elif breach == "order_price":
        order = replace(order, price=D("1.01"))
    elif breach == "fill_price":
        trade = replace(trade, price=D("1.01"))
    elif breach == "amount":
        order = replace(order, amount=D("10"))
    elif breach == "quote_fee":
        trade = replace(trade, fee_paid=D(".01"), fee_currency="USD")
    elif breach == "base_fee":
        trade = replace(trade, fee_paid=D(".01"), fee_currency="XRP")
    first = sync(reconciler, order, [trade])
    assert not first.fully_reconciled
    assert first.applied_fills == 1
    assert governor.get_protected_buy_conflicts("buy")
    snapshot = ledger.snapshot("position")
    assert (
        snapshot.active_base == D("1.99")
        if breach == "base_fee"
        else snapshot.active_base == D("2")
    )
    assert (
        snapshot.active_cost_basis_quote == D("2.02")
        if breach == "fill_price"
        else snapshot.active_cost_basis_quote == D("2")
    )
    assert governor.cash_authority_snapshot().parked_quote == D("20")
    assert governor.cash_authority_snapshot().productive_quote == D("90")
    restarted = LiveFillLedgerReconciler(
        ledger=EconomicPositionLedger(ledger.path),
        governor=CapitalGovernor(governor.db_path, governor.policy),
    )
    replay = sync(restarted, order, [trade])
    assert replay.replayed_fills == 1
    assert not replay.fully_reconciled
    assert restarted.ledger.snapshot("position") == snapshot
    assert restarted.governor.cash_authority_snapshot().blocked


def test_valid_fee_boundary_and_lagging_endpoint_are_not_permanent_conflicts(tmp_path):
    governor, ledger, reconciler, order, trade = setup_buy(tmp_path)
    trade = replace(trade, fee_paid=D(".005"), fee_currency="USD")
    incomplete = sync(reconciler, replace(order, filled_amount=D("3")), [trade])
    assert not incomplete.fully_reconciled
    assert not governor.get_protected_buy_conflicts("buy")
    complete = sync(reconciler, order, [trade])
    assert complete.fully_reconciled
    assert not governor.cash_authority_snapshot().blocked
    assert ledger.snapshot("position").active_entry_fees_quote == D(".005")


def test_cumulative_quantity_cannot_hide_inside_quote_budget(tmp_path):
    governor, ledger, reconciler, order, trade = setup_buy(tmp_path)
    trade = replace(trade, amount_base=D("6"), price=D(".5"))
    first = sync(reconciler, replace(order, filled_amount=D("6")), [trade])
    assert first.fully_reconciled
    second = replace(trade, trade_id="fill-2", occurred_at=1100.5)
    report = sync(reconciler, replace(order, filled_amount=D("12")), [trade, second])
    assert not report.fully_reconciled
    assert ledger.snapshot("position").active_base == D("12")
    assert ledger.snapshot("position").active_cost_basis_quote == D("6")
    assert governor.cash_authority_snapshot().blocked


def test_direct_fill_reconciliation_cannot_bypass_price_audit(tmp_path):
    governor, ledger, reconciler, order, trade = setup_buy(tmp_path)
    sync(reconciler, order, [trade])
    raw = {
        "trade_id": "direct",
        "order_id": "order",
        "account_name": "offline",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "trade_type": "BUY",
        "amount": "1",
        "price": "1.01",
        "fee_paid": "0",
        "fee_currency": None,
        "timestamp": "1970-01-01T00:18:20.5+00:00",
    }
    result = reconciler.reconcile_trade_fill(raw, reconciled_at=1101)
    assert result.status == "APPLIED"
    assert ledger.snapshot("position").active_base == D("3")
    assert governor.get_protected_buy_conflicts("buy")
    assert reconciler.reconcile_trade_fill(raw, reconciled_at=1102).status == "REPLAY"


def test_fee_audit_uses_exact_comparison_under_low_decimal_precision(tmp_path):
    governor, _, reconciler, order, trade = setup_buy(tmp_path)
    trade = replace(
        trade, fee_paid=D(".005000000000000000000000001"), fee_currency="USD"
    )
    with localcontext() as context:
        context.prec = 7
        report = sync(reconciler, order, [trade])
    assert not report.fully_reconciled
    assert any(
        "fee_above_allowance" in reason
        for reason in governor.get_protected_buy_conflicts("buy")
    )


def test_confirmed_quote_product_and_base_fee_keep_every_digit(tmp_path):
    governor, ledger, reconciler, order, trade = setup_buy(tmp_path)
    trade = replace(
        trade,
        amount_base=D("2.000000000000000000000000001"),
        price=D(".999999999999999999999999999"),
        fee_paid=D(".000000000000000000000000001"),
        fee_currency="XRP",
    )
    with localcontext() as context:
        context.prec = 100
        expected_quote = trade.amount_base * trade.price
    with localcontext() as context:
        context.prec = 7
        assert trade.quote_amount == expected_quote
        report = sync(
            reconciler, replace(order, filled_amount=trade.amount_base), [trade]
        )
    assert report.fully_reconciled
    assert ledger.snapshot("position").active_base == D("2")
    assert ledger.snapshot("position").active_cost_basis_quote == expected_quote
    assert not governor.cash_authority_snapshot().blocked


def test_policy_only_health_tag_survives_restart_but_not_missing_volume(tmp_path):
    _, _, reconciler, order, trade = setup_buy(tmp_path)
    report = sync(reconciler, replace(order, order_type="MARKET"), [trade])
    supervisor = AccountingHealthSupervisor(tmp_path / "health.sqlite")
    supervisor.record_reports(position_id="position", reports=(report,), now=1101)
    restarted = AccountingHealthSupervisor(supervisor.path)
    decision = restarted.assess(position_id="position", now=1102)
    assert decision.state.value == "HOLD"
    assert "protected_buy_requires_reconciliation" in decision.reasons
    lagging = sync(reconciler, replace(order, filled_amount=D("3")), [trade])
    supervisor.record_reports(position_id="position", reports=(lagging,), now=1103)
    decision = restarted.assess(position_id="position", now=1104)
    assert "protected_buy_requires_reconciliation" not in decision.reasons
