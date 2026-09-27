import asyncio
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.executor_provenance import (
    synchronize_executor_economic_ledger,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    SPECTRA_HUMMINGBOT_API_REVISION,
    executor_order_snapshot_from_raw,
    executor_trade_snapshot_from_raw,
    synchronize_executor_evidence,
)
from spectra_core.fill_reconciliation import LiveFillLedgerReconciler
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
        tmp_path / "bridge-governor.sqlite3",
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
    proposal_id="entry-bridge-1",
    executor_id="exec-bridge-1",
    intent=IntentKind.ENTRY,
    amount="20",
):
    proposal = CapitalProposal(
        proposal_id=proposal_id,
        pair="XRP-USD",
        intent=intent,
        amount_quote=D(amount),
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
    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id=executor_id,
        account_name="master_account",
        connector_name="kraken",
        controller_id="spectra",
    )
    return proposal


def ledger(tmp_path):
    book = EconomicPositionLedger(tmp_path / "bridge-economic.sqlite3")
    book.open_position(
        position_id="position-1",
        pair="XRP-USD",
        created_at=1_000,
    )
    return book


def order_row(
    order_id,
    side,
    *,
    executor_id="exec-bridge-1",
    created_at="2026-09-27T18:00:00+00:00",
):
    return {
        "executor_id": executor_id,
        "client_order_id": order_id,
        "exchange_order_id": f"kraken-{order_id}",
        "order_type": "open" if side == "BUY" else "take_profit",
        "trade_type": side,
        "amount": "20",
        "price": "0.50" if side == "BUY" else "0.60",
        "status": "FILLED",
        "filled_amount": "20",
        "average_fill_price": "0.50" if side == "BUY" else "0.60",
        "created_at": created_at,
        "updated_at": created_at,
    }


def trade_row(
    trade_id,
    order_id,
    side,
    *,
    amount="20",
    price=None,
    fee="0",
    fee_currency=None,
    executor_id="exec-bridge-1",
    timestamp="2026-09-27T18:01:00+00:00",
):
    return {
        "executor_id": executor_id,
        "order_id": order_id,
        "trade_id": trade_id,
        "account_name": "master_account",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "trade_type": side,
        "amount": amount,
        "price": price or ("0.50" if side == "BUY" else "0.60"),
        "fee_paid": fee,
        "fee_currency": fee_currency,
        "timestamp": timestamp,
    }


def snapshots(orders, trades):
    return (
        executor_order_snapshot_from_raw(
            {
                "executor_id": "exec-bridge-1",
                "count": len(orders),
                "orders": orders,
            },
            expected_executor_id="exec-bridge-1",
        ),
        executor_trade_snapshot_from_raw(
            {
                "executor_id": "exec-bridge-1",
                "count": len(trades),
                "trades": trades,
            },
            expected_executor_id="exec-bridge-1",
        ),
    )


def test_bridge_is_pinned_to_exact_companion_api_revision():
    assert (
        SPECTRA_HUMMINGBOT_API_REVISION
        == "84426ffbf9ba09522b4444664f9770236205ae29"
    )


def test_order_snapshot_rejects_count_mismatch():
    with pytest.raises(ValueError, match="count does not match"):
        executor_order_snapshot_from_raw(
            {
                "executor_id": "exec-bridge-1",
                "count": 2,
                "orders": [order_row("buy-1", "BUY")],
            },
            expected_executor_id="exec-bridge-1",
        )


def test_trade_snapshot_rejects_cross_executor_fill():
    with pytest.raises(ValueError, match="another executor"):
        executor_trade_snapshot_from_raw(
            {
                "executor_id": "exec-bridge-1",
                "count": 1,
                "trades": [
                    trade_row(
                        "trade-1",
                        "buy-1",
                        "BUY",
                        executor_id="exec-other",
                    )
                ],
            },
            expected_executor_id="exec-bridge-1",
        )


