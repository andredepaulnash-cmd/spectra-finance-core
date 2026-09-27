from decimal import Decimal

import pytest

from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
    OwnershipBucket,
)
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.vcap_lifecycle import plan_vcap_lifecycle_from_ledger
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "economic-governor.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("1000"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def prepare_recovery(
    g,
    *,
    proposal_id="position-1:vcap:0001",
    executor_id="exec-economic-1",
    pair="XRP-USD",
    amount="200",
):
    proposal = CapitalProposal(
        proposal_id=proposal_id,
        pair=pair,
        intent=IntentKind.RECOVERY,
        amount_quote=D(amount),
        leverage=D("1"),
    )
    decision = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("5000"),
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
    )
    return proposal


def ledger(tmp_path):
    result = EconomicPositionLedger(tmp_path / "economic-position.sqlite3")
    result.open_position(
        position_id="position-1",
        pair="XRP-USD",
        created_at=1_000,
    )
    return result


def test_base_and_vcap_lots_form_one_economic_position(tmp_path):
    g = governor(tmp_path)
    prepare_recovery(g)
    book = ledger(tmp_path)

    book.record_base_fill(
        position_id="position-1",
        lot_id="base-fill-1",
        pair="XRP-USD",
        base_amount=D("100"),
        cost_basis_quote=D("50"),
        entry_fees_quote=D("0.10"),
        occurred_at=1_010,
    )
    book.record_vcap_fill(
        governor=g,
        position_id="position-1",
        lot_id="vcap-fill-1",
        proposal_id="position-1:vcap:0001",
        base_amount=D("50"),
        cost_basis_quote=D("20"),
        entry_fees_quote=D("0.04"),
        occurred_at=1_020,
    )

    snapshot = book.snapshot("position-1")

    assert snapshot.active_base == D("150")
    assert snapshot.active_cost_basis_quote == D("70")
    assert snapshot.active_entry_fees_quote == D("0.14")
    assert snapshot.active_lot_ids == ("base-fill-1", "vcap-fill-1")
    assert snapshot.recovery_proposal_ids == (
        "position-1:vcap:0001",
    )

    geometry = snapshot.to_vcap_position(current_price=D("0.40"))
    assert geometry.position_base == D("150")
    assert geometry.cost_basis_quote == D("70")
    assert geometry.average_entry_price == D("70") / D("150")


