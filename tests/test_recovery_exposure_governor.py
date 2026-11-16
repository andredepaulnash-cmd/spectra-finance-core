from decimal import Decimal

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    ExecutorAccountingObservation,
    IntentKind,
)
from spectra_core.position_hold_accounting import PositionHoldAccounting


D = Decimal


def governor(tmp_path, *, max_recovery="5000", max_entry=None):
    return CapitalGovernor(
        tmp_path / "capital.sqlite",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("10000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D(max_recovery),
            max_entry_quote=None if max_entry is None else D(max_entry),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def bind_executor(
    g,
    *,
    proposal_id,
    executor_id,
    amount,
    intent=IntentKind.RECOVERY,
):
    proposal = CapitalProposal(
        proposal_id=proposal_id,
        pair="BTC-USD",
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
    assert g.commit(proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal_id,
        executor_id=executor_id,
    )


def test_current_recovery_exposure_aggregates_latest_proven_state(tmp_path):
    g = governor(tmp_path)

    # No accounting observation yet: full authorization stays charged.
    bind_executor(
        g,
        proposal_id="recovery-missing",
        executor_id="exec-missing",
        amount="25",
    )

    # Running grid: use current open-position basis, not turnover.
    bind_executor(
        g,
        proposal_id="recovery-grid",
        executor_id="exec-grid",
        amount="30",
    )
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-grid",
            executor_type="grid_executor",
            status="RUNNING",
            filled_amount_quote=D("48"),
            cum_fees_quote=D("0.04"),
            position_size_quote=D("12.50"),
        )
    )

    # Normally terminated DCA: proven flat, releases recovery exposure.
    bind_executor(
        g,
        proposal_id="recovery-dca-flat",
        executor_id="exec-dca-flat",
        amount="40",
    )
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-dca-flat",
            executor_type="dca_executor",
            status="TERMINATED",
            close_type="TAKE_PROFIT",
            filled_amount_quote=D("80"),
            cum_fees_quote=D("0.08"),
        )
    )

    # Non-recovery provenance is not part of the recovery subset.
    bind_executor(
        g,
        proposal_id="entry-grid",
        executor_id="exec-entry",
        amount="100",
        intent=IntentKind.ENTRY,
    )
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-entry",
            executor_type="grid_executor",
            status="RUNNING",
            filled_amount_quote=D("50"),
            cum_fees_quote=D("0.05"),
            position_size_quote=D("50"),
        )
    )

    assert g.current_recovery_exposure_quote() == D("37.50")


def test_entry_and_recovery_budgets_cannot_exceed_total_capital(tmp_path):
    with pytest.raises(ValueError, match="max_entry_quote plus max_recovery_quote"):
        CapitalGovernor(
            tmp_path / "invalid-capital.sqlite",
            CapitalPolicy(
                reserve_floor_quote=D("0"),
                max_total_committed_quote=D("100"),
                max_pair_quote=D("100"),
                max_position_quote=D("100"),
                max_recovery_quote=D("75"),
                max_entry_quote=D("30"),
                max_leverage=D("1"),
                reservation_ttl_seconds=120,
            ),
        )


