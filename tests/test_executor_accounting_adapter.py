from decimal import Decimal

import pytest

from spectra_core.executor_accounting import (
    executor_accounting_observation_from_raw,
)


def test_strict_adapter_preserves_decimal_accounting_fields():
    observation = executor_accounting_observation_from_raw(
        {
            "id": "exec-1",
            "status": "RUNNING",
            "close_type": None,
            "filled_amount_quote": "15.2500",
            "cum_fees_quote": "0.031",
            "net_pnl_quote": "-0.17",
            "custom_info": {
                "position_size_quote": "11.50",
                "realized_pnl_quote": "0.42",
            },
        },
        expected_executor_id="exec-1",
    )

    assert observation.executor_id == "exec-1"
    assert observation.status == "RUNNING"
    assert observation.filled_amount_quote == Decimal("15.2500")
    assert observation.cum_fees_quote == Decimal("0.031")
    assert observation.position_size_quote == Decimal("11.50")
    assert observation.net_pnl_quote == Decimal("-0.17")
    assert observation.realized_pnl_quote == Decimal("0.42")
    assert observation.close_type is None


def test_strict_adapter_accepts_real_zero_counters():
    observation = executor_accounting_observation_from_raw(
        {
            "executor_id": "exec-zero",
            "status": "RUNNING",
            "filled_amount_quote": 0,
            "cum_fees_quote": 0,
        }
    )

    assert observation.filled_amount_quote == Decimal("0")
    assert observation.cum_fees_quote == Decimal("0")


def test_strict_adapter_rejects_display_aliases_for_required_money():
    with pytest.raises(
        ValueError,
        match="missing required executor field: filled_amount_quote",
    ):
        executor_accounting_observation_from_raw(
            {
                "id": "exec-alias",
                "status": "RUNNING",
                "volume_traded": "10",
                "fees_quote": "0.1",
            }
        )


def test_strict_adapter_rejects_executor_identity_mismatch():
    with pytest.raises(
        ValueError,
        match="does not match requested executor",
    ):
        executor_accounting_observation_from_raw(
            {
                "id": "wrong-executor",
                "status": "RUNNING",
                "filled_amount_quote": "1",
                "cum_fees_quote": "0",
            },
            expected_executor_id="expected-executor",
        )


def test_strict_adapter_rejects_malformed_or_nonfinite_money():
    with pytest.raises(ValueError, match="custom_info must be a dict"):
        executor_accounting_observation_from_raw(
            {
                "id": "exec-custom",
                "status": "RUNNING",
                "filled_amount_quote": "1",
                "cum_fees_quote": "0",
                "custom_info": [],
            }
        )

    with pytest.raises(ValueError, match="filled_amount_quote must be finite"):
        executor_accounting_observation_from_raw(
            {
                "id": "exec-nan",
                "status": "RUNNING",
                "filled_amount_quote": "NaN",
                "cum_fees_quote": "0",
            }
        )
