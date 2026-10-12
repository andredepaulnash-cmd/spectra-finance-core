"""Synthetic evidence only: no exchange, market-data download, or live route."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.dust_lifecycle import (
    DustLifecycle,
    preflight_rounded_sale,
    residual_geometry,
)
from spectra_core.economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
)
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
    synchronize_executor_evidence,
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


def rule(pair="XRP-USD", **changes):
    return replace(
        KrakenTradingRule(
            pair, D(".01"), D("1"), D("0"), D(".01"), D(".01"), D(".01"), True, True
        ),
        **changes,
    )


@pytest.fixture
def environment(tmp_path, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr("spectra_core.capital_governor.time.time", lambda: clock[0])
    governor = CapitalGovernor(
        tmp_path / "governor.sqlite",
        CapitalPolicy(D("0"), D("5000"), D("5000"), D("5000"), D("5000")),
    )
    ledger = EconomicPositionLedger(tmp_path / "ledger.sqlite")
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    lifecycle = DustLifecycle(ledger=ledger, governor=governor, enabled=True)
    return ledger, governor, reconciler, lifecycle, clock


def create_cycle(
    env,
    pid="p1",
    *,
    pair="XRP-USD",
    intent=IntentKind.ENTRY,
    sold="9.96",
    sell_status="CANCELED",
    open_position=True,
):
    ledger, governor, reconciler, lifecycle, clock = env
    now = clock[0]
    if open_position:
        ledger.open_position(position_id=pid, pair=pair, created_at=now)
    proposal, executor = f"proposal:{pid}", f"executor:{pid}"
    governor.reserve(
        CapitalProposal(proposal, pair, intent, D("100")), CapitalSnapshot(D("5000"))
    )
    governor.commit(proposal)
    governor.reconcile_execution(
        proposal_id=proposal,
        executor_id=executor,
        account_name="owner-account",
        connector_name="kraken",
        controller_id="spectra",
    )
    orders = ExecutorOrderSnapshot(
        executor,
        (
            ExecutorOrderEvidence(
                executor,
                f"buy:{pid}",
                "LIMIT",
                "BUY",
                D("10"),
                D("10"),
                "FILLED",
                D("10"),
                D("10"),
                now,
                now,
            ),
            ExecutorOrderEvidence(
                executor,
                f"sell:{pid}",
                "LIMIT",
                "SELL",
                D("10"),
                D("11"),
                sell_status,
                D(sold),
                D("11"),
                now,
                now,
            ),
        ),
    )
    trades = ExecutorTradeSnapshot(
        executor,
        (
            TradeFillEvidence(
                f"buy-fill:{pid}",
                f"buy:{pid}",
                "owner-account",
                "kraken",
                pair,
                "BUY",
                D("10"),
                D("10"),
                D(".1"),
                pair.split("-")[1],
                now,
            ),
            TradeFillEvidence(
                f"sell-fill:{pid}",
                f"sell:{pid}",
                "owner-account",
                "kraken",
                pair,
                "SELL",
                D(sold),
                D("11"),
                D(".1"),
                pair.split("-")[1],
                now,
            ),
        ),
    )
    report = synchronize_executor_evidence(
        reconciler=reconciler,
        position_id=pid,
        proposal_id=proposal,
        order_snapshot=orders,
        trade_snapshot=trades,
        now=now,
    )
    assert report.fully_reconciled
    governor.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor,
            "TERMINATED",
            D("100") + D(sold) * D("11"),
            D(".2"),
            position_size_quote=(D("10") - D(sold)) * D("10"),
            executor_type="grid_executor",
        )
    )
    return orders


def market(env, mid="market1", *, pair="XRP-USD", price="11", venue_rule=None):
    _, _, _, lifecycle, clock = env
    lifecycle.observe_market(
        market_id=mid,
        account_name="owner-account",
        connector_name="kraken",
        price=D(price),
        rule=venue_rule or rule(pair),
        observed_at=clock[0],
        now=clock[0],
    )


def checkpoint(env, orders, cid="checkpoint1", pid="p1", mid="market1"):
    _, _, _, lifecycle, clock = env
    return lifecycle.capture_checkpoint(
        checkpoint_id=cid,
        position_id=pid,
        market_id=mid,
        order_snapshots=(orders,),
        observed_at=clock[0],
        now=clock[0],
    )


def rows(book, table):
    with book._connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_default_off_cannot_observe_classify_or_open(environment):
    ledger, governor, _, _, clock = environment
    disabled = DustLifecycle(ledger=ledger, governor=governor)
    with pytest.raises(ValueError, match="disabled"):
        disabled.classify(checkpoint_id="absent", now=clock[0])
    with pytest.raises(ValueError, match="disabled"):
        disabled.observe_market(
            market_id="m",
            account_name="a",
            connector_name="kraken",
            price=D("11"),
            rule=rule(),
            observed_at=clock[0],
            now=clock[0],
        )
    with pytest.raises(ValueError, match="disabled"):
        disabled.open_successor(
            position_id="p", pair="XRP-USD", checkpoint_ids=("c",), now=clock[0]
        )


@pytest.mark.parametrize(
    "base,price,expected",
    [
        (".099", "11", False),
        (".10", "10", True),
        (".10", "9.999", False),
        (".0099", "10000", False),
    ],
)
def test_exact_quantity_price_and_notional_rounding(base, price, expected):
    result = residual_geometry(base=D(base), price=D(price), rule=rule())
    assert result.executable is expected
    assert result.rounded_base <= D(base)
    assert result.rounded_price <= D(price)


def test_rounding_does_not_use_low_ambient_precision():
    with localcontext() as context:
        context.prec = 3
        result = residual_geometry(
            base=D(".099999999999999999999999999"), price=D("10"), rule=rule()
        )
    assert result.rounded_base == D(".09")
    assert not result.executable


def test_unavailable_venue_is_not_dust():
    with pytest.raises(ValueError, match="supported LIMIT"):
        residual_geometry(
            base=D(".01"), price=D("10"), rule=rule(supports_limit_orders=False)
        )
    with pytest.raises(ValueError, match="positive"):
        residual_geometry(
            base=D(".01"), price=D("10"), rule=rule(min_base_amount_increment=D("0"))
        )


def test_rounded_remainder_preflight_rejects_hidden_dust():
    result = preflight_rounded_sale(
        owned_base=D("1.049"),
        requested_base=D(".95"),
        price=D("10"),
        rule=rule(min_order_size=D(".2"), min_base_amount_increment=D(".1")),
    )
    assert result.sale.rounded_base == D(".9")
    assert result.remainder.owned_base == D(".149")
    assert not result.allowed
    assert "rounded_sale_would_leave_untradeable_residual" in result.reasons
    exact = preflight_rounded_sale(
        owned_base=D("1"), requested_base=D("1"), price=D("10"), rule=rule()
    )
    assert exact.allowed and exact.remainder.owned_base == 0


def test_dust_preserves_all_ownership_basis_inventory_and_cash_history(environment):
    ledger, governor, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    ledger.record_strategic_retained(
        lot_id="strategic", pair="XRP-USD", base_amount=D("100"), occurred_at=clock[0]
    )
    ledger.record_unattributed(
        lot_id="manual", pair="XRP-USD", base_amount=D("100"), occurred_at=clock[0]
    )
    market(environment)
    checkpoint(environment, orders)
    lots_before, events_before = rows(ledger, "economic_lots"), rows(
        ledger, "economic_position_events"
    )
    releases_before = rows(governor, "recovery_recycle_releases")
    result = lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    assert not result.executable and result.owned_base == D(".04")
    assert rows(ledger, "economic_lots") == lots_before
    assert rows(ledger, "economic_position_events") == events_before
    assert rows(governor, "recovery_recycle_releases") == releases_before
    snap = ledger.snapshot("p1")
    assert snap.status == EconomicPositionStatus.DUST
    assert snap.active_cost_basis_quote == D(".4")
    assert snap.active_entry_fees_quote == D(".0004")
    assert snap.strategic_retained_base_same_pair == 100
    assert snap.unattributed_base_same_pair == 100
    assert ledger.list_active_position_ids() == ()
    assert ledger.list_inventory_position_ids() == ("p1",)
    valuation = lifecycle.valuations(market_ids={"p1": "market1"}, now=clock[0])[0]
    assert valuation.marked_value_quote == D(".44")
    assert valuation.unrealized_pnl_quote == D(".0396")
    with pytest.raises(ValueError, match="not ACTIVE"):
        snap.to_vcap_position(current_price=D("11"))


def test_restart_and_replays_do_not_duplicate_classification_or_ownership(environment):
    ledger, governor, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    first = checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id=first.checkpoint_id, now=clock[0])
    restarted = DustLifecycle(
        ledger=EconomicPositionLedger(ledger.path), governor=governor, enabled=True
    )
    restarted.classify(checkpoint_id=first.checkpoint_id, now=clock[0])
    assert checkpoint(environment, orders) == first
    restarted.open_successor(
        position_id="p2",
        pair="XRP-USD",
        checkpoint_ids=(first.checkpoint_id,),
        now=clock[0],
    )
    restarted.open_successor(
        position_id="p2",
        pair="XRP-USD",
        checkpoint_ids=(first.checkpoint_id,),
        now=clock[0] + 1000,
    )
    assert ledger.list_active_position_ids() == ("p2",)
    assert len(rows(ledger, "economic_dust_transitions")) == 1
    assert len(rows(ledger, "economic_dust_successors")) == 1
    assert ledger.get_lot("trade-fill:buy-fill:p1").position_id == "p1"
    with pytest.raises(ValueError, match="replay conflicts"):
        restarted.open_successor(
            position_id="p2",
            pair="XRP-EUR",
            checkpoint_ids=(first.checkpoint_id,),
            now=clock[0],
        )


@pytest.mark.parametrize(
    "status", ["OPEN", "PARTIALLY_FILLED", "PENDING_CANCEL", "UNKNOWN"]
)
def test_pending_and_unknown_orders_cannot_release_slot(environment, status):
    orders = create_cycle(environment, sell_status=status)
    market(environment)
    with pytest.raises(ValueError, match="pending or unknown order"):
        checkpoint(environment, orders)
    assert environment[0].snapshot("p1").status == EconomicPositionStatus.ACTIVE


def test_live_executor_cannot_retire_even_with_terminal_orders(environment):
    orders = create_cycle(environment)
    with environment[1]._connect() as conn:
        conn.execute("UPDATE executor_accounting_observations SET status = 'RUNNING'")
    market(environment)
    with pytest.raises(ValueError, match="not durably terminated"):
        checkpoint(environment, orders)


def test_unreconciled_partial_fill_blocks_checkpoint(environment):
    orders = create_cycle(environment)
    orders = replace(
        orders,
        orders=(orders.orders[0], replace(orders.orders[1], filled_amount=D("9.97"))),
    )
    market(environment)
    with pytest.raises(ValueError, match="unreconciled partial fills"):
        checkpoint(environment, orders)


def test_unjournaled_ledger_mutation_blocks_checkpoint(environment):
    orders = create_cycle(environment)
    with environment[0]._connect() as conn:
        conn.execute(
            "DELETE FROM economic_trade_fill_reconciliation WHERE side = 'SELL'"
        )
    market(environment)
    with pytest.raises(ValueError, match="not fully reconciled"):
        checkpoint(environment, orders)


@pytest.mark.parametrize("after_classify", [False, True])
def test_new_reservation_invalidates_checkpoint_before_slot_or_reentry(
    environment, after_classify
):
    ledger, governor, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    if after_classify:
        lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    governor.reserve(
        CapitalProposal("racing-reservation", "XRP-EUR", IntentKind.ENTRY, D("10")),
        CapitalSnapshot(D("5000")),
    )
    with pytest.raises(ValueError, match="changed durable state"):
        if after_classify:
            lifecycle.open_successor(
                position_id="p2",
                pair="XRP-USD",
                checkpoint_ids=("checkpoint1",),
                now=clock[0],
            )
        else:
            lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    with pytest.raises(ValueError, match="unknown economic position"):
        ledger.snapshot("p2")


def test_pending_reservation_present_at_capture_is_blocked(environment):
    orders = create_cycle(environment)
    governor = environment[1]
    governor.reserve(
        CapitalProposal("pending", "XRP-USD", IntentKind.RECOVERY, D("10")),
        CapitalSnapshot(D("5000")),
    )
    market(environment)
    with pytest.raises(ValueError, match="reservation remains pending"):
        checkpoint(environment, orders)


def test_new_binding_after_checkpoint_is_a_race_and_invalidates_it(environment):
    ledger, _, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    with ledger._connect() as conn:
        conn.execute("""INSERT INTO economic_fill_bindings
            SELECT 'late-order', position_id, role, pair, account_name, connector_name, proposal_id, executor_id, created_at
            FROM economic_fill_bindings LIMIT 1""")
    with pytest.raises(ValueError, match="changed durable state"):
        lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])


def test_classification_transaction_locks_governor_against_pending_race(
    environment, monkeypatch
):
    _, governor, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    original = lifecycle._check
    attempted = []

    def checking(conn, cid, now):
        concurrent = sqlite3.connect(governor.db_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                concurrent.execute("BEGIN IMMEDIATE")
            attempted.append(True)
        finally:
            concurrent.close()
        return original(conn, cid, now)

    monkeypatch.setattr(lifecycle, "_check", checking)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    assert attempted == [True]


def test_changed_lot_rejects_captured_checkpoint(environment):
    ledger, _, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    with ledger._connect() as conn:
        conn.execute("UPDATE economic_lots SET base_remaining = '.05'")
    with pytest.raises(ValueError, match="changed durable state"):
        lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])


def test_recovery_obligation_is_not_erased_to_make_dust(environment):
    orders = create_cycle(environment, intent=IntentKind.RECOVERY)
    market(environment)
    with pytest.raises(ValueError, match="unreleased recovery obligation"):
        checkpoint(environment, orders)
    assert environment[0].snapshot("p1").status == EconomicPositionStatus.ACTIVE
    assert rows(environment[1], "recovery_recycle_releases") == []


def test_unowned_or_foreign_provenance_cannot_be_dust(environment):
    orders = create_cycle(environment)
    market(environment)
    with environment[0]._connect() as conn:
        conn.execute("UPDATE economic_lots SET executor_id = 'foreign'")
    with pytest.raises(ValueError, match="exact scoped owner"):
        checkpoint(environment, orders)


def test_missing_order_snapshot_and_omitted_binding_fail_closed(environment):
    orders = create_cycle(environment)
    market(environment)
    with pytest.raises(ValueError, match="cover exact durable bindings"):
        checkpoint(environment, replace(orders, orders=orders.orders[:1]))


def test_stale_or_future_evidence_does_not_release_slot(environment):
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    lifecycle, clock = environment[3:]
    with pytest.raises(ValueError, match="stale"):
        lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0] + 31)
    with pytest.raises(ValueError, match="future"):
        lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0] - 1)


@pytest.mark.parametrize("threshold_change", [False, True])
def test_price_or_venue_change_reclassifies_and_blocks_entry(
    environment, threshold_change
):
    ledger, _, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    clock[0] += 1
    market(
        environment,
        "market2",
        price="11" if threshold_change else "30",
        venue_rule=rule(min_notional_size=D(".1")) if threshold_change else None,
    )
    assert ledger.snapshot("p1").status == EconomicPositionStatus.DUST_REVIEW
    with pytest.raises(ValueError, match="requires review"):
        lifecycle.open_successor(
            position_id="p2",
            pair="XRP-USD",
            checkpoint_ids=("checkpoint1",),
            now=clock[0],
        )
    with pytest.raises(ValueError, match="guarded successor"):
        ledger.open_position(position_id="p2", pair="XRP-USD", created_at=clock[0])
    checkpoint(environment, orders, cid="checkpoint2", mid="market2")
    with pytest.raises(ValueError, match="remains executable"):
        lifecycle.classify(checkpoint_id="checkpoint2", now=clock[0])


def test_reclassification_does_not_move_old_lots_to_existing_successor(environment):
    ledger, _, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    lifecycle.open_successor(
        position_id="p2", pair="XRP-USD", checkpoint_ids=("checkpoint1",), now=clock[0]
    )
    clock[0] += 1
    market(environment, "market2", price="30")
    assert ledger.snapshot("p2").status == EconomicPositionStatus.ACTIVE
    assert ledger.snapshot("p2").active_base == 0
    assert ledger.snapshot("p1").status == EconomicPositionStatus.DUST_REVIEW
    assert ledger.snapshot("p1").active_base == D(".04")
    assert ledger.list_inventory_position_ids() == ("p1", "p2")


def test_repeated_partial_sell_dust_reentry_preserves_multiple_predecessors(
    environment,
):
    ledger, _, _, lifecycle, clock = environment
    first = create_cycle(environment)
    market(environment)
    checkpoint(environment, first)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    lifecycle.open_successor(
        position_id="p2", pair="XRP-USD", checkpoint_ids=("checkpoint1",), now=clock[0]
    )
    second = create_cycle(environment, "p2", open_position=False)
    checkpoint(environment, second, cid="checkpoint2", pid="p2")
    lifecycle.classify(checkpoint_id="checkpoint2", now=clock[0])
    checkpoint(environment, first, cid="checkpoint1-fresh")
    with pytest.raises(ValueError, match="every same-base"):
        lifecycle.open_successor(
            position_id="p3",
            pair="XRP-USD",
            checkpoint_ids=("checkpoint2",),
            now=clock[0],
        )
    lifecycle.open_successor(
        position_id="p3",
        pair="XRP-USD",
        checkpoint_ids=("checkpoint1-fresh", "checkpoint2"),
        now=clock[0],
    )
    assert ledger.list_active_position_ids() == ("p3",)
    assert ledger.list_inventory_position_ids() == ("p1", "p2", "p3")
    assert sum(ledger.snapshot(pid).active_base for pid in ("p1", "p2")) == D(".08")
    assert ledger.get_lot("trade-fill:buy-fill:p1").proposal_id == "proposal:p1"
    assert ledger.get_lot("trade-fill:buy-fill:p2").proposal_id == "proposal:p2"


def test_alias_cannot_bypass_dust_guard(environment):
    ledger, _, _, lifecycle, clock = environment
    orders = create_cycle(environment, pair="XBT-USD")
    market(environment, pair="XBT-USD")
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    with pytest.raises(ValueError, match="guarded successor"):
        ledger.open_position(position_id="p2", pair="BTC-EUR", created_at=clock[0])


def test_checkpoint_id_conflict_is_not_an_update(environment):
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    with pytest.raises(ValueError, match="replay conflicts"):
        checkpoint(
            environment,
            replace(
                orders,
                orders=(
                    orders.orders[0],
                    replace(orders.orders[1], order_type="OTHER"),
                ),
            ),
        )


def test_reconciled_but_unfilled_executor_is_still_pending_evidence(environment):
    orders = create_cycle(environment)
    governor = environment[1]
    governor.reserve(
        CapitalProposal("unfilled", "XRP-USD", IntentKind.RECOVERY, D("10")),
        CapitalSnapshot(D("5000")),
    )
    governor.commit("unfilled")
    governor.reconcile_execution(
        proposal_id="unfilled",
        executor_id="unfilled-executor",
        account_name="owner-account",
        connector_name="kraken",
        controller_id="spectra",
    )
    market(environment)
    with pytest.raises(ValueError, match="no reconciled owned inventory"):
        checkpoint(environment, orders)


def test_executable_old_residual_cannot_be_classified(environment):
    orders = create_cycle(environment, sold="9")
    market(environment)
    checkpoint(environment, orders)
    with pytest.raises(ValueError, match="remains executable"):
        environment[3].classify(checkpoint_id="checkpoint1", now=environment[4][0])
    assert environment[0].snapshot("p1").status == EconomicPositionStatus.ACTIVE


@pytest.mark.parametrize(
    "column,value",
    [
        ("cost_basis_original_quote", "99"),
        ("cost_basis_remaining_quote", ".3"),
        ("base_remaining", ".01"),
    ],
)
def test_corrupt_geometry_cannot_be_blessed_by_a_new_checkpoint(
    environment, column, value
):
    orders = create_cycle(environment)
    with environment[0]._connect() as conn:
        conn.execute(f"UPDATE economic_lots SET {column} = ?", (value,))
    market(environment)
    with pytest.raises(ValueError, match="differs from exact|does not reconcile"):
        checkpoint(environment, orders)


def test_repeated_partial_sells_reconcile_to_same_owner_before_dust(environment):
    ledger, governor, reconciler, lifecycle, clock = environment
    orders = create_cycle(environment, sold="9.95")
    old_lot = ledger.get_lot("trade-fill:buy-fill:p1")
    new_order = ExecutorOrderEvidence(
        "executor:p1",
        "sell-again",
        "LIMIT",
        "SELL",
        D(".05"),
        D("11"),
        "CANCELED",
        D(".01"),
        D("11"),
        clock[0],
        clock[0],
    )
    orders = replace(orders, orders=orders.orders + (new_order,))
    reconciler.bind_executor_order(
        order_id="sell-again",
        position_id="p1",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="proposal:p1",
        account_name="owner-account",
        connector_name="kraken",
        pair="XRP-USD",
        created_at=clock[0],
    )
    raw = {
        "trade_id": "sell-again-fill",
        "order_id": "sell-again",
        "account_name": "owner-account",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "trade_type": "SELL",
        "amount": ".01",
        "price": "11",
        "fee_paid": "0",
        "timestamp": datetime.fromtimestamp(clock[0], timezone.utc).isoformat(),
    }
    reconciler.reconcile_trade_fill(raw, reconciled_at=clock[0])
    reconciler.reconcile_trade_fill(raw, reconciled_at=clock[0])
    governor.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            "executor:p1",
            "TERMINATED",
            D("209.56"),
            D(".2"),
            position_size_quote=D(".4"),
            executor_type="grid_executor",
        )
    )
    market(environment)
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    current = ledger.get_lot(old_lot.lot_id)
    assert current.base_remaining == D(".04")
    assert current.cost_basis_original_quote == old_lot.cost_basis_original_quote
    assert current.executor_id == old_lot.executor_id
    assert len(rows(ledger, "economic_position_events")) == 2
    assert len(rows(ledger, "economic_exit_allocations")) == 2


def test_concurrent_successor_creation_leaves_exactly_one_active_slot(environment):
    from concurrent.futures import ThreadPoolExecutor

    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    ledger, _, _, lifecycle, clock = environment
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])

    def attempt(pid):
        try:
            lifecycle.open_successor(
                position_id=pid,
                pair="XRP-USD",
                checkpoint_ids=("checkpoint1",),
                now=clock[0],
            )
            return "opened"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("successor-a", "successor-b")))
    assert results.count("opened") == 1
    assert len(ledger.list_active_position_ids()) == 1


def test_fictitious_full_recovery_release_cannot_unlock_real_residual(environment):
    orders = create_cycle(environment, intent=IntentKind.RECOVERY)
    environment[1].record_recovery_recycle_release(
        release_id="over-release", proposal_id="proposal:p1", amount_quote=D("100")
    )
    market(environment)
    with pytest.raises(ValueError, match="exceeds proven disposed basis"):
        checkpoint(environment, orders)


def test_owner_cannot_be_reused_across_dust_successor_identity(environment):
    ledger, governor, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    lifecycle.open_successor(
        position_id="p2", pair="XRP-USD", checkpoint_ids=("checkpoint1",), now=clock[0]
    )
    # Adversarial inconsistent durable state cannot be blessed by a fresh
    # checkpoint; old lots still belong exclusively to the predecessor.
    with ledger._connect() as conn:
        conn.execute(
            """INSERT INTO economic_lots
            SELECT 'foreign-owner-lot', 'p2', pair, ownership, source, base_original,
                cost_basis_original_quote, entry_fees_original_quote,
                base_remaining, cost_basis_remaining_quote, entry_fees_remaining_quote,
                proposal_id, executor_id, created_at, updated_at FROM economic_lots LIMIT 1"""
        )
    with pytest.raises(ValueError, match="crosses economic position"):
        checkpoint(environment, orders, cid="changed")
    assert ledger.get_lot("trade-fill:buy-fill:p1").position_id == "p1"


def test_sell_notional_does_not_round_as_if_it_were_a_quote_budget():
    result = residual_geometry(
        base=D(".1"),
        price=D("9.99"),
        rule=rule(min_notional_size=D(".99"), min_quote_amount_increment=D("1")),
    )
    assert result.rounded_notional_quote == D(".999")
    assert result.executable


@pytest.mark.parametrize("column", ["filled_amount_quote", "cum_fees_quote"])
def test_terminal_executor_totals_expose_omitted_activity(environment, column):
    orders = create_cycle(environment)
    with environment[1]._connect() as conn:
        conn.execute(f"UPDATE executor_accounting_observations SET {column} = '999'")
    market(environment)
    with pytest.raises(ValueError, match="totals contain unreconciled activity"):
        checkpoint(environment, orders)


def test_repeated_dust_cannot_hide_executable_same_owner_accumulation(environment):
    ledger, _, _, lifecycle, clock = environment
    first = create_cycle(environment, sold="9.94")
    market(environment)
    checkpoint(environment, first)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    lifecycle.open_successor(
        position_id="p2", pair="XRP-USD", checkpoint_ids=("checkpoint1",), now=clock[0]
    )
    second = create_cycle(environment, "p2", sold="9.94", open_position=False)
    checkpoint(environment, second, cid="checkpoint2", pid="p2")
    lifecycle.classify(checkpoint_id="checkpoint2", now=clock[0])
    checkpoint(environment, first, cid="checkpoint1-fresh")
    with pytest.raises(ValueError, match="combined old residuals are executable"):
        lifecycle.open_successor(
            position_id="p3",
            pair="XRP-USD",
            checkpoint_ids=("checkpoint1-fresh", "checkpoint2"),
            now=clock[0],
        )
    assert ledger.list_active_position_ids() == ()
    assert ledger.snapshot("p1").active_base == D(".06")
    assert ledger.snapshot("p2").active_base == D(".06")


class DustEntryClient:
    def __init__(self, env, before_rules=None, before_portfolio=None, ambiguous=False):
        self.env = env
        self.before_rules = before_rules
        self.before_portfolio = before_portfolio
        self.ambiguous = ambiguous
        self.calls = []
        self.portfolio = self
        self.connectors = self
        self.executors = self

    async def get_state(self, *, account_names, connector_names, refresh):
        if self.before_portfolio:
            self.before_portfolio()
        return {
            account_names[0]: {
                "kraken": [
                    {
                        "token": "USD",
                        "units": 5000,
                        "available_units": 5000,
                        "value": 5000,
                    },
                    {
                        "token": "XRP",
                        "units": ".04",
                        "available_units": ".04",
                        "value": ".44",
                    },
                ]
            }
        }

    async def get_trading_rules(self, *, connector_name, trading_pairs):
        if self.before_rules:
            self.before_rules()
        return {
            pair: {
                "min_order_size": ".01",
                "min_notional_size": "1",
                "min_order_value": "0",
                "min_price_increment": ".01",
                "min_base_amount_increment": ".01",
                "min_quote_amount_increment": ".01",
                "supports_limit_orders": True,
                "supports_market_orders": True,
            }
            for pair in trading_pairs
        }

    async def create_executor(self, *, executor_config, account_name, controller_id):
        from spectra_core.models import DecisionStatus

        admissions = rows(self.env[0], "economic_dust_entry_admissions")
        assert admissions[-1]["status"] == "CLAIMED"
        proposal_id = admissions[-1]["proposal_id"]
        assert self.env[1].get_decision(proposal_id).status == DecisionStatus.COMMITTED
        self.calls.append((executor_config, account_name, controller_id))
        if self.ambiguous:
            raise RuntimeError("synthetic network ambiguity")
        return {"executor_id": "successor-executor"}


def configure_entry_runtime(env, monkeypatch):
    from mcp_servers.hummingbot_api.tools import executor_create
    from spectra_core import kraken_authorization
    from spectra_core.models import BreakerMode

    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")
    monkeypatch.setattr(executor_create, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: env[1])
    monkeypatch.setattr(executor_create, "get_dust_lifecycle", lambda **kwargs: env[3])
    monkeypatch.setattr(
        kraken_authorization, "get_dust_lifecycle", lambda **kwargs: env[3]
    )
    executor_create.trading_rules_cache.clear()
    return executor_create, kraken_authorization


def prepare_real_entry(env, monkeypatch, **client_options):
    import asyncio

    orders = create_cycle(env)
    market(env)
    checkpoint(env, orders)
    env[3].classify(checkpoint_id="checkpoint1", now=env[4][0])
    executor_create, authorization = configure_entry_runtime(env, monkeypatch)
    client = DustEntryClient(env, **client_options)
    result = asyncio.run(
        authorization.reserve_kraken_capital(
            client,
            account_name="owner-account",
            trading_pair="XRP-USD",
            intent="ENTRY",
            amount_quote="20",
            governor=env[1],
        )
    )
    return executor_create, client, result


def dispatch_real_entry(adapter, client, authorization, *, account="owner-account"):
    import asyncio

    return asyncio.run(
        adapter.create_grid_executor(
            client,
            connector_name="kraken",
            trading_pair="XRP-USD",
            side=1,
            start_price=10,
            end_price=11,
            limit_price=9,
            total_amount_quote=20,
            account_name=account,
            spectra_proposal_id=authorization.proposal.proposal_id,
            spectra_intent="ENTRY",
        )
    )


def test_enabled_existing_authorization_and_grid_create_admit_dust_successor(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)
    assert authorization.snapshot.pair_exposure_quote == D(".44")
    result = dispatch_real_entry(adapter, client, authorization)
    assert "error" not in result
    assert len(client.calls) == 1
    admission = rows(environment[0], "economic_dust_entry_admissions")[0]
    assert admission["status"] == "EXECUTED"
    assert admission["executor_id"] == "successor-executor"
    assert environment[0].snapshot("p1").status == EconomicPositionStatus.DUST
    assert environment[0].get_lot("trade-fill:buy-fill:p1").position_id == "p1"
    assert (
        environment[0].snapshot(admission["successor_id"]).status
        == EconomicPositionStatus.ACTIVE
    )
    repeat = dispatch_real_entry(adapter, client, authorization)
    assert "error" in repeat
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "mutation", ["pending", "order", "owner", "price", "stale", "disabled"]
)
def test_existing_create_revalidates_dust_after_venue_await(
    environment, monkeypatch, mutation
):
    def mutate():
        ledger, governor, _, lifecycle, clock = environment
        if mutation == "pending":
            governor.reserve(
                CapitalProposal("racing-entry", "XRP-EUR", IntentKind.ENTRY, D("5")),
                CapitalSnapshot(D("5000")),
            )
        elif mutation == "order":
            with ledger._connect() as conn:
                conn.execute(
                    """INSERT INTO economic_fill_bindings SELECT 'racing-order', position_id,
                    role, pair, account_name, connector_name, proposal_id, executor_id, created_at
                    FROM economic_fill_bindings LIMIT 1"""
                )
        elif mutation == "owner":
            with ledger._connect() as conn:
                conn.execute("UPDATE economic_lots SET executor_id = 'foreign'")
        elif mutation == "price":
            clock[0] += 1
            market(environment, "changed", price="30")
        elif mutation == "stale":
            clock[0] += 31
        else:
            lifecycle.enabled = False

    adapter, client, authorization = prepare_real_entry(
        environment, monkeypatch, before_rules=mutate
    )
    result = dispatch_real_entry(adapter, client, authorization)
    assert "error" in result
    assert client.calls == []
    assert (
        environment[1].get_decision(authorization.proposal.proposal_id).status.value
        == "RESERVED"
    )


def test_post_commit_race_holds_capital_without_creating_executor(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)
    governor = environment[1]
    original = governor.commit_if_matches

    def commit(proposal):
        result = original(proposal)
        governor.reserve(
            CapitalProposal("after-commit", "XRP-USD", IntentKind.ENTRY, D("5")),
            CapitalSnapshot(D("5000")),
        )
        return result

    monkeypatch.setattr(governor, "commit_if_matches", commit)
    result = dispatch_real_entry(adapter, client, authorization)
    assert "changed durable state" in result["error"]
    assert client.calls == []
    assert (
        governor.get_decision(authorization.proposal.proposal_id).status.value
        == "COMMITTED"
    )


def test_ambiguous_real_create_remains_single_use_across_restart(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(
        environment, monkeypatch, ambiguous=True
    )
    result = dispatch_real_entry(adapter, client, authorization)
    assert "ambiguity" in result["error"]
    assert (
        rows(environment[0], "economic_dust_entry_admissions")[0]["status"] == "CLAIMED"
    )
    restarted = DustLifecycle(
        ledger=EconomicPositionLedger(environment[0].path),
        governor=environment[1],
        enabled=True,
    )
    monkeypatch.setattr(adapter, "get_dust_lifecycle", lambda **kwargs: restarted)
    repeated = dispatch_real_entry(adapter, client, authorization)
    assert "already claimed" in repeated["error"]
    assert len(client.calls) == 1


def test_legacy_executor_caller_cannot_skip_dust_admission(environment, monkeypatch):
    import asyncio

    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    environment[3].classify(checkpoint_id="checkpoint1", now=environment[4][0])
    adapter, _ = configure_entry_runtime(environment, monkeypatch)
    governor = environment[1]
    proposal = CapitalProposal("legacy-bypass", "XRP-USD", IntentKind.ENTRY, D("20"))
    governor.reserve(proposal, CapitalSnapshot(D("5000")))
    client = DustEntryClient(environment)
    result = asyncio.run(
        adapter.create_grid_executor(
            client,
            connector_name="kraken",
            trading_pair="XRP-USD",
            side=1,
            start_price=10,
            end_price=11,
            limit_price=9,
            total_amount_quote=20,
            account_name="owner-account",
            spectra_proposal_id=proposal.proposal_id,
            spectra_intent="ENTRY",
        )
    )
    assert "proposal-bound" in result["error"]
    assert client.calls == []


def test_dust_dispatch_is_bound_to_actual_account(environment, monkeypatch):
    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)
    result = dispatch_real_entry(
        adapter, client, authorization, account="different-account"
    )
    assert "scope changed" in result["error"]
    assert client.calls == []


def test_existing_ledger_open_position_has_enabled_checkpoint_branch(environment):
    ledger, _, _, lifecycle, clock = environment
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    ledger.open_position(
        position_id="existing-open-path",
        pair="XRP-USD",
        created_at=clock[0],
        dust_lifecycle=lifecycle,
        dust_checkpoint_ids=("checkpoint1",),
    )
    assert ledger.list_active_position_ids() == ("existing-open-path",)


def test_disabled_no_dust_preserves_existing_open_before_authorize_behavior(
    environment,
):
    ledger, governor, _, _, clock = environment
    ledger.open_position(position_id="normal", pair="XRP-USD", created_at=clock[0])
    disabled = DustLifecycle(ledger=ledger, governor=governor)
    proposal = CapitalProposal("ordinary", "XRP-USD", IntentKind.ENTRY, D("20"))
    assert (
        disabled.prepare_entry(
            proposal=proposal, account_name="owner-account", now=clock[0]
        )
        is None
    )
    assert (
        disabled.assert_dispatch(
            proposal=proposal, account_name="owner-account", now=clock[0]
        )
        is None
    )


def test_prepare_entry_replay_is_exact_without_reopening_or_dispatch(
    environment, monkeypatch
):
    _, _, authorization = prepare_real_entry(environment, monkeypatch)
    before = rows(environment[0], "economic_dust_entry_admissions")
    result = environment[3].prepare_entry(
        proposal=authorization.proposal,
        account_name="owner-account",
        now=environment[4][0],
    )
    assert result == before[0]["successor_id"]
    assert rows(environment[0], "economic_dust_entry_admissions") == before
    with pytest.raises(ValueError, match="replay conflicts"):
        environment[3].prepare_entry(
            proposal=replace(authorization.proposal, amount_quote=D("30")),
            account_name="owner-account",
            now=environment[4][0],
        )


def test_full_flow_confirmed_sell_dust_admission_create_and_successor_fill(
    environment, monkeypatch
):
    import asyncio

    # create_cycle supplies confirmed BUY/SELL evidence to the actual
    # synchronize_executor_evidence/LiveFillLedgerReconciler path.
    orders = create_cycle(environment)
    ledger, governor, _, lifecycle, clock = environment
    assert ledger.snapshot("p1").status == EconomicPositionStatus.ACTIVE
    assert ledger.snapshot("p1").active_base == D(".04")
    market(environment)
    checkpoint(environment, orders)
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    assert ledger.snapshot("p1").status == EconomicPositionStatus.DUST
    adapter, authorization_api = configure_entry_runtime(environment, monkeypatch)
    client = DustEntryClient(environment)
    authorization = asyncio.run(
        authorization_api.reserve_kraken_capital(
            client,
            account_name="owner-account",
            trading_pair="XRP-USD",
            intent="ENTRY",
            amount_quote="20",
            governor=governor,
        )
    )
    result = dispatch_real_entry(adapter, client, authorization)
    successor = authorization.economic_position_id
    assert successor == result["spectra_position_id"]
    assert (
        environment[3].record_entry_receipt(
            proposal_id=authorization.proposal.proposal_id,
            executor_id=result["executor_id"],
        )
        == successor
    )
    now = environment[4][0]
    order = ExecutorOrderEvidence(
        "successor-executor",
        "new-buy-order",
        "LIMIT",
        "BUY",
        D("2"),
        D("10"),
        "FILLED",
        D("2"),
        D("10"),
        now,
        now,
    )
    fill = TradeFillEvidence(
        "new-buy-fill",
        "new-buy-order",
        "owner-account",
        "kraken",
        "XRP-USD",
        "BUY",
        D("2"),
        D("10"),
        D("0"),
        None,
        now,
    )
    report = synchronize_executor_evidence(
        reconciler=environment[2],
        position_id=successor,
        proposal_id=authorization.proposal.proposal_id,
        order_snapshot=ExecutorOrderSnapshot("successor-executor", (order,)),
        trade_snapshot=ExecutorTradeSnapshot("successor-executor", (fill,)),
        now=now,
    )
    assert report.fully_reconciled
    assert environment[0].snapshot(successor).active_base == D("2")
    assert environment[0].snapshot("p1").active_base == D(".04")
    assert (
        environment[0].get_lot("trade-fill:new-buy-fill").proposal_id
        == authorization.proposal.proposal_id
    )
    assert environment[0].get_lot("trade-fill:buy-fill:p1").proposal_id == "proposal:p1"


def test_actual_fresh_venue_threshold_change_cannot_use_old_checkpoint(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)
    original = client.get_trading_rules

    async def changed_rules(**kwargs):
        result = await original(**kwargs)
        result["XRP-USD"]["min_notional_size"] = ".1"
        return result

    monkeypatch.setattr(client, "get_trading_rules", changed_rules)
    result = dispatch_real_entry(adapter, client, authorization)
    assert "fresh venue rules differ" in result["error"]
    assert client.calls == []


def test_dust_route_does_not_accept_incomplete_rules_that_legacy_route_accepts(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)

    async def incomplete(**kwargs):
        return {"XRP-USD": {"min_order_size": ".01", "min_notional_size": "1"}}

    monkeypatch.setattr(client, "get_trading_rules", incomplete)
    result = dispatch_real_entry(adapter, client, authorization)
    assert "complete fresh venue rules" in result["error"]
    assert client.calls == []


def test_enabled_real_entry_concurrent_dispatch_consumes_admission_once(
    environment, monkeypatch
):
    import asyncio

    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)

    async def create():
        return await adapter.create_grid_executor(
            client,
            connector_name="kraken",
            trading_pair="XRP-USD",
            side=1,
            start_price=10,
            end_price=11,
            limit_price=9,
            total_amount_quote=20,
            account_name="owner-account",
            spectra_proposal_id=authorization.proposal.proposal_id,
            spectra_intent="ENTRY",
        )

    async def run():
        return await asyncio.gather(create(), create())

    results = asyncio.run(run())
    assert sum("error" not in result for result in results) == 1
    assert len(client.calls) == 1


def add_partial_dispatch_marker(ledger, *, status="OPEN"):
    # Minimal durable interface fixture: the real PartialRecycleStore owns the
    # richer manifest columns, while dust reads only this lifecycle boundary.
    with ledger._connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS partial_recycle_manifests (manifest_id TEXT PRIMARY KEY, position_id TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS partial_recycle_dispatch_claims (manifest_id TEXT PRIMARY KEY, sell_executor_id TEXT, claimed_at REAL)"
        )
        conn.execute(
            "INSERT INTO partial_recycle_manifests VALUES ('partial-sell', 'p1', ?)",
            (status,),
        )
        conn.execute(
            "INSERT INTO partial_recycle_dispatch_claims VALUES ('partial-sell', 'separate-sell-executor', 10000)"
        )


@pytest.mark.parametrize("kind", ["partial_claim", "selected_lock"])
def test_current_pending_sell_mutation_blocks_checkpoint(environment, kind):
    orders = create_cycle(environment)
    if kind == "partial_claim":
        add_partial_dispatch_marker(environment[0])
    else:
        environment[0].claim_selected_recovery_lock("p1", "selected:test")
    market(environment)
    with pytest.raises(ValueError, match="mutation lock|pending partial sell"):
        checkpoint(environment, orders)


@pytest.mark.parametrize("kind", ["partial_claim", "selected_lock"])
def test_pending_sell_mutation_after_checkpoint_invalidates_classification(
    environment, kind
):
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    if kind == "partial_claim":
        add_partial_dispatch_marker(environment[0])
    else:
        environment[0].claim_selected_recovery_lock("p1", "selected:test")
    with pytest.raises(ValueError, match="changed durable state"):
        environment[3].classify(checkpoint_id="checkpoint1", now=environment[4][0])


def test_completed_legacy_partial_claim_does_not_permanently_block_dust(environment):
    orders = create_cycle(environment)
    add_partial_dispatch_marker(environment[0], status="COMPLETED")
    market(environment)
    checkpoint(environment, orders)
    environment[3].classify(checkpoint_id="checkpoint1", now=environment[4][0])
    assert environment[0].snapshot("p1").status == EconomicPositionStatus.DUST


def test_pending_partial_sell_after_venue_await_blocks_real_entry(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(
        environment,
        monkeypatch,
        before_rules=lambda: add_partial_dispatch_marker(environment[0]),
    )
    result = dispatch_real_entry(adapter, client, authorization)
    assert "changed durable state" in result["error"]
    assert client.calls == []


def test_real_rejected_sizing_frees_only_empty_unsubmitted_slot_for_valid_retry(
    environment, monkeypatch
):
    import asyncio

    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    environment[3].classify(checkpoint_id="checkpoint1", now=environment[4][0])
    adapter, authorization = configure_entry_runtime(environment, monkeypatch)
    client = DustEntryClient(environment)
    rejected = asyncio.run(
        authorization.reserve_kraken_capital(
            client,
            account_name="owner-account",
            trading_pair="XRP-USD",
            intent="ENTRY",
            amount_quote="6000",
            governor=environment[1],
        )
    )
    assert rejected.decision.status.value == "REJECTED"
    assert environment[0].list_active_position_ids() == ()
    assert (
        rows(environment[0], "economic_dust_entry_admissions")[0]["status"]
        == "CANCELLED"
    )
    retry = asyncio.run(
        authorization.reserve_kraken_capital(
            client,
            account_name="owner-account",
            trading_pair="XRP-USD",
            intent="ENTRY",
            amount_quote="20",
            governor=environment[1],
        )
    )
    result = dispatch_real_entry(adapter, client, retry)
    assert "error" not in result
    assert len(client.calls) == 1
    assert environment[0].snapshot("p1").active_base == D(".04")


def test_expired_reserved_entry_can_recover_after_restart_with_fresh_evidence(
    environment, monkeypatch
):
    import asyncio

    adapter, client, first = prepare_real_entry(environment, monkeypatch)
    ledger, governor, _, lifecycle, clock = environment
    clock[0] += governor.policy.reservation_ttl_seconds + 1
    lifecycle = DustLifecycle(
        ledger=EconomicPositionLedger(ledger.path), governor=governor, enabled=True
    )
    # Refresh the terminated predecessor evidence; the old failed proposal is
    # reaped before new admission, never by clearing a COMMITTED claim.
    lifecycle.reconcile_unsubmitted_entries(now=clock[0])
    assert governor.get_decision(first.proposal.proposal_id).status.value == "RELEASED"
    assert ledger.list_active_position_ids() == ()
    governor.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            "executor:p1",
            "TERMINATED",
            D("209.56"),
            D(".2"),
            position_size_quote=D(".4"),
            executor_type="grid_executor",
        )
    )
    market(environment, "refresh")
    with ledger._connect() as conn:
        cp = conn.execute(
            "SELECT orders_json FROM economic_dust_checkpoints WHERE checkpoint_id='checkpoint1'"
        ).fetchone()[0]
    import json

    raw = json.loads(cp)[0]
    refreshed_orders = ExecutorOrderSnapshot(
        raw["executor_id"],
        tuple(
            ExecutorOrderEvidence(
                **{
                    **o,
                    "amount": D(o["amount"]),
                    "price": D(o["price"]),
                    "filled_amount": D(o["filled_amount"]),
                    "average_fill_price": D(o["average_fill_price"]),
                }
            )
            for o in raw["orders"]
        ),
    )
    checkpoint(environment, refreshed_orders, cid="fresh", mid="refresh")
    _, authorization = configure_entry_runtime(environment, monkeypatch)
    retry = asyncio.run(
        authorization.reserve_kraken_capital(
            client,
            account_name="owner-account",
            trading_pair="XRP-USD",
            intent="ENTRY",
            amount_quote="20",
            governor=governor,
        )
    )
    assert "error" not in dispatch_real_entry(adapter, client, retry)


def test_admitted_but_committed_or_claimed_entry_is_never_reaped(
    environment, monkeypatch
):
    adapter, client, authorization = prepare_real_entry(
        environment, monkeypatch, ambiguous=True
    )
    dispatch_real_entry(adapter, client, authorization)
    environment[4][0] += 1000
    environment[3].reconcile_unsubmitted_entries(now=environment[4][0])
    assert (
        rows(environment[0], "economic_dust_entry_admissions")[0]["status"] == "CLAIMED"
    )
    assert (
        environment[1].get_decision(authorization.proposal.proposal_id).status.value
        == "COMMITTED"
    )
    assert len(environment[0].list_active_position_ids()) == 1


def test_unchanged_terminal_reread_refreshes_evidence_without_new_accounting_delta(
    environment,
):
    orders = create_cycle(environment)
    ledger, governor, _, lifecycle, clock = environment
    old_observations = rows(governor, "executor_accounting_observations")
    clock[0] += 31
    market(environment, "fresh-market")
    with pytest.raises(ValueError, match="stale"):
        checkpoint(environment, orders, mid="fresh-market")
    delta = governor.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            "executor:p1",
            "TERMINATED",
            D("209.56"),
            D(".2"),
            position_size_quote=D(".4"),
            executor_type="grid_executor",
        )
    )
    assert delta.filled_delta_quote == 0
    assert delta.fee_delta_quote == 0
    assert rows(governor, "executor_accounting_observations") == old_observations
    refresh = rows(governor, "executor_accounting_refresh_receipts")[-1]
    assert refresh["observation_id"] == old_observations[-1]["observation_id"]
    assert refresh["observed_at"] == clock[0]
    checkpoint(environment, orders, mid="fresh-market")
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    assert ledger.snapshot("p1").status == EconomicPositionStatus.DUST


def test_foreign_or_wrong_observation_refresh_cannot_freshen_terminal_evidence(
    environment,
):
    orders = create_cycle(environment)
    governor, clock = environment[1], environment[4]
    clock[0] += 31
    with governor._connect() as conn:
        conn.execute(
            """INSERT INTO executor_accounting_refresh_receipts
            (observation_id, proposal_id, executor_id, observed_at)
            VALUES (999, 'proposal:p1', 'executor:p1', ?)""",
            (clock[0],),
        )
    market(environment, "fresh-market")
    with pytest.raises(ValueError, match="stale"):
        checkpoint(environment, orders, mid="fresh-market")


def test_crash_before_reservation_recovers_only_after_no_claim_lease(environment):
    orders = create_cycle(environment)
    market(environment)
    checkpoint(environment, orders)
    ledger, governor, _, lifecycle, clock = environment
    lifecycle.classify(checkpoint_id="checkpoint1", now=clock[0])
    proposal = CapitalProposal("never-reserved", "XRP-USD", IntentKind.ENTRY, D("20"))
    successor = lifecycle.prepare_entry(
        proposal=proposal, account_name="owner-account", now=clock[0]
    )
    assert (
        lifecycle.cancel_unsubmitted_entry(
            proposal_id=proposal.proposal_id, now=clock[0]
        )
        is False
    )
    clock[0] += governor.policy.reservation_ttl_seconds + 1
    restarted = DustLifecycle(
        ledger=EconomicPositionLedger(ledger.path), governor=governor, enabled=True
    )
    restarted.reconcile_unsubmitted_entries(now=clock[0])
    assert ledger.snapshot(successor).status == EconomicPositionStatus.CLOSED
    assert governor.get_decision(proposal.proposal_id) is None
    assert ledger.snapshot("p1").active_base == D(".04")
    assert (
        rows(ledger, "economic_dust_entry_cancellations")[-1]["reason"]
        == "NO_CAPITAL_CLAIM_AFTER_LEASE"
    )


def test_inflight_external_create_keeps_single_use_claim_across_real_await(
    environment, monkeypatch
):
    import asyncio

    adapter, client, authorization = prepare_real_entry(environment, monkeypatch)
    original = client.create_executor

    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def waiting_create(**kwargs):
            entered.set()
            await finish.wait()
            return await original(**kwargs)

        client.create_executor = waiting_create

        async def create():
            return await adapter.create_grid_executor(
                client,
                connector_name="kraken",
                trading_pair="XRP-USD",
                side=1,
                start_price=10,
                end_price=11,
                limit_price=9,
                total_amount_quote=20,
                account_name="owner-account",
                spectra_proposal_id=authorization.proposal.proposal_id,
                spectra_intent="ENTRY",
            )

        first = asyncio.create_task(create())
        await entered.wait()
        second = await create()
        assert "already claimed" in second["error"]
        finish.set()
        return await first

    assert "error" not in asyncio.run(run())
    assert len(client.calls) == 1
