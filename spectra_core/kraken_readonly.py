"""Read-only Kraken private REST client.

This client intentionally exposes only account-query endpoints. It has no
order placement, cancellation, withdrawal, deposit, or transfer methods.
Credentials are read from the process environment and are never logged.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import aiohttp


BASE_URL = "https://api.kraken.com"
_PRIVATE_READ_ENDPOINTS = frozenset(
    {
        "Balance",
        "TradeBalance",
        "OpenOrders",
        "ClosedOrders",
        "QueryOrders",
        "TradesHistory",
        "QueryTrades",
        "Ledgers",
        "QueryLedgers",
    }
)


class KrakenReadOnlyError(RuntimeError):
    """A sanitized error from the read-only Kraken account client."""


class KrakenReadOnlyClient:
    """Minimal Kraken private API client restricted to read-only endpoints."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        session: Any | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        key = str(api_key or "").strip()
        secret = str(api_secret or "").strip()
        if not key or not secret:
            raise ValueError("Kraken read-only API credentials are required")
        try:
            decoded_secret = base64.b64decode(secret, validate=True)
        except Exception as exc:
            raise ValueError("Kraken API secret must be valid base64") from exc
        if not decoded_secret:
            raise ValueError("Kraken API secret must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._api_key = key
        self._api_secret = decoded_secret
        self._session = session
        self._timeout_seconds = float(timeout_seconds)
        self._last_nonce = 0
        self._nonce_lock = asyncio.Lock()

    @classmethod
    def from_env(cls, **kwargs: Any) -> "KrakenReadOnlyClient":
        """Load credentials from KRAKEN_API_KEY and KRAKEN_API_SECRET."""
        return cls(
            os.getenv("KRAKEN_API_KEY", ""),
            os.getenv("KRAKEN_API_SECRET", ""),
            **kwargs,
        )

    def __repr__(self) -> str:
        return "KrakenReadOnlyClient(credentials=<redacted>)"

    async def _next_nonce(self) -> str:
        async with self._nonce_lock:
            candidate = time.time_ns() // 1_000
            self._last_nonce = max(candidate, self._last_nonce + 1)
            return str(self._last_nonce)

    def _signature(self, path: str, encoded_body: str, nonce: str) -> str:
        digest = hashlib.sha256((nonce + encoded_body).encode("utf-8")).digest()
        message = path.encode("utf-8") + digest
        signed = hmac.new(self._api_secret, message, hashlib.sha512).digest()
        return base64.b64encode(signed).decode("ascii")

    async def query(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call one allowlisted private read endpoint; mutation routes are rejected."""
        if endpoint not in _PRIVATE_READ_ENDPOINTS:
            raise ValueError("Kraken endpoint is not in the read-only allowlist")

        nonce = await self._next_nonce()
        values = dict(params or {})
        values["nonce"] = nonce
        encoded_body = urlencode(values, doseq=True)
        path = f"/0/private/{endpoint}"
        headers = {
            "API-Key": self._api_key,
            "API-Sign": self._signature(path, encoded_body, nonce),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        owned_session = self._session is None
        session = self._session or aiohttp.ClientSession(timeout=timeout)
        try:
            async with session.post(
                f"{BASE_URL}{path}",
                data=encoded_body,
                headers=headers,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except Exception as exc:
            # Do not include request headers, body, key, or secret in errors.
            raise KrakenReadOnlyError(
                f"Kraken read-only request failed ({endpoint}): {type(exc).__name__}"
            ) from None
        finally:
            if owned_session:
                await session.close()

        if not isinstance(payload, dict):
            raise KrakenReadOnlyError("Kraken returned an invalid response")
        errors = payload.get("error")
        if errors:
            safe_errors = ", ".join(str(item)[:160] for item in errors[:5])
            raise KrakenReadOnlyError(f"Kraken rejected read request: {safe_errors}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise KrakenReadOnlyError("Kraken response did not contain a result object")
        return result

    async def balance(self) -> dict[str, Any]:
        return await self.query("Balance")

    async def trade_balance(self, asset: str = "ZUSD") -> dict[str, Any]:
        return await self.query("TradeBalance", {"asset": asset})

    async def open_orders(self, *, trades: bool = False) -> dict[str, Any]:
        return await self.query("OpenOrders", {"trades": "true" if trades else "false"})

    async def closed_orders(self, *, trades: bool = False, **filters: Any) -> dict[str, Any]:
        return await self.query(
            "ClosedOrders", {"trades": "true" if trades else "false", **filters}
        )

    async def trades_history(self, **filters: Any) -> dict[str, Any]:
        return await self.query("TradesHistory", filters)

    async def ledgers(self, **filters: Any) -> dict[str, Any]:
        return await self.query("Ledgers", filters)
