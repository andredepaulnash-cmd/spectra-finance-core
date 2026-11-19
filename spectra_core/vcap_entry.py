"""Governor-gated admission from the fresh Kraken mean-reversion shortlist.

The autonomous portfolio supervisor calls this for each eligible fleet
vacancy. This function reserves exactly one $20 seed through CapitalGovernor;
it never creates an executor or dispatches an order. It accepts only a fresh
candidate from the scanner's own mean_reversion_candidates list and requires
the separate paper asset allowlist before reserving capital.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Mapping

from .asset_authority import canonical_kraken_usd_pair, load_approved_kraken_assets
from .kraken_authorization import reserve_kraken_capital
from .models import DecisionStatus, IntentKind
from .runtime import spectra_paper_only_enabled

PAPER_VCAP_SEED_QUOTE = Decimal("20")
MAX_DISCOVERY_AGE_SECONDS = 300.0
LIVE_TRUE_VALUES = {"1", "true", "yes"}


@dataclass(frozen=True)
class VCAPPaperEntryAdmission:
    pair: str
    proposal_id: str
    position_id: str | None
    amount_quote: Decimal
    decision_status: DecisionStatus
    decision_reason: str
    selection_score: float
    scanner_as_of: float
    paper_only: bool = True
    position_slot_opened: bool = False
    order_dispatched: bool = False
    strategy_source: str = "MEAN_REVERSION"


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite numeric data") from exc
    if not isfinite(result):
        raise ValueError(f"{field} must be finite numeric data")
    return result


async def reserve_vcap_candidate_entry(
    client: Any,
    *,
    scan_result: Mapping[str, Any],
    trading_pair: str,
    account_name: str,
    governor=None,
    now: float | None = None,
    strategy_source: str = "MEAN_REVERSION",
) -> VCAPPaperEntryAdmission:
    """Reserve one $20 ENTRY for a fresh scanner candidate.

    No executor is created and no order is sent. Paper entries require the
    manual or fresh auto-discovery allowlist. Live entries additionally require
    both the explicit live-mode flags; the live Governor policy and breaker
    remain independent gates.
    """
    configured_mode = (os.getenv("SPECTRA_PAPER_ONLY") or "").strip().lower()
    live_authorized = (
        configured_mode in {"0", "false", "no"}
        and (os.getenv("SPECTRA_ALLOW_LIVE_EXECUTION") or "").strip().lower()
        in LIVE_TRUE_VALUES
    )
    if configured_mode in {"0", "false", "no"} and not live_authorized:
        raise ValueError(
            "live VCAP candidate admission requires SPECTRA_ALLOW_LIVE_EXECUTION=true"
        )
    paper_only = spectra_paper_only_enabled()
    if not paper_only and not live_authorized:
        raise ValueError(
            "live VCAP candidate admission requires separate explicit authorization"
        )

    if not isinstance(scan_result, Mapping):
        raise ValueError("market discovery result must be a mapping")
    if scan_result.get("paper_only") is not True or scan_result.get("orders_created") != 0:
        raise ValueError("market discovery result is not a research-only scan")
    if scan_result.get("source") != "Kraken spot market data via Hummingbot API":
        raise ValueError("VCAP admission requires the configured Kraken market-data scan")

    timestamp = time.time() if now is None else _finite_number(now, "now")
    scanner_as_of = _finite_number(scan_result.get("as_of"), "scanner as_of")
    age = timestamp - scanner_as_of
    if age < -5.0 or age > MAX_DISCOVERY_AGE_SECONDS:
        raise ValueError("mean-reversion shortlist is stale or has a future timestamp")

    source = str(strategy_source).strip().upper()
    if source not in {"MEAN_REVERSION", "TOP_LOSER_REVERSAL"}:
        raise ValueError("unsupported entry strategy source")
    pair = canonical_kraken_usd_pair(trading_pair)
    lists = scan_result.get("lists")
    list_key = "top_loser_reversal_candidates" if source == "TOP_LOSER_REVERSAL" else "mean_reversion_candidates"
    candidates = lists.get(list_key) if isinstance(lists, Mapping) else None
    if not isinstance(candidates, list):
        raise ValueError(f"market discovery has no {list_key} list")

    candidate = next(
        (
            row for row in candidates
            if isinstance(row, Mapping)
            and canonical_kraken_usd_pair(str(row.get("trading_pair") or "")) == pair
        ),
        None,
    )
    if candidate is None:
        raise ValueError(f"{pair} is not in the current mean-reversion shortlist")
    if candidate.get("eligible") is not True:
        raise ValueError(f"{pair} no longer passes mean-reversion eligibility")
    if candidate.get("capital_authorized") is not False:
        raise ValueError("scanner candidates must remain informational, not pre-authorized")
    if source == "TOP_LOSER_REVERSAL":
        change = candidate.get("change_24h_pct")
        if (
            candidate.get("reversal_pathway_eligible") is not True
            or candidate.get("entry_liquidity_eligible") is not True
            or change is None
            or _finite_number(change, "change_24h_pct") >= 0
            or int(candidate.get("reversal_signal_count") or 0) < 2
        ):
            raise ValueError("top-loser reversal candidate no longer passes its pathway evidence")

    selection_score = _finite_number(candidate.get("selection_score"), "selection_score")
    current_price = _finite_number(candidate.get("current_price"), "current_price")
    volume = _finite_number(candidate.get("volume_24h_usd"), "volume_24h_usd")
    if current_price <= 0 or volume <= 0:
        raise ValueError("candidate price and 24h USD volume must be positive")
    if source == "TOP_LOSER_REVERSAL" and volume < 10_000_000:
        raise ValueError("top-loser reversal entry requires at least $10M 24h quote volume")
    if str(candidate.get("quote_asset") or "USD").strip().upper() != "USD":
        raise ValueError("VCAP entries are restricted to Kraken USD spot pairs")

    try:
        minimum_order_quote = Decimal(str(candidate.get("minimum_order_quote")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("candidate is missing a valid Kraken minimum-order quote") from exc
    maximum_minimum_quote = (
        PAPER_VCAP_SEED_QUOTE * Decimal("10000") / Decimal("10070")
    )
    if (
        not minimum_order_quote.is_finite()
        or minimum_order_quote <= 0
        or minimum_order_quote > maximum_minimum_quote
    ):
        raise ValueError(
            "candidate venue minimum cannot fit inside the $20 entry after fee and slippage allowances"
        )

    if paper_only:
        connector_name = "kraken_paper_trade"
        if connector_name != "kraken_paper_trade":
            raise ValueError("Spectra paper asset authority only permits kraken_paper_trade")
        asset_policy = load_approved_kraken_assets()
        if not asset_policy.configured or pair not in asset_policy.pairs:
            raise ValueError(
                f"{pair} is not in the Governor-approved Kraken asset list"
            )

    authorization = await reserve_kraken_capital(
        client,
        account_name=account_name,
        trading_pair=pair,
        intent=IntentKind.ENTRY,
        amount_quote=PAPER_VCAP_SEED_QUOTE,
        governor=governor,
        strategy_source=source,
    )
    position_id = (
        authorization.economic_position_id
        if authorization.decision.approved
        else None
    )

    return VCAPPaperEntryAdmission(
        pair=pair,
        proposal_id=authorization.proposal.proposal_id,
        position_id=position_id,
        amount_quote=PAPER_VCAP_SEED_QUOTE,
        decision_status=authorization.decision.status,
        decision_reason=authorization.decision.reason,
        selection_score=selection_score,
        scanner_as_of=scanner_as_of,
        position_slot_opened=(
            authorization.decision.approved
            and authorization.economic_position_id is not None
        ),
        paper_only=paper_only,
        strategy_source=source,
    )
