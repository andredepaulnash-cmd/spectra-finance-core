"""Strict Hummingbot executor accounting ingestion.

This module intentionally does not reuse the dashboard/display normalization
helpers. Display code may substitute aliases and default missing values to
zero; capital accounting must preserve missing-data semantics and Decimal
precision.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from .position_hold_accounting import PositionHoldAccounting

from .models import (
    ExecutionProvenance,
    ExecutorAccountingObservation,
    IntentKind,
    RecoveryExposureAssessment,
)


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



_DCA_FLAT_TERMINAL_CLOSE_TYPES = {
    "TIME_LIMIT",
    "STOP_LOSS",
    "TAKE_PROFIT",
    "EXPIRED",
    "EARLY_STOP",
    "TRAILING_STOP",
    "INSUFFICIENT_BALANCE",
    "COMPLETED",
}


def assess_recovery_exposure(
    observation: ExecutorAccountingObservation,
    provenance: ExecutionProvenance,
) -> RecoveryExposureAssessment:
    """Assess quote capital still attributable to one RECOVERY executor.

    This is deliberately conservative. Unknown or ambiguous states retain the
    authorized recovery claim rather than silently freeing recovery budget.

    ``filled_amount_quote`` is never treated generically as exposure:
    GridExecutor exposes current open basis as ``position_size_quote``.
    DCAExecutor's filled amount is usable as opening basis only while RUNNING;
    after close-side execution begins it becomes turnover.
    """
    if observation.executor_id != provenance.executor_id:
        raise ValueError(
            "accounting observation executor does not match provenance: "
            f"{observation.executor_id} != {provenance.executor_id}"
        )

    authorized = provenance.authorized_quote
    if not authorized.is_finite() or authorized < Decimal("0"):
        raise ValueError("authorized_quote must be finite and non-negative")

    if provenance.intent != IntentKind.RECOVERY:
        return RecoveryExposureAssessment(
            executor_id=observation.executor_id,
            exposure_quote=Decimal("0"),
            source="NON_RECOVERY",
        )

    status = observation.status.strip()
    executor_type = (
        ""
        if observation.executor_type is None
        else observation.executor_type.strip()
    )
    close_type = (
        None
        if observation.close_type is None
        else observation.close_type.strip()
    )

    def authorized_fallback(
        source: str,
        *,
        requires_hold: bool = False,
    ) -> RecoveryExposureAssessment:
        return RecoveryExposureAssessment(
            executor_id=observation.executor_id,
            exposure_quote=authorized,
            source=source,
            conservative=True,
            requires_position_hold_reconciliation=requires_hold,
        )

    if executor_type == "grid_executor":
        if status == "NOT_STARTED":
            if (
                observation.filled_amount_quote == Decimal("0")
                and (
                    observation.position_size_quote is None
                    or observation.position_size_quote == Decimal("0")
                )
            ):
                return RecoveryExposureAssessment(
                    executor_id=observation.executor_id,
                    exposure_quote=Decimal("0"),
                    source="GRID_NOT_STARTED",
                )
            return authorized_fallback("GRID_NOT_STARTED_WITH_ACTIVITY")

        if status in {"RUNNING", "SHUTTING_DOWN"}:
            if observation.position_size_quote is None:
                return authorized_fallback("GRID_POSITION_MISSING")

            return RecoveryExposureAssessment(
                executor_id=observation.executor_id,
                exposure_quote=observation.position_size_quote,
                source="GRID_POSITION_BASIS",
            )

        if status == "TERMINATED":
            if close_type == "POSITION_HOLD":
                return authorized_fallback(
                    "POSITION_HOLD_PENDING_RECONCILIATION",
                    requires_hold=True,
                )

            if observation.position_size_quote is not None:
                return RecoveryExposureAssessment(
                    executor_id=observation.executor_id,
                    exposure_quote=observation.position_size_quote,
                    source="GRID_TERMINAL_POSITION_BASIS",
                )

            return authorized_fallback("GRID_TERMINAL_POSITION_MISSING")

        return authorized_fallback("GRID_UNKNOWN_STATUS")

    if executor_type == "dca_executor":
        if status == "NOT_STARTED":
            if observation.filled_amount_quote == Decimal("0"):
                return RecoveryExposureAssessment(
                    executor_id=observation.executor_id,
                    exposure_quote=Decimal("0"),
                    source="DCA_NOT_STARTED",
                )
            return authorized_fallback("DCA_NOT_STARTED_WITH_ACTIVITY")

        if status == "RUNNING":
            return RecoveryExposureAssessment(
                executor_id=observation.executor_id,
                exposure_quote=observation.filled_amount_quote,
                source="DCA_RUNNING_OPEN_BASIS",
            )

        if status == "SHUTTING_DOWN":
            return authorized_fallback("DCA_SHUTTING_DOWN")

        if status == "TERMINATED":
            if close_type == "POSITION_HOLD":
                return authorized_fallback(
                    "POSITION_HOLD_PENDING_RECONCILIATION",
                    requires_hold=True,
                )

            if close_type == "FAILED" or close_type is None:
                return authorized_fallback("DCA_AMBIGUOUS_TERMINAL")

            if close_type in _DCA_FLAT_TERMINAL_CLOSE_TYPES:
                return RecoveryExposureAssessment(
                    executor_id=observation.executor_id,
                    exposure_quote=Decimal("0"),
                    source="DCA_FLAT_TERMINAL",
                )

            return authorized_fallback("DCA_UNKNOWN_TERMINAL_CLOSE_TYPE")

        return authorized_fallback("DCA_UNKNOWN_STATUS")

    return authorized_fallback("UNSUPPORTED_EXECUTOR_TYPE")

def reconcile_recovery_exposure_with_position_holds(
    observation: ExecutorAccountingObservation,
    provenance: ExecutionProvenance,
    holds: tuple[PositionHoldAccounting, ...],
) -> RecoveryExposureAssessment:
    """Resolve a POSITION_HOLD recovery claim from exact persisted hold data.

    Only a uniquely matching, singleton-owned long residual is allowed to
    reduce the conservative authorized recovery claim.

    Missing, duplicated, aggregated, cross-pair, short, or internally
    inconsistent hold evidence remains fail-closed at authorized_quote.
    """
    base = assess_recovery_exposure(
        observation,
        provenance,
    )

    if not base.requires_position_hold_reconciliation:
        return base

    authorized = provenance.authorized_quote
    executor_id = provenance.executor_id

    def fallback(source: str) -> RecoveryExposureAssessment:
        return RecoveryExposureAssessment(
            executor_id=executor_id,
            exposure_quote=authorized,
            source=source,
            conservative=True,
            requires_position_hold_reconciliation=True,
        )

    matching = tuple(
        hold
        for hold in holds
        if executor_id in hold.executor_ids
    )

    if not matching:
        return fallback("POSITION_HOLD_MISSING")

    if len(matching) != 1:
        return fallback("POSITION_HOLD_MULTIPLE_MATCHES")

    hold = matching[0]

    if hold.trading_pair != provenance.pair.strip().upper():
        return fallback("POSITION_HOLD_PAIR_MISMATCH")

    # A shared PositionHold aggregates multiple executors. Its residual cannot
    # be allocated back to one recovery executor without inventing a split.
    if hold.executor_ids != (executor_id,):
        return fallback("POSITION_HOLD_AGGREGATED_OWNERSHIP")

    buy_base = hold.buy_amount_base
    buy_quote = hold.buy_amount_quote
    sell_base = hold.sell_amount_base
    sell_quote = hold.sell_amount_quote

    # Exact flat hold: nothing remains economically open.
    if (
        buy_base == Decimal("0")
        and buy_quote == Decimal("0")
        and sell_base == Decimal("0")
        and sell_quote == Decimal("0")
    ):
        return RecoveryExposureAssessment(
            executor_id=executor_id,
            exposure_quote=Decimal("0"),
            source="POSITION_HOLD_EXACT_FLAT",
        )

    # Phase-2 Kraken RECOVERY is spot-long capital. After Hummingbot settles
    # matched volume, an exact long residual should live entirely on the buy
    # side. Any sell-side residue or zero-cost positive base is ambiguous.
    if (
        buy_base > Decimal("0")
        and buy_quote > Decimal("0")
        and sell_base == Decimal("0")
        and sell_quote == Decimal("0")
    ):
        return RecoveryExposureAssessment(
            executor_id=executor_id,
            exposure_quote=buy_quote,
            source="POSITION_HOLD_EXACT_LONG_BASIS",
        )

    return fallback("POSITION_HOLD_UNRESOLVED_SHAPE")


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
    executor_type = _optional_text(raw, "executor_type")

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
        executor_type=executor_type,
    )
