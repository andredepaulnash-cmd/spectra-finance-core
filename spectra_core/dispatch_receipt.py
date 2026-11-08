"""Ed25519 signing for exact-scope Spectra-to-Hummingbot API dispatch receipts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True)
class DispatchReceipt:
    token: str
    receipt_id: str
    expires_at: float
    body_sha256: str


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _clean(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _private_key() -> Ed25519PrivateKey:
    encoded = os.getenv("SPECTRA_DISPATCH_PRIVATE_KEY", "").strip()
    if not encoded:
        raise RuntimeError(
            "SPECTRA_DISPATCH_PRIVATE_KEY is required for Spectra execution"
        )
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception as exc:
        raise RuntimeError("SPECTRA_DISPATCH_PRIVATE_KEY is malformed") from exc
    if len(raw) != 32:
        raise RuntimeError(
            "SPECTRA_DISPATCH_PRIVATE_KEY must encode 32 raw Ed25519 private-key bytes"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def validate_dispatch_signing_key() -> None:
    """Validate signing configuration before the Governor commits a dispatch."""
    _private_key()


def issue_executor_dispatch_receipt(
    *,
    request_body: dict[str, Any],
    proposal_id: str,
    intent: str,
    amount_quote: Decimal | str | int | float,
    now: float,
    ttl_seconds: int = 30,
    policy_version: str | None = None,
) -> DispatchReceipt:
    """Sign one exact POST /executors request; no wildcard fields are accepted."""
    if not isinstance(request_body, dict):
        raise ValueError("Spectra dispatch request body must be an object")
    config = request_body.get("executor_config")
    if not isinstance(config, dict):
        raise ValueError("Spectra dispatch request requires executor_config")
    account_name = str(request_body.get("account_name") or "").strip()
    connector_name = str(config.get("connector_name") or "").strip().lower()
    trading_pair = str(config.get("trading_pair") or "").strip()
    proposal = str(proposal_id or "").strip()
    normalized_intent = str(intent or "").strip().upper()
    amount = Decimal(str(amount_quote))
    if not account_name or not connector_name or not trading_pair or not proposal:
        raise ValueError("Spectra dispatch receipt scope must be exact and non-empty")
    if normalized_intent not in {"ENTRY", "RECOVERY", "EXIT", "CANCEL"}:
        raise ValueError("Unsupported Spectra dispatch intent")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Spectra dispatch amount must be finite and positive")
    if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 60:
        raise ValueError("Spectra dispatch receipt TTL must be between 1 and 60 seconds")

    canonical_body = _clean(request_body)
    body_hash = canonical_sha256(canonical_body)
    config_hash = canonical_sha256(config)
    receipt_id = uuid.uuid4().hex
    issued_at = float(now)
    expires_at = issued_at + ttl_seconds
    payload = {
        "version": 1,
        "receipt_id": receipt_id,
        "proposal_id": proposal,
        "intent": normalized_intent,
        "account_name": account_name,
        "connector_name": connector_name,
        "trading_pair": trading_pair,
        "operation": "create_executor",
        "amount": str(amount),
        "config_sha256": config_hash,
        "body_sha256": body_hash,
        "method": "POST",
        "path": "/executors",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "policy_version": (
            str(policy_version or os.getenv("SPECTRA_DISPATCH_POLICY_VERSION", "spectra-policy-v1"))
        ),
    }
    if not payload["policy_version"].strip():
        raise ValueError("Spectra dispatch policy version must be non-empty")

    payload_bytes = canonical_json(payload)
    signature = _private_key().sign(payload_bytes)
    token = _b64url_encode(payload_bytes) + "." + _b64url_encode(signature)
    return DispatchReceipt(
        token=token,
        receipt_id=receipt_id,
        expires_at=expires_at,
        body_sha256=body_hash,
    )
