# Native Engine Consolidation Audit — Spectra Alpha 2.0

Status: repository-backed architecture decision; implementation track in progress.
Working branch: `architecture/native-engine-consolidation`

## Executive decision

Do not rewrite Spectra from scratch. Retain the product-specific policy, portfolio, capital governance, provenance, and user interface. Delegate exchange connector lifecycle, paper/live connector implementation, order tracking, order submission, fills, and executor lifecycle to Hummingbot. Integrate the actual Condor runtime where its existing engine and loop contracts fit Spectra's operating model. PAPER and LIVE are selectable operating profiles in Spectra; PAPER is the initial default, not a permanent product restriction.

The existing Alpha 2.0 release and its paper-only deployment remain unchanged while this consolidation track is developed.

## Repository evidence

### Alpha 2.0 release branch
- Core `pyproject.toml` declares only cryptography, PyYAML, and httpx as runtime dependencies; it does not declare Hummingbot or Condor.
- Core README defines the current release as paper-only.
- `spectra_alpha2/safety.py` rejects every configuration except exact paper-only flags, rejects exchange credential environment variables, and accepts only `kraken_paper_trade`.
- `spectra_alpha2/execution.py` implements a custom HTTP paper-execution client, dispatch receipt handling, polling/reconciliation, and connector-specific paper market initialization.
- `spectra_alpha2/market_data.py` implements a custom Hummingbot API client for tickers, trading rules, books, and candles.
- `spectra_alpha2/supervisor.py` coordinates scanning, ranking, Governor authorization, order building, exits, reconciliation, and replacement.
- Core's Compose generator builds Core and Hummingbot API as separate services; Hummingbot API is pinned through its own environment.
- API `environment.yml` pins `hummingbot==20260922`. The API trading router delegates order placement to Hummingbot's account service; API connector services own Hummingbot connector instances.

### Older integration branch: Condor discovery
- The branch `integration/condor-hummingbot-spectra-runtime-20261115` contains a substantial `condor/` package, not just a ranking helper.
- Its `condor/pyproject.toml` identifies the package as `condor`, version `0.1.0`, Python `>=3.12`, with a declared `hummingbot-api-client==1.5.9` dependency. The same branch includes `condor/agents/engine.py` (TickEngine), `condor/runtime/loops.py` (LoopSupervisor), agent/risk/strategy modules, provider registries, and Hummingbot API fetchers.
- Condor's TickEngine owns autonomous agent ticks and session/journal orchestration; its LoopSupervisor owns loop registration, lifecycle transitions, and restart reconciliation. These are real reusable capabilities, but they are not a replacement for Spectra's deterministic portfolio/capital ledger.
- The integration branch's Condor package is vendored source. Its package metadata says 0.1.0, but no immutable upstream repository URL or commit pin has yet been established. Do not call this a verified upstream release until provenance is confirmed.
- Condor's agent risk/approval layer is not a substitute for Spectra CapitalGovernor. Condor agent permissions must not independently authorize exchange orders outside the Spectra authorization contract.

## Keep / delegate / retire

### Keep in Spectra
- CapitalGovernor, capital pools, reservations, single-use authorization, policy checks, position/lot/shard accounting, strategy provenance, and portfolio decisions.
- Gainer/reversal research policy, entry eligibility, approved strategy parameters, risk limits, portfolio replacement decisions, VCAP, DTTP/DTSL, and Spectra UI.
- Spectra-specific audit/event records and portfolio accounting that Hummingbot does not provide.

### Delegate to Hummingbot
- Exchange connectors and credentials management.
- PaperTradeExchange and live connector implementations.
- Order submission, cancellation, exchange rules, order lifecycle, fills, and executor lifecycle.
- Connector startup, readiness, data/order-book tracker lifecycle, and supported market-data services.
- Hummingbot remains the source of truth for exchange orders and fills. Spectra consumes/reconciles those events into its own portfolio ledger; it must not create a competing exchange execution engine.

