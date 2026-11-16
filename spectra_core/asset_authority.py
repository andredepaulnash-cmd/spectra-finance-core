"""Fail-closed Governor-approved asset authority for Spectra paper entries.

An operator-provided approved_assets.json always wins. When no operator file
exists, Spectra may use a short-lived discovered_assets.json produced by the
current research-only market scan. The discovery file is not capital authority:
the Capital Governor still approves every reservation and execution.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime import spectra_paper_only_enabled, spectra_state_dir


_PAIR = re.compile(r"^[A-Z0-9]{2,20}-USD$")
AUTO_ASSET_MAX_AGE_SECONDS = 300.0
AUTO_ASSET_SOURCE = "Spectra automatic Kraken market discovery"
AUTO_ASSET_POLICY = "kraken-usd-mean-reversion-v1"


class AssetAuthorityError(ValueError):
    """A paper entry is not covered by a valid Governor-approved asset list."""


@dataclass(frozen=True)
class ApprovedKrakenAssets:
    configured: bool
    source: str | None
    pairs: frozenset[str]
    path: Path
    reason: str
    automatic: bool = False
    generated_at: float | None = None


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


def discovered_assets_path() -> Path:
    return spectra_state_dir() / "discovered_assets.json"


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise AssetAuthorityError(f"{field} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AssetAuthorityError(f"{field} must be finite numeric data") from exc
    if not math.isfinite(result):
        raise AssetAuthorityError(f"{field} must be finite numeric data")
    return result


def _load_policy_file(
    path: Path,
    *,
    automatic: bool,
    now: float,
) -> ApprovedKrakenAssets:
    if path.is_symlink():
        raise AssetAuthorityError("Governor-approved asset list must not be a symlink")
    if not path.is_file():
        return ApprovedKrakenAssets(
            configured=False,
            source=None,
            pairs=frozenset(),
            path=path,
            reason="governor_approved_asset_list_missing",
            automatic=automatic,
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
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 256:
        raise AssetAuthorityError(
            "approved_assets.json must identify the Governor asset-list source"
        )

    generated_at = None
    if automatic:
        generated_at = _finite_number(raw.get("generated_at"), "generated_at")
        age = now - generated_at
        if age < -5.0 or age > AUTO_ASSET_MAX_AGE_SECONDS:
            return ApprovedKrakenAssets(
                configured=False,
                source=source.strip(),
                pairs=frozenset(),
                path=path,
                reason="automatic_asset_list_stale",
                automatic=True,
                generated_at=generated_at,
            )
        if raw.get("policy") != AUTO_ASSET_POLICY:
            raise AssetAuthorityError("discovered_assets.json policy is not recognized")

    values = raw.get("approved_pairs")
    if not isinstance(values, list) or len(values) > 150:
        raise AssetAuthorityError(
            "approved_assets.json approved_pairs must be a list of at most 150 pairs"
        )

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
        reason=(
            "automatic_discovery_asset_list_loaded"
            if automatic
            else "governor_approved_asset_list_loaded"
        ),
        automatic=automatic,
        generated_at=generated_at,
    )


def load_approved_kraken_assets(*, now: float | None = None) -> ApprovedKrakenAssets:
    """Load manual approval first, otherwise a fresh automatic discovery list.

    An explicitly configured operator file is authoritative: if it is missing
    or invalid, automatic discovery cannot silently bypass it.
    """
    timestamp = time.time() if now is None else _finite_number(now, "now")
    manual_override = (os.getenv("SPECTRA_APPROVED_ASSETS_FILE") or "").strip()
    manual_path = approved_assets_path()
    if manual_override or manual_path.is_file() or manual_path.is_symlink():
        return _load_policy_file(manual_path, automatic=False, now=timestamp)

    auto_enabled = os.getenv("SPECTRA_AUTO_ASSET_SELECTION", "true").strip().lower()
    if auto_enabled not in {"1", "true", "yes"}:
        return ApprovedKrakenAssets(
            configured=False,
            source=None,
            pairs=frozenset(),
            path=manual_path,
            reason="automatic_asset_selection_disabled",
        )
    return _load_policy_file(
        discovered_assets_path(),
        automatic=True,
        now=timestamp,
    )


def write_discovered_kraken_assets(
    scan_result: Mapping[str, Any],
    *,
    now: float | None = None,
    max_assets: int = 100,
) -> ApprovedKrakenAssets:
    """Persist a fresh, research-only scanner shortlist for automatic paper entry."""
    if not isinstance(scan_result, Mapping):
        raise AssetAuthorityError("market discovery result must be a mapping")
    if scan_result.get("paper_only") is not True or scan_result.get("orders_created") != 0:
        raise AssetAuthorityError("automatic asset selection requires a research-only scan")
    if scan_result.get("source") != "Kraken spot market data via Hummingbot API":
        raise AssetAuthorityError("automatic asset selection requires the configured Kraken scan")
    if isinstance(max_assets, bool) or not isinstance(max_assets, int) or not 1 <= max_assets <= 150:
        raise AssetAuthorityError("max_assets must be an integer between 1 and 150")

    timestamp = time.time() if now is None else _finite_number(now, "now")
    scan_as_of = _finite_number(scan_result.get("as_of"), "scanner as_of")
    age = timestamp - scan_as_of
    if age < -5.0 or age > AUTO_ASSET_MAX_AGE_SECONDS:
        raise AssetAuthorityError("market discovery scan is stale or has a future timestamp")

    lists = scan_result.get("lists")
    candidates = lists.get("mean_reversion_candidates") if isinstance(lists, Mapping) else None
    if not isinstance(candidates, list):
        raise AssetAuthorityError("market discovery has no mean-reversion candidate list")

    pairs: list[str] = []
    seen: set[str] = set()
    for row in candidates:
        if not isinstance(row, Mapping) or row.get("eligible") is not True:
            continue
        if row.get("capital_authorized") is not False:
            continue
        if str(row.get("quote_asset") or "USD").strip().upper() != "USD":
            continue
        price = _finite_number(row.get("current_price"), "candidate current_price")
        volume = _finite_number(row.get("volume_24h_usd"), "candidate volume_24h_usd")
        _finite_number(row.get("selection_score"), "candidate selection_score")
        if price <= 0 or volume <= 0:
            continue
        pair = canonical_kraken_usd_pair(str(row.get("trading_pair") or ""))
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
        if len(pairs) >= max_assets:
            break

    path = discovered_assets_path()
    if path.is_symlink():
        raise AssetAuthorityError("automatic discovery asset list must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source": AUTO_ASSET_SOURCE,
        "connector": "kraken_paper_trade",
        "policy": AUTO_ASSET_POLICY,
        "generated_at": timestamp,
        "scan_as_of": scan_as_of,
        "max_age_seconds": AUTO_ASSET_MAX_AGE_SECONDS,
        "candidate_count": len(pairs),
        "approved_pairs": pairs,
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".discovered-assets-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return _load_policy_file(path, automatic=True, now=timestamp)


def require_approved_kraken_pair(
    trading_pair: str,
    *,
    connector_name: str = "kraken_paper_trade",
) -> ApprovedKrakenAssets:
    """Require explicit or fresh auto-discovery approval for paper BUY exposure."""
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
            f"is not configured or is stale ({policy.path})"
        )
    if pair not in policy.pairs:
        raise AssetAuthorityError(
            f"new paper exposure is blocked: {pair} is not in the "
            f"Governor-approved asset list from {policy.source!r}"
        )
    return policy
