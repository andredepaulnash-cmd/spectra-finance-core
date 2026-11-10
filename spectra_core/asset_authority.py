"""Fail-closed Governor-approved asset authority for Spectra paper entries.

The mean-reversion scanner may rank research candidates without an allowlist.
Capital-affecting BUY-side intents require an explicit, source-attributed
approved_assets.json in the isolated paper state (or an operator-selected file).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .runtime import spectra_paper_only_enabled, spectra_state_dir


_PAIR = re.compile(r"^[A-Z0-9]{2,20}-USD$")


class AssetAuthorityError(ValueError):
    """A paper entry is not covered by a valid Governor-approved asset list."""


@dataclass(frozen=True)
class ApprovedKrakenAssets:
    configured: bool
    source: str | None
    pairs: frozenset[str]
    path: Path
    reason: str


def canonical_kraken_usd_pair(value: str) -> str:
    pair = str(value).strip().upper()
    if pair.startswith("XBT-"):
        pair = "BTC-" + pair.split("-", 1)[1]
    if not _PAIR.fullmatch(pair):
        raise AssetAuthorityError(
            f"approved asset must be a canonical Kraken USD spot pair: {value!r}"
        )
    return pair


def approved_assets_path() -> Path:
    override = (os.getenv("SPECTRA_APPROVED_ASSETS_FILE") or "").strip()
    if not override:
        return spectra_state_dir() / "approved_assets.json"
    path = Path(override).expanduser()
    if not path.is_absolute():
        path = spectra_state_dir() / path
    return path


def load_approved_kraken_assets() -> ApprovedKrakenAssets:
    """Load the exact source-attributed allowlist; absence never means allow-all."""
    path = approved_assets_path()
    if not path.is_file():
        return ApprovedKrakenAssets(
            configured=False,
            source=None,
            pairs=frozenset(),
            path=path,
            reason="governor_approved_asset_list_missing",
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AssetAuthorityError(
            f"Governor-approved asset list is unreadable: {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise AssetAuthorityError("approved_assets.json must contain a JSON object")
    if raw.get("schema_version") != 1:
        raise AssetAuthorityError("approved_assets.json schema_version must be 1")
    if raw.get("connector") != "kraken_paper_trade":
        raise AssetAuthorityError(
            "approved_assets.json connector must be kraken_paper_trade"
        )
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise AssetAuthorityError(
            "approved_assets.json must identify the Governor asset-list source"
        )
    values = raw.get("approved_pairs")
    if not isinstance(values, list):
        raise AssetAuthorityError("approved_assets.json approved_pairs must be a list")

    pairs: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise AssetAuthorityError("every approved pair must be a string")
        pair = canonical_kraken_usd_pair(value)
        if pair in pairs:
            raise AssetAuthorityError(f"duplicate approved Kraken pair: {pair}")
        pairs.add(pair)

    return ApprovedKrakenAssets(
        configured=True,
        source=source.strip(),
        pairs=frozenset(pairs),
        path=path,
        reason="governor_approved_asset_list_loaded",
    )


def require_approved_kraken_pair(
    trading_pair: str,
    *,
    connector_name: str = "kraken_paper_trade",
) -> ApprovedKrakenAssets:
    """Require explicit Governor allowlist membership for paper BUY-side exposure."""
    if not spectra_paper_only_enabled():
        return ApprovedKrakenAssets(
            configured=True,
            source="non-paper execution policy",
            pairs=frozenset({canonical_kraken_usd_pair(trading_pair)}),
            path=Path("<not-applicable>"),
            reason="paper_asset_allowlist_not_applicable",
        )
    if connector_name.strip().lower() != "kraken_paper_trade":
        raise AssetAuthorityError(
            "Spectra paper asset authority only permits kraken_paper_trade"
        )
    pair = canonical_kraken_usd_pair(trading_pair)
    policy = load_approved_kraken_assets()
    if not policy.configured:
        raise AssetAuthorityError(
            "new paper exposure is blocked: Governor-approved Kraken asset list "
            f"is not configured ({policy.path})"
        )
    if pair not in policy.pairs:
        raise AssetAuthorityError(
            f"new paper exposure is blocked: {pair} is not in the "
            f"Governor-approved asset list from {policy.source!r}"
        )
    return policy
