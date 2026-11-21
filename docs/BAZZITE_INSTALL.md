# Spectra Finance Alpha 2.0 — Bazzite Installation

## Scope and safety

The installer targets a rootless Podman installation on Bazzite. It does not modify the immutable host OS, use `sudo`, mount the host Docker socket, or remove Alpha 1.0 resources.

The stack is paper-only:
- `SPECTRA_PAPER_ONLY=true`
- `SPECTRA_ALLOW_LIVE_EXECUTION=false`
- `SPECTRA_PAPER_CONNECTORS=kraken_paper_trade`
- Hummingbot API requires signed CapitalGovernor dispatch receipts.
- The API and Core configuration contain no exchange API keys or secrets.

The Hummingbot API uses public Kraken market data and its isolated paper connector. Internal API/MQTT/database passwords and the Core Ed25519 signing key are generated locally; they are not exchange credentials.

## Prerequisites

- Bazzite with rootless Podman.
- Git and Python 3.12 or newer on the host.
- `podman compose` or `podman-compose`. The installer can install `podman-compose` into the project virtual environment if neither is available.
- Network access to GitHub and public container registries.

## Install

From a fresh terminal:

```bash
bash <(curl -fsSL \
  https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/spectra-finance-alpha-2.0/scripts/install_bazzite_alpha2.sh)
```

The installer creates `$HOME/SpectraFinance-Alpha2` with separate Core and Hummingbot API checkouts, creates a host-side virtual environment, generates keyless paper configuration, builds an isolated Compose file, and starts the services. It pins both checkouts to the dedicated `spectra-finance-alpha-2.0` branches.

It records the exact checked-out source commits in `installed-commits.txt`. On an in-place upgrade, it first backs up the running Core SQLite ledger and API receipt database with SQLite's online backup API, exports the Alpha 2.0 PostgreSQL database, and copies internal signing/configuration files under `backups/` with restrictive permissions. It refuses to overwrite local source changes and stops if an existing Alpha 2.0 data volume cannot be safely backed up. It never removes Alpha 1.x files, containers, databases, or volumes and never runs `podman compose down -v`.

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

The user systemd unit is `spectra-alpha2.service`. It is enabled for the user's session when systemd user services are available.

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

The Core SQLite ledger, PostgreSQL database, and EMQX data use Alpha 2.0-scoped named volumes. The `backup` command uses SQLite's online backup API for the Core ledger and writes a mode-0600 database copy under `backups/`. The installer also creates the broader pre-upgrade backup set described above.

`spectra-alpha2 uninstall` stops and removes only Alpha 2.0 containers and its Compose network. It deliberately keeps named volumes and files. It does not run `podman system prune` or delete Alpha 1.0 resources.

## Verification status

A successful CI clean-install workflow verifies the Compose configuration, builds the containers, starts the isolated services, authenticates to the paper API, runs Core's market-data doctor and persistent status checks, and executes one full paper-only strategy cycle. The separate API integration workflow verifies a signed Kraken paper MARKET BUY and Core lot settlement.

VCAP shard automation remains disabled until its approved selection/batching/recycling policy is configured. DTTP/DTSL remains exclusive to TOP_LOSER_REVERSAL and is not enabled until its ratchet specification is implemented. The Architect's Bazzite machine still requires local install/upgrade, backup/restore, rollback, and service-preservation verification. CI success is not a claim that the local machine has already been installed or verified.
