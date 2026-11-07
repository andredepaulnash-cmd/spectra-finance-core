import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from spectra_core.capital_governor import CapitalGovernor
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
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    ExecutorAccountingObservation,
    IntentKind,
)

D = Decimal


def fixture(
    tmp_path,
    *,
    remaining="0.01",
    empty=False,
    intent=IntentKind.ENTRY,
    guarded_empty_noorders=False,
    guarded_recovery=False
):
    # Exact ISO round-trip fixture times; do not weaken production receipt
    # identity checks to hide sub-microsecond float fixture discrepancies.
    now = float(int(time.time()))
    governor = CapitalGovernor(
        tmp_path / "governor.db",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("1000"),
            max_leverage=D("1"),
        ),
    )
    ledger = EconomicPositionLedger(tmp_path / "ledger.db")
    ledger.open_position(position_id="position", pair="XRP-USD", created_at=now - 10)
    if guarded_recovery:
        entry = CapitalProposal("entry-owner", "XRP-USD", IntentKind.ENTRY, D("10"))
        governor.reserve(entry, CapitalSnapshot(free_quote=D("5000")))
        assert governor.commit_entry_submission_if_matches(
            entry,
            position_id="position",
            ledger_path=ledger.path,
            account_name="account",
            connector_name="kraken",
            controller_id="spectra",
        )
        governor.reconcile_execution(
            proposal_id="entry-owner",
            executor_id="entry-executor",
            account_name="account",
            connector_name="kraken",
            controller_id="spectra",
        )
        governor.reconcile_executor_accounting(
            ExecutorAccountingObservation(
                "entry-executor",
                "TERMINATED",
                D("0"),
                D("0"),
                position_size_quote=D("0"),
                close_type="FAILED",
                executor_type="dca_executor",
            )
        )
    proposal = CapitalProposal("owner", "XRP-USD", intent, D("100"))
    governor.reserve(proposal, CapitalSnapshot(free_quote=D("5000")))
    if guarded_empty_noorders:
        assert governor.commit_entry_submission_if_matches(
            proposal,
            position_id="position",
            ledger_path=ledger.path,
            account_name="account",
            connector_name="kraken",
            controller_id="spectra",
        )
    elif guarded_recovery:
        assert governor.commit_recovery_submission_if_matches(
            proposal,
            account_name="account",
            connector_name="kraken",
            controller_id="spectra",
            position_id="position",
        )
    else:
        governor.commit(proposal.proposal_id)
    governor.reconcile_execution(
        proposal_id="owner",
        executor_id="executor",
        account_name="account",
        connector_name="kraken",
        controller_id="spectra",
    )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    role = (
        EconomicFillRole.BASE_ENTRY
        if intent == IntentKind.ENTRY
        else EconomicFillRole.VCAP_RECOVERY
    )
    if not guarded_empty_noorders:
        reconciler.bind_executor_order(
            order_id="buy",
            position_id="position",
            role=role,
            pair="XRP-USD",
            account_name="account",
            connector_name="kraken",
            proposal_id="owner",
            created_at=now - 9,
        )
    orders = (
        []
        if guarded_empty_noorders
        else [
            ExecutorOrderEvidence(
                "executor",
                "buy",
                "LIMIT",
                "BUY",
                D("100"),
                D("0.5"),
                "CANCELED" if empty else "FILLED",
                D("0") if empty else D("100"),
                None,
                now - 9,
                now - 7,
            )
        ]
    )
    trades = []
    if not empty:
        bought = TradeFillEvidence(
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
            now - 8,
        )
        sold = TradeFillEvidence(
            "sell-fill",
            "sell",
            "account",
            "kraken",
            "XRP-USD",
            "SELL",
            D("100") - D(remaining),
            D("1"),
            D("0.2"),
            "USD",
            now - 6,
        )
        for trade in [bought, sold]:
            if trade.side == "SELL":
                reconciler.bind_executor_order(
                    order_id="sell",
                    position_id="position",
                    role=EconomicFillRole.POSITION_EXIT,
                    pair="XRP-USD",
                    account_name="account",
                    connector_name="kraken",
                    proposal_id="owner",
                    created_at=now - 7,
                )
            reconciler.reconcile_trade_fill(
                {
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "account_name": trade.account_name,
                    "connector_name": trade.connector_name,
                    "trading_pair": trade.pair,
                    "trade_type": trade.side,
                    "amount": str(trade.amount_base),
                    "price": str(trade.price),
                    "fee_paid": str(trade.fee_paid),
                    "fee_currency": trade.fee_currency,
                    "timestamp": datetime.fromtimestamp(
                        trade.occurred_at, timezone.utc
                    ).isoformat(),
                },
                reconciled_at=now - 5,
            )
        trades = [bought, sold]
        orders.append(
            ExecutorOrderEvidence(
                "executor",
                "sell",
                "LIMIT",
                "SELL",
                sold.amount_base,
                D("1"),
                "FILLED",
                sold.amount_base,
                D("1"),
                now - 7,
                now - 6,
            )
        )
    governor.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="executor",
            status="TERMINATED",
            filled_amount_quote=D("0") if empty else D("149.99"),
            cum_fees_quote=D("0") if empty else D("0.3"),
            position_size_quote=D("0") if empty else D("0.005"),
            close_type="FAILED" if empty else "POSITION_HOLD",
            executor_type="dca_executor",
        )
    )
    now = time.time()
    evidence = TerminalExecutorEvidence(
        "owner",
        ExecutorOrderSnapshot("executor", tuple(orders)),
        ExecutorTradeSnapshot("executor", tuple(trades)),
        now,
    )
    rule = KrakenTradingRule(
        "XRP-USD", D("1"), D("1"), D("0"), D("0.01"), D("0.1"), D("0.01"), True, True
    )
    all_evidence = (evidence,)
    if guarded_recovery:
        all_evidence += (
            TerminalExecutorEvidence(
                "entry-owner",
                ExecutorOrderSnapshot("entry-executor", ()),
                ExecutorTradeSnapshot("entry-executor", ()),
                now,
            ),
        )
    kwargs = dict(
        event_id="terminal-proof",
        position_id="position",
        governor=governor,
        executor_evidence=all_evidence,
        trading_rule=rule,
        current_price=D("1"),
        rules_observed_at=now,
        price_observed_at=now,
        now=now,
    )
    return governor, ledger, kwargs


