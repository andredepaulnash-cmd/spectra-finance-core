from decimal import Decimal

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    FillReconciliationStatus,
    LiveFillLedgerReconciler,
    trade_fill_evidence_from_raw,
)
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "fill-governor.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("10000"),
            max_pair_quote=D("10000"),
            max_position_quote=D("10000"),
            max_recovery_quote=D("5000"),
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
    account_name="master_account",
    connector_name="kraken",
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
            free_quote=D("10000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert decision.status == DecisionStatus.RESERVED
    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id=executor_id,
        account_name=account_name,
        connector_name=connector_name,
        controller_id="spectra",
    )
    return proposal


def book(tmp_path, *, position_id="position-1", pair="XRP-USD"):
    ledger = EconomicPositionLedger(tmp_path / "economic-fill.sqlite3")
    ledger.open_position(
        position_id=position_id,
        pair=pair,
        created_at=1_000,
    )
    return ledger


def fill(
    *,
    trade_id,
    order_id,
    side,
    amount,
    price,
    fee="0",
    fee_currency=None,
    pair="XRP-USD",
    account_name="master_account",
    connector_name="kraken",
    timestamp="2026-09-27T18:00:00+00:00",
):
    return {
        "trade_id": trade_id,
        "order_id": order_id,
        "account_name": account_name,
        "connector_name": connector_name,
        "trading_pair": pair,
        "trade_type": side,
        "amount": amount,
        "price": price,
        "fee_paid": fee,
        "fee_currency": fee_currency,
        "timestamp": timestamp,
    }


def bind(
    reconciler,
    *,
    order_id,
    role,
    proposal_id,
    position_id="position-1",
    pair="XRP-USD",
):
    return reconciler.bind_executor_order(
        order_id=order_id,
        position_id=position_id,
        role=role,
        proposal_id=proposal_id,
        account_name="master_account",
        connector_name="kraken",
        pair=pair,
        created_at=1_100,
    )


def test_parser_matches_pinned_hummingbot_trade_history_shape():
    evidence = trade_fill_evidence_from_raw(
        fill(
            trade_id="trade-1",
            order_id="client-order-1",
            side="BUY",
            amount="20",
            price="0.50",
            fee="0.02",
            fee_currency="USD",
        )
    )

    assert evidence.trade_id == "trade-1"
    assert evidence.order_id == "client-order-1"
    assert evidence.pair == "XRP-USD"
    assert evidence.side == "BUY"
    assert evidence.amount_base == D("20")
    assert evidence.price == D("0.50")
    assert evidence.fee_paid == D("0.02")
    assert evidence.fee_currency == "USD"
    assert evidence.quote_amount == D("10.00")


def test_missing_confirmed_fee_field_fails_closed():
    raw = fill(
        trade_id="trade-no-fee",
        order_id="order-no-fee",
        side="BUY",
        amount="1",
        price="1",
    )
    del raw["fee_paid"]

    with pytest.raises(ValueError, match="confirmed fee_paid"):
        trade_fill_evidence_from_raw(raw)


def test_unbound_trade_fill_cannot_mutate_economic_ledger(tmp_path):
    g = governor(tmp_path)
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    result = reconciler.reconcile_trade_fill(
        fill(
            trade_id="manual-fill",
            order_id="manual-order",
            side="BUY",
            amount="43",
            price="0.50",
            fee="0",
        )
    )

    assert result.status == FillReconciliationStatus.UNBOUND
    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("0")
    assert snapshot.active_cost_basis_quote == D("0")


