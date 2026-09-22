from decimal import Decimal

import pytest

from spectra_core.executor_accounting import assess_recovery_exposure
from spectra_core.models import (
    ExecutionProvenance,
    ExecutorAccountingObservation,
    IntentKind,
)


D = Decimal


def provenance(
    *,
    intent=IntentKind.RECOVERY,
    authorized="25",
):
    return ExecutionProvenance(
        proposal_id="proposal-1",
        executor_id="exec-1",
        pair="BTC-USD",
        intent=intent,
        authorized_quote=D(authorized),
        leverage=D("1"),
    )


def observation(
    *,
    executor_type,
    status,
    filled="0",
    position=None,
    close_type=None,
):
    return ExecutorAccountingObservation(
        executor_id="exec-1",
        executor_type=executor_type,
        status=status,
        filled_amount_quote=D(filled),
        cum_fees_quote=D("0"),
        position_size_quote=None if position is None else D(position),
        close_type=close_type,
    )


def test_non_recovery_executor_contributes_zero():
    result = assess_recovery_exposure(
        observation(
            executor_type="grid_executor",
            status="RUNNING",
            filled="10",
            position="8",
        ),
        provenance(intent=IntentKind.ENTRY),
    )

    assert result.exposure_quote == D("0")
    assert result.source == "NON_RECOVERY"


def test_grid_running_uses_current_position_basis():
    result = assess_recovery_exposure(
        observation(
            executor_type="grid_executor",
            status="RUNNING",
            filled="40",
            position="11.75",
        ),
        provenance(),
    )

    assert result.exposure_quote == D("11.75")
    assert result.source == "GRID_POSITION_BASIS"
    assert result.conservative is False


def test_grid_missing_position_fails_closed_to_authorized_quote():
    result = assess_recovery_exposure(
        observation(
            executor_type="grid_executor",
            status="RUNNING",
            filled="9",
        ),
        provenance(authorized="25"),
    )

    assert result.exposure_quote == D("25")
    assert result.conservative is True


def test_dca_running_uses_open_fill_basis():
    result = assess_recovery_exposure(
        observation(
            executor_type="dca_executor",
            status="RUNNING",
            filled="13.25",
        ),
        provenance(),
    )

    assert result.exposure_quote == D("13.25")
    assert result.source == "DCA_RUNNING_OPEN_BASIS"
    assert result.conservative is False


def test_dca_shutting_down_keeps_full_authorized_claim():
    result = assess_recovery_exposure(
        observation(
            executor_type="dca_executor",
            status="SHUTTING_DOWN",
            filled="31",
        ),
        provenance(authorized="25"),
    )

    assert result.exposure_quote == D("25")
    assert result.source == "DCA_SHUTTING_DOWN"
    assert result.conservative is True


def test_dca_normal_terminal_releases_recovery_exposure():
    result = assess_recovery_exposure(
        observation(
            executor_type="dca_executor",
            status="TERMINATED",
            filled="50",
            close_type="TAKE_PROFIT",
        ),
        provenance(),
    )

    assert result.exposure_quote == D("0")
    assert result.source == "DCA_FLAT_TERMINAL"


def test_failed_dca_terminal_does_not_assume_flat():
    result = assess_recovery_exposure(
        observation(
            executor_type="dca_executor",
            status="TERMINATED",
            filled="17",
            close_type="FAILED",
        ),
        provenance(authorized="25"),
    )

    assert result.exposure_quote == D("25")
    assert result.conservative is True


def test_position_hold_requires_separate_reconciliation():
    result = assess_recovery_exposure(
        observation(
            executor_type="grid_executor",
            status="TERMINATED",
            filled="20",
            position="9",
            close_type="POSITION_HOLD",
        ),
        provenance(authorized="25"),
    )

    assert result.exposure_quote == D("25")
    assert result.requires_position_hold_reconciliation is True
    assert result.conservative is True


def test_unknown_executor_type_fails_closed():
    result = assess_recovery_exposure(
        observation(
            executor_type="mystery_executor",
            status="RUNNING",
            filled="5",
        ),
        provenance(authorized="25"),
    )

    assert result.exposure_quote == D("25")
    assert result.source == "UNSUPPORTED_EXECUTOR_TYPE"
    assert result.conservative is True


def test_executor_identity_mismatch_is_rejected():
    obs = observation(
        executor_type="grid_executor",
        status="RUNNING",
        position="5",
    )

    prov = ExecutionProvenance(
        proposal_id="proposal-1",
        executor_id="different-executor",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        authorized_quote=D("25"),
        leverage=D("1"),
    )

    with pytest.raises(ValueError, match="does not match provenance"):
        assess_recovery_exposure(obs, prov)
