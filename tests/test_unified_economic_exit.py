import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.economic_exit import (
    execute_economic_position_exit,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_exit import (
    EconomicExitManifestStatus,
    EconomicExitManifestStore,
    EconomicExitProgressState,
    assess_position_exit_manifest,
    create_or_load_position_exit_manifest,
    validate_manifest_against_ledger,
)
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
)
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.vcap_profit_exit import (
    VCAPProfitExitAction,
    VCAPProfitExitDecision,
    VCAPProfitState,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "unified-exit-governor.sqlite3",
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


def prepare_execution(
    g,
    *,
    proposal_id,
    executor_id,
    intent,
    amount,
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
            free_quote=D("5000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert decision.status == DecisionStatus.RESERVED
    assert g.commit(proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal_id,
        executor_id=executor_id,
        account_name="master_account",
        connector_name="kraken",
        controller_id="spectra",
    )
    return proposal


def trading_rule(*, min_order_size="1", min_quote="1"):
    return KrakenTradingRule(
        trading_pair="XRP-USD",
        min_order_size=D(min_order_size),
        min_notional_size=D(min_quote),
        min_order_value=D("0"),
        min_price_increment=D("0.0001"),
        min_base_amount_increment=D("0.1"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def book(tmp_path):
    ledger = EconomicPositionLedger(tmp_path / "unified-exit.sqlite3")
    ledger.open_position(
        position_id="xrp-position-1",
        pair="XRP-USD",
        created_at=1_000,
    )
    return ledger


def build_owned_position(tmp_path):
    g = governor(tmp_path)
    ledger = book(tmp_path)

    prepare_execution(
        g,
        proposal_id="entry-1",
        executor_id="exec-entry-1",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    prepare_execution(
        g,
        proposal_id="recovery-1",
        executor_id="exec-recovery-1",
        intent=IntentKind.RECOVERY,
        amount="4",
    )
    prepare_execution(
        g,
        proposal_id="recovery-2",
        executor_id="exec-recovery-2",
        intent=IntentKind.RECOVERY,
        amount="4",
    )

    ledger.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-lot",
        proposal_id="entry-1",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        entry_fees_quote=D("0"),
        occurred_at=1_010,
    )
    ledger.record_vcap_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="recovery-lot-1",
        proposal_id="recovery-1",
        base_amount=D("10"),
        cost_basis_quote=D("4"),
        entry_fees_quote=D("0"),
        occurred_at=1_020,
    )
    ledger.record_vcap_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="recovery-lot-2",
        proposal_id="recovery-2",
        base_amount=D("10"),
        cost_basis_quote=D("4"),
        entry_fees_quote=D("0"),
        occurred_at=1_030,
    )
    ledger.record_strategic_retained(
        lot_id="retained-43",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("17"),
        occurred_at=1_040,
    )
    ledger.record_unattributed(
        lot_id="unattributed-20",
        pair="XRP-USD",
        base_amount=D("20"),
        occurred_at=1_050,
    )
    return g, ledger


def exit_decision():
    return VCAPProfitExitDecision(
        pair="XRP-USD",
        action=VCAPProfitExitAction.EXIT,
        current_price=D("0.50"),
        target_exit_price=D("0.49"),
        protected_floor_price=D("0.49"),
        estimated_net_proceeds_quote=D("20"),
        estimated_net_profit_quote=D("2"),
        estimated_net_profit_bps=D("1111.111111111111111111111111"),
        continuation_support=0.0,
        state=VCAPProfitState(
            updated_at=2_000,
            profit_floor_reached_at=1_900,
            peak_net_profit_bps=D("1200"),
            peak_at=1_950,
        ),
        suggested_trailing_delta_bps=D("35"),
        reasons=("runtime_test_profit_exit",),
    )


def test_manifest_covers_entry_and_all_recovery_but_not_retained_xrp(tmp_path):
    g, ledger = build_owned_position(tmp_path)
    store = EconomicExitManifestStore(ledger.path)

    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule(),
        current_price=D("0.50"),
        now=2_000,
        store=store,
    )

    assert manifest.status == EconomicExitManifestStatus.OPEN
    assert manifest.target_active_base == D("40")
    assert manifest.target_cost_basis_quote == D("18")
    assert manifest.strategic_retained_base_at_creation == D("43")
    assert manifest.unattributed_base_at_creation == D("20")
    assert manifest.proposal_ids == (
        "entry-1",
        "recovery-1",
        "recovery-2",
    )
    assert {owner.intent for owner in manifest.owners} == {
        IntentKind.ENTRY,
        IntentKind.RECOVERY,
    }
    assert set(manifest.lot_ids) == {
        "entry-lot",
        "recovery-lot-1",
        "recovery-lot-2",
    }
    assert "retained-43" not in manifest.lot_ids
    assert "unattributed-20" not in manifest.lot_ids

    replay = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule(),
        current_price=D("0.60"),
        now=2_100,
        store=store,
    )
    assert replay == manifest


def test_manifest_rejects_active_trade_without_exact_execution_ownership(
    tmp_path,
):
    g = governor(tmp_path)
    ledger = book(tmp_path)
    ledger.record_base_fill(
        position_id="xrp-position-1",
        lot_id="legacy-unowned",
        pair="XRP-USD",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    ledger.record_strategic_retained(
        lot_id="retained-43",
        pair="XRP-USD",
        base_amount=D("43"),
        occurred_at=1_020,
    )

    with pytest.raises(ValueError, match="lacks exact proposal/executor"):
        create_or_load_position_exit_manifest(
            ledger=ledger,
            governor=g,
            position_id="xrp-position-1",
            trading_rule=trading_rule(),
            current_price=D("0.50"),
            now=2_000,
        )

    assert ledger.get_lot("retained-43").base_remaining == D("43")


def test_manifest_refuses_unsellable_owner_instead_of_using_retained_inventory(
    tmp_path,
):
    g = governor(tmp_path)
    ledger = book(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-dust",
        executor_id="exec-entry-dust",
        intent=IntentKind.ENTRY,
        amount="1",
    )
    ledger.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="dust-lot",
        proposal_id="entry-dust",
        base_amount=D("0.5"),
        cost_basis_quote=D("0.25"),
        occurred_at=1_010,
    )
    ledger.record_strategic_retained(
        lot_id="retained-43",
        pair="XRP-USD",
        base_amount=D("43"),
        occurred_at=1_020,
    )

    with pytest.raises(ValueError, match="below Kraken min order size"):
        create_or_load_position_exit_manifest(
            ledger=ledger,
            governor=g,
            position_id="xrp-position-1",
            trading_rule=trading_rule(min_order_size="1"),
            current_price=D("0.50"),
            now=2_000,
        )

    assert ledger.get_lot("retained-43").base_remaining == D("43")


def test_open_manifest_rejects_new_active_trade_owner_after_exit_scope_freezes(
    tmp_path,
):
    g = governor(tmp_path)
    ledger = book(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry-frozen",
        executor_id="exec-entry-frozen",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    ledger.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-frozen-lot",
        proposal_id="entry-frozen",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1_010,
    )
    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule(),
        current_price=D("0.50"),
        now=2_000,
    )

    prepare_execution(
        g,
        proposal_id="recovery-late",
        executor_id="exec-recovery-late",
        intent=IntentKind.RECOVERY,
        amount="5",
    )
    ledger.record_vcap_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="late-recovery-lot",
        proposal_id="recovery-late",
        base_amount=D("10"),
        cost_basis_quote=D("5"),
        occurred_at=2_010,
    )

    with pytest.raises(RuntimeError, match="new ACTIVE_TRADE inventory"):
        validate_manifest_against_ledger(manifest, ledger=ledger)


