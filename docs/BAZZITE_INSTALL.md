# Spectra Finance Alpha 2.0 — Bazzite Installation

## Scope and safety

The installer targets a rootless Podman installation on Bazzite. It does not modify the immutable host OS, use `sudo`, or mount the host Docker socket. It replaces the existing `$HOME/SpectraFinance-Alpha` installation in place: stops and removes the legacy `spectra-alpha` Compose project and its named volumes when the old Compose file is present, then deletes that installation directory before installing Alpha 2.0 there. This is intentionally destructive and removes the old Alpha database, bot files, credentials, logs, and configuration.

The stack is paper-only:
- `SPECTRA_PAPER_ONLY=true`
- `SPECTRA_ALLOW_LIVE_EXECUTION=false`
- `SPECTRA_PAPER_CONNECTORS=kraken_paper_trade`
- Hummingbot API requires signed CapitalGovernor dispatch receipts.
- The API and Core configuration contain no exchange API keys or secrets.

The Hummingbot API uses public Kraken market data and its isolated paper connector. Internal API/MQTT/database passwords and the Core Ed25519 signing key are generated locally; they are not exchange credentials.

## Prerequisites

- Bazzite with rootless Podman.
- Git, curl, and Python 3.12 or newer on the host.
- `podman compose` or `podman-compose`. The installer can install `podman-compose` into the project virtual environment if neither is available.
- Network access to GitHub, PyPI, and public container registries.

## Install

From a fresh terminal:

```bash
bash <(curl -fsSL \
  https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/release/alpha2-bazzite-desktop-20261118/scripts/install_bazzite_alpha2.sh)
```

The installer reports available home-filesystem space but does not impose an unverified fixed minimum. It creates `$HOME/SpectraFinance-Alpha` with Core in `spectra-core` and Hummingbot API in `spectra-hummingbot-api`, creates a host-side virtual environment, generates keyless paper configuration, builds an isolated Compose file, and starts the services. The current implementation builds the Hummingbot API image locally, so actual storage use depends on image layers, build cache, and existing Podman data; check `podman system df` before starting if storage is tight.

It records the exact checked-out source commits in `installed-commits.txt`.

## Commands

The installer creates `~/.local/bin/spectra-alpha2`:

```bash
spectra-alpha2 status
spectra-alpha2 doctor
spectra-alpha2 logs
spectra-alpha2 backup
spectra-alpha2 stop
spectra-alpha2 start
spectra-alpha2 restart
```

The new user systemd unit is `spectra-alpha2.service`. It is enabled for the user's session when systemd user services are available.

## Local endpoints

- Hummingbot API: `http://127.0.0.1:8002` (authenticated)
- API OpenAPI document: `http://127.0.0.1:8002/openapi.json`
- MQTT: `127.0.0.1:1884`
- EMQX management dashboard: `http://127.0.0.1:18084`

The Core and Hummingbot API communicate over the private Compose network. PostgreSQL is not published to a host port. The host Docker socket, host `/proc`, host disk probe, and host-gateway mapping are not mounted.

## Portfolio behavior

The hard maximum is 74 reserved/open positions. The supervisor admits no more than 10 new positions per cycle, only when Condor recommends an eligible market and CapitalGovernor authorizes the $20 initial entry. A pending order consumes a slot immediately. A slot is released only after all independent lots have been sold and reconciled.

The simulated portfolio starts with 25,000 USDT, split into the 6,250 USDT initial-entry pool and 18,750 USDT recovery pool. The maximum cumulative gross exposure per asset is 1,000 USDT.

## Persistence and backups

The Core SQLite ledger, PostgreSQL database, and EMQX data use Alpha 2.0-scoped named volumes. The `backup` command uses SQLite's online backup API and writes a mode-0600 database copy under `backups/`. The in-place installer deliberately deletes the prior Alpha installation and does not retain its old data.

`spectra-alpha2 uninstall` stops and removes only Alpha 2.0 containers and its Compose network. It deliberately keeps Alpha 2.0 named volumes and files. It does not run `podman system prune` or delete unrelated Podman projects.

## Verification status

A successful CI clean-install workflow verifies the Compose configuration, builds the containers, starts the isolated services, authenticates to the paper API, and runs Core's market-data doctor and persistent status checks. The separate API integration workflow verifies a real signed Kraken paper buy and Core lot settlement.

The Architect's Bazzite machine still requires the installer to be run locally. CI success is not a claim that the local machine has already been installed or verified.
