"""Read the authenticated Spectra runtime boundary from a Hummingbot API."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def fetch_spectra_runtime(client, **_kw) -> dict:
    """Fetch the paper/live mode status without exposing server credentials.

    HummingbotAPIClient owns the authenticated aiohttp session. Its public
    routers do not yet include the Spectra extension route, so this narrow
    adapter uses that already-authenticated session and the client's base URL.
    A 404 is an explicit unsupported/legacy state, never paper-ready.
    """
    session = getattr(client, "_session", None)
    base_url = getattr(client, "base_url", None)
    if session is None or session.closed or not base_url:
        raise RuntimeError("Hummingbot API client is not initialized")

    url = f"{str(base_url).rstrip('/')}/spectra/runtime"
    async with session.get(url, timeout=5) as response:
        if response.status == 404:
            return {
                "product": "Spectra Finance",
                "mode": "UNSUPPORTED",
                "paper_only": False,
                "live_dispatch_available": False,
                "paper_connector_count": 0,
                "error": "Hummingbot API does not expose the Spectra runtime boundary",
            }
        response.raise_for_status()
        payload = await response.json()
        if not isinstance(payload, dict):
            logger.warning("Unexpected Spectra runtime response from %s", base_url)
            return {
                "mode": "UNAVAILABLE",
                "paper_only": False,
                "live_dispatch_available": False,
                "paper_connector_count": 0,
                "error": "Invalid Spectra runtime response",
            }
        return payload
