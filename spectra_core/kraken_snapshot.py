from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .models import CapitalSnapshot


ZERO = Decimal("0")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Kraken portfolio field {field!r} is not a valid decimal: {value!r}"
        ) from exc

    if not result.is_finite() or result < ZERO:
        raise RuntimeError(
            f"Kraken portfolio field {field!r} must be finite and non-negative"
        )

    return result


def _split_pair(trading_pair: str) -> tuple[str, str]:
    pair = trading_pair.strip().upper()

    if "-" not in pair:
        raise ValueError(
            f"Spectra requires BASE-QUOTE trading pair notation: {trading_pair!r}"
        )

    base, quote = pair.rsplit("-", 1)

    if not base or not quote:
        raise ValueError(
            f"Invalid Spectra trading pair: {trading_pair!r}"
        )

    return base, quote


async def build_kraken_capital_snapshot(
    client: Any,
    *,
    account_name: str,
    trading_pair: str,
    recovery_exposure_quote: Decimal,
    refresh: bool = True,
) -> CapitalSnapshot:
    """
    Build a fail-closed capital snapshot from the real Kraken account.

    free_quote:
        Exchange-reported available units of the pair's quote asset.

    committed_quote:
        Locked quote currency plus the USD value of all non-quote holdings.
        This deliberately treats unrelated Kraken holdings as committed capital
        for the global Spectra envelope.

    pair_exposure_quote / position_exposure_quote:
        USD value of the selected pair's base asset.

    recovery_exposure_quote:
        Recovery-attributed capital supplied by Spectra's provenance/accounting
        ledger. This is a subset attribution, not additional economic exposure.
        It must never be added to pair/position/global exchange exposure.
    """
    base, quote = _split_pair(trading_pair)
    recovery_exposure = _decimal(
        recovery_exposure_quote,
        "recovery_exposure_quote",
    )

    state = await client.portfolio.get_state(
        account_names=[account_name],
        connector_names=["kraken"],
        refresh=refresh,
    )

    if not isinstance(state, dict):
        raise RuntimeError("Kraken portfolio state is not a mapping")

    account = state.get(account_name)
    if not isinstance(account, dict):
        raise RuntimeError(
            f"Kraken account {account_name!r} is missing from portfolio state"
        )

    balances = account.get("kraken")
    if not isinstance(balances, list):
        raise RuntimeError(
            f"Kraken balances are missing for account {account_name!r}"
        )

    free_quote = ZERO
    locked_quote = ZERO
    non_quote_value = ZERO
    pair_exposure = ZERO
    saw_quote = False

    for index, row in enumerate(balances):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Kraken balance row {index} is not a mapping"
            )

        token = str(
            row.get("token", row.get("asset", ""))
        ).strip().upper()

        if not token:
            raise RuntimeError(
                f"Kraken balance row {index} has no token/asset"
            )

        units = _decimal(
            row.get("units", 0),
            f"{token}.units",
        )
        available = _decimal(
            row.get("available_units", 0),
            f"{token}.available_units",
        )

        if available > units:
            raise RuntimeError(
                f"Kraken {token} available_units exceeds total units"
            )

        if token == quote:
            saw_quote = True
            free_quote += available
            locked_quote += units - available
            continue

        if units == ZERO:
            continue

        if row.get("value") is None:
            raise RuntimeError(
                f"Kraken {token} has non-zero units but no USD value"
            )

        value = _decimal(row["value"], f"{token}.value")
        non_quote_value += value

        if token == base:
            pair_exposure += value

    if not saw_quote:
        raise RuntimeError(
            f"Kraken portfolio has no {quote} balance row"
        )

    return CapitalSnapshot(
        free_quote=free_quote,
        committed_quote=locked_quote + non_quote_value,
        pair_exposure_quote=pair_exposure,
        position_exposure_quote=pair_exposure,
        recovery_exposure_quote=recovery_exposure,
    )
