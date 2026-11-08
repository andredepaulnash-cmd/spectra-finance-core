#!/usr/bin/env python3
"""Generate a local Ed25519 Core/API dispatch key pair without printing secrets."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _set_env(path: Path, values: dict[str, str], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated = []
    seen = set()
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            if key not in seen:
                updated.append(f"{key}={values[key]}")
                seen.add(key)
            continue
        updated.append(line)
    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={value}")
    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    os.chmod(path, mode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-env", type=Path, default=Path(".env"))
    parser.add_argument(
        "--api-env",
        type=Path,
        default=Path("../spectra-hummingbot-api/.env"),
    )
    args = parser.parse_args()

    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    _set_env(
        args.core_env,
        {
            "SPECTRA_DISPATCH_PRIVATE_KEY": _encode(private_raw),
            "SPECTRA_DISPATCH_POLICY_VERSION": "spectra-policy-v1",
            "SPECTRA_PAPER_ONLY": "true",
        },
        mode=0o600,
    )
    _set_env(
        args.api_env,
        {
            "SPECTRA_DISPATCH_PUBLIC_KEY": _encode(public_raw),
            "SPECTRA_REQUIRE_CORE_RECEIPTS": "true",
            "SPECTRA_DISPATCH_POLICY_VERSION": "spectra-policy-v1",
            "SPECTRA_PAPER_ONLY": "true",
        },
        mode=0o600,
    )
    print(f"Created/updated Core signing configuration: {args.core_env}")
    print(f"Created/updated API verification configuration: {args.api_env}")
    print("Private key was not printed; keep the Core .env file private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