class FakeExecutors:
    def __init__(self):
        self.stop_calls = []
        self.get_calls = []

    async def stop_executor(self, *, executor_id, keep_position):
        self.stop_calls.append((executor_id, keep_position))
        return {"status": "stopped", "executor_id": executor_id}

    async def get_executor(self, executor_id):
        self.get_calls.append(executor_id)
        filled = {
            "exec-entry-1": "10",
            "exec-recovery-1": "4",
            "exec-recovery-2": "4",
        }[executor_id]
        return {
            "id": executor_id,
            "executor_type": "dca_executor",
            "status": "TERMINATED",
            "close_type": "TAKE_PROFIT",
            "filled_amount_quote": filled,
            "cum_fees_quote": "0",
            "net_pnl_quote": "0",
            "custom_info": {"realized_pnl_quote": "0"},
        }


class FakeClient:
    def __init__(self):
        self.executors = FakeExecutors()


def test_exit_requests_every_active_owner_and_never_targets_retained_lot(
    tmp_path,
):
    g, ledger = build_owned_position(tmp_path)
    client = FakeClient()

    result = asyncio.run(
        execute_economic_position_exit(
            client,
            decision=exit_decision(),
            governor=g,
            ledger=ledger,
            position_id="xrp-position-1",
            trading_rule=trading_rule(),
            now=2_000,
        )
    )

    assert result["exit_requested"] is True
    assert result["target_active_base"] == "40"
    assert result["remaining_active_base"] == "40"
    assert result["strategic_retained_base"] == "43"
    assert result["retained_inventory_in_manifest"] is False
    assert result["economic_exit_state"] == EconomicExitProgressState.PENDING.value
    assert result["capital_recycling_state"] == "PENDING_RECONCILIATION"
    assert set(client.executors.stop_calls) == {
        ("exec-entry-1", False),
        ("exec-recovery-1", False),
        ("exec-recovery-2", False),
    }
    assert "retained-43" not in {
        lot_id
        for owner in result["owners"]
        for lot_id in owner["active_lot_ids"]
    }