def test_terminal_dust_preserves_lots_basis_fees_and_rotates_unique_slot(tmp_path):
    governor, ledger, kwargs = fixture(tmp_path)
    before = ledger.get_lot("trade-fill:buy-fill")
    certificate = ledger.retire_terminal_position(**kwargs)
    assert certificate["kind"] == "RESIDUAL"
    assert certificate["capital_released_quote"] == "0"
    assert ledger.get_lot(before.lot_id) == before
    assert ledger.snapshot("position").status == EconomicPositionStatus.RESIDUAL
    assert ledger.list_active_position_ids() == ()
    assert ledger.list_residual_position_ids() == ("position",)
    restarted = EconomicPositionLedger(ledger.path)
    assert restarted.get_retirement_certificate("position") == certificate
    assert restarted.get_lot(before.lot_id) == before
    with pytest.raises(ValueError, match="closed economic position"):
        restarted.record_base_fill(
            position_id="position",
            lot_id="late-buy",
            pair="XRP-USD",
            base_amount=D("1"),
            cost_basis_quote=D("1"),
            occurred_at=kwargs["now"] + 2,
        )
    restarted.open_position(
        position_id="replacement", pair="XXRP-USDC", created_at=kwargs["now"] + 1
    )
    with pytest.raises(ValueError, match="already has ACTIVE"):
        restarted.open_position(
            position_id="duplicate", pair="XRP-USD", created_at=kwargs["now"] + 2
        )
    assert restarted.get_lot(before.lot_id) == before


