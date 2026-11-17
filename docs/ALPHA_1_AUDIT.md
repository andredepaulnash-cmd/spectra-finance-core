# Alpha 1.x Technical Audit — Initial Findings

- Audit status: **PRELIMINARY / IN PROGRESS**
- Audited refs: Core `spectra-core-phase2`; Hummingbot API `main`
- No local checkout, runtime, or test command was executed during this repository/API inspection.
- No Alpha 1.x component is approved for direct reuse until its tests and integration contract are reviewed.

## Repository shape

### Spectra Core / Condor repository
The existing Core default branch contains a broad Condor application, frontend, MCP tools, many agent/skill directories, routines, market scanners, and a separate `spectra_core` trading domain. The existing Python project is named `condor`, requires Python >=3.12, and has a wide dependency set including Telegram, Hummingbot API client, plotting, data science, MCP, FastAPI, LLM and speech-related packages.

**Assessment:** This is a substantial existing product, not a minimal trading-engine codebase. Alpha 2.0 must not inherit its entire dependency graph or agent/tool surface merely to reuse portfolio functionality. Condor's useful scanner/analysis and lifecycle capabilities can be evaluated selectively.

### Existing CapitalGovernor
The existing `spectra_core/capital_governor.py` is a deterministic capital reservation boundary backed by SQLite. It persists proposal reservations, execution receipts, and executor accounting observations, and uses existing position-hold/executor-accounting modules.

**Potential reuse:** Its atomic reservation and reconciliation concepts are useful.

**Not yet approved:** It has not been proven to enforce the Alpha 2.0 exact $25,000 pools, 74-position ceiling, $1,000 per-asset gross-notional cap, fee reserve, exact VCAP ladder, two-shard semaphore, or paper-only authorization contract as one coherent policy.

### Existing VCAP implementation
The existing VCAP implementation is spread across recovery fingerprints, tranche planning, execution, lifecycle state, profit exit, partial recycle, and persistence modules. The inspected tranche planner dynamically chooses tranche counts and weights from recovery behavior and exchange rules. The execution planner explicitly replans one tranche at a time.

**Assessment:** This is materially different from the fixed Alpha 2.0 ladder and per-shard lifecycle. It must not be imported wholesale. Individual Decimal arithmetic, exchange-rule, accounting, persistence, and restart techniques may be reused only after focused tests establish correctness.

### Existing market scanner
The inspected `routines/market_scanner.py` scans Binance perpetual USDT tickers and requests candles through a Hummingbot API client. The Alpha 2.0 directive requires public market data and uses Kraken fee assumptions for paper execution.

**Assessment:** This scanner is not an approved Alpha 2.0 default. Venue, market type, quote asset, data source, liquidity filters, and execution venue must be aligned explicitly. No silent Binance-perpetual/Kraken-spot mismatch.

### Hummingbot API repository
The API repository is a full Hummingbot API deployment with Docker configuration, bot/controller functionality, API authentication, exchange connector configuration, broker/database setup, and additional Gateway functionality. Its README includes workflows for configuring exchange accounts and strategies, so the upstream surface is broader than Alpha 2.0 needs.

**Assessment:** Reuse only the narrow paper connector and public market-data/executor capabilities that can be isolated and verified. Do not copy the generic credential onboarding, live connector, wallet/Gateway, or live strategy surfaces into Alpha 2.0. No private exchange credential fields or flows will be introduced.

## Initial reuse decisions

| Area | Initial decision | Reason |
|---|---|---|
| Existing Condor runtime | Audit selectively | Mature but broad; do not pull all agents/dependencies into Alpha 2.0 |
| Existing market scanner | Rewrite/adapt | Current inspected scanner is Binance perpetual-specific |
| Existing CapitalGovernor | Reuse concepts; rewrite/port selectively | Must satisfy new exact limits and paper-only contract |
| Existing dynamic VCAP planner | Do not reuse as policy | Conflicts with exact fixed ladder and shard rules |
| Existing VCAP ledger/restart techniques | Audit selectively | Potentially useful after test-level verification |
| Hummingbot API | Reuse narrow isolated paper execution only | Full API exposes unrelated/live-capable surfaces |
| Existing frontend | Defer | Dashboard is not a prerequisite for proving deterministic trading lifecycle |

## Audit gaps

- Inspect exact current Condor startup/stop lifecycle and scanner/recommendation interfaces.
- Inspect current paper connector implementation, connector registration, and order/fill event behavior.
- Inspect API compose/runtime image pins and verify whether a public-data-only paper connector starts without private exchange credentials.
- Inspect existing Core tests and workflow results; do not infer pass status from source presence.
- Inspect the Governor file and blueprints when the Architect supplies them; reconcile before treating this preliminary audit as final.
- Confirm licensing and pinned upstream Hummingbot version/commit before packaging.

## Conclusion

The clean Alpha 2.0 branch is intentionally not importing the previous application tree. Existing Alpha 1.x source remains available in prior branches and Git history. This audit supports a small, testable domain core first, followed by narrow integrations. It does not yet certify any existing component for production/paper use.
