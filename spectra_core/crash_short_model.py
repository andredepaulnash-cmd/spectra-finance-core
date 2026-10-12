"""Frozen, fractional PAPER price-path model. This is not a venue contract model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

PROTOCOL_SHA256 = "bef20f04281c40fe8a23de7955080505db6c358f28ba5d8d6082745e127e426b"
INPUT_SHA256 = "5f4b40da6cd468c0d66bce148e1648c71d29f41942d16b137ca55aa32c12eefd"
POLICY_ID = "crash_short_proxy_v1_btc_eth_fresh24h"
ASSETS = ("BTC", "ETH")
STEP = 300
DAY = 86400
FROZEN_START = 1767225600
FROZEN_END = 1782864000
INITIAL_EQUITY = Decimal("2000")
ENTRY_NOTIONAL = Decimal("750")
RESERVE = Decimal("500")
ONE_WAY_COST = Decimal("0.0045")
DAILY_ADVERSE_FUNDING = Decimal("0.001")


def canonical(value: Any) -> str:
    """Stable JSON for durable evidence; no NaN or lossy Decimal conversion."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class PaperSource:
    """Caller attests synthetic inputs; frozen-file loader verifies actual bytes.

    This is immutable provenance, never an exchange/account permission. No
    arbitrary historical or current-market source is supported in this release.
    """

    kind: str
    dataset_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"SYNTHETIC", "FROZEN_JAN_JUNE_2026"}:
            raise ValueError("Only synthetic or frozen Jan-June inputs are supported")
        if len(self.dataset_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.dataset_sha256
        ):
            raise ValueError("A lowercase SHA256 input identity is required")
        if self.kind == "FROZEN_JAN_JUNE_2026" and self.dataset_sha256 != INPUT_SHA256:
            raise ValueError("Unknown frozen input hash")

    def validate_time(self, timestamp: int) -> None:
        if type(timestamp) is not int or timestamp < 0 or timestamp % STEP:
            raise ValueError(
                "timestamp must be a nonnegative aligned five-minute integer"
            )
        if self.kind == "FROZEN_JAN_JUNE_2026" and not (
            FROZEN_START <= timestamp < FROZEN_END
        ):
            raise ValueError("Outside the frozen Jan-June window")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "dataset_sha256": self.dataset_sha256}


# A complete schema documents the gate; there is deliberately no contract engine.
REQUIRED_CONTRACT_METADATA = (
    "venue",
    "product_id",
    "underlying",
    "settlement_currency",
    "collateral_currency",
    "contract_unit",
    "contract_step",
    "minimum_contracts",
    "tick_size",
    "trading_hours",
    "listing_date",
    "expiry_roll_rules",
    "order_types",
    "reduce_only",
    "fee_schedule",
    "funding_convention",
    "initial_margin",
    "maintenance_margin",
    "liquidation_recourse",
    "account_route_eligibility",
    "source_url",
    "verified_at",
    "effective_at",
)


def contract_model_gate(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Unknown real metadata fails closed; even complete metadata grants no route."""
    metadata = metadata or {}
    missing = [
        key
        for key in REQUIRED_CONTRACT_METADATA
        if metadata.get(key) is None or metadata.get(key) == ""
    ]
    return {
        "status": "BLOCKED_UNKNOWN_METADATA" if missing else "UNSUPPORTED_MODEL",
        "missing": missing,
        "live_dispatch_available": False,
        "reason": "This release implements only hypothetical fractional spot-price shorts",
    }
