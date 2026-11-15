#!/usr/bin/env python3
"""Read-only upstream version discovery for the Spectra Release Manager.

This command discovers candidates. It never changes the manifest, source code,
installed services, credentials, databases, or approved versions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "components.json"
API_ROOT = "https://api.github.com"


def github_get(path: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "spectra-release-manager",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(API_ROOT + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise RuntimeError(f"GitHub API request failed for {path}: {exc}") from exc


def discover(component: dict[str, Any]) -> dict[str, Any]:
    tracking = component["tracking"]
    result: dict[str, Any] = {
        "id": component["id"],
        "name": component["name"],
        "repository": component["repository"],
        "tracking": tracking,
        "approved_ref": component.get("approved_ref"),
        "candidate_ref": None,
        "candidate_url": None,
        "published_at": None,
        "status": "not-monitored",
    }
    if tracking == "internal-release":
        result["status"] = "spectra-release-managed"
        return result

    repo = component["repository"]
    if tracking == "latest-release":
        release = github_get(f"/repos/{repo}/releases/latest")
        candidate = release.get("tag_name")
        result["candidate_url"] = release.get("html_url")
        result["published_at"] = release.get("published_at")
    elif tracking == "branch":
        ref = component["tracking_ref"]
        branch = github_get(f"/repos/{repo}/branches/{ref}")
        candidate = branch["commit"]["sha"]
        result["candidate_url"] = branch["commit"].get("html_url")
        result["published_at"] = branch["commit"].get("commit", {}).get("committer", {}).get("date")
    else:
        raise RuntimeError(f"Unsupported tracking mode {tracking!r} for {component['id']}")

    result["candidate_ref"] = candidate
    approved = result["approved_ref"]
    result["status"] = "unapproved-candidate" if not approved else (
        "update-available" if candidate != approved else "approved-current"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="release-candidates.json")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("components"), list):
        raise RuntimeError("Unsupported or invalid release manifest schema")

    observed_at = datetime.now(timezone.utc).isoformat()
    results = [discover(component) for component in manifest["components"]]
    report = {
        "platform": manifest["platform"],
        "observed_at": observed_at,
        "policy": manifest["policy"],
        "components": results,
        "safety_note": (
            "Discovery only: no versions were approved, no files or services were "
            "changed, and no update or deployment was performed."
        ),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"spectra release check: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
