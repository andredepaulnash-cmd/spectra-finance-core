from decimal import Decimal

from spectra_core.executor_accounting import (
    reconcile_recovery_exposure_with_position_holds,
)
from spectra_core.models import (
    ExecutionProvenance,
    ExecutorAccountingObservation,
    IntentKind,
)
from spectra_core.position_hold_accounting import PositionHoldAccounting


D = Decimal


def provenance(amount="25"):
    return ExecutionProvenance(
        proposal_id="recovery-1",
        executor_id="exec-recovery-1",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        authorized_quote=D(amount),
        leverage=D("1"),
    )


def position_hold_observation():
    return ExecutorAccountingObservation(
        executor_id="exec-recovery-1",
        executor_type="grid_executor",
        status="TERMINATED",
        close_type="POSITION_HOLD",
        filled_amount_quote=D("5"),
        cum_fees_quote=D("0.02"),
        position_size_quote=D("15"),
    )


def hold(
    *,
    executor_ids=("exec-recovery-1",),
    pair="BTC-USD",
    buy_base="0.001",
    buy_quote="15",
    sell_base="0",
    sell_quote="0",
    controller="spectra",
):
    return PositionHoldAccounting(
        account_name="master_account",
        connector_name="kraken",
        trading_pair=pair,
        controller_id=controller,
        status="ACTIVE",
        buy_amount_base=D(buy_base),
        buy_amount_quote=D(buy_quote),
        sell_amount_base=D(sell_base),
        sell_amount_quote=D(sell_quote),
        realized_pnl_quote=D("0"),
        cum_fees_quote=D("0.02"),
        executor_ids=executor_ids,
        last_updated="2026-09-22T13:00:00+00:00",
    )


def test_singleton_exact_long_hold_reduces_to_residual_basis():
    result = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (hold(buy_quote="15.125"),),
    )

    assert result.exposure_quote == D("15.125")
    assert result.source == "POSITION_HOLD_EXACT_LONG_BASIS"
    assert result.conservative is False
    assert result.requires_position_hold_reconciliation is False


def test_exact_flat_singleton_hold_releases_recovery_exposure():
    result = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (
            hold(
                buy_base="0",
                buy_quote="0",
                sell_base="0",
                sell_quote="0",
            ),
        ),
    )

    assert result.exposure_quote == D("0")
    assert result.source == "POSITION_HOLD_EXACT_FLAT"
    assert result.requires_position_hold_reconciliation is False


def test_missing_hold_keeps_authorized_claim():
    result = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (),
    )

    assert result.exposure_quote == D("25")
    assert result.source == "POSITION_HOLD_MISSING"
    assert result.conservative is True
    assert result.requires_position_hold_reconciliation is True


def test_aggregated_hold_does_not_guess_executor_share():
    result = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (
            hold(
                executor_ids=(
                    "exec-recovery-1",
                    "exec-recovery-2",
                ),
                buy_quote="40",
            ),
        ),
    )

    assert result.exposure_quote == D("25")
    assert result.source == "POSITION_HOLD_AGGREGATED_OWNERSHIP"
    assert result.conservative is True


def test_multiple_matching_holds_remain_conservative():
    result = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (
            hold(controller="spectra-a"),
            hold(controller="spectra-b"),
        ),
    )

    assert result.exposure_quote == D("25")
    assert result.source == "POSITION_HOLD_MULTIPLE_MATCHES"


def test_short_or_unsettled_shape_remains_conservative():
    short = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (
            hold(
                buy_base="0",
                buy_quote="0",
                sell_base="0.001",
                sell_quote="15",
            ),
        ),
    )

    unsettled = reconcile_recovery_exposure_with_position_holds(
        position_hold_observation(),
        provenance(),
        (
            hold(
                buy_base="0.001",
                buy_quote="15",
                sell_base="0.0002",
                sell_quote="3",
            ),
        ),
    )

    assert short.exposure_quote == D("25")
    assert short.source == "POSITION_HOLD_UNRESOLVED_SHAPE"
    assert unsettled.exposure_quote == D("25")
    assert unsettled.source == "POSITION_HOLD_UNRESOLVED_SHAPE"


def test_non_position_hold_assessment_is_unchanged():
    observation = ExecutorAccountingObservation(
        executor_id="exec-recovery-1",
        executor_type="grid_executor",
        status="RUNNING",
        filled_amount_quote=D("30"),
        cum_fees_quote=D("0.02"),
        position_size_quote=D("12.5"),
    )

    result = reconcile_recovery_exposure_with_position_holds(
        observation,
        provenance(),
        (),
    )

    assert result.exposure_quote == D("12.5")
    assert result.source == "GRID_POSITION_BASIS"
    assert result.requires_position_hold_reconciliation is False
