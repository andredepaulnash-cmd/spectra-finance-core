"""Ed25519 receipts binding every Hummingbot paper order to a Core authorization."""
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

from .safety import PaperSafetySettings


@dataclass(frozen=True, slots=True)
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


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _private_key() -> Ed25519PrivateKey:
    PaperSafetySettings.from_environment(os.environ).assert_paper_mode()
    encoded = os.getenv("SPECTRA_DISPATCH_PRIVATE_KEY", "").strip()
    if not encoded:
        raise RuntimeError("Core dispatch signing key is not configured")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception as exc:
        raise RuntimeError("Core dispatch signing key is malformed") from exc
    if len(raw) != 32:
        raise RuntimeError("Core dispatch signing key must contain 32 Ed25519 private-key bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


def validate_dispatch_signing_key() -> None:
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
    operation: str = "place_trade",
    method: str = "POST",
    path: str = "/trading/orders",
    account_name: str | None = None,
    connector_name: str | None = None,
    trading_pair: str | None = None,
    executor_id: str | None = None,
) -> DispatchReceipt:
    """Issue one exact-scope, short-lived paper order or cancellation receipt.

    Callers must pass the authorization ID returned by CapitalGovernor as
    proposal_id. This function does not reserve or authorize capital itself.
    """
    _private_key()
    if not isinstance(request_body, dict):
        raise ValueError("dispatch request body must be an object")
    proposal = str(proposal_id or "").strip()
    normalized_intent = str(intent or "").strip().upper()
    amount = Decimal(str(amount_quote))
    if not proposal or not uuid.UUID(proposal).hex if False else False:
        pass
    if not proposal:
        raise ValueError("CapitalGovernor authorization ID is required")
    if normalized_intent not in {"ENTRY", "RECOVERY", "EXIT", "CANCEL"}:
        raise ValueError("unsupported dispatch intent")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("authorized quote amount must be positive")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 60:
        raise ValueError("receipt TTL must be between 1 and 60 seconds")

    method = str(method).upper()
    path = "/" + str(path).strip().lstrip("/")
    connector = "kraken_paper_trade"
    if operation == "place_trade":
        if method != "POST" or path != "/trading/orders":
            raise ValueError("paper trade receipt must target POST /trading/orders")
        required = ("account_name", "connector_name", "trading_pair", "trade_type", "amount", "order_type", "position_action")
        if any(request_body.get(field) in (None, "") for field in required):
            raise ValueError("paper trade body is missing required fields")
        resolved_account = str(request_body["account_name"]).strip()
        resolved_connector = str(request_body["connector_name"]).strip().lower()
        resolved_pair = str(request_body["trading_pair"]).strip()
        if resolved_connector != connector:
            raise ValueError("Alpha 2.0 dispatch is restricted to kraken_paper_trade")
        if not resolved_pair.endswith("-USDT"):
            raise ValueError("Alpha 2.0 paper orders must use USDT-quoted markets")
        if request_body["trade_type"] not in {"BUY", "SELL"}:
            raise ValueError("unsupported paper order side")
        if request_body["order_type"] not in {"LIMIT", "MARKET", "LIMIT_MAKER"}:
            raise ValueError("unsupported paper order type")
        if request_body["order_type"] in {"LIMIT", "LIMIT_MAKER"} and request_body.get("price") is None:
            raise ValueError("paper limit order requires a price")
        base_amount = Decimal(str(request_body["amount"]))
        if not base_amount.is_finite() or base_amount <= 0:
            raise ValueError("paper order base amount must be positive")
        scope = {
            key: request_body.get(key)
            for key in ("account_name","connector_name","trading_pair","trade_type","amount","order_type","price","position_action")
        }
        config_hash = canonical_sha256(scope)
    elif operation == "cancel_order":
        parts = path.strip("/").split("/")
        if method != "POST" or len(parts) != 6 or parts[0] != "trading" or parts[3] != "orders" or parts[5] != "cancel":
            raise ValueError("cancel receipt must target an exact paper order cancel path")
        resolved_account, resolved_connector, client_order_id = parts[1], parts[2], parts[4]
        if resolved_connector != connector or not client_order_id:
            raise ValueError("only Kraken paper orders may be cancelled")
        if request_body:
            raise ValueError("cancel order body must be empty")
        config_hash = canonical_sha256({
            "account_name": resolved_account,
            "connector_name": resolved_connector,
            "client_order_id": client_order_id,
        })
    else:
        raise ValueError("unsupported Alpha 2.0 dispatch operation")

    if account_name is not None and account_name != resolved_account:
        raise ValueError("receipt account does not match authorized account")
    if connector_name is not None and connector_name != resolved_connector:
        raise ValueError("receipt connector does not match authorized connector")
    if trading_pair is not None and trading_pair != resolved_pair if operation == "place_trade" else False:
        raise ValueError("receipt market does not match authorized market")

    body_hash = canonical_sha256(request_body)
    issued_at = float(now)
    expires_at = issued_at + ttl_seconds
    policy = str(policy_version or os.getenv("SPECTRA_DISPATCH_POLICY_VERSION", "")).strip()
    if not policy:
        raise ValueError("SPECTRA_DISPATCH_POLICY_VERSION is required")
    payload = {
        "version": 1,
        "receipt_id": uuid.uuid4().hex,
        "proposal_id": proposal,
        "intent": normalized_intent,
        "account_name": resolved_account,
        "connector_name": resolved_connector,
        "trading_pair": resolved_pair,
        "operation": operation,
        "amount": str(amount),
        "config_sha256": config_hash,
        "body_sha256": body_hash,
        "method": method,
        "path": path,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "policy_version": policy,
    }
    if operation == "cancel_order":
        payload["config_sha256"] = config_hash
    payload_bytes = canonical_json(payload)
    signature = _private_key().sign(payload_bytes)
    return DispatchReceipt(
        token=_encode(payload_bytes) + "." + _encode(signature),
        receipt_id=payload["receipt_id"],
        expires_at=expires_at,
        body_sha256=body_hash,
    )
