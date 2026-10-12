import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_partial_recovery_recycling import add_recovery, governor, ledger, rule
from test_vcap_lifecycle_controller import healthy_accounting

from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.partial_attempt import PartialAttemptDisposition as Outcome
from spectra_core.partial_attempt import PartialAttemptPolicy, assess_partial_attempt
from spectra_core.partial_recycle import PartialRecycleStore, plan_partial_recycle
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    plan_vcap_lifecycle_from_ledger,
)
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)


@pytest.fixture
def harness(tmp_path):
    g, book = governor(tmp_path), ledger(tmp_path)
    add_recovery(
        g,
        book,
        proposal_id="owner",
        executor_id="executor",
        lot_id="lot",
        base="10",
        basis="4",
        occurred_at=1010,
    )
    rules = rule(min_quote="4")
    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rules,
        current_price=D("0.6"),
        expected_exit_fee_bps=D(40),
        slippage_allowance_bps=D(5),
        now=2000,
    )
    assert decision.manifest is not None
    args = dict(
        manifest=decision.manifest,
        ledger=book,
        governor=g,
        store=PartialRecycleStore(book.path),
        trading_rule=rules,
        current_price=D("0.3"),
        observed_at=2000,
        now=2000,
        expected_exit_fee_bps=D(40),
        slippage_allowance_bps=D(5),
        target_net_profit_bps=D(174),
        pending_order_ids=(),
        policy=PartialAttemptPolicy(enabled=True),
    )
    return args


def snapshot(args):
    result = []
    for path in (args["ledger"].path, args["governor"].db_path):
        with sqlite3.connect(path) as conn:
            result.append(tuple(conn.iterdump()))
    return tuple(result)


def test_default_disabled_and_no_spend_authority(harness):
    outcome = assess_partial_attempt(**{**harness, "policy": None})
    assert outcome.disposition == Outcome.DISABLED
    assert outcome.permits_dispatch is False


def test_price_deferral_then_same_manifest_reactivation_is_read_only(harness):
    before = snapshot(harness)
    low = assess_partial_attempt(**harness)
    high = assess_partial_attempt(
        **{**harness, "current_price": D("0.6"), "now": 2001, "observed_at": 2001}
    )
    assert low.disposition == Outcome.DEFER_PRICE
    assert "current_price_quote_minimum" in low.reasons
    assert "current_price_net_profit_floor" in low.reasons
    assert high.disposition == Outcome.ELIGIBLE
    assert high.permits_dispatch is False
    assert low.manifest_id == high.manifest_id
    assert snapshot(harness) == before


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"pending_order_ids": None}, "pending_order_evidence_missing"),
        ({"pending_order_ids": ("order",)}, "existing_position_order_pending"),
        ({"observed_at": 2001}, "price_observation_missing_or_stale"),
        ({"observed_at": 1900}, "price_observation_missing_or_stale"),
        ({"observed_at": None}, "price_observation_time_invalid"),
        ({"observed_at": float("nan")}, "price_observation_time_invalid"),
        ({"current_price": D("NaN")}, "price_observation_invalid"),
        ({"current_price": D(0)}, "price_observation_invalid"),
        ({"expected_exit_fee_bps": D(10000)}, "cost_policy_invalid"),
        ({"slippage_allowance_bps": D(-1)}, "cost_policy_invalid"),
    ],
)
def test_unknown_pending_stale_future_invalid_costs_fail_closed(
    harness, updates, reason
):
    before = snapshot(harness)
    result = assess_partial_attempt(**{**harness, **updates})
    assert result.disposition == Outcome.BLOCKED
    assert result.reasons == (reason,)
    assert snapshot(harness) == before


def test_actual_quantity_minimum_is_not_price_deferral(harness):
    rules = replace(harness["trading_rule"], min_order_size=D(11))
    result = assess_partial_attempt(
        **{**harness, "trading_rule": rules, "current_price": D(10000)}
    )
    assert result.reasons == ("base_minimum_requires_explicit_repair",)


