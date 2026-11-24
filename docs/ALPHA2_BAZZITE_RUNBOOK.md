# Spectra Finance Alpha 2.0 — Bazzite install and operator runbook

## Release scope

This runbook installs the paper-only Alpha 2.0 release candidate from the `alpha2-paper-usd-release` Core branch and the dedicated `spectra-finance-alpha-2.0` Hummingbot API branch.

The execution path verified in CI is Kraken USD spot paper trading through `kraken_paper_trade`. Live execution is disabled, and exchange API credentials are rejected by the paper-safety boundary.

This release does not include feature parity with the separate `spectra_core` integration branch. VCAP recovery and the advanced DTTP/DTSL behavior from PRs #139–#143 require a deliberate port and separate acceptance tests.

## Host prerequisites

- Bazzite/Linux with rootless Podman working for the logged-in user.
- Git and Python 3 available on the host.
- Internet access for cloning the repositories and building containers.
- Enough free disk space for the Hummingbot API image, PostgreSQL, EMQX and paper data.

Check Podman before installation:

```bash
podman info
```

Do not run the installer with `sudo`. It is designed for rootless Podman and a user-owned installation.

## Fresh installation

Clone the installer source and run it:

```bash
git clone --branch alpha2-paper-usd-release --single-branch \
  https://github.com/andredepaulnash-cmd/spectra-finance-core.git \
  "$HOME/spectra-alpha2-installer"

bash "$HOME/spectra-alpha2-installer/scripts/install_bazzite_alpha2.sh"
```

The default installation directory is:

```text
$HOME/SpectraFinance-Alpha2/
```

The installer checks existing directories and Git origins, refuses to overwrite local source modifications, creates an isolated Core Python virtual environment, prepares keyless paper-only API configuration, generates the Compose file, builds the stack, waits for authenticated API readiness and runs the Core doctor.

It creates a desktop launcher named **Spectra Finance Alpha 2.0**.

## Operator commands

The command wrapper is installed at `$HOME/.local/bin/spectra-alpha2`.

```bash
~/.local/bin/spectra-alpha2 status
~/.local/bin/spectra-alpha2 doctor
~/.local/bin/spectra-alpha2 logs
~/.local/bin/spectra-alpha2 once
~/.local/bin/spectra-alpha2 start
~/.local/bin/spectra-alpha2 stop
~/.local/bin/spectra-alpha2 restart
```

- `start` starts the persistent Core service. Its default command runs the supervised paper-trading loop continuously; the container is configured to restart unless stopped.
- `stop` stops the stack without deleting its named data volumes. `restart` restarts the services.
- `status` reports the paper-only state, open positions, pending authorizations and capital pools.
- `doctor` checks authenticated API access, Kraken paper tickers/rules, public candles and discovery configuration.
- `logs` follows the Core and API service logs.
- `once` runs one supervised portfolio cycle on demand without replacing the persistent service.
- The desktop command center provides the corresponding controls.

The default scan interval is 10 seconds (`SPECTRA_SCAN_INTERVAL_SECONDS`). The portfolio target and hard ceiling are 50 simultaneous positions, with a $20 initial MARKET BUY budget per position. The supervisor can submit up to 50 new entries in a cycle while vacancies remain. It ranks positive 24-hour USD gainers by percentage change and uses deliberately permissive paper-test defaults: at least $100,000 in 24-hour quote volume and no more than a 1% top-of-book spread. Fresh ticker data, valid Kraken trading rules, positive two-sided order books, minimum order sizes, signed capital authorization and the configured 0.5% market-buy slippage ceiling still apply. After a position is fully sold and the exit fill is reconciled, its slot is eligible for replacement in that same cycle using another qualifying gainer; the just-closed asset is not immediately re-entered in that cycle. Every entry is a MARKET BUY through Kraken's paper connector. Ordinary top-gainer exits use the configured fee-aware LIMIT SELL only after the live paper bid reaches its target. The current release does not activate VCAP recovery or the advanced DTTP/DTSL reversal engine; those remain outside this release's verified scope.

If an order outcome is uncertain, the supervisor retains its capital reservation and reconciles it on a later cycle rather than blindly resubmitting the order. Check `logs` and `status` before intervening manually.

## Updating

Use the installed updater:

```bash
~/.local/bin/spectra-alpha2 update
```

The updater is pinned to the verified Core release branch and dedicated API branch. Before changing source or rebuilding containers, it creates a pre-upgrade snapshot of the Core SQLite ledger, API dispatch-receipt database when present, PostgreSQL state, release manifest and important configuration files.

The update refuses to proceed if the existing Core/API/PostgreSQL stack cannot be backed up or if local source modifications are detected.

The desktop command center also provides a confirmed **Update release** action.

## Manual backup

Before maintenance, create a complete backup:

```bash
~/.local/bin/spectra-alpha2 backup
```

Backups are stored under:

```text
$HOME/SpectraFinance-Alpha2/backups/
```

A manual backup includes the Core SQLite ledger, API dispatch receipts when present, PostgreSQL dump, environment/configuration files, Compose definition and installed Core/API commit manifest. Backup files are owner-readable only.

Do not use `podman compose down -v` for routine stop/update operations; `-v` removes named data volumes.

## Service endpoints

The installer binds the local service endpoints to loopback:

| Service | Local endpoint |
|---|---|
| Hummingbot API | `http://127.0.0.1:8002` |
| MQTT | `127.0.0.1:1884` |
| EMQX dashboard | `http://127.0.0.1:18084` |

API credentials are generated locally and stored in owner-only environment files. They are internal service credentials, not exchange credentials.

## Paper-trading configuration

- Execution connector: `kraken_paper_trade`
- Quote asset: USD
- Paper balance: 25,000 USD
- Default entry liquidity threshold: 100,000 USD 24-hour quote volume
- Default maximum top-of-book spread: 1%
- Maximum simultaneous positions: 50
- Initial paper buy budget: 20 USD per position
- Maximum new entries attempted per cycle: 50
- All purchases are MARKET BUY orders.
- LIMIT orders are reserved for authorized exit sells.
- Live execution remains disabled; no exchange API keys are configured or accepted.

A successful doctor does not guarantee that all 50 slots can be filled. The strategy fills only when enough distinct positive 24-hour USD gainers satisfy the permissive discovery filters and Kraken's minimum-order/execution checks. If fewer than 50 qualify, it continues scanning and fills vacancies as qualifying opportunities appear. This build automates initial entries, fee-aware take-profit exits and slot replacement; VCAP mean-reversion recovery and advanced DTTP/DTSL remain separate, unimplemented release work.

## Data-preserving stop and uninstall

Stop services without removing data:

```bash
~/.local/bin/spectra-alpha2 stop
```

The `uninstall` command stops and removes only the Alpha 2.0 containers/network after confirmation; it retains named volumes and files. Review and back up the data directory before any manual volume removal.

## Verification boundary

GitHub CI has passed a clean paper-only Compose installation, a real Kraken USD paper MARKET BUY through the Core executor, fill/fee reconciliation into the Spectra ledger, and one supervised strategy cycle.

That CI result does not replace the final host check on the target Bazzite machine. The first host installation should be treated as a paper-only acceptance run: verify `doctor`, `status`, logs, a supervised cycle and persistence after restart before relying on it.
