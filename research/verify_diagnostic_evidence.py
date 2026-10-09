"""Verify a pinned diagnostic ZIP without extracting or executing its source.

This utility establishes artifact identity and archive integrity. It does not
rerun a strategy, establish source authenticity, or authorize production use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile


MANIFEST_NAME = "deliverables/Diagnostic_Evidence_Manifest.json"
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(bundle: Path, expected_sha256: str) -> dict:
    """Fail on a changed pin, duplicate name, missing payload, or bad bytes."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("A lowercase SHA-256 pin is required")
    observed = _digest(bundle)
    if observed != expected_sha256:
        raise ValueError("Diagnostic bundle differs from its frozen SHA-256 pin")

    with zipfile.ZipFile(bundle) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive member names")
        if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Diagnostic payload exceeds the verification budget")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Nonportable archive member path")
        if MANIFEST_NAME not in names:
            raise ValueError("Missing diagnostic manifest")

        manifest = json.loads(archive.read(MANIFEST_NAME))
        payloads = manifest.get("payloads")
        if not isinstance(payloads, dict) or not payloads:
            raise ValueError("Diagnostic manifest must name its payloads")
        if set(names) != set(payloads) | {MANIFEST_NAME}:
            raise ValueError("Archive members and declared payloads differ")
        for name, spec in payloads.items():
            data = archive.read(name)
            size = spec.get("bytes")
            if type(size) is not int or size < 0 or len(data) != size:
                raise ValueError(f"Payload byte count differs: {name}")
            if hashlib.sha256(data).hexdigest() != spec.get("sha256"):
                raise ValueError(f"Payload digest differs: {name}")

    return {
        "status": "PASS",
        "bundle_sha256": observed,
        "payload_count": len(payloads),
        "scope": "Pinned artifact identity and complete payload integrity only",
        "historical_strategy_rerun": False,
        "production_promotion": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(verify_bundle(args.bundle, args.sha256), indent=2))


if __name__ == "__main__":
    main()
