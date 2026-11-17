# Spectra Finance Engineering Baseline

Last reviewed: 2026-11-16

## Operating rules

- Preserve the existing Condor intelligence application and Hummingbot execution stack; Spectra is the integrated product, not a replacement trading engine.
- Keep paper and live modes available. Paper-only enforcement must remain effective during development.
- Live orders, transfers, credential disclosure, and movement of funds require the Architect's separate explicit authorization.
- Never delete credentials, source history, database volumes, or user data as a routine troubleshooting step. Back up and inventory first.
- Make changes on feature branches, inspect the exact source before editing, add regression tests, and report verified outcomes separately from assumptions.
- Do not repeat a failed diagnostic or ask the Architect to manually navigate large source files when repository-side inspection or scripted checks can do the work.

## Repository roles

- `spectra-finance-core`: Condor application, agents, user interface, and product integration.
- `spectra-hummingbot-api`: Hummingbot API, exchange connectors, account services, and execution infrastructure.
- Spectra integration should connect these existing systems through documented configuration and health checks rather than fork or duplicate their engines.

## Initial audit findings

- The core README identifies Spectra Finance as an integrated Condor + Hummingbot product.
- Condor's `pyproject.toml` declares Python >=3.12.
- Hummingbot API's `environment.yml` pins Python 3.12. Use Python 3.12 as the shared supported baseline for the first clean installation; do not assume the host's Python 3.14 virtual environment is compatible.
- The Hummingbot API already contains a paper-only guard and a native paper-trade connector factory. Preserve these controls.
- The current API startup paths still derive eligible connectors from stored credential filenames and then filter live names in paper-only mode. This can skip stored live credentials safely, but does not by itself guarantee initialization of the configured `*_paper_trade` connector when it has no credential file.
- The connector factory currently checks for Gateway connectors before its `_paper_trade` branch. Verify Hummingbot's connector-settings behavior and cover both cases with tests before changing branch order.
- The local machine's current Docker deployment and GitHub default-branch source may differ. Never assume a GitHub-side fix is already present locally; compare commit/source state before deployment.

## Staged acceptance plan

1. Install and start Condor independently; verify its local dashboard/health and logs.
2. Install and start Hummingbot API independently; verify API health, database, and broker.
3. Verify Condor can authenticate to the local Hummingbot API and retrieve account/bot state.
4. Verify paper connector initialization without reading or using live exchange credentials.
5. Add the Spectra unified launch/configuration experience and branding.
6. Integrate Governor, VCAP, portfolio policy, DEX/Gateway, and the remaining strategy capabilities with automated regression coverage.
7. Validate live-mode configuration paths without submitting live orders; live trading remains separately authorized.

## Current feature branches

- Core: `spectra/integration-foundation-2026-11`
- Hummingbot API: `spectra/integration-foundation-2026-11`

This file is an engineering record, not a claim that any deployment or acceptance test has passed.
