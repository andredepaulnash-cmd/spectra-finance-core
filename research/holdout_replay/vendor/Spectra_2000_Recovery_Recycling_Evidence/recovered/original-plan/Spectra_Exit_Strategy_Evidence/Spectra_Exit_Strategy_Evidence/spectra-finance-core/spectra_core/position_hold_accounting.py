from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


ZERO = Decimal("0")


@dataclass(frozen=True)
class PositionHoldAccounting:
    """Exact persisted accounting for one ACTIVE Hummingbot PositionHold."""

    account_name: str
    connector_name: str
    trading_pair: str
    controller_id: str
    status: str

    buy_amount_base: Decimal
    buy_amount_quote: Decimal
    sell_amount_base: Decimal
    sell_amount_quote: Decimal
    realized_pnl_quote: Decimal
    cum_fees_quote: Decimal

    executor_ids: tuple[str, ...]
    last_updated: str | None = None


def _required_text(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"position hold field {field} must be a non-empty string"
        )

    return value.strip()


def _exact_decimal_string(
    raw: dict[str, Any],
    field: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    value = raw.get(field)

    # The strict accounting endpoint deliberately emits decimal strings.
    # Accepting int/float here would weaken that exact-accounting contract.
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"position hold field {field} must be an exact decimal string"
        )

    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(
            f"position hold field {field} is not a valid decimal"
        ) from exc

    if not result.is_finite():
        raise ValueError(
            f"position hold field {field} must be finite"
        )

    if not allow_negative and result < ZERO:
        raise ValueError(
            f"position hold field {field} cannot be negative"
        )

    return result


def position_hold_accounting_from_raw(
    raw: Any,
    *,
    expected_account_name: str | None = None,
    expected_connector_name: str | None = None,
    expected_trading_pair: str | None = None,
    expected_controller_id: str | None = None,
) -> PositionHoldAccounting:
    """Validate one exact PositionHold accounting row.

    This parser is intentionally stricter than the display/UI path. Accounting
    quantities must arrive as decimal strings, ownership must be explicit, and
    only ACTIVE persisted holds are accepted.
    """
    if not isinstance(raw, dict):
        raise ValueError("position hold accounting row must be a dict")

    account_name = _required_text(raw, "account_name")
    connector_name = _required_text(raw, "connector_name")
    trading_pair = _required_text(raw, "trading_pair").upper()
    controller_id = _required_text(raw, "controller_id")
    status = _required_text(raw, "status").upper()

    if status != "ACTIVE":
        raise ValueError(
            f"position hold status must be ACTIVE, got {status!r}"
        )

    expectations = (
        ("account_name", account_name, expected_account_name),
        ("connector_name", connector_name, expected_connector_name),
        ("trading_pair", trading_pair, expected_trading_pair),
        ("controller_id", controller_id, expected_controller_id),
    )

    for field, actual, expected in expectations:
        if expected is None:
            continue

        normalized_expected = expected.strip()
        if not normalized_expected:
            raise ValueError(f"expected {field} cannot be empty")

        if field == "trading_pair":
            normalized_expected = normalized_expected.upper()

        if actual != normalized_expected:
            raise ValueError(
                f"position hold {field} {actual!r} does not match "
                f"requested {normalized_expected!r}"
            )

    executor_ids_raw = raw.get("executor_ids")

    if not isinstance(executor_ids_raw, list) or not executor_ids_raw:
        raise ValueError(
            "position hold executor_ids must be a non-empty list"
        )

    executor_ids: list[str] = []

    for value in executor_ids_raw:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "position hold executor_ids must contain non-empty strings"
            )
        executor_ids.append(value.strip())

    if len(set(executor_ids)) != len(executor_ids):
        raise ValueError(
            "position hold executor_ids must be unique"
        )

    last_updated_raw = raw.get("last_updated")
    if last_updated_raw is not None:
        if (
            not isinstance(last_updated_raw, str)
            or not last_updated_raw.strip()
        ):
            raise ValueError(
                "position hold last_updated must be a non-empty string or null"
            )
        last_updated = last_updated_raw.strip()
    else:
        last_updated = None

    return PositionHoldAccounting(
        account_name=account_name,
        connector_name=connector_name,
        trading_pair=trading_pair,
        controller_id=controller_id,
        status=status,
        buy_amount_base=_exact_decimal_string(
            raw,
            "buy_amount_base",
        ),
        buy_amount_quote=_exact_decimal_string(
            raw,
            "buy_amount_quote",
        ),
        sell_amount_base=_exact_decimal_string(
            raw,
            "sell_amount_base",
        ),
        sell_amount_quote=_exact_decimal_string(
            raw,
            "sell_amount_quote",
        ),
        realized_pnl_quote=_exact_decimal_string(
            raw,
            "realized_pnl_quote",
            allow_negative=True,
        ),
        cum_fees_quote=_exact_decimal_string(
            raw,
            "cum_fees_quote",
        ),
        executor_ids=tuple(executor_ids),
        last_updated=last_updated,
    )

