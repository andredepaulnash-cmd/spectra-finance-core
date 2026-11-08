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
    operation: str = "create_executor",
    method: str = "POST",
    path: str = "/executors",
    executor_id: str | None = None,
    account_name: str | None = None,
    connector_name: str | None = None,
    trading_pair: str | None = None,
) -> DispatchReceipt:
    """Sign one exact, single-use executor create or stop request."""
    if not isinstance(request_body, dict):
        raise ValueError("Spectra dispatch request body must be an object")
    proposal = str(proposal_id or "").strip()
    normalized_intent = str(intent or "").strip().upper()
    amount = Decimal(str(amount_quote))
    if not proposal:
        raise ValueError("Spectra dispatch proposal/manifest identity must be non-empty")
    if normalized_intent not in {"ENTRY", "RECOVERY", "EXIT", "CANCEL"}:
        raise ValueError("Unsupported Spectra dispatch intent")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Spectra dispatch amount must be finite and positive")
    if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 60:
        raise ValueError("Spectra dispatch receipt TTL must be between 1 and 60 seconds")

    normalized_method = str(method).upper()
    normalized_path = "/" + str(path).strip().lstrip("/")
    if operation == "create_executor":
        if normalized_method != "POST" or normalized_path.rstrip("/") != "/executors":
            raise ValueError("Executor creation receipt must target POST /executors")
        config = request_body.get("executor_config")
        if not isinstance(config, dict):
            raise ValueError("Executor creation receipt requires executor_config")
        resolved_account = str(request_body.get("account_name") or "").strip()
        resolved_connector = str(config.get("connector_name") or "").strip().lower()
        resolved_pair = str(config.get("trading_pair") or "").strip()
        config_hash = canonical_sha256(config)
        if executor_id is not None:
            raise ValueError("Executor creation receipt cannot include executor_id")
    elif operation == "stop_executor":
        resolved_executor_id = str(executor_id or "").strip()
        if (
            normalized_method != "POST"
            or normalized_path != f"/executors/{resolved_executor_id}/stop"
            or not resolved_executor_id
        ):
            raise ValueError("Executor stop receipt must target its exact POST stop path")
        if not isinstance(request_body.get("keep_position"), bool):
            raise ValueError("Executor stop receipt requires a boolean keep_position body")
        resolved_account = str(account_name or "").strip()
        resolved_connector = str(connector_name or "").strip().lower()
        resolved_pair = str(trading_pair or "").strip()
        config_hash = canonical_sha256({"executor_id": resolved_executor_id})
    else:
        raise ValueError("Unsupported Spectra dispatch operation")

    if not resolved_account or not resolved_connector or not resolved_pair:
        raise ValueError("Spectra dispatch receipt scope must be exact and non-empty")

    canonical_body = _clean(request_body)
    body_hash = canonical_sha256(canonical_body)
    receipt_id = uuid.uuid4().hex
    issued_at = float(now)
    expires_at = issued_at + ttl_seconds
    payload = {
        "version": 1,
        "receipt_id": receipt_id,
        "proposal_id": proposal,
        "intent": normalized_intent,
        "account_name": resolved_account,
        "connector_name": resolved_connector,
        "trading_pair": resolved_pair,
        "operation": operation,
        "amount": str(amount),
        "config_sha256": config_hash,
        "body_sha256": body_hash,
        "method": normalized_method,
        "path": normalized_path.rstrip("/") or "/",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "policy_version": (
            str(policy_version or os.getenv("SPECTRA_DISPATCH_POLICY_VERSION", "spectra-policy-v1"))
        ),
    }
    if operation == "stop_executor":
        payload["executor_id"] = str(executor_id)
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
        token=token,
        receipt_id=receipt_id,
        expires_at=expires_at,
        body_sha256=body_hash,
    )
