#!/usr/bin/env python3
"""Generate the internal Core/API Ed25519 receipt keypair (not exchange credentials)."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def update_env(path: Path, values: dict[str, str], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output = []
    seen = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            if key not in seen:
                output.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.chmod(path, mode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-env", type=Path, default=Path(".env"))
    parser.add_argument("--api-env", type=Path, default=Path("../spectra-hummingbot-api/.env"))
    args = parser.parse_args()

    def read_env(path: Path) -> dict[str,str]:
        values={}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key,value=line.split("=",1)
                    values[key.strip()]=value.strip()
        return values

    core_values=read_env(args.core_env)
    api_values=read_env(args.api_env)
    existing_private=core_values.get("SPECTRA_DISPATCH_PRIVATE_KEY")
    existing_public=api_values.get("SPECTRA_DISPATCH_PUBLIC_KEY")
    if bool(existing_private) != bool(existing_public):
        raise SystemExit("Core/API dispatch keys are incomplete; refusing to rotate or overwrite a one-sided keypair.")
    if existing_private and existing_public:
        try:
            private_raw=base64.urlsafe_b64decode(existing_private+"="*((4-len(existing_private)%4)%4))
            public_raw=base64.urlsafe_b64decode(existing_public+"="*((4-len(existing_public)%4)%4))
            private=Ed25519PrivateKey.from_private_bytes(private_raw)
            derived_public=private.public_key().public_bytes(
                serialization.Encoding.Raw,serialization.PublicFormat.Raw
            )
        except Exception as exc:
            raise SystemExit(f"Existing Alpha 2.0 dispatch keypair is invalid: {exc}") from exc
        if derived_public!=public_raw:
            raise SystemExit("Existing Core private key does not match API public key; refusing automatic rotation.")
    else:
        private=Ed25519PrivateKey.generate()
        private_raw=private.private_bytes(
            serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()
        )
        public_raw=private.public_key().public_bytes(
            serialization.Encoding.Raw,serialization.PublicFormat.Raw
        )
    policy = "spectra-alpha2-v1"
    update_env(args.api_env, {
        "SPECTRA_PAPER_ONLY": "true",
        "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
        "SPECTRA_PAPER_CONNECTORS": "kraken_paper_trade",
        "SPECTRA_REQUIRE_CORE_RECEIPTS": "true",
        "SPECTRA_DISPATCH_POLICY_VERSION": policy,
        "SPECTRA_DISPATCH_PUBLIC_KEY": encode(public_raw),
        "SPECTRA_DISPATCH_RECEIPT_DB": "bots/data/spectra_dispatch_receipts.sqlite3",
    })
    api_values = {}
    for line in args.api_env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key,value=line.split("=",1)
            api_values[key.strip()]=value.strip()
    if not api_values.get("USERNAME") or not api_values.get("PASSWORD"):
        raise SystemExit("API .env must contain generated USERNAME and PASSWORD for internal service authentication.")
    update_env(args.core_env, {
        "SPECTRA_PAPER_ONLY": "true",
        "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
        "SPECTRA_PAPER_CONNECTORS": "kraken_paper_trade",
        "SPECTRA_DISPATCH_POLICY_VERSION": policy,
        "SPECTRA_DISPATCH_PRIVATE_KEY": encode(private_raw),
        "SPECTRA_HB_API_URL": "http://hummingbot-api:8000",
        "SPECTRA_HB_API_USERNAME": api_values["USERNAME"],
        "SPECTRA_HB_API_PASSWORD": api_values["PASSWORD"],
        "SPECTRA_DB_PATH": "/data/spectra_alpha2.sqlite3",
    })
    print(f"Core signing configuration written to {args.core_env}")
    print(f"API verification configuration written to {args.api_env}")
    print("Private signing key was not printed. It is not an exchange credential.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