def test_exact_entry_buy_and_sell_sync_end_to_end_without_touching_retained(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    book.record_strategic_retained(
        lot_id="retained-43",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("20"),
        occurred_at=1_100,
    )
    reconciler = LiveFillLedgerReconciler(ledger=book, governor=g)

    orders, trades = snapshots(
        [
            order_row("buy-1", "BUY"),
            order_row(
                "sell-1",
                "SELL",
                created_at="2026-09-27T18:02:00+00:00",
            ),
        ],
        [
            trade_row("trade-buy", "buy-1", "BUY"),
            trade_row(
                "trade-sell",
                "sell-1",
                "SELL",
                timestamp="2026-09-27T18:03:00+00:00",
            ),
        ],
    )

    report = synchronize_executor_evidence(
        reconciler=reconciler,
        position_id="position-1",
        proposal_id="entry-bridge-1",
        order_snapshot=orders,
        trade_snapshot=trades,
        now=2_000,
    )

    assert report.known_orders == 2
    assert report.bound_orders == 2
    assert report.confirmed_fills == 2
    assert report.ledgered_fills == 2
    assert report.applied_fills == 2
    assert report.replayed_fills == 0
    assert report.unresolved_order_ids == ()
    assert report.unresolved_trade_ids == ()
    assert report.fully_reconciled is True

    snapshot = book.snapshot("position-1")
    assert snapshot.active_base == D("0")
    assert snapshot.realized_pnl_quote == D("2.00")
    assert snapshot.strategic_retained_base_same_pair == D("43")
    assert book.get_lot("retained-43").base_remaining == D("43")


def test_restart_sync_is_idempotent_and_reports_replayed_fills(tmp_path):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=book, governor=g)

    orders, trades = snapshots(
        [
            order_row("buy-replay", "BUY"),
            order_row(
                "sell-replay",
                "SELL",
                created_at="2026-09-27T18:02:00+00:00",
            ),
        ],
        [
            trade_row("trade-buy-replay", "buy-replay", "BUY"),
            trade_row(
                "trade-sell-replay",
                "sell-replay",
                "SELL",
                timestamp="2026-09-27T18:03:00+00:00",
            ),
        ],
    )

    first = synchronize_executor_evidence(
        reconciler=reconciler,
        position_id="position-1",
        proposal_id="entry-bridge-1",
        order_snapshot=orders,
        trade_snapshot=trades,
        now=2_000,
    )
    second = synchronize_executor_evidence(
        reconciler=reconciler,
        position_id="position-1",
        proposal_id="entry-bridge-1",
        order_snapshot=orders,
        trade_snapshot=trades,
        now=2_100,
    )

    assert first.applied_fills == 2
    assert second.applied_fills == 0
    assert second.replayed_fills == 2
    assert second.ledgered_fills == 2
    assert second.fully_reconciled is True
    assert book.snapshot("position-1").realized_pnl_quote == D("2.00")


def test_confirmed_fill_for_unknown_executor_order_fails_before_mutation(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=book, governor=g)

    orders, trades = snapshots(
        [order_row("known-buy", "BUY")],
        [trade_row("unknown-trade", "unknown-order", "BUY")],
    )

    with pytest.raises(ValueError, match="unknown executor order"):
        synchronize_executor_evidence(
            reconciler=reconciler,
            position_id="position-1",
            proposal_id="entry-bridge-1",
            order_snapshot=orders,
            trade_snapshot=trades,
            now=2_000,
        )

    snapshot = book.snapshot("position-1")
    assert snapshot.active_base == D("0")
    assert reconciler.get_binding("known-buy") is None


def test_sell_without_owned_source_lot_is_reported_unresolved(tmp_path):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)
    reconciler = LiveFillLedgerReconciler(ledger=book, governor=g)

    orders, trades = snapshots(
        [order_row("sell-pending", "SELL")],
        [],
    )

    report = synchronize_executor_evidence(
        reconciler=reconciler,
        position_id="position-1",
        proposal_id="entry-bridge-1",
        order_snapshot=orders,
        trade_snapshot=trades,
        now=2_000,
    )

    assert report.known_orders == 1
    assert report.bound_orders == 0
    assert report.confirmed_fills == 0
    assert report.unresolved_order_ids == ("sell-pending",)
    assert report.fully_reconciled is False


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, order_payload, *, fail_trades=False):
        self.order_payload = order_payload
        self.fail_trades = fail_trades
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        if url.endswith("/orders"):
            return FakeResponse(self.order_payload)
        if url.endswith("/trades") and self.fail_trades:
            raise RuntimeError("trade provenance backend unavailable")
        raise AssertionError(f"unexpected URL: {url}")


class FakeExecutors:
    def __init__(self, session):
        self.session = session
        self.base_url = "http://hummingbot.test"


class FakeClient:
    def __init__(self, session):
        self.executors = FakeExecutors(session)


def test_backend_failure_cannot_half_bind_or_mutate_ledger(tmp_path):
    g = governor(tmp_path)
    prepare_execution(g)
    book = ledger(tmp_path)

    order_payload = {
        "executor_id": "exec-bridge-1",
        "count": 1,
        "orders": [order_row("buy-outage", "BUY")],
    }
    session = FakeSession(order_payload, fail_trades=True)

    with pytest.raises(
        RuntimeError,
        match="executor trade provenance unavailable",
    ):
        asyncio.run(
            synchronize_executor_economic_ledger(
                FakeClient(session),
                position_id="position-1",
                proposal_id="entry-bridge-1",
                governor=g,
                ledger=book,
                now=2_000,
            )
        )

    snapshot = book.snapshot("position-1")
    assert snapshot.active_base == D("0")

    # The reconciler tables may not even exist because mutation construction is
    # deliberately after both network fetches. Economic inventory is unchanged.
    check = LiveFillLedgerReconciler(ledger=book, governor=g)
    assert check.get_binding("buy-outage") is None
