"""Strict Hummingbot executor accounting ingestion.

This module intentionally does not reuse the dashboard/display normalization
helpers. Display code may substitute aliases and default missing values to
zero; capital accounting must preserve missing-data semantics and Decimal
precision.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from .models import ExecutorAccountingObservation


def _decimal_value(
    container: dict[str, Any],
    key: str,
    *,
    required: bool,
    allow_negative: bool,
) -> Decimal | None:
    if key not in container or container[key] is None:
        if required:
            raise ValueError(f"missing required executor field: {key}")
        return None

    raw = container[key]

    # bool is an int subclass and must never be accepted as money.
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be numeric, not bool")

    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{key} is not a valid decimal") from exc

    if not value.is_finite():
        raise ValueError(f"{key} must be finite")

    if not allow_negative and value < Decimal("0"):
        raise ValueError(f"{key} cannot be negative")

    return value


def _executor_id(raw: dict[str, Any]) -> str:
    values: list[str] = []

    for key in ("id", "executor_id"):
        if key not in raw or raw[key] is None:
            continue

        value = raw[key]
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")

        value = value.strip()
        if not value:
            raise ValueError(f"{key} cannot be empty")

        values.append(value)

    if not values:
        raise ValueError("executor payload has no id")

    if len(set(values)) != 1:
        raise ValueError("executor id fields disagree")

    return values[0]


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)

    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")

    value = value.strip()
    if not value:
        raise ValueError(f"{key} cannot be empty")

    return value


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)

    if value is None or value == "":
        return None

    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")

    value = value.strip()
    return value or None


def executor_accounting_observation_from_raw(
    raw: Any,
    *,
    expected_executor_id: str | None = None,
) -> ExecutorAccountingObservation:
    """Convert one raw Hummingbot executor payload into strict accounting data.

    Required accounting counters use canonical Hummingbot field names only.
    Display aliases such as ``volume_traded`` and ``fees_quote`` are
    intentionally not accepted here.
    """
    if not isinstance(raw, dict):
        raise ValueError("executor payload must be a dict")

    executor_id = _executor_id(raw)

    if expected_executor_id is not None:
        expected = expected_executor_id.strip()
        if not expected:
            raise ValueError("expected_executor_id cannot be empty")
        if executor_id != expected:
            raise ValueError(
                "executor payload id does not match requested executor: "
                f"{executor_id} != {expected}"
            )

    status = _required_text(raw, "status")
    close_type = _optional_text(raw, "close_type")

    filled_amount_quote = _decimal_value(
        raw,
        "filled_amount_quote",
        required=True,
        allow_negative=False,
    )
    cum_fees_quote = _decimal_value(
        raw,
        "cum_fees_quote",
        required=True,
        allow_negative=False,
    )
    net_pnl_quote = _decimal_value(
        raw,
        "net_pnl_quote",
        required=False,
        allow_negative=True,
    )

    custom_info = raw.get("custom_info")
    if custom_info is None:
        custom_info = {}
    elif not isinstance(custom_info, dict):
        raise ValueError("custom_info must be a dict when present")

    position_size_quote = _decimal_value(
        custom_info,
        "position_size_quote",
        required=False,
        allow_negative=False,
    )
    realized_pnl_quote = _decimal_value(
        custom_info,
        "realized_pnl_quote",
        required=False,
        allow_negative=True,
    )

    assert filled_amount_quote is not None
    assert cum_fees_quote is not None

    return ExecutorAccountingObservation(
        executor_id=executor_id,
        status=status,
        filled_amount_quote=filled_amount_quote,
        cum_fees_quote=cum_fees_quote,
        position_size_quote=position_size_quote,
        net_pnl_quote=net_pnl_quote,
        realized_pnl_quote=realized_pnl_quote,
        close_type=close_type,
    )
