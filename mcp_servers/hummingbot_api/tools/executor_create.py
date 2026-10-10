"""Typed executor creation for the Hummingbot MCP server (FEAT-062).

One impl per executor type, behind one tool each. The typed signature registered in
``server.py`` *is* the config schema, so a wrong field is a host-side validation error
naming the field rather than a server round trip — which is what let the old
``manage_executors(action="create", executor_config={...})`` cost two calls per create
and still fail late.

Two invariants every impl here shares:

- **Omitted means omitted.** Every optional parameter arrives as ``None`` and is dropped
  by :func:`_compact` instead of being sent as an explicit default. The backend owns its
  own defaults, and — decisively — the user's saved defaults merge *underneath* whatever
  the call sends, so writing a default into the payload would silently clobber a saved
  one.
- **Saved defaults still merge.** Each impl runs the assembled config through
  ``executor_preferences.merge_with_defaults`` exactly as the mega-tool did, so a default
  saved before the split keeps applying after it.

Signatures were verified field-by-field against ``GET /executors/types/{type}/config`` on
a live API server. See ``mcp_servers/TOOL_STYLE.md``.
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from math import isfinite
from time import time
from typing import Any

from mcp_servers.hummingbot_api.executor_preferences import executor_preferences
from mcp_servers.hummingbot_api.hummingbot_client import trading_rules_cache
from mcp_servers.hummingbot_api.settings import settings
from spectra_core import CapitalProposal, IntentKind
from spectra_core.decimal_math import exact_decimal_sum
from spectra_core.models import BreakerMode
from spectra_core.runtime import (
    get_breaker_mode,
    get_capital_governor,
    get_economic_position_ledger,
)

logger = logging.getLogger("hummingbot-mcp")


def _protected_attempt_text(value: Any, field: str) -> str:
    """Use the governor's normalized string identity, with bounded input."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"protected BUY {field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 1024 or any(
        ord(char) < 32 or ord(char) == 127 for char in normalized
    ):
        raise ValueError(f"protected BUY {field} has invalid identity geometry")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"protected BUY {field} is not valid UTF-8") from exc
    return normalized


