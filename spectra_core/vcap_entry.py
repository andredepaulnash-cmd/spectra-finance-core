"""Governor-gated admission from the live Kraken mean-reversion shortlist.

This is an explicit operator-triggered paper-entry reservation, not an order
dispatcher. It accepts only a fresh candidate from the scanner's own
mean_reversion_candidates list, requires the separate paper asset allowlist,
and reserves exactly the configured $20 seed through CapitalGovernor.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
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
    order_dispatched: bool = False


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
) -> VCAPPaperEntryAdmission:
    """Reserve one $20 ENTRY for a fresh, explicitly approved scanner candidate.

    No executor is created and no order is sent. The returned proposal and
    canonical prefill position ID are the exact inputs for the separately
    guarded Kraken paper executor submission path.
    """
    if not spectra_paper_only_enabled():
        raise ValueError("VCAP candidate admission is paper-only")
    if (os.getenv("SPECTRA_ALLOW_LIVE_EXECUTION") or "false").strip().lower() in LIVE_TRUE_VALUES:
        raise ValueError("VCAP candidate admission requires live execution to remain disabled")

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

    pair = canonical_kraken_usd_pair(trading_pair)
    lists = scan_result.get("lists")
    candidates = lists.get("mean_reversion_candidates") if isinstance(lists, Mapping) else None
    if not isinstance(candidates, list):
        raise ValueError("market discovery has no mean-reversion candidate list")

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

    selection_score = _finite_number(candidate.get("selection_score"), "selection_score")
    current_price = _finite_number(candidate.get("current_price"), "current_price")
    volume = _finite_number(candidate.get("volume_24h_usd"), "volume_24h_usd")
    if current_price <= 0 or volume <= 0:
        raise ValueError("candidate price and 24h USD volume must be positive")
    if str(candidate.get("quote_asset") or "USD").strip().upper() != "USD":
        raise ValueError("VCAP paper entries are restricted to Kraken USD spot pairs")

    asset_policy = load_approved_kraken_assets()
    if not asset_policy.configured or pair not in asset_policy.pairs:
        raise ValueError(
            f"{pair} is not in the explicit Governor-approved paper asset list"
        )

    authorization = await reserve_kraken_capital(
        client,
        account_name=account_name,
        trading_pair=pair,
        intent=IntentKind.ENTRY,
        amount_quote=PAPER_VCAP_SEED_QUOTE,
        governor=governor,
    )
    if authorization.decision.approved and not authorization.economic_position_id:
        raise RuntimeError("approved paper ENTRY has no canonical economic position ID")

    return VCAPPaperEntryAdmission(
        pair=pair,
        proposal_id=authorization.proposal.proposal_id,
        position_id=authorization.economic_position_id,
        amount_quote=PAPER_VCAP_SEED_QUOTE,
        decision_status=authorization.decision.status,
        decision_reason=authorization.decision.reason,
        selection_score=selection_score,
        scanner_as_of=scanner_as_of,
    )
