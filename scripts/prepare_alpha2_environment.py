#!/usr/bin/env python3
"""Create keyless, isolated paper-only Hummingbot/Core runtime configuration."""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path


FORBIDDEN=("KRAKEN_API_KEY","KRAKEN_SECRET","EXCHANGE_API_KEY","EXCHANGE_API_SECRET","BINANCE_API_KEY","BINANCE_SECRET")
POLICY="spectra-alpha2-v1"


def read_env(path:Path)->dict[str,str]:
    values={}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key,value=line.split("=",1)
                values[key.strip()]=value.strip()
    return values


def write_env(path:Path,values:dict[str,str])->None:
    existing=read_env(path)
    existing.update(values)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key,value in existing.items()),encoding="utf-8")
    os.chmod(path,0o600)


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-repo",type=Path,default=Path("../spectra-hummingbot-api"))
    args=parser.parse_args()
    core_root=Path(__file__).resolve().parents[1]
    api_root=args.api_repo.resolve()
    if not (api_root/"docker-compose.yml").is_file():
        raise SystemExit(f"Hummingbot API repository not found at {api_root}")
    api_env=api_root/".env"
    api_values=read_env(api_env)
    defaults={
        "API_BIND":"127.0.0.1",
        "DB_BIND":"127.0.0.1",
        "USERNAME":"spectra_alpha2",
        "PASSWORD":secrets.token_hex(32),
        "CONFIG_PASSWORD":secrets.token_hex(32),
        "BROKER_USERNAME":"spectra_alpha2",
        "BROKER_PASSWORD":secrets.token_hex(32),
        "BROKER_DASHBOARD_PASSWORD":secrets.token_hex(32),
        "POSTGRES_PASSWORD":secrets.token_hex(32),
        "SPECTRA_PAPER_ONLY":"true",
        "SPECTRA_PAPER_CONNECTORS":"kraken_paper_trade",
        "SPECTRA_ALLOW_LIVE_EXECUTION":"false",
        "SPECTRA_REQUIRE_CORE_RECEIPTS":"true",
        "SPECTRA_DISPATCH_POLICY_VERSION":POLICY,
        "SPECTRA_DISPATCH_RECEIPT_DB":"bots/data/spectra_dispatch_receipts.sqlite3",
    }
    for key,value in defaults.items():
        if key not in api_values:
            api_values[key]=value
    for key,value in {
        "SPECTRA_PAPER_ONLY":"true",
        "SPECTRA_PAPER_CONNECTORS":"kraken_paper_trade",
        "SPECTRA_ALLOW_LIVE_EXECUTION":"false",
        "SPECTRA_REQUIRE_CORE_RECEIPTS":"true",
        "SPECTRA_DISPATCH_POLICY_VERSION":POLICY,
    }.items():
        api_values[key]=value
    for key in FORBIDDEN:
        if key in api_values and api_values[key]:
            raise SystemExit(f"Refusing to continue: exchange credential variable {key} is present.")
    write_env(api_env,api_values)

    emqx=api_root/".emqx"
    emqx.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(emqx,0o700)
    bootstrap=emqx/"auth-bootstrap.csv"
    bootstrap.write_text(
        "user_id,password,is_superuser\n"
        f"{api_values['BROKER_USERNAME']},{api_values['BROKER_PASSWORD']},false\n",
        encoding="utf-8",
    )
    os.chmod(bootstrap,0o600)

    credential_dir=api_root/"bots/credentials/master_account"
    credential_dir.mkdir(parents=True,exist_ok=True)
    unexpected=[p.name for p in credential_dir.iterdir() if p.name!="conf_client.yml"]
    if unexpected:
        raise SystemExit(
            "Refusing to overwrite or inspect exchange credential files in master_account: "
            + ", ".join(sorted(unexpected))
            + ". Use a fresh Alpha 2.0 API checkout."
        )
    connector_script=core_root/"scripts/configure_alpha_paper_connector.py"
    env=os.environ.copy()
    env.update({
        "SPECTRA_PAPER_ONLY":"true",
        "SPECTRA_PAPER_CONNECTORS":"kraken_paper_trade",
        "SPECTRA_ALLOW_LIVE_EXECUTION":"false",
    })
    subprocess.run(
        [sys.executable,str(connector_script),"--config",str(credential_dir/"conf_client.yml")],
        check=True,env=env,
    )
    remaining=[p.name for p in credential_dir.iterdir() if p.name!="conf_client.yml"]
    if remaining:
        raise SystemExit("Unexpected credential files appeared during paper connector setup: "+", ".join(remaining))
    write_env(core_root/".env",{
        "POSTGRES_PASSWORD":api_values["POSTGRES_PASSWORD"],
        "BROKER_DASHBOARD_PASSWORD":api_values["BROKER_DASHBOARD_PASSWORD"],
    })
    core_env=core_root/".env.core"
    subprocess.run(
        [
            sys.executable,str(core_root/"scripts/generate_spectra_dispatch_keys.py"),
            "--core-env",str(core_env),"--api-env",str(api_env),
        ],
        check=True,
    )
    core_values=read_env(core_env)
    for key in FORBIDDEN:
        if key in core_values and core_values[key]:
            raise SystemExit(f"Refusing to continue: exchange credential variable {key} is present in Core env.")
    os.chmod(core_env,0o600)
    print("Alpha 2.0 paper environment prepared.")
    print("Connector: kraken_paper_trade (keyless public market data + isolated paper execution).")
    print("Paper-only flags enforced; live execution disabled; no exchange credentials created or accepted.")
    print(f"Core environment: {core_env}")
    print(f"API environment: {api_env}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