def test_initial_entry_budget_is_separate_from_vcap_recovery_budget(tmp_path):
    g = governor(tmp_path, max_entry="100")
    bind_executor(
        g,
        proposal_id="initial-entry",
        executor_id="initial-entry-executor",
        amount="100",
        intent=IntentKind.ENTRY,
    )
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="initial-entry-executor",
            executor_type="order_executor",
            status="TERMINATED",
            close_type="COMPLETED",
            filled_amount_quote=D("100"),
            cum_fees_quote=D("0.60"),
        )
    )

    assert g.current_entry_exposure_quote() == D("100")

    over_budget = CapitalProposal(
        proposal_id="entry-over-budget",
        pair="ETH-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("0.01"),
        leverage=D("1"),
    )
    rejected = g.reserve(
        over_budget,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert rejected.status == DecisionStatus.REJECTED
    assert "initial entry budget exceeded" in rejected.reason

    # The independent VCAP pool remains available after the initial-entry
    # allocation is exhausted.
    recovery = CapitalProposal(
        proposal_id="recovery-after-entry-budget",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("100"),
        leverage=D("1"),
    )
    accepted = g.reserve(
        recovery,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert accepted.status == DecisionStatus.RESERVED


def test_entry_budget_uses_base_entry_ledger_and_keeps_uncertified_flat_capital_charged(
    tmp_path,
):
    g = governor(tmp_path, max_entry="100")
    ledger = EconomicPositionLedger(tmp_path / "economic.sqlite")
    ledger.open_position(
        position_id="entry-position",
        pair="BTC-USD",
        created_at=1000,
    )
    proposal = CapitalProposal(
        proposal_id="ledger-entry",
        pair="BTC-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("80"),
        leverage=D("1"),
        account_name="master_account",
        connector_name="kraken_paper_trade",
        quote_asset="USD",
    )
    snapshot = CapitalSnapshot(
        free_quote=D("1000"),
        committed_quote=D("0"),
        pair_exposure_quote=D("0"),
        position_exposure_quote=D("0"),
        recovery_exposure_quote=D("0"),
    )
    assert g.reserve(proposal, snapshot).status == DecisionStatus.RESERVED
    assert g.commit_entry_submission_if_matches(
        proposal,
        position_id="entry-position",
        ledger_path=ledger.path,
        account_name="master_account",
        connector_name="kraken_paper_trade",
        controller_id="spectra",
    )
    assert g.reconcile_execution(
        proposal_id="ledger-entry",
        executor_id="entry-executor",
        account_name="master_account",
        connector_name="kraken_paper_trade",
        controller_id="spectra",
    )
    ledger.record_entry_fill(
        governor=g,
        position_id="entry-position",
        lot_id="base-entry-lot",
        proposal_id="ledger-entry",
        base_amount=D("1"),
        cost_basis_quote=D("80"),
        entry_fees_quote=D("0.48"),
        occurred_at=1001,
    )
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="entry-executor",
            executor_type="order_executor",
            status="TERMINATED",
            close_type="COMPLETED",
            filled_amount_quote=D("80"),
            cum_fees_quote=D("0.48"),
            position_size_quote=D("80"),
        )
    )

    assert g.current_entry_exposure_quote() == D("80.48")

    # A full sale without a verified terminal-flat certificate must not
    # release the original entry authority.
    ledger.record_partial_exit(
        event_id="entry-position-full-sale",
        position_id="entry-position",
        base_sold=D("1"),
        gross_proceeds_quote=D("80"),
        occurred_at=1002,
    )
    assert g.current_entry_exposure_quote() == D("80")


def test_press_and_rotation_also_consume_the_non_recovery_entry_budget(tmp_path):
    g = governor(tmp_path, max_entry="100")
    bind_executor(
        g,
        proposal_id="press-entry",
        executor_id="press-executor",
        amount="60",
        intent=IntentKind.PRESS,
    )
    bind_executor(
        g,
        proposal_id="rotation-entry",
        executor_id="rotation-executor",
        amount="40",
        intent=IntentKind.ROTATION,
    )

    proposal = CapitalProposal(
        proposal_id="entry-after-press-and-rotation",
        pair="XRP-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("0.01"),
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

    assert decision.status == DecisionStatus.REJECTED
    assert "initial entry budget exceeded" in decision.reason


def test_running_executor_uses_current_position_value_for_entry_budget(tmp_path):
    g = governor(tmp_path, max_entry="60")
    bind_executor(
        g,
        proposal_id="running-entry",
        executor_id="running-entry-executor",
        amount="50",
        intent=IntentKind.ENTRY,
    )
    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="running-entry-executor",
            executor_type="grid_executor",
            status="RUNNING",
            filled_amount_quote=D("50"),
            cum_fees_quote=D("0.05"),
            position_size_quote=D("50"),
        )
    )

    assert g.current_entry_exposure_quote() == D("50.05")
    proposal = CapitalProposal(
        proposal_id="running-entry-over-budget",
        pair="BTC-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D("10"),
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
    assert decision.status == DecisionStatus.REJECTED
    assert "initial entry budget exceeded" in decision.reason


def test_position_hold_stays_charged_until_hold_reconciliation(tmp_path):
    g = governor(tmp_path)

    bind_executor(
        g,
        proposal_id="recovery-held",
        executor_id="exec-held",
        amount="25",
    )

    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-held",
            executor_type="grid_executor",
            status="RUNNING",
            filled_amount_quote=D("12.50"),
            cum_fees_quote=D("0.02"),
            position_size_quote=D("9.75"),
        )
    )

    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-held",
            executor_type="grid_executor",
            status="TERMINATED",
            close_type="POSITION_HOLD",
            filled_amount_quote=D("2.75"),
            cum_fees_quote=D("0.03"),
            position_size_quote=D("9.75"),
        )
    )

    # Until the position-hold ledger is reconciled, fail closed at the full
    # authorized recovery claim rather than assuming the executor is flat.
    assert g.current_recovery_exposure_quote() == D("25")


