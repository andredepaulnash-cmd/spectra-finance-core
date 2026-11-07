"""Independent cash-authority regressions, using real durable Governor state.

Venue fixtures control observable fills, never the resulting cash accounting.
The central rule is sale purpose: a recovery-child sale parks its net proceeds,
even when it closes a parent; a whole exit remains productive.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from threading import Barrier

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    FillReconciliationStatus,
    LiveFillLedgerReconciler,
)
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.partial_recycle import (
    PartialRecyclePolicy,
    PartialRecycleStore,
    plan_partial_recycle,
)
from tests import test_protected_recovery_exit as partial_evidence
from tests import test_protected_whole_exit as whole_evidence
from tests.test_live_fill_reconciliation import fill as live_trade_row
from tests.test_partial_recovery_recycling import add_recovery, ledger, rule
from tests.test_protected_recovery_exit import protected_case
from tests.test_protected_whole_exit import whole_case

D = Decimal
SCOPE = dict(account_name="master_account", connector_name="kraken", quote_asset="USD")


def make_governor(tmp_path, *, reserve_floor="0"):
    return CapitalGovernor(
        tmp_path / "park-authority.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D(reserve_floor),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("5000"),
            reservation_ttl_seconds=120,
        ),
    )


def activate(g, *, productive="100", parked="0", **changes):
    fields = dict(
        **SCOPE,
        opening_productive_quote=D(productive),
        opening_parked_quote=D(parked),
        activation_id="reviewed-opening-1",
        activated_at=2000,
    )
    fields.update(changes)
    return g.activate_cash_authority(**fields)


def proposal(identifier, amount, *, intent=IntentKind.ENTRY, **scope_changes):
    scope = dict(SCOPE)
    scope.update(scope_changes)
    return CapitalProposal(
        proposal_id=identifier,
        pair="XRP-USD",
        intent=intent,
        amount_quote=D(amount),
        **scope,
    )


def venue_snapshot(amount="100", **changes):
    fields = dict(SCOPE, free_quote=D(amount))
    fields.update(changes)
    return CapitalSnapshot(**fields)


def cash(g):
    value = g.cash_authority_snapshot()
    return None if value is None else asdict(value)


def cash_fill(g, *, purpose="RECOVERY_PARTIAL", identifier="cash-trade-1", **changes):
    fields = dict(
        event_id=f"cash-event:{identifier}",
        manifest_id="recovery-manifest-1",
        trade_id=identifier,
        order_id="sale-order-1",
        sale_purpose=purpose,
        pair="XRP-USD",
        account_name=SCOPE["account_name"],
        connector_name=SCOPE["connector_name"],
        gross_proceeds_quote=D("10"),
        exit_fee_quote=D("0.04"),
        executor_id="synthetic-sale-1",
        base_sold=D("20"),
        price=D("0.5"),
        fee_currency="USD",
        fee_paid=D("0.04"),
        occurred_at=2002,
    )
    fields.update(changes)
    g.bind_cash_sale(
        manifest_id=fields["manifest_id"],
        sell_executor_id=fields["executor_id"],
        pair=fields["pair"],
        sale_purpose=fields["sale_purpose"],
        account_name=fields["account_name"],
        connector_name=fields["connector_name"],
        quote_asset="USD",
    )
    assert g.begin_cash_fill(**fields)
    return fields


def settle(g, fields):
    proof = hashlib.sha256(repr(sorted(fields.items())).encode()).hexdigest()
    return g.settle_cash_fill(event_id=fields["event_id"], settlement_evidence=proof)


def test_unbound_legacy_database_does_not_invent_historical_parked_cash(tmp_path):
    g = make_governor(tmp_path)
    old = CapitalProposal("legacy", "XRP-USD", IntentKind.ENTRY, D("10"))
    assert g.reserve(old, CapitalSnapshot(free_quote=D("100"))).status == (
        DecisionStatus.RESERVED
    )
    restarted = CapitalGovernor(g.db_path, g.policy)
    assert restarted.get_decision("legacy").status == DecisionStatus.RESERVED
    assert cash(restarted) is None


def test_activation_is_immutable_idempotent_and_survives_restart(tmp_path):
    g = make_governor(tmp_path)
    activate(g, productive="73.25", parked="26.75")
    before = cash(g)
    activate(g, productive="73.25", parked="26.75")
    restarted = CapitalGovernor(g.db_path, g.policy)
    assert cash(restarted) == before
    assert before["productive_quote"] == D("73.25")
    assert before["parked_quote"] == D("26.75")
    assert activate(g, productive="100", parked="0") is False
    assert cash(g)["productive_quote"] == D("73.25")
    assert cash(g)["parked_quote"] == D("26.75")


@pytest.mark.parametrize("stage", ["RESERVED", "COMMITTED"])
def test_activation_requires_existing_spend_claims_to_be_reconciled(tmp_path, stage):
    g = make_governor(tmp_path)
    old = CapitalProposal("legacy-open", "XRP-USD", IntentKind.RECOVERY, D("10"))
    assert g.reserve(old, CapitalSnapshot(free_quote=D("100"))).status == (
        DecisionStatus.RESERVED
    )
    if stage == "COMMITTED":
        assert g.commit_if_matches(old)
    with pytest.raises(RuntimeError):
        activate(g)
    assert cash(g) is None


@pytest.mark.parametrize("intent", [IntentKind.ENTRY, IntentKind.RECOVERY])
def test_parked_cash_cannot_fund_ordinary_or_recovery_reservations(tmp_path, intent):
    g = make_governor(tmp_path)
    activate(g, productive="70", parked="30")
    too_large = g.reserve(
        proposal("cannot-raid-park", "70.01", intent=intent), venue_snapshot("100")
    )
    assert too_large.status == DecisionStatus.REJECTED
    allowed = g.reserve(
        proposal("within-productive", "70", intent=intent), venue_snapshot("100")
    )
    assert allowed.status == DecisionStatus.RESERVED
    assert allowed.available_after_quote == D("30")
    assert cash(g)["parked_quote"] == D("30")


def test_unknown_venue_cash_increase_never_grants_productive_capital(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    decision = g.reserve(proposal("unknown-deposit", "100.01"), venue_snapshot("999"))
    assert decision.status == DecisionStatus.REJECTED
    assert cash(g)["productive_quote"] == D("100")


def test_venue_free_cash_is_an_independent_upper_bound(tmp_path):
    g = make_governor(tmp_path)
    activate(g, productive="100", parked="20")
    decision = g.reserve(proposal("venue-lower", "15.01"), venue_snapshot("35"))
    assert decision.status == DecisionStatus.REJECTED
    allowed = g.reserve(proposal("venue-exact", "15"), venue_snapshot("35"))
    assert allowed.status == DecisionStatus.RESERVED


def test_reserve_floor_is_preserved_after_park_and_pending_claims(tmp_path):
    g = make_governor(tmp_path, reserve_floor="30")
    activate(g, productive="70", parked="30")
    first = proposal("first", "60")
    assert g.reserve(first, venue_snapshot("100")).status == DecisionStatus.RESERVED
    assert g.reserve(
        proposal("floor-breach", "10.01"), venue_snapshot("100")
    ).status == (DecisionStatus.REJECTED)
    assert g.reserve(proposal("floor-exact", "10"), venue_snapshot("100")).status == (
        DecisionStatus.RESERVED
    )


@pytest.mark.parametrize(
    "venue_free,amount,status",
    [
        ("600", "100", DecisionStatus.RESERVED),
        ("600", "100.01", DecisionStatus.REJECTED),
        ("599", "100", DecisionStatus.REJECTED),
    ],
)
def test_park_can_satisfy_physical_floor_without_funding_buy(
    tmp_path,
    venue_free,
    amount,
    status,
):
    g = make_governor(tmp_path, reserve_floor="500")
    activate(g, productive="100", parked="500")
    request = proposal("physical-floor-boundary", amount)
    decision = g.reserve(request, venue_snapshot(venue_free))
    assert decision.status == status
    if status == DecisionStatus.RESERVED:
        assert decision.available_after_quote == D("500")
        assert g.commit_if_matches(request)
        assert cash(g)["productive_quote"] == D("0")
    else:
        assert not g.commit_if_matches(request)
        assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["parked_quote"] == D("500")


def test_zero_floor_does_not_allow_venue_parked_cash_to_fund_buy(tmp_path):
    g = make_governor(tmp_path, reserve_floor="0")
    activate(g, productive="100", parked="50")
    request = proposal("venue-has-only-park", "1")
    assert g.reserve(request, venue_snapshot("50")).status == DecisionStatus.REJECTED
    assert not g.commit_if_matches(request)
    assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["parked_quote"] == D("50")


def test_commit_burns_productive_once_without_subtracting_committed_twice(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    spend = proposal("spend", "60")
    assert g.reserve(spend, venue_snapshot("100")).status == DecisionStatus.RESERVED
    assert g.commit_if_matches(spend)
    assert cash(g)["productive_quote"] == D("40")
    assert not g.commit_if_matches(spend)
    assert cash(g)["productive_quote"] == D("40")
    assert activate(g) is True
    assert cash(g)["productive_quote"] == D("40")
    # Venue still reports pre-buy free cash; the old COMMITTED venue claim is
    # conservatively charged there, but the productive debit is already final.
    next_spend = proposal("next-spend", "40")
    assert (
        g.reserve(next_spend, venue_snapshot("100")).status == DecisionStatus.RESERVED
    )
    assert g.commit_if_matches(next_spend)
    assert cash(g)["productive_quote"] == D("0")


def test_release_of_reserved_claim_does_not_mint_productive_cash(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    pending = proposal("reserved", "80")
    assert g.reserve(pending, venue_snapshot("100")).status == DecisionStatus.RESERVED
    assert g.release(pending.proposal_id)
    assert cash(g)["productive_quote"] == D("100")
    assert g.reserve(proposal("replacement", "100"), venue_snapshot("100")).status == (
        DecisionStatus.RESERVED
    )


def test_expired_claim_restores_only_the_reservation_not_new_cash(
    tmp_path, monkeypatch
):
    import spectra_core.capital_governor as governor_module

    g = make_governor(tmp_path)
    activate(g)
    monkeypatch.setattr(governor_module.time, "time", lambda: 3000)
    pending = proposal("expires-before-commit", "80")
    assert g.reserve(pending, venue_snapshot("100")).status == DecisionStatus.RESERVED
    monkeypatch.setattr(governor_module.time, "time", lambda: 3121)
    assert not g.commit_if_matches(pending)
    assert g.get_decision(pending.proposal_id).status == DecisionStatus.EXPIRED
    assert cash(g)["productive_quote"] == D("100")
    assert g.reserve(
        proposal("replacement-after-expiry", "100"), venue_snapshot("100")
    ).status == (DecisionStatus.RESERVED)


def test_new_sale_cash_is_held_before_inventory_reconciliation(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g)
    assert cash(g)["held_quote"] == D("9.96")
    assert g.reserve(
        proposal("during-cross-db-gap", "1"), venue_snapshot("109.96")
    ).status == (DecisionStatus.REJECTED)
    restarted = CapitalGovernor(g.db_path, g.policy)
    assert cash(restarted)["held_quote"] == D("9.96")
    assert restarted.reserve(
        proposal("after-crash", "1"), venue_snapshot("109.96")
    ).status == (DecisionStatus.REJECTED)
    settle(restarted, fields)
    assert cash(restarted)["parked_quote"] == D("9.96")
    assert cash(restarted)["productive_quote"] == D("100")
    assert cash(restarted)["held_quote"] == D("0")


@pytest.mark.parametrize(
    "purpose,destination",
    [
        ("RECOVERY_PARTIAL", "parked_quote"),
        ("WHOLE_EXIT", "productive_quote"),
    ],
)
def test_confirmed_net_cash_routes_exactly_once_by_sale_purpose(
    tmp_path, purpose, destination
):
    g = make_governor(tmp_path)
    activate(g, productive="20")
    fields = cash_fill(g, purpose=purpose)
    settle(g, fields)
    expected = D("9.96") + (D("20") if destination == "productive_quote" else D("0"))
    assert cash(g)[destination] == expected
    before = cash(g)
    cash_fill(g, purpose=purpose)
    settle(g, fields)
    restarted = CapitalGovernor(g.db_path, g.policy)
    assert cash(restarted) == before


def test_lagged_venue_cash_does_not_double_credit_or_debit_sale_money(tmp_path):
    g = make_governor(tmp_path)
    activate(g, productive="20")
    fields = cash_fill(g, purpose="WHOLE_EXIT")
    settle(g, fields)
    assert g.reserve(
        proposal("before-venue-catches-up", "20.01"), venue_snapshot("20")
    ).status == (DecisionStatus.REJECTED)
    assert g.reserve(
        proposal("after-venue-catches-up", "29.96"), venue_snapshot("29.96")
    ).status == (DecisionStatus.RESERVED)
    assert cash(g)["productive_quote"] == D("29.96")


def test_base_paid_sell_fee_does_not_reduce_quote_cash_proceeds(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g, fee_currency="XRP", fee_paid=D("0.04"), exit_fee_quote=D("0"))
    settle(g, fields)
    assert cash(g)["parked_quote"] == D("10")
    assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["blocked"] is False


@pytest.mark.parametrize(
    "changed",
    [
        {"gross_proceeds_quote": D("10.01")},
        {"exit_fee_quote": D("0.05"), "fee_paid": D("0.05")},
        {"order_id": "different-order"},
        {"price": D("0.5005")},
        {"base_sold": D("19")},
        {"occurred_at": 2003},
    ],
)
def test_conflicting_fill_replay_never_grants_fresh_cash(tmp_path, changed):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g)
    settle(g, fields)
    before = cash(g)
    assert g.begin_cash_fill(**(fields | changed)) is False
    after = cash(g)
    assert after["productive_quote"] == before["productive_quote"]
    assert after["parked_quote"] == before["parked_quote"]
    assert after["blocked"] is True
    assert g.reserve(
        proposal("conflict-cannot-spend", "1"), venue_snapshot("1000")
    ).status == (DecisionStatus.REJECTED)


def test_same_venue_trade_cannot_be_credited_under_another_event_identity(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g)
    settle(g, fields)
    assert g.begin_cash_fill(**(fields | {"event_id": "different-cash-event"})) is False
    assert cash(g)["parked_quote"] == D("9.96")


def test_saved_manifest_sale_purpose_cannot_be_reinterpreted(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g)
    assert (
        g.bind_cash_sale(
            manifest_id=fields["manifest_id"],
            sell_executor_id=fields["executor_id"],
            pair="XRP-USD",
            sale_purpose="WHOLE_EXIT",
            quote_asset="USD",
            account_name=SCOPE["account_name"],
            connector_name=SCOPE["connector_name"],
        )
        is False
    )
    assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["held_quote"] == D("9.96")


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_name", "other-account"),
        ("connector_name", "binance"),
        ("quote_asset", "EUR"),
    ],
)
def test_bound_cash_cannot_be_spent_from_another_scope(tmp_path, field, value):
    g = make_governor(tmp_path)
    activate(g)
    decision = g.reserve(
        proposal("wrong-scope", "1", **{field: value}),
        venue_snapshot("100", **{field: value}),
    )
    assert decision.status == DecisionStatus.REJECTED
    assert cash(g)["productive_quote"] == D("100")


def test_unscoped_legacy_proposal_cannot_spend_activated_cash(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    legacy = CapitalProposal("unscoped", "XRP-USD", IntentKind.ENTRY, D("1"))
    assert g.reserve(legacy, CapitalSnapshot(free_quote=D("100"))).status == (
        DecisionStatus.REJECTED
    )


def test_fill_before_reviewed_activation_cannot_become_new_cash(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g)
    assert (
        g.begin_cash_fill(
            **(
                fields
                | {
                    "event_id": "historical-event",
                    "trade_id": "historical-trade",
                    "occurred_at": 1999,
                }
            )
        )
        is False
    )
    assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["held_quote"] == D("9.96")


def test_pending_fill_blocks_previously_reserved_commit_and_recovery_carve(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    envelope = proposal("reserved-recovery-envelope", "60", intent=IntentKind.RECOVERY)
    assert g.reserve(envelope, venue_snapshot("100")).status == DecisionStatus.RESERVED
    cash_fill(g)
    assert not g.commit_if_matches(envelope)
    child = proposal("blocked-child", "10", intent=IntentKind.RECOVERY)
    assert not g.carve_reserved_recovery_tranche(
        envelope_proposal_id=envelope.proposal_id, tranche_proposal=child
    )
    assert g.get_decision(child.proposal_id) is None
    assert cash(g)["productive_quote"] == D("100")


def test_lower_scoped_venue_balance_blocks_old_commit_and_carve(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    envelope = proposal("old-envelope", "60", intent=IntentKind.RECOVERY)
    assert g.reserve(envelope, venue_snapshot("100")).status == DecisionStatus.RESERVED
    # A later valid scoped venue observation reports less free cash. Even a
    # rejected new proposal must not leave old claims trusting the old balance.
    assert g.reserve(
        proposal("later-observation", "1"), venue_snapshot("30")
    ).status == (DecisionStatus.REJECTED)
    assert not g.commit_if_matches(envelope)
    assert not g.carve_reserved_recovery_tranche(
        envelope_proposal_id=envelope.proposal_id,
        tranche_proposal=proposal("new-child", "10", intent=IntentKind.RECOVERY),
    )
    assert cash(g)["productive_quote"] == D("100")
    assert g.get_decision("new-child") is None


def test_recovery_carve_preserves_fee_claim_and_does_not_burn_twice(tmp_path):
    g = make_governor(tmp_path)
    activate(g, parked="25", entry_fee_buffer_bps=D("40"))
    envelope = proposal("fee-envelope", "60", intent=IntentKind.RECOVERY)
    assert g.reserve(envelope, venue_snapshot("125")).status == DecisionStatus.RESERVED
    child = proposal("fee-child", "10", intent=IntentKind.RECOVERY)
    assert g.carve_reserved_recovery_tranche(
        envelope_proposal_id=envelope.proposal_id, tranche_proposal=child
    )
    assert cash(g)["productive_quote"] == D("100")
    assert g.commit_if_matches(child)
    assert cash(g)["productive_quote"] == D("89.96")
    remainder = proposal(envelope.proposal_id, "50", intent=IntentKind.RECOVERY)
    assert g.commit_if_matches(remainder)
    assert cash(g)["productive_quote"] == D("39.76")
    assert cash(g)["parked_quote"] == D("25")
    assert not g.commit_if_matches(child)
    assert not g.commit_if_matches(remainder)
    assert cash(g)["productive_quote"] == D("39.76")


def test_concurrent_reserve_and_commit_cannot_overdraw_productive_cash(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    barrier = Barrier(2)

    def worker(identifier):
        instance = CapitalGovernor(g.db_path, g.policy)
        request = proposal(identifier, "70")
        barrier.wait()
        decision = instance.reserve(request, venue_snapshot("1000"))
        return decision.status, instance.commit_if_matches(request)

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(worker, ["concurrent-a", "concurrent-b"]))
    assert sum(status == DecisionStatus.RESERVED for status, _ in outcomes) == 1
    assert sum(committed for _, committed in outcomes) == 1
    assert cash(g)["productive_quote"] == D("30")


def test_exact_cash_arithmetic_preserves_sub_cent_credit_without_tolerance(tmp_path):
    g = make_governor(tmp_path)
    activate(g, productive="1")
    fields = cash_fill(
        g,
        purpose="WHOLE_EXIT",
        gross_proceeds_quote=D("0.000000000000000000000000000009"),
        exit_fee_quote=D("0.000000000000000000000000000001"),
        base_sold=D("0.000000000000000000000000000018"),
        fee_paid=D("0.000000000000000000000000000001"),
    )
    settle(g, fields)
    assert cash(g)["productive_quote"] == D("1.000000000000000000000000000008")
    assert g.reserve(
        proposal("exact-main-claim", "1"), venue_snapshot("10")
    ).status == (DecisionStatus.RESERVED)
    assert g.reserve(
        proposal("exact-last-shard", "0.000000000000000000000000000008"),
        venue_snapshot("10"),
    ).status == (DecisionStatus.RESERVED)
    assert g.reserve(
        proposal("microscopic-overdraft", "0.000000000000000000000000000004"),
        venue_snapshot("10"),
    ).status == (DecisionStatus.REJECTED)


def test_settlement_ack_without_pending_exact_fill_never_grants_cash(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    assert g.settle_cash_fill("missing-trade", settlement_evidence="a" * 64) is False
    assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["blocked"] is True


def test_changed_settlement_evidence_never_recredits_a_settled_fill(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    fields = cash_fill(g)
    settle(g, fields)
    assert g.settle_cash_fill(fields["event_id"], settlement_evidence="a" * 64) is False
    assert cash(g)["parked_quote"] == D("9.96")
    assert cash(g)["productive_quote"] == D("100")
    assert cash(g)["blocked"] is True


def authorized_buy(g, *, identifier="buy", amount="60"):
    request = proposal(identifier, amount)
    assert g.reserve(request, venue_snapshot("100")).status == DecisionStatus.RESERVED
    assert g.commit_if_matches(request)
    assert g.reconcile_execution(
        proposal_id=request.proposal_id,
        executor_id=f"executor:{identifier}",
        account_name=SCOPE["account_name"],
        connector_name=SCOPE["connector_name"],
        controller_id="spectra",
    )
    return request


def buy_fill_fields(request, **changes):
    fields = dict(
        trade_id="actual-buy-1",
        order_id="actual-buy-order",
        proposal_id=request.proposal_id,
        executor_id=f"executor:{request.proposal_id}",
        pair=request.pair,
        account_name=SCOPE["account_name"],
        connector_name=SCOPE["connector_name"],
        gross_spent_quote=D("59"),
        base_acquired=D("118"),
        price=D("0.5"),
        fee_paid=D("1"),
        fee_currency="USD",
        occurred_at=2002,
    )
    fields.update(changes)
    return fields


def test_exact_buy_evidence_does_not_repeat_committed_cash_debit(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    request = authorized_buy(g)
    fields = buy_fill_fields(request)
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("40")
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("40")
    assert cash(g)["blocked"] is False


def test_actual_buy_fees_over_authorization_are_charged_once_and_freeze_spending(
    tmp_path,
):
    g = make_governor(tmp_path)
    activate(g)
    request = authorized_buy(g)
    fields = buy_fill_fields(request, gross_spent_quote=D("60"), base_acquired=D("120"))
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("39")
    assert cash(g)["blocked"] is True
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("39")
    assert g.reserve(
        proposal("after-fee-excess", "1"), venue_snapshot("1000")
    ).status == (DecisionStatus.REJECTED)


def test_configured_entry_fee_buffer_authorizes_normal_fee_without_raiding_park(
    tmp_path,
):
    g = make_governor(tmp_path)
    activate(g, parked="50", entry_fee_buffer_bps=D("40"))
    request = authorized_buy(g, identifier="fee-buffered-entry", amount="10")
    fields = buy_fill_fields(
        request,
        gross_spent_quote=D("10"),
        base_acquired=D("20"),
        fee_paid=D("0.04"),
    )
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("89.96")
    assert cash(g)["parked_quote"] == D("50")
    assert cash(g)["blocked"] is False
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("89.96")


@pytest.mark.parametrize("intent", [IntentKind.ENTRY, IntentKind.RECOVERY])
def test_capital_without_room_for_entry_fee_cannot_reserve_notional_only(
    tmp_path, intent
):
    g = make_governor(tmp_path)
    activate(g, productive="10", entry_fee_buffer_bps=D("40"))
    assert g.reserve(
        proposal("fee-shortfall", "10", intent=intent), venue_snapshot("10")
    ).status == (DecisionStatus.REJECTED)
    assert cash(g)["productive_quote"] == D("10")


def test_base_asset_buy_fee_does_not_invent_a_quote_cash_debit(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    request = authorized_buy(g)
    fields = buy_fill_fields(
        request,
        gross_spent_quote=D("60"),
        base_acquired=D("120"),
        fee_paid=D("1"),
        fee_currency="XRP",
    )
    assert g.record_cash_buy_fill(**fields)
    assert cash(g)["productive_quote"] == D("40")
    assert cash(g)["blocked"] is False


def test_conflicting_buy_replay_never_changes_cash_authority_twice(tmp_path):
    g = make_governor(tmp_path)
    activate(g)
    request = authorized_buy(g)
    fields = buy_fill_fields(request)
    assert g.record_cash_buy_fill(**fields)
    assert g.record_cash_buy_fill(**(fields | {"order_id": "wrong-order"})) is False
    assert cash(g)["productive_quote"] == D("40")
    assert cash(g)["blocked"] is True


@pytest.mark.parametrize(
    "fee,remaining,blocked",
    [
        ("0.04", "89.96", False),
        ("0.05", "89.95", True),
    ],
)
def test_real_bound_buy_fill_preserves_inventory_and_cash_truth_with_fee_buffer(
    tmp_path,
    fee,
    remaining,
    blocked,
):
    g = make_governor(tmp_path)
    activate(g, entry_fee_buffer_bps=D("40"))
    request = authorized_buy(g, identifier="live-bound-entry", amount="10")
    book = EconomicPositionLedger(tmp_path / "cash-buy-economic.sqlite3")
    book.open_position(
        position_id="cash-funded-position",
        pair="XRP-USD",
        created_at=2001,
    )
    reconciler = LiveFillLedgerReconciler(ledger=book, governor=g)
    reconciler.bind_executor_order(
        order_id="live-bound-buy-order",
        position_id="cash-funded-position",
        role=EconomicFillRole.BASE_ENTRY,
        proposal_id=request.proposal_id,
        account_name=SCOPE["account_name"],
        connector_name=SCOPE["connector_name"],
        pair=request.pair,
        created_at=2001,
    )
    confirmed = live_trade_row(
        trade_id="live-bound-buy-trade",
        order_id="live-bound-buy-order",
        side="BUY",
        amount="20",
        price="0.5",
        fee=fee,
        fee_currency="USD",
        timestamp="1970-01-01T00:33:22+00:00",
    )
    applied = reconciler.reconcile_trade_fill(confirmed, reconciled_at=2003)
    assert applied.status == FillReconciliationStatus.APPLIED
    owned = book.get_lot("trade-fill:live-bound-buy-trade")
    assert owned.proposal_id == request.proposal_id
    assert owned.executor_id == "executor:live-bound-entry"
    assert owned.base_remaining == D("20")
    assert owned.cost_basis_remaining_quote == D("10")
    assert owned.entry_fees_remaining_quote == D(fee)
    assert cash(g)["productive_quote"] == D(remaining)
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["held_quote"] == D("0")
    assert cash(g)["blocked"] is blocked
    before = cash(g)
    restarted_book = EconomicPositionLedger(book.path)
    restarted_governor = CapitalGovernor(g.db_path, g.policy)
    replay = LiveFillLedgerReconciler(
        ledger=restarted_book,
        governor=restarted_governor,
    ).reconcile_trade_fill(confirmed, reconciled_at=2004)
    assert replay.status == FillReconciliationStatus.REPLAY
    assert cash(restarted_governor) == before
    assert restarted_book.snapshot("cash-funded-position").active_base == D("20")
    spend = restarted_governor.reserve(
        proposal("entry-after-confirmed-buy", "1"),
        venue_snapshot(remaining),
    )
    assert spend.status == (
        DecisionStatus.REJECTED if blocked else DecisionStatus.RESERVED
    )


def test_recovery_sale_ack_without_fill_does_not_park_or_release_cash(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    activate(g, productive="1000")
    client = partial_evidence.EvidenceClient(decision.manifest)
    partial_evidence.install_evidence(monkeypatch, decision.manifest)
    result = partial_evidence.execute(protected_case, client)
    assert result["executed"] is True
    assert result["result"]["completed"] is False
    assert cash(g)["productive_quote"] == D("1000")
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["held_quote"] == D("0")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")


def test_recovery_child_sale_parks_all_net_proceeds_once_and_keeps_entry_owned(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    activate(g, productive="1000")
    manifest = decision.manifest
    client = partial_evidence.EvidenceClient(manifest)
    client.executors.known = True
    traded = partial_evidence.fill(manifest, "park-integration-trade-1", "10")
    partial_evidence.install_evidence(monkeypatch, manifest, (traded,), status="FILLED")
    result = partial_evidence.synchronize(protected_case, client)
    assert result["completed"] is True
    net = traded.amount_base * traded.price - traded.fee_paid
    assert cash(g)["parked_quote"] == net
    assert cash(g)["productive_quote"] == D("1000")
    assert cash(g)["held_quote"] == D("0")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("0")
    partial_evidence.synchronize(protected_case, client)
    assert cash(g)["parked_quote"] == net


def test_last_recovery_child_sale_is_park_even_when_parent_event_is_final_exit(
    tmp_path,
    monkeypatch,
):
    g = make_governor(tmp_path)
    book = ledger(tmp_path)
    add_recovery(
        g,
        book,
        proposal_id="recovery-profit",
        executor_id="recovery-profit-exec",
        lot_id="recovery-profit-lot",
        base="10",
        basis="4",
        occurred_at=1020,
    )
    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=2000,
        policy=PartialRecyclePolicy(target_net_profit_bps=D("174")),
    )
    assert decision.manifest is not None
    activate(g, productive="1000")
    manifest = decision.manifest
    client = partial_evidence.EvidenceClient(manifest)
    client.executors.known = True
    traded = partial_evidence.fill(manifest, "last-child-trade-1", "10")
    partial_evidence.install_evidence(monkeypatch, manifest, (traded,), status="FILLED")
    result = partial_evidence.synchronize((g, book, decision), client)
    assert result["completed"] is True
    assert book.snapshot(manifest.position_id).active_base == D("0")
    with book._connect() as conn:
        event_type = conn.execute(
            "SELECT event_type FROM economic_position_events WHERE event_type = 'FINAL_EXIT'"
        ).fetchone()
    assert event_type is not None
    assert (
        cash(g)["parked_quote"] == traded.amount_base * traded.price - traded.fee_paid
    )
    assert cash(g)["productive_quote"] == D("1000")


def test_whole_sale_with_recovery_owners_returns_productive_cash_not_parked_cash(
    whole_case,
    monkeypatch,
):
    g, book, manifest = whole_case
    activate(g, productive="1000")
    client = whole_evidence.EvidenceClient(manifest)
    client.executors.known = True
    client.executors.sell_status = "TERMINATED"
    client.executors.sell_close_type = "COMPLETED"
    traded = whole_evidence.fill(manifest, "whole-purpose-trade-1", "40")
    whole_evidence.install_evidence(monkeypatch, manifest, (traded,), status="FILLED")
    result = whole_evidence.synchronize(whole_case, client)
    assert result["completed"] is True
    net = traded.amount_base * traded.price - traded.fee_paid
    assert cash(g)["productive_quote"] == D("1000") + net
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["held_quote"] == D("0")
    assert book.get_lot("recovery-lot-1").base_remaining == D("0")
    assert book.get_lot("retained-43").base_remaining == D("43")
    whole_evidence.synchronize(whole_case, client)
    assert cash(g)["productive_quote"] == D("1000") + net


def test_crash_after_economic_disposal_keeps_cash_held_until_exact_replay(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    activate(g, productive="1000")
    manifest = decision.manifest
    client = partial_evidence.EvidenceClient(manifest)
    client.executors.known = True
    traded = partial_evidence.fill(manifest, "cross-db-crash-trade-1", "10")
    partial_evidence.install_evidence(monkeypatch, manifest, (traded,), status="FILLED")
    original = book.record_partial_exit

    def commit_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected stop after economic ledger commit")

    monkeypatch.setattr(book, "record_partial_exit", commit_then_crash)
    with pytest.raises(RuntimeError, match="after economic ledger commit"):
        partial_evidence.synchronize(protected_case, client)
    net = traded.amount_base * traded.price - traded.fee_paid
    assert cash(g)["held_quote"] == net
    assert cash(g)["parked_quote"] == D("0")
    assert cash(g)["productive_quote"] == D("1000")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("0")
    assert g.reserve(
        proposal("cannot-spend-cross-db-gap", "1"), venue_snapshot("1000")
    ).status == (DecisionStatus.REJECTED)
    restarted = EconomicPositionLedger(book.path)
    result = partial_evidence.synchronize(
        protected_case,
        client,
        book=restarted,
        manifest=PartialRecycleStore(book.path).load(manifest.manifest_id),
    )
    assert result["completed"] is True
    assert cash(g)["held_quote"] == D("0")
    assert cash(g)["parked_quote"] == net
    assert cash(g)["productive_quote"] == D("1000")