def test_terminal_vcap_dust_keeps_recovery_owner_and_exposure(tmp_path):
    governor, ledger, kwargs = fixture(tmp_path, intent=IntentKind.RECOVERY)
    before_exposure = governor.current_recovery_exposure_quote()
    lot = ledger.get_lot("trade-fill:buy-fill")
    ledger.retire_terminal_position(**kwargs)
    assert ledger.get_lot(lot.lot_id) == lot
    assert ledger.snapshot("position").recovery_proposal_ids == ("owner",)
    assert governor.current_recovery_exposure_quote() == before_exposure


def test_guarded_recovery_flat_executor_label_cannot_free_known_residual_basis(
    tmp_path,
):
    governor, ledger, kwargs = fixture(
        tmp_path, intent=IntentKind.RECOVERY, guarded_recovery=True
    )
    governor.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            "executor",
            "TERMINATED",
            D("149.99"),
            D("0.3"),
            position_size_quote=D("0"),
            close_type="COMPLETED",
            executor_type="dca_executor",
        )
    )
    kwargs["now"] = time.time()
    remaining_basis = ledger.snapshot("position").active_cost_basis_quote
    assert remaining_basis > 0
    assert governor.current_recovery_exposure_quote() >= remaining_basis
    ledger.retire_terminal_position(**kwargs)
    assert ledger.snapshot("position").status == EconomicPositionStatus.RESIDUAL
    assert governor.current_recovery_exposure_quote() >= remaining_basis


def test_empty_placeholder_closes_only_with_exact_terminal_nofill_proof(tmp_path):
    _, ledger, kwargs = fixture(tmp_path, empty=True)
    assert ledger.bound_proposal_ids("position") == ("owner",)
    assert ledger.list_pending_retirement_position_ids() == ("position",)
    certificate = ledger.retire_terminal_position(**kwargs)
    assert certificate["kind"] == "EMPTY_TERMINAL"
    assert ledger.snapshot("position").status == EconomicPositionStatus.CLOSED
    assert ledger.list_pending_retirement_position_ids() == ()


def test_guarded_receipt_bound_entry_zero_orders_can_retire(tmp_path):
    _, ledger, kwargs = fixture(tmp_path, empty=True, guarded_empty_noorders=True)
    assert kwargs["executor_evidence"][0].order_snapshot.orders == ()
    assert ledger.bound_proposal_ids("position") == ()
    assert ledger.retire_terminal_position(**kwargs)["kind"] == "EMPTY_TERMINAL"