### Integrate from Condor
- Reuse TickEngine/LoopSupervisor only for suitable autonomous research/agent tasks and operational scheduling, not as a second portfolio trading supervisor.
- Reuse provider/fetcher and Hummingbot API client patterns where they remove duplicated data access and have verified contracts.
- Reuse agent delegation, task/session records, and tool orchestration for research workflows only after the zero-cost model-routing policy and permissions are enforced.
- Do not let Condor's own executor tools place trading orders directly. Trading actions must pass through Spectra's authorization adapter and Hummingbot.
- Do not run two competing loop supervisors for the same Spectra portfolio. Spectra's portfolio cycle should have one owner; Condor agents can provide bounded research results to it.

## Selectable PAPER / LIVE operating profiles

Mode is a first-class persisted Spectra operating setting, surfaced in the UI and API. It is not inferred from scattered environment flags.

Each profile resolves to an explicit Hummingbot account and connector:
- PAPER → paper connector/account, paper portfolio namespace, simulated balances and fills.
- LIVE → live connector/account, live credential reference held in Hummingbot's supported encrypted credential store, live portfolio namespace and actual exchange balances/fills.

The strategy, Governor policy interface, order intent model, VCAP rules, and portfolio lifecycle are shared. The execution adapter resolves the active profile and binds it into every authorization and dispatch receipt. A mode switch is rejected while orders are pending, positions are not reconciled, or the target profile is not ready; it never silently transfers balances or positions between ledgers.

The profile boundary includes:
- profile, account, connector, market, side, order type, quantity/notional cap, slippage bound, strategy origin, policy version, expiry, and idempotency key in every authorization;
- separate paper/live account identities, databases or namespaces, and portfolio ledgers;
- credentials only in Hummingbot's supported encrypted credential store, never in Core environment variables or logs;
- explicit user-controlled live enablement, bounded exposure controls, loss limits, kill switch, stale-data checks, reconciliation, and restart recovery;
- no model, scanner, Condor component, or strategy loop can change the active mode or bypass CapitalGovernor.

PAPER and LIVE are both supported product modes. A live profile can be implemented and tested without activating it or submitting real orders during development.

## Migration sequence

1. Record the exact Hummingbot API/core versions and trace the vendored Condor source to its upstream repository/commit.
2. Build a capability matrix for Core, Hummingbot API, and Condor: native equivalent, gaps, retained Spectra-specific logic, and test criteria.
3. Introduce a typed execution-profile contract and a narrow execution port; keep the current paper adapter working behind it.
4. Make the Hummingbot adapter profile-aware and event-driven. Move connector readiness, order lifecycle, and fill state ownership to Hummingbot while preserving Governor pre-trade authorization.
5. Replace duplicate market-data and lifecycle code with Hummingbot/Condor native services only where their API is stable and sufficient.
6. Integrate Condor's bounded research/agent loops without giving them direct trading authority or duplicating the portfolio supervisor.
7. Add live profile configuration and credential references through Hummingbot's supported store; test profile isolation, authorization binding, caps, kill switch, idempotency, reconciliation, and restart recovery.
8. Run deterministic paper parity and full lifecycle acceptance tests before promoting any release.

## Non-goals and preservation

- Do not merge or rewrite the existing release branch.
- Do not delete old branches or local data.
- Do not change strategy parameters, VCAP rules, position limits, or capital allocations during infrastructure consolidation.
- Do not load real credentials, activate the live profile, or submit real orders during this implementation track.
- Do not claim the product is operational until a clean install and full lifecycle acceptance pass on the target Bazzite machine.

## Acceptance criteria

- Spectra owns decisions and capital authorization; Hummingbot owns exchange execution and exchange state.
- PAPER and LIVE are selectable, separately namespaced profiles using the same Spectra strategy/governance contract.
- No duplicated exchange connector or order engine remains in Core.
- Paper behavior remains equivalent under deterministic contract tests.
- Condor integration uses traceable source and verified capabilities, without direct ungoverned trading.
- Full lifecycle: discover → recommend → authorize → execute → reconcile → manage → exit → replace → restart/recover.