def test_rounding_does_not_silently_trim_manifest_or_reassign_remainder(harness):
    rules = replace(harness["trading_rule"], min_base_amount_increment=D(3))
    result = assess_partial_attempt(
        **{**harness, "trading_rule": rules, "current_price": D(10)}
    )
    assert result.reasons == ("quantity_rounding_requires_explicit_reconciliation",)
    assert result.quantity == 10
    assert harness["ledger"].get_lot("lot").base_remaining == 10


def test_partial_fill_reassesses_current_exact_inventory_and_basis(harness):
    book = harness["ledger"]
    book.record_partial_exit(
        event_id="partial",
        position_id="xrp-position-1",
        base_sold=D(5),
        gross_proceeds_quote=D(3),
        occurred_at=2001,
        eligible_lot_ids=("lot",),
    )
    result = assess_partial_attempt(**{**harness, "current_price": D("0.6")})
    assert result.quantity == 5
    assert result.basis_quote == 2
    assert result.disposition == Outcome.DEFER_PRICE
    assert harness["store"].open_for_position("xrp-position-1") == harness["manifest"]


def test_consumed_selected_lot_or_completed_manifest_never_reassigned(harness):
    harness["store"].mark_completed(harness["manifest"].manifest_id, now=2001)
    result = assess_partial_attempt(**harness)
    assert result.reasons == ("manifest_not_current_open_intent",)


def test_owner_tamper_is_blocked(harness):
    with sqlite3.connect(harness["ledger"].path) as conn:
        conn.execute(
            "UPDATE economic_lots SET executor_id='foreign' WHERE lot_id='lot'"
        )
    assert assess_partial_attempt(**harness).reasons == (
        "native_manifest_validation_failed",
    )


def test_lifecycle_deferral_preserves_rungs_intent_reservations_and_rechecks(
    harness, tmp_path
):
    args = harness
    store = VCAPStateStore(tmp_path / "lifecycle.db")
    state = VCAPLifecycleState(
        envelope_proposal_id="envelope",
        pair="XRP-USD",
        next_sequence=3,
        remaining_authorized_quote=D(50),
        recovery_proposal_ids=("owner",),
        position_base=D(10),
        cost_basis_quote=D(4),
        last_known_price=D("0.3"),
        profit_state=None,
        status=VCAPLifecycleStatus.ACTIVE,
        updated_at=2000,
    )
    store.save(state)
    params = dict(
        position_id="xrp-position-1",
        ledger=args["ledger"],
        state=state,
        store=store,
        fingerprint=AssetFingerprint.cold_start(
            AssetObservation("XRP-USD", 2000, 0.3, 1000000)
        ),
        trading_rule=args["trading_rule"],
        current_price=D("0.3"),
        seed_quote=D(40),
        expected_exit_fee_bps=D(40),
        slippage_allowance_bps=D(5),
        now=2000,
        governor=args["governor"],
        accounting_health=healthy_accounting("xrp-position-1", 2000),
    )
    before = snapshot(args)
    default = plan_vcap_lifecycle_from_ledger(**params)
    assert default.action == VCAPLifecycleAction.PARTIAL_RECYCLE
    store.save(state)
    for _ in range(3):
        result = plan_vcap_lifecycle_from_ledger(
            **params,
            partial_attempt_policy=PartialAttemptPolicy(enabled=True),
            partial_price_observed_at=2000,
            pending_order_ids=(),
        )
        assert result.action == VCAPLifecycleAction.HOLD
        assert result.state == state
        assert result.execution_decision is None
        assert store.load("envelope") == state
    assert snapshot(args) == before
    assert args["store"].open_for_position("xrp-position-1") == args["manifest"]


def test_future_execution_price_is_not_an_accepted_input(harness):
    with pytest.raises(TypeError):
        assess_partial_attempt(**harness, next_open=D(100))


def test_native_additive_cost_floor_is_not_weakened_by_compounding(harness):
    result = assess_partial_attempt(
        **{**harness, "current_price": D("0.4087991875451763952893969410")}
    )
    assert result.disposition == Outcome.DEFER_PRICE
    assert "current_price_net_profit_floor" in result.reasons
    result = assess_partial_attempt(
        **{
            **harness,
            "expected_exit_fee_bps": D(5000),
            "slippage_allowance_bps": D(5000),
        }
    )
    assert result.disposition == Outcome.BLOCKED
    assert result.reasons == ("cost_policy_invalid",)