def test_retirement_uses_actual_attached_ledger_when_capital_database_has_shadow_tables(
    tmp_path,
):
    governor, ledger, kwargs = fixture(tmp_path)
    shadow = EconomicPositionLedger(governor.db_path)
    shadow.open_position(
        position_id="position", pair="BTC-USD", created_at=kwargs["now"] - 20
    )
    shadow.record_base_fill(
        position_id="position",
        lot_id="shadow-owned",
        pair="BTC-USD",
        base_amount=D("2"),
        cost_basis_quote=D("100"),
        occurred_at=kwargs["now"] - 19,
    )
    before = shadow.snapshot("position")
    assert ledger.retire_terminal_position(**kwargs)["pair"] == "XRP-USD"
    assert shadow.snapshot("position") == before
    assert shadow.get_retirement_certificate("position") is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ("stale_rule", "stale"),
        ("stale_price", "stale"),
        ("running", "TERMINATED"),
        ("inflight_sell", "terminal"),
        ("missing_trade", "durably reconciled"),
        ("missing_order", "bound orders"),
        ("cumulative_mismatch", "cumulative fill"),
        ("tradable", "tradable"),
        ("uncertain_recovery", "protected asset submission"),
    ],
)
def test_uncertain_or_tradable_inventory_keeps_active_slot(tmp_path, change, reason):
    governor, ledger, kwargs = fixture(tmp_path)
    evidence = kwargs["executor_evidence"][0]
    if change == "stale_rule":
        kwargs["rules_observed_at"] -= 31
    elif change == "stale_price":
        kwargs["price_observed_at"] -= 31
    elif change == "running":
        governor.reconcile_executor_accounting(
            ExecutorAccountingObservation(
                "executor",
                "RUNNING",
                D("149.99"),
                D("0.3"),
                executor_type="dca_executor",
            )
        )
        kwargs["now"] = time.time()
    elif change == "inflight_sell":
        orders = evidence.order_snapshot.orders
        kwargs["executor_evidence"] = (
            replace(
                evidence,
                order_snapshot=replace(
                    evidence.order_snapshot,
                    orders=(orders[0], replace(orders[1], status="OPEN")),
                ),
            ),
        )
    elif change == "missing_trade":
        kwargs["executor_evidence"] = (
            replace(
                evidence,
                trade_snapshot=replace(
                    evidence.trade_snapshot, trades=evidence.trade_snapshot.trades[:1]
                ),
            ),
        )
    elif change == "missing_order":
        kwargs["executor_evidence"] = (
            replace(
                evidence,
                order_snapshot=replace(
                    evidence.order_snapshot, orders=evidence.order_snapshot.orders[:1]
                ),
            ),
        )
    elif change == "cumulative_mismatch":
        orders = evidence.order_snapshot.orders
        kwargs["executor_evidence"] = (
            replace(
                evidence,
                order_snapshot=replace(
                    evidence.order_snapshot,
                    orders=(
                        orders[0],
                        replace(orders[1], status="CANCELED", filled_amount=D("99.98")),
                    ),
                ),
            ),
        )
    elif change == "tradable":
        kwargs["trading_rule"] = replace(
            kwargs["trading_rule"],
            min_order_size=D("0.001"),
            min_notional_size=D("0.001"),
            min_base_amount_increment=D("0.001"),
        )
    elif change == "uncertain_recovery":
        proposal = CapitalProposal(
            "ambiguous", "XXRP-USDC", IntentKind.RECOVERY, D("5")
        )
        governor.reserve(proposal, CapitalSnapshot(free_quote=D("5000")))
        governor.commit("ambiguous")
    before = ledger.snapshot("position")
    with pytest.raises(ValueError, match=reason):
        ledger.retire_terminal_position(**kwargs)
    assert ledger.snapshot("position") == before
    assert ledger.get_retirement_certificate("position") is None
    with pytest.raises(ValueError, match="already has ACTIVE"):
        ledger.open_position(
            position_id="unsafe", pair="XRP-USDC", created_at=kwargs["now"] + 1
        )


def test_partial_dispatch_interlock_even_after_manifest_completed(tmp_path):
    _, ledger, kwargs = fixture(tmp_path)
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "CREATE TABLE partial_recycle_manifests(manifest_id TEXT,position_id TEXT,status TEXT)"
        )
        conn.execute(
            "CREATE TABLE partial_recycle_dispatch_claims(manifest_id TEXT PRIMARY KEY,claimed_at REAL)"
        )
        conn.execute(
            "INSERT INTO partial_recycle_manifests VALUES('partial','position','COMPLETED')"
        )
        conn.execute(
            "INSERT INTO partial_recycle_dispatch_claims VALUES('partial',?)",
            (kwargs["now"] - 1,),
        )
    with pytest.raises(ValueError, match="partial dispatch"):
        ledger.retire_terminal_position(**kwargs)
    assert ledger.snapshot("position").status == EconomicPositionStatus.ACTIVE


def test_exact_flat_waits_for_terminal_certificate_before_rotation_claim(tmp_path):
    _, ledger, kwargs = fixture(tmp_path, remaining="0")
    assert ledger.snapshot("position").status == EconomicPositionStatus.CLOSED
    assert ledger.list_pending_retirement_position_ids() == ("position",)
    assert ledger.retire_terminal_position(**kwargs)["kind"] == "FLAT_TERMINAL"
    assert ledger.list_pending_retirement_position_ids() == ()
