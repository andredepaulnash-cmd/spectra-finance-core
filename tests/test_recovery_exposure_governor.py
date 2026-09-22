from decimal import Decimal

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    ExecutorAccountingObservation,
    IntentKind,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "capital.sqlite",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("5000"),
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
