# Spectra Finance — local deployment and validation

Spectra Finance combines the Condor intelligence layer, Spectra Core orchestration and Capital Governor, and the Hummingbot execution stack. This document distinguishes the implemented pieces from the remaining integration work.

## Default operating configuration

A fresh installation defaults to isolated paper execution:

- Simulated starting equity: **25,000 USD**
- Fleet target: **83 unique active positions**
- Initial allocation per new position: **20 USD**
- VCAP/DCS additions: separately planned and authorized by the Capital Governor
- Paper venue in the current integration: Kraken's `kraken_paper_trade`
- No exchange API key is required for the paper connector

The recovered historical defaults use a 25% reserve floor and a 5% per-asset exposure ceiling. These are configurable starting policy values, not permanent strategy constants. The 20 USD amount is the initial entry allocation, not a lifetime position cap.

The installer preserves customized account balances and capital policies. It initializes a fresh account at exactly 25,000 USD. It upgrades only the exact untouched legacy demo defaults, and only when the paper position ledger contains no position history. Existing traded or customized state is preserved.

## Paper and live modes

Paper mode is the default and remains isolated from live account state.

Live capability is preserved, not removed. It requires both the API and Core environment files to be explicitly configured with:

```text
SPECTRA_PAPER_ONLY=false
SPECTRA_ALLOW_LIVE_EXECUTION=true
```

The live state directory is separate from paper state. Its initial capital policy has zero allocation limits and its breaker starts HALTED. Live operation therefore also requires an independently configured live capital policy and a deliberate breaker release. Broad engineering permission or a paper-mode setting does not authorize live orders.

## Install

On Linux/Bazzite with Git, curl, Node.js (or permission for a user-local install), and Docker Compose or rootless Podman:

```bash
curl -fsSL https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/fix/spectra-connectivity-and-agent-auth/install-alpha.sh | bash
```

The installer:
- Uses a user-local Python 3.12 environment and user-local Node install if needed.
- Builds the React dashboard and installs the Core dependencies.
- Builds the Hummingbot API image and starts the API, Postgres and EMQX services.
- Removes the host container-engine socket mount from the API Compose file.
- Generates the Core dispatch signing key locally; the API receives only the public key.
- Keeps API and Core execution-mode flags consistent.
- Creates separate paper and live state directories; the live breaker remains HALTED.
- Starts Spectra Core and the local dashboard at `http://127.0.0.1:8088/overview`.

## Verified components

The Hummingbot API integration PR has passed CI and is merged. Its tests cover:
- Explicit paper-pair initialization and the managed PaperTradeExchange clock.
- A Core-signed paper order.
- Simulated balance changes and order/trade history reconciliation.
- Rejection of live connectors on the paper-only path.
- Dispatch receipt validation and replay/body-tampering protections.

The Core repository contains the recovered market-discovery scanner, multi-horizon asset fingerprinting, Capital Governor, protected executor dispatch, economic position ledger, VCAP tranche/lifecycle modules, and multi-position VCAP servicing scheduler. The paper entry limit is now 83 positions, with regression tests for the 83rd and 84th slots.

## Not yet certified — current engineering work

The following are still required before calling the full automated portfolio operational:

1. A persistent autonomous portfolio admission controller that discovers eligible assets, selects them using the recovered intelligence/strategy logic, obtains Governor authorization, creates the initial position, and automatically replenishes vacancies.
2. End-to-end connection of the Core-managed executor path to the API's managed paper clock, with confirmed fills, fee/balance accounting and durable reconciliation.
3. A complete restart test proving paper balances, orders, position ownership, VCAP/DCS state and the 83-position fleet recover without double-spending or losing state.
4. A complete VCAP/DCS lifecycle test from entry through repair/patch tranches, profit-floor exit and automatic fleet rotation.
5. Local verification on the target Bazzite machine.

The existing multi-position VCAP scheduler services positions already admitted to the lifecycle. It is not yet the autonomous portfolio scheduler that creates and replenishes the fleet. The 83-position cap is implemented; the autonomous replenishment loop is the next major integration task.

## Operator controls

```bash
spectra status
spectra stop
spectra start
spectra restart
spectra logs
spectra doctor
```

The dashboard and API bind to loopback by default. The installer and updater preserve credentials, database volumes, paper history and customized local policy. No live order is submitted by installation, update, health checks or tests.