def test_same_pair_retained_and_unattributed_inventory_is_not_active_trade(
    tmp_path,
):
    book = ledger(tmp_path)
    book.record_base_fill(
        position_id="position-1",
        lot_id="trade-xrp",
        pair="XRP-USD",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    retained = book.record_strategic_retained(
        lot_id="retained-xrp",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("18"),
        occurred_at=1_020,
    )
    unattributed = book.record_unattributed(
        lot_id="manual-xrp",
        pair="XRP-USD",
        base_amount=D("20"),
        occurred_at=1_030,
    )

    snapshot = book.snapshot("position-1")

    assert snapshot.active_base == D("20")
    assert snapshot.strategic_retained_base_same_pair == D("43")
    assert snapshot.unattributed_base_same_pair == D("20")
    assert retained.ownership == OwnershipBucket.STRATEGIC_RETAINED
    assert unattributed.ownership == OwnershipBucket.UNATTRIBUTED


def test_full_trade_exit_cannot_touch_43_retained_or_20_unattributed_xrp(
    tmp_path,
):
    book = ledger(tmp_path)
    book.record_base_fill(
        position_id="position-1",
        lot_id="trade-xrp",
        pair="XRP-USD",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        entry_fees_quote=D("0.02"),
        occurred_at=1_010,
    )
    book.record_strategic_retained(
        lot_id="retained-xrp",
        pair="XRP-USD",
        base_amount=D("43"),
        occurred_at=1_020,
    )
    book.record_unattributed(
        lot_id="manual-xrp",
        pair="XRP-USD",
        base_amount=D("20"),
        occurred_at=1_030,
    )

    result = book.record_partial_exit(
        event_id="exit-trade-xrp",
        position_id="position-1",
        base_sold=D("20"),
        gross_proceeds_quote=D("11"),
        exit_fee_quote=D("0.02"),
        occurred_at=1_040,
    )

    assert result.position_closed is True
    assert result.active_base_remaining == D("0")

    snapshot = book.snapshot("position-1")
    assert snapshot.status == EconomicPositionStatus.CLOSED
    assert snapshot.active_base == D("0")
    assert snapshot.strategic_retained_base_same_pair == D("43")
    assert snapshot.unattributed_base_same_pair == D("20")

    assert book.get_lot("retained-xrp").base_remaining == D("43")
    assert book.get_lot("manual-xrp").base_remaining == D("20")


def test_partial_exit_allocates_basis_and_fees_only_across_active_lots(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_recovery(
        g,
        pair="XRP-USD",
        amount="160",
    )
    book = ledger(tmp_path)
    book.record_base_fill(
        position_id="position-1",
        lot_id="base",
        pair="XRP-USD",
        base_amount=D("10"),
        cost_basis_quote=D("1000"),
        entry_fees_quote=D("1"),
        occurred_at=1_010,
    )
    book.record_vcap_fill(
        governor=g,
        position_id="position-1",
        lot_id="vcap",
        proposal_id="position-1:vcap:0001",
        base_amount=D("2"),
        cost_basis_quote=D("160"),
        entry_fees_quote=D("0.2"),
        occurred_at=1_020,
    )
    book.record_strategic_retained(
        lot_id="retained",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("3000"),
        occurred_at=1_030,
    )

    result = book.record_partial_exit(
        event_id="partial-1",
        position_id="position-1",
        base_sold=D("6"),
        gross_proceeds_quote=D("630"),
        exit_fee_quote=D("0.3"),
        occurred_at=1_040,
    )

    assert result.allocated_cost_basis_quote == D("580")
    assert result.allocated_entry_fees_quote == D("0.6")
    assert result.realized_pnl_quote == D("49.1")
    assert result.active_base_remaining == D("6")

    snapshot = book.snapshot("position-1")
    assert snapshot.active_base == D("6")
    assert snapshot.active_cost_basis_quote == D("580")
    assert snapshot.active_entry_fees_quote == D("0.6")
    assert snapshot.strategic_retained_base_same_pair == D("43")


def test_exit_cannot_sell_more_than_active_trade_even_if_other_xrp_exists(
    tmp_path,
):
    book = ledger(tmp_path)
    book.record_base_fill(
        position_id="position-1",
        lot_id="active-20",
        pair="XRP-USD",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    book.record_strategic_retained(
        lot_id="retained-43",
        pair="XRP-USD",
        base_amount=D("43"),
        occurred_at=1_020,
    )

    with pytest.raises(
        ValueError,
        match="exceeds ACTIVE_TRADE inventory",
    ):
        book.record_partial_exit(
            event_id="bad-exit",
            position_id="position-1",
            base_sold=D("21"),
            gross_proceeds_quote=D("12"),
            occurred_at=1_030,
        )

    assert book.get_lot("retained-43").base_remaining == D("43")


def test_vcap_lot_requires_exact_recovery_provenance_and_authorization(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_recovery(
        g,
        amount="25",
    )
    book = ledger(tmp_path)

    recorded = book.record_vcap_fill(
        governor=g,
        position_id="position-1",
        lot_id="vcap-exact",
        proposal_id="position-1:vcap:0001",
        base_amount=D("50"),
        cost_basis_quote=D("20"),
        occurred_at=1_020,
    )
    assert recorded.executor_id == "exec-economic-1"

    with pytest.raises(
        ValueError,
        match="exceeds authorized recovery quote",
    ):
        book.record_vcap_fill(
            governor=g,
            position_id="position-1",
            lot_id="vcap-over",
            proposal_id="position-1:vcap:0001",
            base_amount=D("60"),
            cost_basis_quote=D("26"),
            occurred_at=1_030,
        )


def test_lot_replay_remains_idempotent_after_partial_exit(tmp_path):
    book = ledger(tmp_path)
    original = book.record_base_fill(
        position_id="position-1",
        lot_id="base-replay",
        pair="XRP-USD",
        base_amount=D("10"),
        cost_basis_quote=D("100"),
        entry_fees_quote=D("1"),
        occurred_at=1_010,
    )
    assert original.base_remaining == D("10")

    book.record_partial_exit(
        event_id="sell-half",
        position_id="position-1",
        base_sold=D("5"),
        gross_proceeds_quote=D("55"),
        exit_fee_quote=D("0.1"),
        occurred_at=1_020,
    )

    replay = book.record_base_fill(
        position_id="position-1",
        lot_id="base-replay",
        pair="XRP-USD",
        base_amount=D("10"),
        cost_basis_quote=D("100"),
        entry_fees_quote=D("1"),
        occurred_at=1_010,
    )

    assert replay.base_original == D("10")
    assert replay.base_remaining == D("5")
    assert replay.cost_basis_original_quote == D("100")
    assert replay.cost_basis_remaining_quote == D("50")


def test_exit_event_replay_preserves_original_result_after_later_exit(tmp_path):
    book = ledger(tmp_path)
    book.record_base_fill(
        position_id="position-1",
        lot_id="base",
        pair="XRP-USD",
        base_amount=D("10"),
        cost_basis_quote=D("100"),
        occurred_at=1_010,
    )

    first = book.record_partial_exit(
        event_id="exit-1",
        position_id="position-1",
        base_sold=D("2"),
        gross_proceeds_quote=D("24"),
        occurred_at=1_020,
    )
    assert first.active_base_remaining == D("8")

    book.record_partial_exit(
        event_id="exit-2",
        position_id="position-1",
        base_sold=D("3"),
        gross_proceeds_quote=D("36"),
        occurred_at=1_030,
    )

    replay = book.record_partial_exit(
        event_id="exit-1",
        position_id="position-1",
        base_sold=D("2"),
        gross_proceeds_quote=D("24"),
        occurred_at=1_020,
    )
    assert replay == first
    assert replay.active_base_remaining == D("8")


def btc_observation(timestamp, price):
    return AssetObservation(
        trading_pair="BTC-USD",
        timestamp=timestamp,
        price=price,
        quote_volume_24h=1_000_000,
        spread_bps=5.0,
        depth_quote=50_000.0,
        depth_imbalance=0.1,
        microprice_edge_bps=0.5,
    )


def btc_fingerprint():
    price = 100.0
    fp = AssetFingerprint.cold_start(btc_observation(1_000, price))
    for i in range(1, 41):
        price = 101.0 if i % 2 else 100.0
        fp = fp.update(btc_observation(1_000 + i * 60, price))
    return fp


def btc_rule():
    return KrakenTradingRule(
        trading_pair="BTC-USD",
        min_order_size=D("0.01"),
        min_notional_size=D("10"),
        min_order_value=D("0"),
        min_price_increment=D("0.01"),
        min_base_amount_increment=D("0.001"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def test_unified_lifecycle_geometry_comes_from_whole_active_trade_ledger(
    tmp_path,
):
    book = EconomicPositionLedger(tmp_path / "economic-btc.sqlite3")
    book.open_position(
        position_id="btc-economic-1",
        pair="BTC-USD",
        created_at=1_000,
    )
    book.record_base_fill(
        position_id="btc-economic-1",
        lot_id="btc-base",
        pair="BTC-USD",
        base_amount=D("10"),
        cost_basis_quote=D("1100"),
        entry_fees_quote=D("1"),
        occurred_at=1_010,
    )
    book.record_base_fill(
        position_id="btc-economic-1",
        lot_id="btc-second-active",
        pair="BTC-USD",
        base_amount=D("2"),
        cost_basis_quote=D("180"),
        entry_fees_quote=D("0.2"),
        occurred_at=1_020,
    )
    book.record_strategic_retained(
        lot_id="btc-retained",
        pair="BTC-USD",
        base_amount=D("50"),
        cost_basis_quote=D("4500"),
        occurred_at=1_030,
    )

    state_store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    state = VCAPLifecycleState(
        envelope_proposal_id="btc-envelope",
        pair="BTC-USD",
        next_sequence=1,
        remaining_authorized_quote=D("100"),
        recovery_proposal_ids=(),
        position_base=D("1"),
        cost_basis_quote=D("1"),
        last_known_price=D("100"),
        profit_state=None,
        status=VCAPLifecycleStatus.ACTIVE,
        updated_at=3_400,
    )
    state_store.save(state)

    fp = btc_fingerprint()
    decision = plan_vcap_lifecycle_from_ledger(
        position_id="btc-economic-1",
        ledger=book,
        state=state,
        store=state_store,
        fingerprint=fp,
        trading_rule=btc_rule(),
        current_price=D("100"),
        seed_quote=D("20"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
        now=fp.last_seen_at,
    )

    # Stale geometry in VCAPLifecycleState is replaced by the complete
    # ACTIVE_TRADE ledger geometry: 10 BTC + 2 BTC, but not the 50 retained BTC.
    assert decision.state.position_base == D("12")
    assert decision.state.cost_basis_quote == D("1280")
    assert decision.profit_decision is not None
    snapshot = book.snapshot("btc-economic-1")
    assert snapshot.active_entry_fees_quote == D("1.2")
    assert snapshot.strategic_retained_base_same_pair == D("50")
