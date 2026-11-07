"""Synthetic accounting tests only: no market backtests or live exchange calls."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction as F

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import ExecutorOrderEvidence
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
    TradeFillEvidence,
)
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    IntentKind,
)
from spectra_core.selected_recovery import (
    RecoveryLimits,
    RecoveryRung,
    SelectedRecoveryBook,
    original_inventory_manifest,
)

D = Decimal
LADDER = tuple(
    RecoveryRung(name, D(price))
    for name, price in (("r1", "8"), ("r2", "6"), ("r3", "4"))
)


def decimal(value):
    value = F(value)
    result = D(value.numerator) / D(value.denominator)
    assert F(result) == value
    return result


class Harness:
    def __init__(
        self, path, *, cash=140, protected=0, recovery_cap=1000, asset_cap=5000, count=5
    ):
        self.g = CapitalGovernor(
            path / "governor.sqlite",
            CapitalPolicy(D(0), D(10000), D(10000), D(10000), D(10000)),
        )
        self.ledger = EconomicPositionLedger(path / "economic.sqlite")
        self.ledger.open_position(position_id="parent", pair="XRP-USD", created_at=1)
        self.r = LiveFillLedgerReconciler(ledger=self.ledger, governor=self.g)
        self.authorize("entry", 100, intent=IntentKind.ENTRY)
        self.bind("entry", EconomicFillRole.BASE_ENTRY)
        self.reconcile(self.fill("entry", "original", 10, "9.96", ".4"))
        self.path = path / "selected.sqlite"
        self.b = SelectedRecoveryBook(self.path, enabled=True)
        self.b.initialize(
            ledger=self.ledger,
            governor=self.g,
            position_id="parent",
            account_name="account",
            connector_name="kraken",
            controller_id="controller",
            child_ids=tuple("abcde"[:count]),
            recovery_cash_quote=D(cash),
            limits=RecoveryLimits(D(recovery_cap), D(asset_cap), D(protected)),
        )

    def reconcile(self, fill):
        self.r.reconcile_trade_fill(
            {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "account_name": fill.account_name,
                "connector_name": fill.connector_name,
                "trading_pair": fill.pair,
                "trade_type": fill.side,
                "amount": str(fill.amount_base),
                "price": str(fill.price),
                "fee_paid": str(fill.fee_paid),
                "fee_currency": fill.fee_currency,
                "timestamp": "1970-01-01T00:00:03Z",
            },
            reconciled_at=4,
        )

    def authorize(
        self,
        name,
        amount,
        *,
        intent=IntentKind.RECOVERY,
        account="account",
        connector="kraken",
        controller="controller",
        scoped=True,
    ):
        p = CapitalProposal(name, "XRP-USD", intent, decimal(amount), D(1))
        assert self.g.reserve(p, CapitalSnapshot(D(10000))).approved
        assert self.g.commit_if_matches(p)
        scope = (
            dict(
                account_name=account, connector_name=connector, controller_id=controller
            )
            if scoped
            else {}
        )
        assert self.g.reconcile_execution(
            proposal_id=name, executor_id=f"exec:{name}", **scope
        )

    def bind(self, name, role=EconomicFillRole.VCAP_RECOVERY, **kwargs):
        return self.r.bind_executor_order(
            order_id=f"order:{name}",
            position_id="parent",
            role=role,
            proposal_id=name,
            account_name=kwargs.get("account", "account"),
            connector_name=kwargs.get("connector", "kraken"),
            pair="XRP-USD",
            created_at=2,
        )

    @staticmethod
    def fill(name, trade, base, price, fee=0, *, side="BUY"):
        return TradeFillEvidence(
            trade,
            f"order:{name}",
            "account",
            "kraken",
            "XRP-USD",
            side,
            decimal(base),
            decimal(price),
            decimal(fee),
            "USD",
            3,
        )

    def plan(self, child="a", price=8, **kwargs):
        return self.b.plan_patch(
            child_id=child,
            ladder=kwargs.pop("ladder", LADDER),
            current_price=D(price),
            remaining_authorized_quote=D(kwargs.pop("authorized", 1000)),
            minimum_quote=D(kwargs.pop("minimum", 1)),
            evidence_allowed=kwargs.pop("evidence", True),
            parent_action_pending=kwargs.pop("parent_pending", False),
            **kwargs,
        )

    def start(self, name, price=8, child="a"):
        plan = self.plan(child, price)
        assert plan is not None
        self.authorize(name, plan.amount_quote)
        self.bind(name)
        self.b.bind_patch_order(plan, order_id=f"order:{name}", reconciler=self.r)
        return plan

    def patch(self, name="p1", *, amount=20, price=8, trade=None, terminal=True):
        # Fee is 0.4% of all-in budget for exact synthetic arithmetic.
        cost = F(amount) * F(996, 1000)
        fill = self.fill(name, trade or name, cost / F(price), price, F(amount) - cost)
        self.reconcile(fill)
        self.b.ingest_patch_fill(fill, reconciler=self.r)
        if terminal:
            pending = self.b.snapshot()["pending"]
            self.finish(name, F(pending["filled_base"]))
        return fill

    @staticmethod
    def order(name, base, *, side="BUY", status="FILLED", filled=None, price=8):
        return ExecutorOrderEvidence(
            f"exec:{name}",
            f"order:{name}",
            "LIMIT",
            side,
            decimal(base),
            decimal(price),
            status,
            decimal(base if filled is None else filled),
            decimal(price),
            2,
            4,
        )

    def finish(self, name, base, **kwargs):
        self.b.confirm_terminal(self.order(name, base, **kwargs))

    def first(self):
        self.start("p1")
        return self.patch()

    def exit_plan(self, price=10, **kwargs):
        return self.b.plan_exit(
            current_price=D(price),
            expected_exit_cost_bps=D(kwargs.pop("fee", 40)),
            minimum_base=D(kwargs.pop("min_base", ".001")),
            minimum_quote=D(kwargs.pop("min_quote", 1)),
            base_increment=D(kwargs.pop("increment", ".001")),
            **kwargs,
        )

    def sell(self, name="sell", *, fraction=F(1), price=10, terminal=True):
        plan = self.exit_plan(price)
        assert plan is not None
        order = self.order(
            name, plan.target_base, side="SELL", status="OPEN", filled=0, price=price
        )
        self.b.bind_exit_order(plan, order=order)
        fill = self.fill(
            name,
            name,
            plan.target_base * fraction,
            price,
            plan.target_base * fraction * F(price) * F(4, 1000),
            side="SELL",
        )
        self.b.ingest_sale_fill(
            fill, executor_id=order.executor_id, manifest_id=plan.manifest_id
        )
        if terminal:
            self.b.confirm_terminal(
                replace(
                    order,
                    status="FILLED" if fraction == 1 else "CANCELED",
                    filled_amount=fill.amount_base,
                )
            )
        return plan, fill, order


def basis(lots):
    return sum((F(x["cost"]) + F(x["fees"]) for x in lots), F(0))


def assert_no_change(book, operation, error=ValueError):
    before = book.snapshot()
    with pytest.raises(error):
        operation()
    assert book.snapshot() == before


def test_default_disabled_never_proposes_or_mutates(tmp_path):
    h = Harness(tmp_path)
    off = SelectedRecoveryBook(h.path)
    assert (
        off.plan_patch(
            child_id="a",
            ladder=LADDER,
            current_price=D(8),
            remaining_authorized_quote=D(20),
            minimum_quote=D(1),
            evidence_allowed=True,
        )
        is None
    )
    assert (
        off.plan_exit(
            current_price=D(10),
            expected_exit_cost_bps=D(40),
            minimum_base=D(1),
            minimum_quote=D(1),
            base_increment=D(1),
        )
        is None
    )
    assert_no_change(off, lambda: off.confirm_terminal(h.order("x", 1)))


def test_no_upfront_healthy_or_unfilled_split_and_planning_is_pure(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path)
    before = h.b.snapshot()
    for price in (10, 11):
        assert h.plan(price=price) is None
    monkeypatch.setattr(
        h.g, "reserve", lambda *a, **k: pytest.fail("planner tried reserving")
    )
    assert h.plan() is not None
    assert h.plan(evidence=False) is None
    assert h.plan(parent_pending=True) is None
    assert h.b.snapshot() == before
    assert (
        not before["split"] and before["children"] == {} and before["selected"] is None
    )


def test_split_only_at_first_confirmed_fill(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    assert not h.b.snapshot()["split"]
    assert h.b.snapshot()["selected"] is None
    h.patch(terminal=False)
    state = h.b.snapshot()
    assert state["split"] and state["selected"] == "a"
    assert basis(state["children"]["a"]) == 40
    assert all(basis(state["children"][cid]) == 20 for cid in "bcde")


def test_cancel_without_fill_keeps_parent_unsplit(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    h.finish("p1", 10, status="CANCELED", filled=0)
    assert not h.b.snapshot()["split"]
    assert h.b.snapshot()["selected"] is None
    assert h.plan("b") is not None


def test_three_rungs_double_current_basis_preserve_every_sibling(tmp_path):
    h = Harness(tmp_path)
    h.first()
    siblings = {cid: h.b.snapshot()["children"][cid] for cid in "bcde"}
    for i, price, amount in ((2, 6, 40), (3, 4, 80)):
        plan = h.start(f"p{i}", price)
        assert plan.amount_quote == amount and plan.rung_id == f"r{i}"
        h.patch(f"p{i}", price=price, amount=amount)
        state = h.b.snapshot()
        assert basis(state["children"]["a"]) == 2 * amount
        assert {cid: state["children"][cid] for cid in "bcde"} == siblings
    assert h.b.snapshot()["cash"]["VCAP"] == "0"
    assert h.plan(price=1) is None


def test_gap_cannot_skip_or_repeat_rung(tmp_path):
    h = Harness(tmp_path)
    assert h.plan(price=4).rung_id == "r1"
    h.first()
    assert h.plan(price=7) is None
    assert h.plan(price=6).amount_quote == 40
    assert_no_change(h.b, lambda: h.plan("b", price=4))
    assert_no_change(
        h.b, lambda: h.plan(ladder=(RecoveryRung("r1", D(8)), RecoveryRung("r2", D(5))))
    )


def test_partial_cancel_restart_completes_frozen_budget(tmp_path):
    h = Harness(tmp_path)
    h.first()
    h.start("p2", 6)
    fill = h.patch("p2", price=6, amount=15, terminal=False)
    h.b = SelectedRecoveryBook(h.path, enabled=True)
    h.finish("p2", 10, status="CANCELED", filled=fill.amount_base)
    assert h.plan(price=5).amount_quote == 25
    plan = h.start("retry", 5)
    assert plan.rung_id == "r2"
    h.patch("retry", price=5, amount=25)
    assert basis(h.b.snapshot()["children"]["a"]) == 80
    assert h.plan(price=4).amount_quote == 80


def test_full_fill_waits_for_authoritative_terminal_totals(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    fill = h.patch(terminal=False)
    assert h.plan(price=6) is None
    assert_no_change(h.b, lambda: h.finish("p1", 3))
    assert_no_change(
        h.b,
        lambda: h.b.confirm_terminal(
            replace(h.order("p1", fill.amount_base), executor_id="wrong")
        ),
    )
    h.finish("p1", fill.amount_base)
    assert h.plan(price=6) is not None


def test_restart_and_duplicate_fill_are_idempotent(tmp_path):
    h = Harness(tmp_path)
    fill = h.first()
    h.b = SelectedRecoveryBook(h.path, enabled=True)
    before = h.b.snapshot()
    h.b.ingest_patch_fill(fill, reconciler=h.r)
    assert h.b.snapshot() == before
    assert_no_change(
        h.b,
        lambda: h.b.ingest_patch_fill(replace(fill, fee_paid=D(".09")), reconciler=h.r),
    )


def test_stale_or_edited_plan_rejected(tmp_path):
    h = Harness(tmp_path)
    plan = h.plan()
    h.authorize("p1", 20)
    h.bind("p1")
    assert_no_change(
        h.b,
        lambda: h.b.bind_patch_order(
            replace(plan, amount_quote=F(10)), order_id="order:p1", reconciler=h.r
        ),
    )
    h.b.bind_patch_order(plan, order_id="order:p1", reconciler=h.r)
    h.authorize("p2", 20)
    h.bind("p2")
    assert_no_change(
        h.b, lambda: h.b.bind_patch_order(plan, order_id="order:p2", reconciler=h.r)
    )


@pytest.mark.parametrize(
    "option,value",
    [("cash", 19), ("protected", 121), ("recovery_cap", 19), ("asset_cap", 119)],
)
def test_bounded_funding_includes_whole_asset(tmp_path, option, value):
    h = Harness(tmp_path, **{option: value})
    before = h.b.snapshot()
    assert h.plan() is None
    assert h.b.snapshot() == before


def test_never_shrink_unfunded_next_rung(tmp_path):
    h = Harness(tmp_path, cash=60)
    h.first()
    h.start("p2", 6)
    h.patch("p2", amount=40, price=6)
    assert h.plan(price=4) is None
    assert h.b.snapshot()["ladders"]["a"][2]["budget"] is None
    assert h.b.snapshot()["selected"] == "a"


def test_authorization_minimum_and_evidence_are_additional_gates(tmp_path):
    h = Harness(tmp_path)
    assert h.plan(authorized=19) is None
    assert h.plan(minimum=21) is None
    assert h.plan(evidence=False) is None
    assert_no_change(h.b, lambda: h.plan(evidence=1))


def test_exact_scoped_provenance_and_quote_budget_required(tmp_path):
    h = Harness(tmp_path)
    plan = h.plan()
    h.authorize("wrongbudget", 19)
    h.bind("wrongbudget")
    assert_no_change(
        h.b,
        lambda: h.b.bind_patch_order(
            plan, order_id="order:wrongbudget", reconciler=h.r
        ),
    )
    h.authorize("wrongcontroller", 20, controller="another")
    h.bind("wrongcontroller")
    assert_no_change(
        h.b,
        lambda: h.b.bind_patch_order(
            plan, order_id="order:wrongcontroller", reconciler=h.r
        ),
    )
    assert_no_change(
        h.b, lambda: h.b.bind_patch_order(plan, order_id="unknown", reconciler=h.r)
    )


def test_original_manifest_rejects_unattributed_or_unscoped_entry(tmp_path):
    h = Harness(tmp_path)
    h.ledger.record_base_fill(
        position_id="parent",
        lot_id="ambiguous",
        pair="XRP-USD",
        base_amount=D(1),
        cost_basis_quote=D(10),
        occurred_at=4,
    )
    with pytest.raises(ValueError, match="exact owned"):
        original_inventory_manifest(
            ledger=h.ledger,
            governor=h.g,
            position_id="parent",
            account_name="account",
            connector_name="kraken",
            controller_id="controller",
        )


def test_unreconciled_fill_does_not_split(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    fill = h.fill("p1", "missing", "2.49", 8, ".08")
    assert_no_change(h.b, lambda: h.b.ingest_patch_fill(fill, reconciler=h.r))
    assert not h.b.snapshot()["split"]


def test_original_inventory_drift_blocks_split(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    h.ledger.record_partial_exit(
        event_id="other-sale",
        position_id="parent",
        base_sold=D(1),
        gross_proceeds_quote=D(8),
        exit_fee_quote=D(0),
        occurred_at=3,
    )
    fill = h.fill("p1", "p1", "2.49", 8, ".08")
    h.reconcile(fill)
    assert_no_change(h.b, lambda: h.b.ingest_patch_fill(fill, reconciler=h.r))
    assert not h.b.snapshot()["split"]


def test_fill_owner_fee_or_scope_mismatch_atomic(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    fill = h.fill("p1", "p1", "2.49", 8, ".08")
    h.reconcile(fill)
    for changed in (
        replace(fill, account_name="other"),
        replace(fill, connector_name="other"),
        replace(fill, pair="XRP-EUR"),
        replace(fill, side="SELL"),
        replace(fill, fee_currency="XRP"),
        replace(fill, fee_paid=D(".09")),
    ):
        assert_no_change(
            h.b, lambda changed=changed: h.b.ingest_patch_fill(changed, reconciler=h.r)
        )


def test_duplicate_trade_concurrently_applies_once(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    fill = h.fill("p1", "p1", "2.49", 8, ".08")
    h.reconcile(fill)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: SelectedRecoveryBook(h.path, enabled=True).ingest_patch_fill(
                    fill, reconciler=h.r
                ),
                range(2),
            )
        )
    assert basis(h.b.snapshot()["children"]["a"]) == 40
    assert h.b.snapshot()["cash"]["VCAP"] == "120"


def test_fee_aware_exit_includes_original_and_all_patches(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan = h.exit_plan()
    slices = json.loads(plan.slices_json)
    assert plan.all_in_basis_quote == 40
    assert plan.target_base == F("4.49")
    assert {s["kind"] for s in slices} == {"ORIGINAL", "PATCH"}
    assert plan.minimum_net_proceeds_quote == F("40.696")
    assert h.exit_plan(price=9, fee=100) is None
    assert h.exit_plan(min_base=5) is None
    assert h.exit_plan(min_quote=50) is None
    assert h.exit_plan(increment=".1") is None


def test_exit_manifest_tampering_and_partial_target_rejected(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan = h.exit_plan()
    order = h.order(
        "sell", plan.target_base, side="SELL", status="OPEN", filled=0, price=10
    )
    assert_no_change(
        h.b, lambda: h.b.bind_exit_order(replace(plan, slices_json="[]"), order=order)
    )
    assert_no_change(
        h.b, lambda: h.b.bind_exit_order(plan, order=replace(order, amount=D(1)))
    )
    assert_no_change(
        h.b, lambda: h.b.bind_exit_order(plan, order=replace(order, price=D(8)))
    )
    assert_no_change(
        h.b,
        lambda: h.b.bind_exit_order(plan, order=replace(order, order_type="MARKET")),
    )


def test_partial_sale_retains_child_lock_and_disallows_more_patching(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan, fill, order = h.sell(fraction=F(1, 2))
    state = h.b.snapshot()
    assert state["selected"] == "a"
    assert basis(state["children"]["a"]) == 20
    assert h.plan(price=6) is None
    assert_no_change(h.b, lambda: h.plan("b", 6))
    assert {s["kind"] for s in state["children"]["a"]} == {"ORIGINAL", "PATCH"}
    h.b = SelectedRecoveryBook(h.path, enabled=True)
    h.sell("sell2")
    assert h.b.snapshot()["selected"] is None
    assert h.plan("b", 8) is not None


def test_complete_sale_waits_for_terminal_before_unlock(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan, fill, order = h.sell(terminal=False)
    assert h.b.snapshot()["selected"] == "a"
    assert_no_change(h.b, lambda: h.plan("b"))
    h.b.confirm_terminal(
        replace(order, status="FILLED", filled_amount=fill.amount_base)
    )
    assert h.b.snapshot()["selected"] is None


def test_same_owner_settlement_recycles_without_new_money(tmp_path):
    h = Harness(tmp_path, cash=20)
    h.first()
    before_siblings = {cid: h.b.snapshot()["children"][cid] for cid in "bcde"}
    h.sell()
    state = h.b.snapshot()
    assert F(state["cash"]["PRODUCTIVE"]) == F("19.92")
    assert F(state["cash"]["VCAP"]) == F("24.8004")
    assert F(state["realized"]["PRODUCTIVE"]) == F("-.08")
    assert F(state["realized"]["VCAP"]) == F("4.8004")
    assert {cid: state["children"][cid] for cid in "bcde"} == before_siblings
    h.start("next", 8, "b")
    h.patch("next")
    assert F(h.b.snapshot()["cash"]["VCAP"]) == F("4.8004")
    assert h.b.snapshot()["initial_cash"]["VCAP"] == "20"


def test_sale_receipt_replay_restart_and_conflict(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan, fill, order = h.sell()
    h.b = SelectedRecoveryBook(h.path, enabled=True)
    before = h.b.snapshot()
    h.b.ingest_sale_fill(
        fill, executor_id=order.executor_id, manifest_id=plan.manifest_id
    )
    assert h.b.snapshot() == before
    assert_no_change(
        h.b,
        lambda: h.b.ingest_sale_fill(
            fill, executor_id="wrong", manifest_id=plan.manifest_id
        ),
    )


def test_actual_below_floor_sale_is_recorded_flagged_and_blocks_new_recovery(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan = h.exit_plan()
    order = h.order(
        "sell", plan.target_base, side="SELL", status="OPEN", filled=0, price=10
    )
    h.b.bind_exit_order(plan, order=order)
    fill = h.fill("sell", "bad-fee", plan.target_base, 10, 10, side="SELL")
    h.b.ingest_sale_fill(
        fill, executor_id=order.executor_id, manifest_id=plan.manifest_id
    )
    h.b.confirm_terminal(
        replace(order, status="FILLED", filled_amount=fill.amount_base)
    )
    assert h.b.snapshot()["floor_breaches"] == ["bad-fee"]
    assert h.plan("b") is None
    assert sum(map(F, h.b.snapshot()["realized"].values())) < 0


def test_rational_splitting_exact_with_no_decimal_rounding(tmp_path):
    h = Harness(tmp_path, count=3)
    # With thirds, the exact proposal is intentionally not a Decimal order.
    plan = h.plan()
    assert plan.amount_quote == F(100, 3)
    assert h.b.snapshot()["children"] == {}


def test_no_cash_topup_or_reinitialize_after_progress(tmp_path):
    h = Harness(tmp_path)
    h.first()
    assert_no_change(
        h.b,
        lambda: h.b.initialize(
            ledger=h.ledger,
            governor=h.g,
            position_id="parent",
            account_name="account",
            connector_name="kraken",
            controller_id="controller",
            child_ids=tuple("abcde"),
            recovery_cash_quote=D(1000),
            limits=RecoveryLimits(D(1000), D(5000)),
        ),
    )


def test_database_conservation_corruption_fails_closed(tmp_path):
    h = Harness(tmp_path)
    state = h.b.snapshot()
    state["cash"]["VCAP"] = "999"
    with sqlite3.connect(h.path) as conn:
        conn.execute("UPDATE selected_recovery_book SET state=?", (json.dumps(state),))
    with pytest.raises(ValueError, match="conservation"):
        h.b.snapshot()


@pytest.mark.parametrize("bad", [True, 1.2, D("NaN"), D("Infinity"), D(-1), "8"])
def test_invalid_numeric_inputs_do_not_mutate(tmp_path, bad):
    h = Harness(tmp_path)
    assert_no_change(
        h.b,
        lambda: h.b.plan_patch(
            child_id="a",
            ladder=LADDER,
            current_price=bad,
            remaining_authorized_quote=D(20),
            minimum_quote=D(1),
            evidence_allowed=True,
        ),
    )


def test_source_settlements_keep_exact_original_provenance(tmp_path):
    h = Harness(tmp_path)
    h.first()
    h.sell()
    rows = h.b.snapshot()["sale_allocations"]["sell"]
    original = next(row for row in rows if row["owner"] == "PRODUCTIVE")
    patch = next(row for row in rows if row["owner"] == "VCAP")
    assert (
        original["source_lot_id"],
        original["proposal_id"],
        original["executor_id"],
    ) == ("trade-fill:original", "entry", "exec:entry")
    assert (
        F(original["base_sold"]),
        F(original["cost_released"]),
        F(original["entry_fees_released"]),
    ) == (2, F("19.92"), F(".08"))
    assert patch["source_lot_id"] == "trade-fill:p1"
    assert F(original["net_proceeds"]) + F(patch["net_proceeds"]) == F("44.7204")


def test_exact_child_source_corruption_detected_even_when_totals_match(tmp_path):
    h = Harness(tmp_path)
    h.first()
    state = h.b.snapshot()
    state["children"]["b"][0]["proposal"] = "different-original-owner"
    with sqlite3.connect(h.path) as conn:
        conn.execute("UPDATE selected_recovery_book SET state=?", (json.dumps(state),))
    with pytest.raises(ValueError, match="source owner"):
        h.b.snapshot()


def test_patch_execution_above_bound_rung_is_not_imported(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    fill = h.fill("p1", "high-price", "1.992", 10, ".08")
    h.reconcile(fill)
    assert_no_change(h.b, lambda: h.b.ingest_patch_fill(fill, reconciler=h.r))
    assert not h.b.snapshot()["split"]


def test_added_original_lot_invalidates_pending_split(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    h.authorize("entry2", 10, intent=IntentKind.ENTRY)
    h.bind("entry2", EconomicFillRole.BASE_ENTRY)
    h.reconcile(h.fill("entry2", "new-original", 1, 10))
    fill = h.fill("p1", "p1", "2.49", 8, ".08")
    h.reconcile(fill)
    assert_no_change(h.b, lambda: h.b.ingest_patch_fill(fill, reconciler=h.r))


def test_persisted_trade_cannot_be_replaced_by_similar_economic_lot(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    fill = h.fill("p1", "p1", "2.49", 8, ".08")
    h.reconcile(fill)
    with sqlite3.connect(h.ledger.path) as conn:
        conn.execute(
            "DELETE FROM economic_trade_fill_reconciliation WHERE trade_id='p1'"
        )
    assert_no_change(h.b, lambda: h.b.ingest_patch_fill(fill, reconciler=h.r))


def test_failed_mutation_rolls_back_journal_and_state(tmp_path, monkeypatch):
    import spectra_core.selected_recovery as module

    h = Harness(tmp_path)
    h.start("p1")
    fill = h.fill("p1", "p1", "2.49", 8, ".08")
    h.reconcile(fill)
    before = h.b.snapshot()
    validate = module._validate_state

    def fail_after_transition(state):
        validate(state)
        if state["split"]:
            raise RuntimeError("synthetic commit validation interruption")

    monkeypatch.setattr(module, "_validate_state", fail_after_transition)
    with pytest.raises(RuntimeError):
        h.b.ingest_patch_fill(fill, reconciler=h.r)
    assert h.b.snapshot() == before
    monkeypatch.setattr(module, "_validate_state", validate)
    h.b.ingest_patch_fill(fill, reconciler=h.r)
    assert h.b.snapshot()["split"]


def test_marked_equity_counts_all_unsold_siblings(tmp_path):
    h = Harness(tmp_path)
    h.first()
    h.start("p2", 6)
    h.patch("p2", amount=40, price=6)
    h.start("p3", 4)
    h.patch("p3", amount=80, price=4)
    h.sell(price=6)
    state = h.b.snapshot()
    remaining = [lot for lots in state["children"].values() for lot in lots]
    cash = sum(map(F, state["cash"].values()))
    mark = sum((F(lot["base"]) * 6 for lot in remaining), F(0))
    realized = sum(map(F, state["realized"].values()))
    initial = 100 + 140
    assert cash + mark - initial == realized + mark - basis(remaining)
    assert cash + mark < initial  # selected-child profit is not portfolio profit
    assert basis(remaining) == 80


def test_multiple_partial_buy_fills_and_sale_fills_conserve_exact_slices(tmp_path):
    h = Harness(tmp_path)
    h.start("p1")
    first = h.patch(amount=5, trade="p1-a", terminal=False)
    second = h.patch(amount=15, trade="p1-b", terminal=False)
    h.finish("p1", F(first.amount_base) + F(second.amount_base))
    plan = h.exit_plan()
    order = h.order(
        "sell", plan.target_base, side="SELL", status="OPEN", filled=0, price=10
    )
    h.b.bind_exit_order(plan, order=order)
    for index, amount in enumerate((F(".01"), F("1.23"), plan.target_base - F("1.24"))):
        fill = h.fill(
            "sell", f"sale:{index}", amount, 10, amount * F(".04"), side="SELL"
        )
        h.b.ingest_sale_fill(
            fill, executor_id=order.executor_id, manifest_id=plan.manifest_id
        )
        h.b = SelectedRecoveryBook(h.path, enabled=True)
    h.b.confirm_terminal(
        replace(order, status="FILLED", filled_amount=decimal(plan.target_base))
    )
    state = h.b.snapshot()
    assert state["selected"] is None
    assert F(state["cash"]["VCAP"]) == F("144.8004")
    assert F(state["cash"]["PRODUCTIVE"]) == F("19.92")
    assert (
        sum(
            F(row["base_sold"])
            for rows in state["sale_allocations"].values()
            for row in rows
        )
        == plan.target_base
    )


def test_no_whole_parent_or_other_child_approval_can_change_exit_manifest(tmp_path):
    h = Harness(tmp_path)
    h.first()
    plan = h.exit_plan()
    order = h.order(
        "sell", plan.target_base, side="SELL", status="OPEN", filled=0, price=10
    )
    h.b.bind_exit_order(plan, order=order)
    fill = h.fill("sell", "sale", plan.target_base, 10, 0, side="SELL")
    assert_no_change(
        h.b,
        lambda: h.b.ingest_sale_fill(
            fill, executor_id=order.executor_id, manifest_id="whole-parent-exit"
        ),
    )
    assert_no_change(
        h.b,
        lambda: h.b.ingest_sale_fill(
            replace(fill, amount_base=D(10)),
            executor_id=order.executor_id,
            manifest_id=plan.manifest_id,
        ),
    )
