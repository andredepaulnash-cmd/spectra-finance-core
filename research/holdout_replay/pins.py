"""Verify the immutable October 9 financial/source freeze before replay imports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FROZEN_PROTOCOL_SHA256 = "ef3148ec5b896aded5e812b71d687ea64eddc931c5e86533bfca6f0d08c37134"
FROZEN_COMMIT = "931d296775425fbea26140ecf2a654767b691874"
NATIVE_STRATEGY_SHA256 = "9260c013c5c85088f0098d76d39f754b456d581d93e8dda98c5bbf0711d6b838"
ARMS = ("OFF", "SHARED_RAW", "SPLIT_RAW", "SHARED_QUALITY", "SPLIT_QUALITY")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verified_json(path: Path, expected: str):
    payload = Path(path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"Immutable source hash mismatch: {path}")
    return json.loads(payload)


def verify_freeze(repo_root: Path):
    root = Path(repo_root).resolve()
    protocol = verified_json(root / "research/holdout/frozen-protocol.json",
                             FROZEN_PROTOCOL_SHA256)
    verified = {}
    payloads = {}
    for entry in [*protocol["candidates"], protocol["collector"]]:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Frozen candidate escaped repository root")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError(f"Immutable source hash mismatch: {entry['path']}")
        verified[entry["name"]] = dict(entry)
        payloads[entry["name"]] = payload
    financial = json.loads(payloads["financial-spec"])
    universe = json.loads(payloads["fixed-universe"])
    manifest = json.loads(payloads["native-manifest"])
    if tuple(financial["comparison_arms"]) != ARMS:
        raise ValueError("Frozen comparison arm identity changed")
    if financial["native_strategy_sha256"] != NATIVE_STRATEGY_SHA256:
        raise ValueError("Frozen strategy identity changed")
    if manifest["native_generated_strategy_sha256"] != NATIVE_STRATEGY_SHA256:
        raise ValueError("Native manifest strategy identity changed")
    if len(universe["pairs"]) != 619 or len(set(universe["pairs"])) != 619:
        raise ValueError("Frozen universe identity is incomplete or duplicated")
    return dict(protocol=protocol, financial=financial, universe=universe,
                native_manifest=manifest, verified_candidates=verified,
                repo_root=root, verified_payloads=payloads)