def _protected_buy_attempt_id(
    *,
    proposal_id: Any,
    account_name: Any,
    controller_id: Any,
    position_id: Any,
    intent: Any,
    connector_name: Any,
    trading_pair: Any,
) -> str:
    """Bind one backend attempt to an exact authorized capital generation.

    Capital proposal IDs cannot be reserved twice; the proposal is the durable
    authorization generation. Prices, timestamps, saved defaults and native
    caller IDs deliberately do not create a different attempt for that claim.
    Canonical JSON avoids delimiter collisions, and the fixed 79-character ID
    fits the backend's 128-character executor identity field.
    """
    identity = {
        "version": 1,
        "proposal_id": _protected_attempt_text(proposal_id, "proposal_id"),
        "account_name": _protected_attempt_text(account_name, "account_name"),
        "controller_id": _protected_attempt_text(controller_id, "controller_id"),
        "position_id": _protected_attempt_text(position_id, "position_id"),
        "intent": IntentKind(_protected_attempt_text(intent, "intent").upper()).value,
        "connector_name": _protected_attempt_text(
            connector_name, "connector_name"
        ).lower(),
        "trading_pair": _protected_attempt_text(trading_pair, "trading_pair").upper(),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "spectra-buy-v1-" + hashlib.sha256(encoded).hexdigest()


def _compact(config: dict[str, Any]) -> dict[str, Any]:
    """Drop the keys the caller left unset.

    An omitted parameter must not reach the backend as an explicit ``None``: the
    backend's own default would be overwritten, and so would the user's saved default,
    which merges underneath this dict.
    """
    return {key: value for key, value in config.items() if value is not None}


def _triple_barrier(
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    time_limit: int | None = None,
    trailing_stop_activation_price: float | None = None,
    trailing_stop_trailing_delta: float | None = None,
    open_order_type: int | None = None,
    take_profit_order_type: int | None = None,
    stop_loss_order_type: int | None = None,
    time_limit_order_type: int | None = None,
) -> dict[str, Any]:
    """Assemble a ``TripleBarrierConfig`` from flat, typed parameters.

    The nested shape is the backend's; flattening it is the whole point of the split, so
    it is rebuilt here rather than asked of the model. A trailing stop needs BOTH of its
    fields to mean anything, so a half-specified one is dropped rather than sent.
    """
    barrier = _compact(
        {
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "time_limit": time_limit,
            "open_order_type": open_order_type,
            "take_profit_order_type": take_profit_order_type,
            "stop_loss_order_type": stop_loss_order_type,
            "time_limit_order_type": time_limit_order_type,
        }
    )
    if trailing_stop_activation_price is not None and (
        trailing_stop_trailing_delta is not None
    ):
        barrier["trailing_stop"] = {
            "activation_price": trailing_stop_activation_price,
            "trailing_delta": trailing_stop_trailing_delta,
        }
    return barrier


def _amount(value: Any) -> float | None:
    """Read a config amount as a positive float, or ``None`` if it is not one.

    ``order_executor`` types its amount as a string and a saved default can carry
    anything; a value that will not parse is one this check has nothing to say
    about, not one to refuse.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _rule(rules: dict[str, Any], *names: str) -> float | None:
    """Read a venue rule as a finite non-negative float.

    Exchange rules may legitimately use zero to mean "no minimum".
    That is different from a missing or malformed rule and must not be
    collapsed to None.
    """
    import math

    for name in names:
        raw = rules.get(name)

        if raw is None or isinstance(raw, bool):
            continue

        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue

        if math.isfinite(value) and value >= 0:
            return value

    return None


async def _base_amount_violation(
    client: Any,
    *,
    connector: str,
    pair: str,
    config: dict[str, Any],
    min_order_size: float | None,
    min_notional: float | None,
) -> str | None:
    """Check a base-denominated amount (position, order) against both minimums."""
    amount = _amount(config.get("amount"))
    if amount is None:
        return None

    if min_order_size is not None and amount < min_order_size:
        return (
            f"Order size {amount:g} is below {connector}'s minimum of "
            f"{min_order_size:g} for {pair}. Increase amount to at least "
            f"{min_order_size:g} (amount is in the BASE currency)."
        )

    if min_notional is None:
        return None
    price = _amount(config.get("entry_price")) or _amount(config.get("price"))
    if price is None:
        # Imported here, not at module scope: `condor.fetchers.market_data` is
        # reached through the `condor.fetchers` package __init__, which pulls the
        # connectors -> pool_data chain, and this MCP server is also run
        # standalone. Same lazy-import precedent as tools/portfolio.py.
        from condor.fetchers.market_data import fetch_current_price

        # `_amount` still guards the result: a price this check can divide by has
        # to be a positive number, and the fetcher returns the payload's value
        # verbatim. A price we cannot read blocks nothing.
        price = _amount(await fetch_current_price(client, connector, pair))
    if price is None:
        return None
    notional = amount * price
    if notional >= min_notional:
        return None
    base = pair.split("-")[0]
    return (
        f"Order notional {_usd(notional)} is below {connector}'s minimum of "
        f"{_usd(min_notional)} for {pair}. Increase the amount to at least "
        f"{min_notional / price:.8g} {base} at a price of {price:g}."
    )


async def _trading_rule_violation(
    client: Any, executor_type: str, config: dict[str, Any]
) -> str | None:
    """Name the venue rule this config breaks, or ``None`` to let it through.

    The point is WHERE this runs: before the POST, so the caller is told the
    minimum it missed instead of reading a backend stack trace — or, worse,
    watching an executor sit there never filling. It refuses only on a rule the
    venue actually stated; anything unknown (no rules endpoint, no price, an
    unparseable amount) passes, because a rules outage must not stop trading.
    """
    connector = config.get("connector_name")
    pair = config.get("trading_pair")
    if not isinstance(connector, str) or not isinstance(pair, str):
        return None

    rules = await trading_rules_cache.get(client, connector, pair)

    # Generic Condor historically treats unavailable venue rules as fail-open.
    # Spectra's real-money seat does not: unknown exchange constraints mean
    # capital execution is not authorized.
    if settings.agent_slug == "spectra" and rules is None:
        error = (
            f"Spectra execution refused: trading rules unavailable "
            f"for {connector} {pair}"
        )
        logger.warning("%s", error)
        return error
    if not rules:
        return None
    min_order_size = _rule(rules, "min_order_size")
    min_notional = _rule(rules, "min_notional_size", "min_notional")

    if settings.agent_slug == "spectra":
        if min_order_size is None or min_notional is None:
            return (
                "Spectra execution refused: trading rules are incomplete "
                f"for {connector} {pair}; both min_order_size and "
                "min_notional are required"
            )

    if executor_type in ("position_executor", "order_executor"):
        return await _base_amount_violation(
            client,
            connector=connector,
            pair=pair,
            config=config,
            min_order_size=min_order_size,
            min_notional=min_notional,
        )

    if min_notional is None:
        # Every remaining type is funded in quote, so min_order_size — a base
        # amount — says nothing about it without a price.
        return None

    if executor_type == "grid_executor":
        per_level = _amount(config.get("min_order_amount_quote"))
        total = _amount(config.get("total_amount_quote"))

        if settings.agent_slug == "spectra":
            start_price = _amount(config.get("start_price"))
            end_price = _amount(config.get("end_price"))

            if start_price is None or end_price is None:
                return (
                    "Spectra Grid execution refused: start_price and "
                    "end_price must be positive numbers"
                )

            # Every Grid child order must satisfy BOTH:
            #   quote notional >= venue minimum
            #   base amount >= venue minimum
            #
            # base = quote / price, so the highest Grid price is the
            # strictest point for a fixed quote amount.
            required_per_level = max(
                min_notional,
                min_order_size * max(start_price, end_price),
            )

            # Do not delegate Spectra child sizing to Hummingbot's
            # generic backend default. If the strategy omitted the
            # floor, derive an explicit venue-legal one.
            if per_level is None:
                per_level = required_per_level
                config["min_order_amount_quote"] = float(required_per_level)

            if per_level < required_per_level:
                return (
                    "Spectra Grid child order floor "
                    f"{_usd(per_level)} is below the venue-legal "
                    f"minimum of {_usd(required_per_level)} for "
                    f"{connector} {pair}"
                )

            if total is None:
                return (
                    "Spectra Grid execution refused: "
                    "total_amount_quote must be positive"
                )

            if total < per_level:
                return (
                    "Spectra Grid total_amount_quote "
                    f"{_usd(total)} cannot fund even one legal child "
                    f"order of {_usd(per_level)}"
                )

            return None

        if per_level is not None and per_level < min_notional:
            return (
                f"Grid min_order_amount_quote {_usd(per_level)} is below "
                f"{connector}'s minimum order notional of "
                f"{_usd(min_notional)} for {pair}. Raise "
                f"min_order_amount_quote to at least "
                f"{_usd(min_notional)}."
            )

        if total is not None and total < min_notional:
            return (
                f"Grid total_amount_quote {_usd(total)} is below "
                f"{connector}'s minimum order notional of "
                f"{_usd(min_notional)} for {pair} — not one level "
                f"could be placed. Fund the grid with at least "
                f"{_usd(min_notional)}."
            )

        return None

    if executor_type == "dca_executor":
        levels = config.get("amounts_quote")

        if settings.agent_slug == "spectra":
            prices = config.get("prices")

            if not isinstance(levels, (list, tuple)) or not levels:
                return (
                    "Spectra DCA execution refused: amounts_quote "
                    "must contain at least one level"
                )

            if not isinstance(prices, (list, tuple)) or len(prices) != len(levels):
                return (
                    "Spectra DCA execution refused: prices and "
                    "amounts_quote must contain the same number "
                    "of levels"
                )

            for index, (level, price_raw) in enumerate(
                zip(levels, prices),
                start=1,
            ):
                value = _amount(level)
                price = _amount(price_raw)

                if value is None or price is None:
                    return (
                        f"Spectra DCA level {index} has an invalid " "amount or price"
                    )

                required = max(
                    min_notional,
                    min_order_size * price,
                )

                if value < required:
                    return (
                        f"Spectra DCA level {index} of {len(levels)} "
                        f"is {_usd(value)}, below the venue-legal "
                        f"minimum of {_usd(required)} for "
                        f"{connector} {pair}"
                    )

            return None

        if not isinstance(levels, (list, tuple)):
            return None

        for index, level in enumerate(levels, start=1):
            value = _amount(level)

            if value is not None and value < min_notional:
                return (
                    f"DCA level {index} of {len(levels)} is "
                    f"{_usd(value)}, below {connector}'s minimum "
                    f"order notional of {_usd(min_notional)} for "
                    f"{pair}. Raise that level to at least "
                    f"{_usd(min_notional)}."
                )

        return None

    if executor_type == "lp_executor":
        quote_amount = _amount(config.get("quote_amount"))
        if quote_amount is not None and quote_amount < min_notional:
            return (
                f"LP quote_amount {_usd(quote_amount)} is below {connector}'s "
                f"minimum order notional of {_usd(min_notional)} for {pair}. Fund "
                f"the position with at least {_usd(min_notional)}."
            )
    return None


def _authorize_spectra_execution(
    executor_type: str,
    config: dict[str, Any],
    proposal_id: str | None,
    intent: str | None,
    *,
    position_id: str | None = None,
    account_name: str = "master_account",
    controller_id: str = "spectra",
    protected_buy_config: dict[str, Any] | None = None,
    protected_buy_policy: dict[str, Any] | None = None,
) -> str | None:
    """Commit the exact Spectra capital claim before any external mutation."""
    if settings.agent_slug != "spectra":
        return None
    if protected_buy_config is None or protected_buy_policy is None:
        return "Spectra capital execution requires its exact bounded BUY config and declared policy"

    try:
        breaker_mode = get_breaker_mode()
    except Exception as exc:
        return (
            "Spectra breaker could not be read; execution is fail-closed "
            f"as HALTED: {exc}"
        )

    if breaker_mode == BreakerMode.HALTED:
        return "Spectra breaker is HALTED; execution refused before " "capital commit"

    if breaker_mode not in {
        BreakerMode.NORMAL,
        BreakerMode.DEFENSIVE,
    }:
        return (
            "Spectra breaker returned an unknown state; execution is "
            "fail-closed as HALTED"
        )

    if executor_type not in {"grid_executor", "dca_executor"}:
        return (
            f"Spectra execution is not enabled for {executor_type}; "
            "only Kraken spot grid/DCA are authorized in this phase"
        )

    if not (proposal_id or "").strip():
        return "Spectra execution requires a capital proposal_id"

    if not (intent or "").strip():
        return "Spectra execution requires a capital intent"

    connector = str(config.get("connector_name") or "").strip().lower()
    if connector != "kraken":
        return (
            f"Spectra live execution is Kraken spot only; "
            f"connector {connector!r} is not authorized"
        )

    pair = str(config.get("trading_pair") or "").strip()
    if not pair:
        return "Spectra execution requires a trading_pair"

    try:
        leverage = Decimal(str(config.get("leverage", 1)))
    except Exception:
        return "Spectra execution leverage could not be parsed"

    if leverage != Decimal("1"):
        return "Spectra Kraken spot execution requires leverage=1"

    # Make the backend payload explicit. Saved/backend defaults are not
    # allowed to decide these risk-sensitive fields for Spectra spot.
    config["leverage"] = 1

    if executor_type == "grid_executor":
        config["keep_position"] = False
        config["deduct_base_fees"] = False

    try:
        if executor_type == "grid_executor":
            amount_quote = Decimal(str(config["total_amount_quote"]))
        else:
            amounts = config.get("amounts_quote") or []
            amount_quote = exact_decimal_sum(
                (Decimal(str(value)) for value in amounts),
            )
    except Exception:
        return "Spectra execution quote amount could not be determined"

    if amount_quote <= 0:
        return "Spectra execution quote amount must be positive"

    try:
        intent_kind = IntentKind(str(intent).strip().upper())
    except ValueError:
        return f"Unknown Spectra capital intent: {intent!r}"

    proposal = CapitalProposal(
        proposal_id=str(proposal_id).strip(),
        pair=pair,
        intent=intent_kind,
        amount_quote=amount_quote,
        leverage=leverage,
        account_name=account_name,
        connector_name=connector,
        quote_asset=(pair.strip().upper().rsplit("-", 1)[1] if "-" in pair else None),
    )

    try:
        governor = get_capital_governor()
        if intent_kind == IntentKind.ENTRY:
            if not (position_id or "").strip():
                return "Spectra ENTRY requires its exact spectra_position_id"
            ledger = get_economic_position_ledger()
            committed = governor.commit_entry_submission_if_matches(
                proposal,
                position_id=position_id,
                ledger_path=ledger.path,
                account_name=account_name,
                connector_name=connector,
                controller_id=controller_id,
                protected_buy_config=protected_buy_config,
                protected_buy_policy=protected_buy_policy,
            )
        else:
            committed = governor.commit_recovery_submission_if_matches(
                proposal,
                position_id=position_id,
                account_name=account_name,
                connector_name=connector,
                controller_id=controller_id,
                ledger_path=(
                    get_economic_position_ledger().path
                    if protected_buy_config is not None
                    else None
                ),
                protected_buy_config=protected_buy_config,
                protected_buy_policy=protected_buy_policy,
            )
    except Exception as exc:
        return f"Spectra capital governor unavailable: {exc}"

    if not committed:
        return (
            "Spectra capital claim refused: missing, expired, already used, "
            "or its pair/amount/intent/leverage does not match this execution"
        )

    return None


async def _bounded_protected_buy(
    client: Any,
    executor_type: str,
    config: dict[str, Any],
    *,
    max_price_slippage_bps: Any,
    fee_allowance_bps: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use a fresh venue book and immutable parcel reference before capital commit."""
    from spectra_core.economic_position import normalized_base_asset
    from spectra_core.execution_policy import build_protected_buy_limit_config
    from spectra_core.kraken_market_fabric import normalize_kraken_trading_rules

    from .execution_book import fetch_protected_kraken_book

    if max_price_slippage_bps is None or fee_allowance_bps is None:
        raise ValueError(
            "protected BUY requires explicit price-slippage and fee allowances"
        )
    if isinstance(max_price_slippage_bps, bool) or isinstance(fee_allowance_bps, bool):
        raise ValueError("protected BUY policy allowances must be finite decimals")
    pair = str(config.get("trading_pair") or "").strip().upper()
    if str(config.get("connector_name") or "").strip().lower() != "kraken":
        raise ValueError("protected BUY requires Kraken spot")
    if not pair or "-" not in pair:
        raise ValueError("protected BUY requires its exact base/quote pair")
    payload = await client.connectors.get_trading_rules(
        connector_name="kraken",
        trading_pairs=[pair],
    )
    if not isinstance(payload, dict) or not payload:
        raise ValueError("protected BUY trading rules unavailable")
    if not isinstance(payload, dict) or not isinstance(payload.get(pair), dict):
        raise ValueError("protected BUY trading rules lack exact pair scope")
    raw_rule = payload[pair]
    if (
        raw_rule.get("supports_limit_orders") is not True
        or "min_order_size" not in raw_rule
        or not any(
            field in raw_rule for field in ("min_notional_size", "min_order_value")
        )
    ):
        raise ValueError(
            "protected BUY trading rules are incomplete or lack LIMIT support"
        )
    rules, rejected = normalize_kraken_trading_rules(
        payload, quote_assets=(pair.rsplit("-", 1)[1],)
    )
    rule = rules.get(pair)
    if rule is None:
        raise ValueError(
            f"protected BUY trading rules are invalid: {rejected.get(pair, 'unknown')}"
        )
    book = await fetch_protected_kraken_book(rule)
    if book.pair != pair:
        raise ValueError("protected BUY venue book pair differs from authorized pair")
    venue_base = getattr(book, "venue_min_order_size", None)
    venue_quote = getattr(book, "venue_min_cost_quote", None)
    rules_time = getattr(book, "venue_rules_observed_at", None)
    rules_hash = getattr(book, "venue_rules_sha256", None)
    if (
        not isinstance(venue_base, Decimal)
        or not venue_base.is_finite()
        or venue_base <= 0
        or not isinstance(venue_quote, Decimal)
        or not venue_quote.is_finite()
        or venue_quote <= 0
        or getattr(book, "venue_base_asset", None) != normalized_base_asset(pair)
        or getattr(book, "venue_quote_asset", None) != pair.rsplit("-", 1)[1]
        or isinstance(rules_time, bool)
        or not isinstance(rules_time, (int, float))
        or not isfinite(rules_time)
        or rules_time <= 0
        or not isinstance(rules_hash, str)
        or len(rules_hash) != 64
        or any(char not in "0123456789abcdef" for char in rules_hash)
    ):
        raise ValueError("protected BUY lacks exact trusted venue minimum evidence")
    now = time()
    if rules_time > now or now - rules_time > 30:
        raise ValueError("protected BUY venue minimum evidence is stale or future")
    rule = replace(
        rule,
        min_order_size=max(rule.min_order_size, venue_base),
        min_notional_size=max(rule.min_notional_size, venue_quote),
    )
    order = build_protected_buy_limit_config(
        executor_type,
        config,
        trading_rule=rule,
        best_ask=book.best_ask,
        market_observed_at=book.market_observed_at,
        now=time(),
        max_price_slippage_bps=Decimal(str(max_price_slippage_bps)),
        fee_allowance_bps=Decimal(str(fee_allowance_bps)),
    )
    policy = {
        field: str(getattr(order, field))
        for field in (
            "authorized_quote_budget",
            "reference_price",
            "price_cap",
            "amount_base",
            "max_principal_quote",
            "max_fee_quote",
            "max_total_quote",
        )
    }
    policy.update(
        max_price_slippage_bps=str(Decimal(str(max_price_slippage_bps))),
        fee_allowance_bps=str(Decimal(str(fee_allowance_bps))),
        venue_book={
            "pair": book.pair,
            "exchange_pair": book.exchange_pair,
            "best_bid": str(book.best_bid),
            "best_ask": str(book.best_ask),
            "requested_at": book.requested_at,
            "received_at": book.received_at,
            "source": book.source,
            "response_sha256": book.response_sha256,
            "venue_min_order_size": str(venue_base),
            "venue_min_cost_quote": str(venue_quote),
            "venue_base_asset": book.venue_base_asset,
            "venue_quote_asset": book.venue_quote_asset,
            "venue_rules_observed_at": rules_time,
            "venue_rules_sha256": rules_hash,
            "effective_min_order_size": str(rule.min_order_size),
            "effective_min_quote": str(rule.effective_min_quote),
        },
    )
    return order.executor_config, policy


async def create_executor(
    client: Any,
    executor_type: str,
    config: dict[str, Any],
    *,
    account_name: str | None = None,
    controller_id: str | None = None,
    save_as_default: bool = False,
    spectra_proposal_id: str | None = None,
    spectra_intent: str | None = None,
    spectra_position_id: str | None = None,
    spectra_max_price_slippage_bps: str | float | Decimal | None = None,
    spectra_fee_allowance_bps: str | float | Decimal | None = None,
) -> dict[str, Any]:
    """Merge saved defaults, stamp the type, and create the executor.

    The shared tail of all five ``create_*_executor`` tools. ``config`` arrives already
    compacted and already type-checked by the host, so there is nothing left to validate
    server-side — the hand-written field validation the mega-tool needed
    (``validate_executor_config``) existed only because the config was an opaque dict.
    """
    account = account_name or "master_account"
    tag = "spectra" if settings.agent_slug == "spectra" else (controller_id or "main")

    merged_config = executor_preferences.merge_with_defaults(executor_type, config)
    merged_config["type"] = executor_type
    # A saved default may carry a controller_id; the explicit tool parameter is the
    # one the risk gate attributed the position to, so it wins and never travels
    # inside the config.
    merged_config.pop("controller_id", None)

    backend_config = merged_config
    protected_buy_policy = None
    if settings.agent_slug == "spectra":
        try:
            try:
                mode = get_breaker_mode()
            except Exception as exc:
                raise ValueError(
                    f"Spectra breaker could not be read; execution is fail-closed as HALTED: {exc}"
                ) from exc
            if mode == BreakerMode.HALTED:
                raise ValueError(
                    "Spectra breaker is HALTED; execution refused before capital commit"
                )
            if mode not in {BreakerMode.NORMAL, BreakerMode.DEFENSIVE}:
                raise ValueError(
                    "Spectra breaker returned an unknown state; fail-closed as HALTED"
                )
            if executor_type not in {"grid_executor", "dca_executor"}:
                raise ValueError(
                    f"Spectra execution is not enabled for {executor_type}"
                )
            account = _protected_attempt_text(
                account_name if account_name is not None else account, "account_name"
            )
            attempt_id = _protected_buy_attempt_id(
                proposal_id=spectra_proposal_id,
                account_name=account,
                controller_id=tag,
                position_id=spectra_position_id,
                intent=spectra_intent,
                connector_name=merged_config.get("connector_name"),
                trading_pair=merged_config.get("trading_pair"),
            )
            # A caller or saved default cannot create another backend identity
            # for the same authorized capital claim. Stamp the derived identity
            # after the pure price/quantity adapter and before capital commit.
            merged_config.pop("id", None)
            if not (spectra_proposal_id or "").strip():
                raise ValueError("Spectra execution requires a capital proposal_id")
            if not (spectra_intent or "").strip():
                raise ValueError("Spectra execution requires a capital intent")
            cash_state = get_capital_governor().cash_authority_snapshot()
            protected_conflict = get_capital_governor().protected_buy_scope_rejection(
                account_name=account,
                connector_name=merged_config.get("connector_name"),
            )
            if protected_conflict:
                raise ValueError(protected_conflict)
            if cash_state is not None and cash_state.blocked:
                raise ValueError(
                    f"protected BUY cash authority is blocked: {cash_state.reason}"
                )
            backend_config, protected_buy_policy = await _bounded_protected_buy(
                client,
                executor_type,
                merged_config,
                max_price_slippage_bps=spectra_max_price_slippage_bps,
                fee_allowance_bps=spectra_fee_allowance_bps,
            )
            backend_config["id"] = attempt_id
        except Exception as exc:
            error = f"Spectra protected BUY refused before capital commit: {exc}"
            return {
                "action": "create",
                "executor_type": executor_type,
                "error": error,
                "formatted_output": f"Error creating {executor_type}: {error}",
            }

    logger.info(
        "create_%s: controller_id=%r, account=%s, pair=%s",
        executor_type,
        tag,
        account,
        merged_config.get("trading_pair"),
    )

    # The protected adapter already validated exact rule/step/minimum geometry.
    # Generic Condor paths retain their existing venue checks.
    violation = (
        None
        if protected_buy_policy is not None
        else await _trading_rule_violation(client, executor_type, merged_config)
    )
    if violation:
        # Refused here, not by the backend: below a venue minimum the API answers
        # with an opaque error or accepts an order that never fills.
        logger.info("create_%s refused by a trading rule: %s", executor_type, violation)
        return {
            "action": "create",
            "executor_type": executor_type,
            "error": violation,
            "formatted_output": f"Error creating {executor_type}: {violation}",
        }

    spectra_error = _authorize_spectra_execution(
        executor_type,
        merged_config,
        spectra_proposal_id,
        spectra_intent,
        position_id=spectra_position_id,
        account_name=account,
        controller_id=tag,
        protected_buy_config=(
            backend_config if protected_buy_policy is not None else None
        ),
        protected_buy_policy=protected_buy_policy,
    )
    if spectra_error:
        logger.warning(
            "create_%s refused by Spectra Core: %s", executor_type, spectra_error
        )
        return {
            "action": "create",
            "executor_type": executor_type,
            "error": spectra_error,
            "formatted_output": f"Error creating {executor_type}: {spectra_error}",
        }

    try:
        result = await client.executors.create_executor(
            executor_config=backend_config,
            account_name=account,
            controller_id=tag,
        )
    except Exception as exc:
        return {
            "action": "create",
            "executor_type": executor_type,
            "error": str(exc),
            "formatted_output": f"Error creating {executor_type}: {exc}",
        }

    if settings.agent_slug == "spectra":
        executor_id = result.get("executor_id") if isinstance(result, Mapping) else None
        if not isinstance(executor_id, str) or not executor_id.strip():
            error = (
                "Spectra execution result is ambiguous: Hummingbot returned "
                "a response without a non-empty string executor_id. "
                "Capital remains COMMITTED; "
                "do not retry blindly."
            )
            logger.error("%s", error)
            return {
                "action": "create",
                "executor_type": executor_type,
                "expected_executor_id": backend_config["id"],
                "error": error,
                "result": result,
                "formatted_output": f"Error creating {executor_type}: {error}",
            }

        if executor_id != backend_config["id"]:
            error = (
                "Spectra execution result is ambiguous: Hummingbot returned "
                "an executor_id different from the committed protected BUY "
                "attempt. Capital remains COMMITTED; do not retry blindly."
            )
            logger.error("%s", error)
            return {
                "action": "create",
                "executor_type": executor_type,
                "expected_executor_id": backend_config["id"],
                "error": error,
                "result": result,
                "formatted_output": f"Error creating {executor_type}: {error}",
            }

        try:
            reconciled = get_capital_governor().reconcile_execution(
                proposal_id=str(spectra_proposal_id or ""),
                executor_id=str(executor_id),
                account_name=account,
                connector_name=str(merged_config.get("connector_name") or ""),
                controller_id=tag,
            )
        except Exception as exc:
            error = (
                "Spectra executor was created but receipt reconciliation failed; "
                f"executor_id={executor_id}. Capital remains protected. "
                f"Do not retry blindly: {exc}"
            )
            logger.exception("%s", error)
            return {
                "action": "create",
                "executor_id": executor_id,
                "executor_type": executor_type,
                "error": error,
                "result": result,
                "formatted_output": f"Error reconciling {executor_type}: {error}",
            }

        if not reconciled:
            error = (
                "Spectra executor was created but its committed capital claim "
                f"could not be reconciled; executor_id={executor_id}. "
                "Do not retry blindly."
            )
            logger.error("%s", error)
            return {
                "action": "create",
                "executor_id": executor_id,
                "executor_type": executor_type,
                "error": error,
                "result": result,
                "formatted_output": f"Error reconciling {executor_type}: {error}",
            }
    else:
        executor_id = result.get("executor_id") or result.get("id")

    if save_as_default:
        executor_preferences.update_defaults(executor_type, config)

    formatted = (
        "Executor created successfully!\n\n"
        f"Executor ID: {executor_id or 'N/A'}\n"
        f"Type: {backend_config['type']}\n"
        f"Account: {account}\n"
        f"Controller: {tag}\n"
    )
    if save_as_default:
        formatted += f"\nConfiguration saved as default for {executor_type}"

    return {
        "action": "create",
        "executor_id": executor_id,
        "executor_type": backend_config["type"],
        "requested_executor_type": executor_type,
        "account": account,
        "controller_id": tag,
        "config_used": backend_config,
        "saved_as_default": save_as_default,
        "result": result,
        "formatted_output": formatted,
    }


async def create_position_executor(
    client: Any,
    *,
    connector_name: str,
    trading_pair: str,
    side: int,
    amount: float,
    entry_price: float | None = None,
    leverage: int | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    time_limit: int | None = None,
    trailing_stop_activation_price: float | None = None,
    trailing_stop_trailing_delta: float | None = None,
    open_order_type: int | None = None,
    take_profit_order_type: int | None = None,
    stop_loss_order_type: int | None = None,
    time_limit_order_type: int | None = None,
    level_id: str | None = None,
    account_name: str | None = None,
    controller_id: str | None = None,
    save_as_default: bool = False,
) -> dict[str, Any]:
    """Open a directional position with triple-barrier exits."""
    config = _compact(
        {
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "side": side,
            "amount": amount,
            "entry_price": entry_price,
            "leverage": leverage,
            "level_id": level_id,
        }
    )
    barrier = _triple_barrier(
        stop_loss=stop_loss,
        take_profit=take_profit,
        time_limit=time_limit,
        trailing_stop_activation_price=trailing_stop_activation_price,
        trailing_stop_trailing_delta=trailing_stop_trailing_delta,
        open_order_type=open_order_type,
        take_profit_order_type=take_profit_order_type,
        stop_loss_order_type=stop_loss_order_type,
        time_limit_order_type=time_limit_order_type,
    )
    if barrier:
        config["triple_barrier_config"] = barrier

    return await create_executor(
        client,
        "position_executor",
        config,
        account_name=account_name,
        controller_id=controller_id,
        save_as_default=save_as_default,
    )


async def create_grid_executor(
    client: Any,
    *,
    connector_name: str,
    trading_pair: str,
    side: int,
    start_price: float,
    end_price: float,
    limit_price: float,
    total_amount_quote: float,
    take_profit: float | None = None,
    time_limit: int | None = None,
    open_order_type: int | None = None,
    take_profit_order_type: int | None = None,
    min_spread_between_orders: float | None = None,
    min_order_amount_quote: float | None = None,
    max_open_orders: int | None = None,
    max_orders_per_batch: int | None = None,
    order_frequency: int | None = None,
    activation_bounds: float | None = None,
    safe_extra_spread: float | None = None,
    leverage: int | None = None,
    keep_position: bool | None = None,
    coerce_tp_to_step: bool | None = None,
    deduct_base_fees: bool | None = None,
    level_id: str | None = None,
    account_name: str | None = None,
    controller_id: str | None = None,
    save_as_default: bool = False,
    spectra_proposal_id: str | None = None,
    spectra_intent: str | None = None,
    spectra_position_id: str | None = None,
    spectra_max_price_slippage_bps: str | float | Decimal | None = None,
    spectra_fee_allowance_bps: str | float | Decimal | None = None,
) -> dict[str, Any]:
    """Run a grid of limit orders across a price range.

    Raises ``ValueError`` when the three prices do not describe the requested direction:
    the backend accepts an inverted grid and it simply never fills, so the invariant the
    guide states in prose is checked here where the caller can still fix it.
    """
    if side == 1 and not (limit_price < start_price < end_price):
        raise ValueError(
            "LONG grid (side=1) requires limit_price < start_price < end_price, got "
            f"limit_price={limit_price}, start_price={start_price}, "
            f"end_price={end_price}"
        )
    if side == 2 and not (start_price < end_price < limit_price):
        raise ValueError(
            "SHORT grid (side=2) requires start_price < end_price < limit_price, got "
            f"start_price={start_price}, end_price={end_price}, "
            f"limit_price={limit_price}"
        )

    config = _compact(
        {
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "side": side,
            "start_price": start_price,
            "end_price": end_price,
            "limit_price": limit_price,
            "total_amount_quote": total_amount_quote,
            "min_spread_between_orders": min_spread_between_orders,
            "min_order_amount_quote": min_order_amount_quote,
            "max_open_orders": max_open_orders,
            "max_orders_per_batch": max_orders_per_batch,
            "order_frequency": order_frequency,
            "activation_bounds": activation_bounds,
            "safe_extra_spread": safe_extra_spread,
            "leverage": leverage,
            "keep_position": keep_position,
            "coerce_tp_to_step": coerce_tp_to_step,
            "deduct_base_fees": deduct_base_fees,
            "level_id": level_id,
        }
    )
    # Required by the backend schema, unlike the position executor's, so it is always
    # sent — even empty, which means "every barrier at its default".
    config["triple_barrier_config"] = _triple_barrier(
        take_profit=take_profit,
        time_limit=time_limit,
        open_order_type=open_order_type,
        take_profit_order_type=take_profit_order_type,
    )

    return await create_executor(
        client,
        "grid_executor",
        config,
        account_name=account_name,
        controller_id=controller_id,
        save_as_default=save_as_default,
        spectra_proposal_id=spectra_proposal_id,
        spectra_intent=spectra_intent,
        spectra_position_id=spectra_position_id,
        spectra_max_price_slippage_bps=spectra_max_price_slippage_bps,
        spectra_fee_allowance_bps=spectra_fee_allowance_bps,
    )


async def create_dca_executor(
    client: Any,
    *,
    connector_name: str,
    trading_pair: str,
    side: int,
    amounts_quote: list[float],
    prices: list[float],
    leverage: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    time_limit: int | None = None,
    trailing_stop_activation_price: float | None = None,
    trailing_stop_trailing_delta: float | None = None,
    mode: str | None = None,
    level_id: str | None = None,
    account_name: str | None = None,
    controller_id: str | None = None,
    save_as_default: bool = False,
    spectra_proposal_id: str | None = None,
    spectra_intent: str | None = None,
    spectra_position_id: str | None = None,
    spectra_max_price_slippage_bps: str | float | Decimal | None = None,
    spectra_fee_allowance_bps: str | float | Decimal | None = None,
) -> dict[str, Any]:
    """Average into a position over a ladder of price levels.

    Raises ``ValueError`` when ``amounts_quote`` and ``prices`` are not parallel: they
    are two halves of one list of levels, and a length mismatch is a silently wrong
    ladder rather than an error the backend reports.
    """
    if len(amounts_quote) != len(prices):
        raise ValueError(
            "amounts_quote and prices are parallel lists — one entry each per DCA "
            f"level, got {len(amounts_quote)} amounts and {len(prices)} prices"
        )
    if not amounts_quote:
        raise ValueError("amounts_quote must contain at least one level")

    config = _compact(
        {
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "side": side,
            "amounts_quote": amounts_quote,
            "prices": prices,
            "leverage": leverage,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "time_limit": time_limit,
            "mode": mode,
            "level_id": level_id,
        }
    )
    if trailing_stop_activation_price is not None and (
        trailing_stop_trailing_delta is not None
    ):
        # Flat on the DCA config, not nested in a triple barrier.
        config["trailing_stop"] = {
            "activation_price": trailing_stop_activation_price,
            "trailing_delta": trailing_stop_trailing_delta,
        }

    return await create_executor(
        client,
        "dca_executor",
        config,
        account_name=account_name,
        controller_id=controller_id,
        save_as_default=save_as_default,
        spectra_proposal_id=spectra_proposal_id,
        spectra_intent=spectra_intent,
        spectra_position_id=spectra_position_id,
        spectra_max_price_slippage_bps=spectra_max_price_slippage_bps,
        spectra_fee_allowance_bps=spectra_fee_allowance_bps,
    )


async def create_order_executor(
    client: Any,
    *,
    connector_name: str,
    trading_pair: str,
    side: int,
    amount: str,
    execution_strategy: str,
    price: float | None = None,
    chaser_distance: float | None = None,
    chaser_refresh_threshold: float | None = None,
    leverage: int | None = None,
    position_action: str | None = None,
    level_id: str | None = None,
    account_name: str | None = None,
    controller_id: str | None = None,
    save_as_default: bool = False,
) -> dict[str, Any]:
    """Place one order with a chosen execution strategy.

    Raises ``ValueError`` when the strategy and its companion field disagree — a LIMIT
    with no price, or a LIMIT_CHASER with no chaser config, is rejected by the backend
    only after a round trip.
    """
    if execution_strategy in ("LIMIT", "LIMIT_MAKER") and price is None:
        raise ValueError(f"execution_strategy={execution_strategy} requires a price")
    if execution_strategy == "LIMIT_CHASER" and (
        chaser_distance is None or chaser_refresh_threshold is None
    ):
        raise ValueError(
            "execution_strategy=LIMIT_CHASER requires both chaser_distance and "
            "chaser_refresh_threshold"
        )

    config = _compact(
        {
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "side": side,
            "amount": amount,
            "execution_strategy": execution_strategy,
            "price": price,
            "leverage": leverage,
            "position_action": position_action,
            "level_id": level_id,
        }
    )
    if chaser_distance is not None and chaser_refresh_threshold is not None:
        config["chaser_config"] = {
            "distance": chaser_distance,
            "refresh_threshold": chaser_refresh_threshold,
        }

    return await create_executor(
        client,
        "order_executor",
        config,
        account_name=account_name,
        controller_id=controller_id,
        save_as_default=save_as_default,
    )


async def create_lp_executor(
    client: Any,
    *,
    connector_name: str,
    lp_provider: str,
    trading_pair: str,
    pool_address: str,
    lower_price: float,
    upper_price: float,
    side: int,
    base_amount: float | None = None,
    quote_amount: float | None = None,
    upper_limit_price: float | None = None,
    lower_limit_price: float | None = None,
    swap_provider: str | None = None,
    keep_position: bool | None = None,
    extra_params: dict[str, Any] | None = None,
    account_name: str | None = None,
    controller_id: str | None = None,
    save_as_default: bool = False,
) -> dict[str, Any]:
    """Open a CLMM liquidity position inside a price range.

    Raises ``ValueError`` on a range that is not a range, or on a position funded with
    nothing: both are accepted by the backend and fail on-chain.
    """
    if lower_price >= upper_price:
        raise ValueError(
            f"lower_price must be below upper_price, got {lower_price} >= {upper_price}"
        )
    if not (base_amount or quote_amount):
        raise ValueError(
            "an LP position needs funding — give base_amount, quote_amount, or both"
        )

    config = _compact(
        {
            "connector_name": connector_name,
            "lp_provider": lp_provider,
            "trading_pair": trading_pair,
            "pool_address": pool_address,
            "lower_price": lower_price,
            "upper_price": upper_price,
            "side": side,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "upper_limit_price": upper_limit_price,
            "lower_limit_price": lower_limit_price,
            "swap_provider": swap_provider,
            "keep_position": keep_position,
            "extra_params": extra_params,
        }
    )

    return await create_executor(
        client,
        "lp_executor",
        config,
        account_name=account_name,
        controller_id=controller_id,
        save_as_default=save_as_default,
    )