def test_confirmed_entry_fill_creates_exact_active_trade_lot(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-1",
        executor_id="exec-entry-1",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    bind(
        reconciler,
        order_id="entry-order-1",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-1",
    )

    result = reconciler.reconcile_trade_fill(
        fill(
            trade_id="entry-trade-1",
            order_id="entry-order-1",
            side="BUY",
            amount="20",
            price="0.50",
            fee="0.02",
            fee_currency="USD",
        )
    )

    assert result.status == FillReconciliationStatus.APPLIED
    assert result.result_id == "trade-fill:entry-trade-1"

    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("20")
    assert snapshot.active_cost_basis_quote == D("10.00")
    assert snapshot.active_entry_fees_quote == D("0.02")

    lot = ledger.get_lot("trade-fill:entry-trade-1")
    assert lot.proposal_id == "entry-1"
    assert lot.executor_id == "exec-entry-1"


def test_trade_fill_replay_does_not_double_book_inventory(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-replay",
        executor_id="exec-entry-replay",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    bind(
        reconciler,
        order_id="entry-replay-order",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-replay",
    )
    raw = fill(
        trade_id="entry-replay-trade",
        order_id="entry-replay-order",
        side="BUY",
        amount="20",
        price="0.50",
        fee="0.02",
        fee_currency="USD",
    )

    first = reconciler.reconcile_trade_fill(raw)
    second = reconciler.reconcile_trade_fill(raw)

    assert first.status == FillReconciliationStatus.APPLIED
    assert second.status == FillReconciliationStatus.REPLAY
    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("20")
    assert snapshot.active_cost_basis_quote == D("10.00")
    assert snapshot.active_entry_fees_quote == D("0.02")


def test_crash_after_lot_commit_before_receipt_is_replay_safe(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-crash",
        executor_id="exec-entry-crash",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    bind(
        reconciler,
        order_id="entry-crash-order",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-crash",
    )
    raw = fill(
        trade_id="entry-crash-trade",
        order_id="entry-crash-order",
        side="BUY",
        amount="20",
        price="0.50",
        fee="0",
    )

    reconciler.reconcile_trade_fill(raw)

    # Simulate a crash after the economic lot committed but before the fill
    # reconciliation receipt survived.
    with reconciler._connect() as conn:
        conn.execute(
            """
            DELETE FROM economic_trade_fill_reconciliation
            WHERE trade_id = ?
            """,
            ("entry-crash-trade",),
        )

    recovered = reconciler.reconcile_trade_fill(raw)
    assert recovered.status == FillReconciliationStatus.APPLIED

    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("20")
    assert snapshot.active_cost_basis_quote == D("10.00")


def test_multiple_vcap_trade_fills_share_one_proposal_without_duplicate_id(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="vcap-multi",
        executor_id="exec-vcap-multi",
        intent=IntentKind.RECOVERY,
        amount="100",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="vcap-order-a",
        role=EconomicFillRole.VCAP_RECOVERY,
        proposal_id="vcap-multi",
    )
    bind(
        reconciler,
        order_id="vcap-order-b",
        role=EconomicFillRole.VCAP_RECOVERY,
        proposal_id="vcap-multi",
    )

    reconciler.reconcile_trade_fill(
        fill(
            trade_id="vcap-trade-a",
            order_id="vcap-order-a",
            side="BUY",
            amount="60",
            price="1",
            fee="0",
        )
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="vcap-trade-b",
            order_id="vcap-order-b",
            side="BUY",
            amount="40",
            price="1",
            fee="0",
        )
    )

    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("100")
    assert snapshot.active_cost_basis_quote == D("100")
    assert snapshot.recovery_proposal_ids == ("vcap-multi",)


def test_multiple_fills_cannot_exceed_proposal_authorization(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="vcap-cap",
        executor_id="exec-vcap-cap",
        intent=IntentKind.RECOVERY,
        amount="100",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    for suffix in ("a", "b", "c"):
        bind(
            reconciler,
            order_id=f"cap-order-{suffix}",
            role=EconomicFillRole.VCAP_RECOVERY,
            proposal_id="vcap-cap",
        )

    reconciler.reconcile_trade_fill(
        fill(
            trade_id="cap-trade-a",
            order_id="cap-order-a",
            side="BUY",
            amount="60",
            price="1",
            fee="0",
        )
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="cap-trade-b",
            order_id="cap-order-b",
            side="BUY",
            amount="40",
            price="1",
            fee="0",
        )
    )

    with pytest.raises(
        ValueError,
        match="exceed proposal authorized quote",
    ):
        reconciler.reconcile_trade_fill(
            fill(
                trade_id="cap-trade-c",
                order_id="cap-order-c",
                side="BUY",
                amount="1",
                price="1",
                fee="0",
            )
        )

    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_cost_basis_quote == D("100")


def test_quote_fee_and_base_fee_are_accounted_without_estimation(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-fees",
        executor_id="exec-entry-fees",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="fee-order-quote",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-fees",
    )
    bind(
        reconciler,
        order_id="fee-order-base",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-fees",
    )

    reconciler.reconcile_trade_fill(
        fill(
            trade_id="fee-trade-quote",
            order_id="fee-order-quote",
            side="BUY",
            amount="10",
            price="0.50",
            fee="0.01",
            fee_currency="USD",
        )
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="fee-trade-base",
            order_id="fee-order-base",
            side="BUY",
            amount="10",
            price="0.50",
            fee="0.10",
            fee_currency="XRP",
        )
    )

    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("19.90")
    assert snapshot.active_cost_basis_quote == D("10.00")
    assert snapshot.active_entry_fees_quote == D("0.01")


def test_third_currency_fee_fails_closed(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-third-fee",
        executor_id="exec-third-fee",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    bind(
        reconciler,
        order_id="third-fee-order",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-third-fee",
    )

    with pytest.raises(
        ValueError,
        match="third-currency trade fee",
    ):
        reconciler.reconcile_trade_fill(
            fill(
                trade_id="third-fee-trade",
                order_id="third-fee-order",
                side="BUY",
                amount="10",
                price="0.50",
                fee="0.1",
                fee_currency="KRAK",
            )
        )

    assert ledger.snapshot("position-1").active_base == D("0")


def test_exit_fill_can_only_consume_lots_owned_by_its_exact_provenance(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-a",
        executor_id="exec-a",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    prepare_execution(
        g,
        proposal_id="entry-b",
        executor_id="exec-b",
        intent=IntentKind.ENTRY,
        amount="30",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="buy-a",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-a",
    )
    bind(
        reconciler,
        order_id="buy-b",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-b",
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="buy-trade-a",
            order_id="buy-a",
            side="BUY",
            amount="20",
            price="0.50",
            fee="0",
        )
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="buy-trade-b",
            order_id="buy-b",
            side="BUY",
            amount="30",
            price="0.50",
            fee="0",
        )
    )

    bind(
        reconciler,
        order_id="sell-a",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="entry-a",
    )

    with pytest.raises(
        ValueError,
        match="eligible ACTIVE_TRADE inventory",
    ):
        reconciler.reconcile_trade_fill(
            fill(
                trade_id="bad-sell-a",
                order_id="sell-a",
                side="SELL",
                amount="21",
                price="0.60",
                fee="0",
            )
        )

    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("50")


def test_exact_exit_closes_trade_without_touching_strategic_retained_xrp(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-owned",
        executor_id="exec-owned",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="owned-buy",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-owned",
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="owned-buy-trade",
            order_id="owned-buy",
            side="BUY",
            amount="20",
            price="0.50",
            fee="0",
        )
    )
    ledger.record_strategic_retained(
        lot_id="retained-43",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("20"),
        occurred_at=1_200,
    )

    bind(
        reconciler,
        order_id="owned-sell",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="entry-owned",
    )
    result = reconciler.reconcile_trade_fill(
        fill(
            trade_id="owned-sell-trade",
            order_id="owned-sell",
            side="SELL",
            amount="20",
            price="0.60",
            fee="0.02",
            fee_currency="USD",
        )
    )

    assert result.status == FillReconciliationStatus.APPLIED
    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("0")
    assert snapshot.strategic_retained_base_same_pair == D("43")
    assert ledger.get_lot("retained-43").base_remaining == D("43")


