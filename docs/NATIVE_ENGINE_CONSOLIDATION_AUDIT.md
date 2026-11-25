# Native Engine Consolidation Audit — Spectra Alpha 2.0

Status: initial repository-backed audit; implementation not yet changed.
Working branch: `architecture/native-engine-consolidation`

## Executive decision

Do not rewrite Spectra from scratch. Retain the product-specific policy, portfolio, capital governance, provenance, and user interface. Reduce custom infrastructure by delegating exchange connector lifecycle, paper/live connector implementation, order tracking, order submission, fills, and executor lifecycle to the pinned Hummingbot runtime. Use Condor's existing capabilities only where verified against the exact upstream version and API; do not preserve a parallel home-grown Condor substitute merely because it already exists.

This branch is an architectural consolidation track. The existing Alpha 2.0 release and its paper-only deployment remain unchanged.

## Repository evidence

- Core `pyproject.toml` declares only cryptography, PyYAML, and httpx as runtime dependencies; it does not declare Hummingbot or Condor.
- Core README explicitly defines Alpha 2.0 as paper-only, with `SPECTRA_PAPER_ONLY=true`, `SPECTRA_ALLOW_LIVE_EXECUTION=false`, no private exchange credentials, and simulated order execution.
- `spectra_alpha2/safety.py` rejects every configuration except exact paper-only flags, rejects exchange credential environment variables, and accepts only `kraken_paper_trade`.
- `spectra_alpha2/execution.py` implements a custom HTTP paper-execution client, dispatch receipt handling, polling/reconciliation, and connector-specific paper market initialization.
- `spectra_alpha2/market_data.py` implements a custom Hummingbot API client for tickers, trading rules, books, and candles.
- `spectra_alpha2/supervisor.py` coordinates scanning, Condor ranking, Governor authorization, order building, exits, reconciliation, and replacement.
- `spectra_alpha2/condor.py` is a small deterministic ranking implementation, not evidence that the full Condor engine is integrated.
- Core's Compose generator builds Core and Hummingbot API as separate services; Hummingbot API is pinned through its own environment.
- API `environment.yml` pins `hummingbot==20260922`.
- API `utils/spectra_paper_guard.py` and `services/unified_connector_service.py` hard-enforce a paper-only connector policy. The trading router delegates actual order placement to Hummingbot's account service.

## Keep / delegate / retire

### Keep in Spectra
- CapitalGovernor, capital pools, reservations, single-use authorization, policy checks, position/lot/shard accounting, strategy provenance, and portfolio decisions.
- Gainer/reversal research policy, entry eligibility, user-approved strategy parameters, risk limits, portfolio replacement decisions, VCAP policy, DTTP/DTSL policy, and Spectra UI.
- Audit/event records that are not already reliably represented by Hummingbot order/executor state.

### Delegate to Hummingbot
- Exchange connectors and credentials management.
- PaperTradeExchange and live connector implementations.
- Order submission, cancellation, exchange rules, order lifecycle, fills, and executor lifecycle.
- Connector startup, readiness, data/order-book tracker lifecycle, and supported market-data services.
- Avoid duplicating Hummingbot's order/fill state in a second custom execution engine; Spectra should reconcile and enrich the authoritative Hummingbot events into its portfolio ledger.

### Condor decision pending exact upstream audit
- Identify the exact Condor repository, pinned commit/version, and runtime actually installed in the user's deployment.
- Compare its market ingestion, tick engine, loops, scheduling, asset discovery, ranking, and strategy hooks against current Spectra modules.
- Adopt existing Condor components only when their contracts, maintenance state, and tests are verified. Do not assume the small `CondorAnalyzer` class is the Condor engine.

## Live-mode architecture (design target; not activated)

Paper and live should be explicit execution profiles over Hummingbot, not two independently implemented order engines. A shared Spectra authorization contract must bind:
- profile (paper or live), account, connector, market, side, order type, amount/quote cap, slippage bound, strategy origin, policy version, expiry, and idempotency key;
- a distinct Governor authorization and audit trail for each environment;
- environment-specific credentials stored only in Hummingbot's supported encrypted credential store, never in Core environment variables or logs;
- separate paper/live account identities, databases or environment namespaces, and portfolio ledgers;
- default-deny live capability, explicit human enablement, bounded per-order/per-asset/portfolio exposure, loss limits, kill switch, stale-data checks, reconciliation, and restart recovery.

No code path should allow a model, scanner, Condor component, or strategy loop to turn on live mode or bypass Governor authorization. A software flag alone is not sufficient authorization.

## Migration sequence

1. Pin and document exact Hummingbot API/core versions and exact Condor source/version used by the deployed stack.
2. Build a capability matrix for each current Core/API module: native equivalent, gaps, retained Spectra-specific logic, and migration/test criteria.
3. Refactor Core behind a narrow execution port and event/reconciliation adapter; preserve existing paper behavior and contract tests.
4. Replace custom paper HTTP execution details with Hummingbot-native order/executor interfaces where practical; do not remove the Governor's pre-trade authorization.
5. Replace duplicate market-data and lifecycle code with native Hummingbot services where their API is stable and sufficient.
6. Audit Condor upstream and integrate only proven existing components; otherwise retain a thin adapter rather than inventing a faux integration.
7. Add a separately namespaced live profile and tests for authorization, credentials isolation, order caps, kill switch, idempotency, fill reconciliation, and restart recovery. Do not enable live execution as part of this migration.
8. Run parity tests against current Alpha 2.0 paper behavior before any release promotion.

## Non-goals and preservation

- Do not merge or rewrite the existing release branch.
- Do not delete old branches or local data.
- Do not change strategy parameters, VCAP rules, position limits, or capital allocations during infrastructure consolidation.
- Do not activate live execution, load real credentials, or submit real orders as part of this code audit.
- Do not claim the product is operational until a clean install and full lifecycle acceptance pass on the target Bazzite machine.

## Acceptance criteria

- Spectra owns decisions and capital authorization; Hummingbot owns exchange execution and exchange state.
- No duplicated exchange connector or order engine remains in Core.
- Paper behavior remains equivalent under deterministic contract tests.
- Condor integration is traceable to a pinned upstream version and verified capabilities.
- Paper and live environments are isolated; live mode is default-deny and requires direct human enablement.
- Full lifecycle: discover → recommend → authorize → execute → reconcile → manage → exit → replace → restart/recover.
