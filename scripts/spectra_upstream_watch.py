#!/usr/bin/env python3
"""Read-only upstream discovery for the Spectra release manager.

The watcher records newly observed upstream releases/commits as a reviewable
candidate snapshot. It never changes the approved component pins, installs
software, triggers a deployment, or enables trading.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "release" / "spectra-release-manifest.json"
DEFAULT_CANDIDATE = ROOT / "release" / "upstream-candidates.json"
API = "https://api.github.com"


def fetch_json(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "spectra-finance-release-watch",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"upstream query failed for {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected GitHub response for {url}: expected an object")
    return value


def collect_upstream(fetcher: Callable[[str], dict[str, Any]] = fetch_json) -> dict[str, Any]:
    client = fetcher(f"{API}/repos/hummingbot/hummingbot/releases/latest")
    gateway = fetcher(f"{API}/repos/hummingbot/gateway/releases/latest")
    condor = fetcher(f"{API}/repos/hummingbot/condor/commits/main")
    api = fetcher(f"{API}/repos/andredepaulnash-cmd/spectra-hummingbot-api/commits/main")

    required = (
        (client, "tag_name", "Hummingbot Client latest release"),
        (gateway, "tag_name", "Gateway latest release"),
        (condor, "sha", "Condor main commit"),
        (api, "sha", "Spectra Hummingbot API main commit"),
    )
    for value, key, label in required:
        if not value.get(key):
            raise RuntimeError(f"{label} response did not include {key}")

    return {
        "hummingbot_client": {
            "repository": "hummingbot/hummingbot",
            "release": client["tag_name"],
            "published_at": client.get("published_at"),
        },
        "gateway": {
            "repository": "hummingbot/gateway",
            "release": gateway["tag_name"],
        },
        "condor": {
            "repository": "hummingbot/condor",
            "ref": "main",
            "revision": condor["sha"],
        },
        "hummingbot_api": {
            "repository": "andredepaulnash-cmd/spectra-hummingbot-api",
            "ref": "main",
            "revision": api["sha"],
        },
    }


def build_candidate(
    manifest: dict[str, Any],
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
    prior_candidate: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    observed = collect_upstream(fetcher)
    baseline = (
        prior_candidate.get("upstream")
        if isinstance(prior_candidate, dict) and isinstance(prior_candidate.get("upstream"), dict)
        else manifest.get("upstream_snapshot", {})
    )
    changed = sorted(
        name for name, value in observed.items()
        if baseline.get(name) != value
    )
    if not changed:
        return None
    return {
        "snapshot_schema": 1,
        "baseline": "prior-candidate" if prior_candidate else "release-manifest",
        "upstream": observed,
        "changed_components": changed,
        "promotion_note": (
            "Discovery only. These upstream refs are candidates, not approved pins. "
            "Compatibility CI and human review are required before updating the release manifest."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--candidate-output", type=Path, default=DEFAULT_CANDIDATE)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        prior = None
        if args.candidate_output.exists():
            prior = json.loads(args.candidate_output.read_text(encoding="utf-8"))
        candidate = build_candidate(manifest, prior_candidate=prior)
        if candidate is None:
            print("Upstream snapshot unchanged; no candidate update is needed.")
            return 0
        args.candidate_output.parent.mkdir(parents=True, exist_ok=True)
        args.candidate_output.write_text(
            json.dumps(candidate, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print("Upstream changes detected: " + ", ".join(candidate["changed_components"]))
        print(f"Wrote review-only candidate snapshot: {args.candidate_output}")
        return 0
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
