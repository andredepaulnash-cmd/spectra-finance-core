# Spectra Finance Alpha — Linux paper-trading demo

This is the first installable alpha track for a local demonstration. It pins the Spectra Core candidate and the matching Spectra Hummingbot API candidate; it does not install from floating `main` branches.

## Install

On a Linux host with Git, curl, Node.js (or permission for a user-local install), and Docker Compose or rootless Podman:

```bash
curl -fsSL https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/alpha/one-command-paper-demo-20261108/install-alpha.sh | bash
```

Both the Spectra Core and Spectra Hummingbot API repositories are public. No GitHub login is required to clone either repository.

The installer:
- Creates a new `~/SpectraFinance-Alpha` directory; it does not delete or modify another Spectra installation.
- Uses a user-local uv Python 3.12 environment and a user-local Node install if needed.
- Builds the React frontend and installs the pinned Condor/Core dependencies.
- Builds the matching Hummingbot API image and starts its API, Postgres and EMQX services.
- Removes the host container-engine socket mount from the alpha Compose file. Paper mode does not need host Docker/Podman control.
- Generates the Ed25519 dispatch key pair on the machine. The private key stays in the Core `.env`; the API receives only the public key.
- Configures both processes with `SPECTRA_PAPER_ONLY=true` and `SPECTRA_ALLOW_LIVE_EXECUTION=false`.
- Updates the existing master-account Hummingbot configuration to enable only the Kraken paper connector, preserves unrelated account settings, assigns a $1,000 simulated USD balance, and registers the local API in Condor.
- Creates isolated paper state with a $1,000 total committed-capital ceiling, a $100 reserve floor, 1x leverage, no recovery budget, and a paper-only `NORMAL` breaker; live state is untouched.
- Creates the keyless `kraken_paper_trade` account config through Hummingbot's API and waits for the connector to initialize before starting Condor.
- Starts Condor and opens Spectra Overview at `http://127.0.0.1:8088/overview`.

## Paper trading scope

The selected venue is Kraken. The alpha uses Hummingbot's `kraken_paper_trade` connector and public market data with simulated orders and balances. **A Kraken private API key is not required for this paper-trading path and should not be entered into this alpha.** Tradable pairs are determined by the connector's current Kraken market metadata; the alpha must not claim a pair is tradable until the connector confirms it.

The initial simulated balances are configured in `spectra-hummingbot-api/bots/credentials/master_account/conf_client.yml` and are not real funds. Paper fills are simulations, not exchange executions.

## Control

After installation:

```bash
spectra status
spectra stop
spectra start
spectra restart
spectra logs
```

The dashboard and API bind to loopback. This alpha is intended for a local demonstration, not a public multi-user service.

## AI provider

Condor is installed as the intelligence layer. Its model/provider selection remains configurable; an operator can select a supported provider/model and supply their own provider credential locally. No AI API key is bundled or transmitted by the installer.

## Alpha limitations — stated plainly

- This is a paper-only developer alpha, not a production SaaS release.
- The two selected GitHub changes are still draft PR candidates. The installer pins their branch names but does not merge those PRs.
- The Kraken paper connector and market-data path are being verified separately from executor submission. The current dashboard's manual Create/Stop routes still need to be wired through the Spectra Capital Governor and Core-signed receipt path; the API correctly rejects direct unsigned mutations. Do not claim paper order entry, stop, fill, or PnL reconciliation is operational until that integration is completed and tested.
- Both source repositories are public. Installation does not require GitHub authentication.
- This script has not yet been exercised end-to-end on a clean Bazzite and a second Linux distribution. CI is now building and starting the exact API image, initializing the Kraken paper connector, and checking the reported paper-only runtime; it does not yet certify a full executor order/fill cycle.

Do not enable live execution or replace the paper connector with `kraken` in this alpha.