def test_base_denom_sell_fee_consumes_only_owned_trade_base(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-base-fee",
        executor_id="exec-base-fee",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="base-fee-buy",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-base-fee",
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="base-fee-buy-trade",
            order_id="base-fee-buy",
            side="BUY",
            amount="20",
            price="0.50",
            fee="0",
        )
    )

    bind(
        reconciler,
        order_id="base-fee-sell",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="entry-base-fee",
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="base-fee-sell-trade",
            order_id="base-fee-sell",
            side="SELL",
            amount="19.9",
            price="0.60",
            fee="0.1",
            fee_currency="XRP",
        )
    )

    assert ledger.snapshot("position-1").active_base == D("0")


def test_sell_crash_before_reconciliation_receipt_is_replay_safe(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-sell-crash",
        executor_id="exec-sell-crash",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="sell-crash-buy",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-sell-crash",
    )
    reconciler.reconcile_trade_fill(
        fill(
            trade_id="sell-crash-buy-trade",
            order_id="sell-crash-buy",
            side="BUY",
            amount="20",
            price="0.50",
            fee="0",
        )
    )

    bind(
        reconciler,
        order_id="sell-crash-order",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="entry-sell-crash",
    )
    raw = fill(
        trade_id="sell-crash-trade",
        order_id="sell-crash-order",
        side="SELL",
        amount="20",
        price="0.60",
        fee="0",
    )
    reconciler.reconcile_trade_fill(raw)

    with reconciler._connect() as conn:
        conn.execute(
            """
            DELETE FROM economic_trade_fill_reconciliation
            WHERE trade_id = ?
            """,
            ("sell-crash-trade",),
        )

    recovered = reconciler.reconcile_trade_fill(raw)
    assert recovered.status == FillReconciliationStatus.APPLIED
    snapshot = ledger.snapshot("position-1")
    assert snapshot.active_base == D("0")
    assert snapshot.realized_pnl_quote == D("2.00")


def test_batch_reconciliation_applies_oldest_fill_first(tmp_path):
    g = governor(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-batch",
        executor_id="exec-batch",
        intent=IntentKind.ENTRY,
        amount="20",
    )
    ledger = book(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind(
        reconciler,
        order_id="batch-buy",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id="entry-batch",
    )

    rows = [
        fill(
            trade_id="batch-newer",
            order_id="batch-buy",
            side="BUY",
            amount="10",
            price="0.50",
            fee="0",
            timestamp="2026-09-27T18:02:00+00:00",
        ),
        fill(
            trade_id="batch-older",
            order_id="batch-buy",
            side="BUY",
            amount="10",
            price="0.50",
            fee="0",
            timestamp="2026-09-27T18:01:00+00:00",
        ),
    ]

    results = reconciler.reconcile_trade_fills(rows)

    assert [result.trade_id for result in results] == [
        "batch-older",
        "batch-newer",
    ]
    assert ledger.snapshot("position-1").active_base == D("20")