def test_reserve_uses_atomic_reconciled_recovery_not_stale_snapshot(tmp_path):
    g = governor(tmp_path, max_recovery="40")

    bind_executor(
        g,
        proposal_id="existing-recovery",
        executor_id="existing-executor",
        amount="30",
    )

    # Intentionally stale caller snapshot: the executor has already moved to
    # RECONCILED, but this snapshot still says recovery attribution is zero.
    stale_snapshot = CapitalSnapshot(
        free_quote=D("1000"),
        committed_quote=D("0"),
        pair_exposure_quote=D("0"),
        position_exposure_quote=D("0"),
        recovery_exposure_quote=D("0"),
    )

    proposal = CapitalProposal(
        proposal_id="new-recovery",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("20"),
        leverage=D("1"),
    )

    decision = g.reserve(proposal, stale_snapshot)

    # 30 reconciled + 0 active + 20 requested = 50 > 40.
    assert decision.status == DecisionStatus.REJECTED
    assert "recovery budget exceeded" in decision.reason
    assert "50 > 40" in decision.reason


def test_exact_scoped_position_hold_reduces_recovery_to_residual_basis(tmp_path):
    g = governor(tmp_path)

    proposal = CapitalProposal(
        proposal_id="recovery-scoped-hold",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("25"),
        leverage=D("1"),
    )

    assert g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    ).status == DecisionStatus.RESERVED

    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id="exec-scoped-hold",
        account_name="master_account",
        connector_name="Kraken",
        controller_id="spectra",
    )

    provenance = g.get_execution_provenance(proposal.proposal_id)
    assert provenance is not None
    assert provenance.account_name == "master_account"
    assert provenance.connector_name == "kraken"
    assert provenance.controller_id == "spectra"

    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-scoped-hold",
            executor_type="grid_executor",
            status="TERMINATED",
            close_type="POSITION_HOLD",
            filled_amount_quote=D("12.50"),
            cum_fees_quote=D("0.03"),
            position_size_quote=D("9.75"),
        )
    )

    g.record_position_hold_snapshot(
        account_name="master_account",
        connector_name="kraken",
        trading_pair="BTC-USD",
        controller_id="spectra",
        holds=(
            PositionHoldAccounting(
                account_name="master_account",
                connector_name="kraken",
                trading_pair="BTC-USD",
                controller_id="spectra",
                status="ACTIVE",
                buy_amount_base=D("0.0001"),
                buy_amount_quote=D("7.50"),
                sell_amount_base=D("0"),
                sell_amount_quote=D("0"),
                realized_pnl_quote=D("0"),
                cum_fees_quote=D("0.03"),
                executor_ids=("exec-scoped-hold",),
            ),
        ),
    )

    assert g.current_recovery_exposure_quote() == D("7.50")


def test_null_scope_provenance_cannot_consume_position_hold_evidence(tmp_path):
    g = governor(tmp_path)

    bind_executor(
        g,
        proposal_id="recovery-legacy-scope",
        executor_id="exec-legacy-scope",
        amount="25",
    )

    g.reconcile_executor_accounting(
        ExecutorAccountingObservation(
            executor_id="exec-legacy-scope",
            executor_type="grid_executor",
            status="TERMINATED",
            close_type="POSITION_HOLD",
            filled_amount_quote=D("10"),
            cum_fees_quote=D("0.02"),
            position_size_quote=D("8"),
        )
    )

    g.record_position_hold_snapshot(
        account_name="master_account",
        connector_name="kraken",
        trading_pair="BTC-USD",
        controller_id="spectra",
        holds=(
            PositionHoldAccounting(
                account_name="master_account",
                connector_name="kraken",
                trading_pair="BTC-USD",
                controller_id="spectra",
                status="ACTIVE",
                buy_amount_base=D("0.0001"),
                buy_amount_quote=D("6"),
                sell_amount_base=D("0"),
                sell_amount_quote=D("0"),
                realized_pnl_quote=D("0"),
                cum_fees_quote=D("0.02"),
                executor_ids=("exec-legacy-scope",),
            ),
        ),
    )

    provenance = g.get_execution_provenance("recovery-legacy-scope")
    assert provenance is not None
    assert provenance.account_name is None
    assert provenance.connector_name is None
    assert provenance.controller_id is None

    # A historical/null-scope executor cannot borrow evidence from a snapshot
    # that merely looks related. Without exact provenance, retain full auth.
    assert g.current_recovery_exposure_quote() == D("25")
