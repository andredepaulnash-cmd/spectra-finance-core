"""Shared Core-signed transport for protected Hummingbot API mutations."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from time import time
from typing import Any

from spectra_core.dispatch_receipt import (
    issue_executor_dispatch_receipt,
    validate_dispatch_signing_key,
)


def _session_headers(client: Any):
    session = getattr(client, "_session", None)
    headers = getattr(session, "headers", None)
    if headers is None:
        raise RuntimeError(
            "Spectra requires the authenticated Hummingbot API session to carry its dispatch receipt"
        )
    return headers


def validate_dispatch_transport(client: Any) -> None:
    """Fail before capital/ownership commits if signing or header transport is absent."""
    validate_dispatch_signing_key()
    _session_headers(client)


async def dispatch_with_receipt(
    client: Any,
    *,
    api_method: str,
    request_body: dict[str, Any],
    request_kwargs: dict[str, Any],
    proposal_id: str,
    intent: str,
    amount_quote: Decimal | str | int | float,
    operation: str = "create_executor",
    method: str = "POST",
    path: str = "/executors",
    executor_id: str | None = None,
    account_name: str | None = None,
    connector_name: str | None = None,
    trading_pair: str | None = None,
):
    """Sign and send one exact request, restoring shared session headers afterward."""
    validate_dispatch_transport(client)
    receipt = issue_executor_dispatch_receipt(
        request_body=request_body,
        proposal_id=proposal_id,
        intent=intent,
        amount_quote=amount_quote,
        now=time(),
        operation=operation,
        method=method,
        path=path,
        executor_id=executor_id,
        account_name=account_name,
        connector_name=connector_name,
        trading_pair=trading_pair,
    )
    lock = getattr(client, "_spectra_dispatch_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(client, "_spectra_dispatch_lock", lock)
    async with lock:
        headers = _session_headers(client)
        header_name = "X-Spectra-Dispatch-Receipt"
        previous = headers.get(header_name)
        headers[header_name] = receipt.token
        try:
            method_fn = getattr(client.executors, api_method, None)
            if method_fn is None or not callable(method_fn):
                raise RuntimeError(f"Unsupported Hummingbot API mutation method: {api_method}")
            result = await method_fn(**request_kwargs)
        finally:
            if previous is None:
                headers.pop(header_name, None)
            else:
                headers[header_name] = previous
    return result, receipt