def bind_and_apply_sell(
    reconciler,
    *,
    proposal_id,
    order_id,
    trade_id,
    amount,
    price,
    occurred_at,
):
    provenance = reconciler.governor.get_execution_provenance(proposal_id)
    assert provenance is not None
    reconciler.bind_executor_order(
        order_id=order_id,
        position_id="xrp-position-1",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id=proposal_id,
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        created_at=occurred_at - 1,
    )
    return reconciler.reconcile_trade_fill(
        {
            "trade_id": trade_id,
            "order_id": order_id,
            "account_name": "master_account",
            "connector_name": "kraken",
            "trading_pair": "XRP-USD",
            "trade_type": "SELL",
            "amount": amount,
            "price": price,
            "fee_paid": "0",
            "fee_currency": None,
            "timestamp": datetime.fromtimestamp(
                occurred_at,
                tz=timezone.utc,
            ).isoformat(),
        }
    )


def test_confirmed_sell_fills_flat_manifest_compute_realized_pnl_and_keep_43(
    tmp_path,
):
    g, ledger = build_owned_position(tmp_path)
    store = EconomicExitManifestStore(ledger.path)
    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule(),
        current_price=D("0.50"),
        now=2_000,
        store=store,
    )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind_and_apply_sell(
        reconciler,
        proposal_id="entry-1",
        order_id="sell-entry",
        trade_id="trade-sell-entry",
        amount="20",
        price="0.50",
        occurred_at=2_010,
    )
    bind_and_apply_sell(
        reconciler,
        proposal_id="recovery-1",
        order_id="sell-r1",
        trade_id="trade-sell-r1",
        amount="10",
        price="0.50",
        occurred_at=2_020,
    )
    bind_and_apply_sell(
        reconciler,
        proposal_id="recovery-2",
        order_id="sell-r2",
        trade_id="trade-sell-r2",
        amount="10",
        price="0.50",
        occurred_at=2_030,
    )

    progress = assess_position_exit_manifest(
        manifest,
        ledger=ledger,
        store=store,
        now=2_040,
    )

    assert progress.state == EconomicExitProgressState.COMPLETED
    assert progress.remaining_active_base == D("0")
    assert progress.realized_pnl_quote == D("2.00")
    assert progress.strategic_retained_base == D("43")
    assert ledger.get_lot("retained-43").base_remaining == D("43")
    assert ledger.get_lot("unattributed-20").base_remaining == D("20")
    assert store.load(manifest.manifest_id).status == (
        EconomicExitManifestStatus.COMPLETED
    )

    # Exact binding replay must remain valid even though its source lot is flat.
    replay = reconciler.bind_executor_order(
        order_id="sell-entry",
        position_id="xrp-position-1",
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="entry-1",
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        created_at=2_009,
    )
    assert replay.order_id == "sell-entry"


def test_partial_fill_leaves_manifest_open_and_never_consumes_other_buckets(
    tmp_path,
):
    g, ledger = build_owned_position(tmp_path)
    store = EconomicExitManifestStore(ledger.path)
    manifest = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule(),
        current_price=D("0.50"),
        now=2_000,
        store=store,
    )
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)

    bind_and_apply_sell(
        reconciler,
        proposal_id="entry-1",
        order_id="sell-entry-partial",
        trade_id="trade-entry-partial",
        amount="5",
        price="0.50",
        occurred_at=2_010,
    )

    progress = assess_position_exit_manifest(
        manifest,
        ledger=ledger,
        store=store,
        now=2_020,
    )

    assert progress.state == EconomicExitProgressState.PENDING
    assert progress.remaining_active_base == D("35")
    assert ledger.get_lot("retained-43").base_remaining == D("43")
    assert ledger.get_lot("unattributed-20").base_remaining == D("20")
    assert store.load(manifest.manifest_id).status == EconomicExitManifestStatus.OPEN
