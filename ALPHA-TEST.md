# Spectra Finance Alpha — Linux paper-trading demo

This is the first installable alpha track for a local demonstration. It pins the Spectra Core candidate and the matching Spectra Hummingbot API candidate; it does not install from floating `main` branches.

## Install

On a Linux host with access to the private Spectra Hummingbot API repository:

```bash
curl -fsSL https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/alpha/one-command-paper-demo-20261108/install-alpha.sh | bash
```

The Core repository is public. The Hummingbot API repository is currently private, so the installing GitHub account must already have read access. If the machine is not authenticated, run `gh auth login` once and authorize access to `andredepaulnash-cmd/spectra-hummingbot-api`, then rerun the same install command. This installer deliberately does not change repository visibility.

The installer:
- Creates a new `~/SpectraFinance-Alpha` directory; it does not delete or modify another Spectra installation.
- Uses a user-local uv Python 3.12 environment and a user-local Node install if needed.
- Builds the React frontend and installs the pinned Condor/Core dependencies.
- Builds the matching Hummingbot API image and starts its API, Postgres and EMQX services.
- Removes the host container-engine socket mount from the alpha Compose file. Paper mode does not need host Docker/Podman control.
- Generates the Ed25519 dispatch key pair on the machine. The private key stays in the Core `.env`; the API receives only the public key.
- Configures both processes with `SPECTRA_PAPER_ONLY=true` and `SPECTRA_ALLOW_LIVE_EXECUTION=false`.
- Configures the Kraken paper connector with simulated balances and registers the local API in Condor.
- Starts Condor and opens the local dashboard at `http://127.0.0.1:8088`.

## Paper trading scope

The selected venue is Kraken. The alpha uses Hummingbot's `kraken_paper_trade` connector and public market data with simulated orders and balances. **A Kraken private API key is not required for this paper-trading path and should not be entered into this alpha.** Tradable pairs are determined by the connector's current Kraken market metadata; the alpha must not claim a pair is tradable until the connector confirms it.

The initial simulated balances are configured in `spectra-hummingbot-api/bots/conf/conf_client.yml` and are not real funds. Paper fills are simulations, not exchange executions.

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
- The complete Core → signed receipt → API → Kraken paper connector order/fill reconciliation still requires a successful local end-to-end test before calling the alpha operational.
- The Hummingbot API repository is private today, so installation requires authorized GitHub access.
- This script has not yet been exercised end-to-end on a clean Bazzite and a second Linux distribution. The CI check validates shell syntax and safety invariants, not a full container build or paper fill.

Do not enable live execution or replace the paper connector with `kraken` in this alpha.
