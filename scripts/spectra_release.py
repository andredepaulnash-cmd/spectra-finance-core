#!/usr/bin/env python3
"""Validate and inspect the Spectra Finance release-candidate manifest.

This first release-manager slice is deliberately read-only. It validates the
component inventory and reports the promotion blockers; it does not fetch,
install, update, restart, or alter any trading component.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "release" / "spectra-release-manifest.json"
AUTHORITY = (
    "Condor may think. Hummingbot may execute. "
    "Only Spectra Core may authorize capital."
)
REQUIRED_COMPONENTS = {
    "spectra_core",
    "condor",
    "hummingbot_api",
    "hummingbot_client",
    "gateway",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _secret_like_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if re.search(r"(password|secret|api[_-]?key|access[_-]?token)", str(key), re.I):
                found.append(path)
            found.extend(_secret_like_keys(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_secret_like_keys(child, f"{prefix}[{index}]"))
    return found


def validate_document(document: Any, require_approved: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["manifest root must be a JSON object"]
    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if document.get("manifest_kind") != "release-candidate-inventory":
        errors.append("manifest_kind must be release-candidate-inventory")
    if document.get("release_status") not in {"draft", "candidate", "approved"}:
        errors.append("release_status must be draft, candidate, or approved")
    if document.get("authority_invariant") != AUTHORITY:
        errors.append("authority_invariant does not match the capital-authority contract")

    components = document.get("components")
    if not isinstance(components, dict):
        errors.append("components must be an object")
        components = {}
    missing = sorted(REQUIRED_COMPONENTS - set(components))
    if missing:
        errors.append("missing required components: " + ", ".join(missing))

    for component_id, component in components.items():
        if not isinstance(component, dict):
            errors.append(f"components.{component_id} must be an object")
            continue
        for field in ("kind", "repository", "runtime_status", "update_policy"):
            if not component.get(field):
                errors.append(f"components.{component_id}.{field} is required")
        revision = component.get("revision") or component.get("upstream_revision")
        if revision and revision not in {"SELF", "same-as-spectra-core-release"}:
            if not SHA_RE.fullmatch(str(revision)):
                errors.append(f"components.{component_id} revision must be a full 40-character commit SHA")
        if component.get("enabled") is True and component.get("runtime_status", "").startswith("not-verified"):
            errors.append(f"components.{component_id} cannot be enabled while runtime status is not verified")

    deployment = document.get("deployment")
    if not isinstance(deployment, dict):
        errors.append("deployment must be an object")
        deployment = {}
    if deployment.get("paper_only_default") is not True:
        errors.append("paper_only_default must remain true")
    if deployment.get("live_execution_enabled") is not False:
        errors.append("live_execution_enabled must remain false")
    if deployment.get("capital_authority") != "spectra_core":
        errors.append("capital_authority must remain spectra_core")
    if not isinstance(deployment.get("blockers"), list):
        errors.append("deployment.blockers must be an array")

    secret_keys = _secret_like_keys(document)
    if secret_keys:
        errors.append("manifest must never contain credential fields: " + ", ".join(secret_keys))

    if document.get("release_status") == "approved":
        if deployment.get("ready_for_installable_release") is not True:
            errors.append("an approved release must be marked ready_for_installable_release")
        if deployment.get("blockers"):
            errors.append("an approved release cannot retain promotion blockers")
    if require_approved and document.get("release_status") != "approved":
        errors.append("--require-approved requested, but manifest is not approved")
    return errors


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "status", "plan"), nargs="?", default="validate")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--require-approved", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors = validate_document(manifest, require_approved=args.require_approved)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"Manifest valid: {args.manifest}")
        print(f"Release status: {manifest['release_status']}")
        print("Live execution: disabled")
        print(f"Promotion blockers: {len(manifest['deployment'].get('blockers', []))}")
    elif args.command == "status":
        for name, component in manifest["components"].items():
            print(f"{name}: {component['runtime_status']} ({component['update_policy']})")
    elif args.command == "plan":
        print("Read-only update plan; no component will be changed:")
        for name, component in manifest["components"].items():
            print(f"- {name}: {component['update_policy']}")
        for blocker in manifest["deployment"].get("blockers", []):
            print(f"BLOCKER: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
