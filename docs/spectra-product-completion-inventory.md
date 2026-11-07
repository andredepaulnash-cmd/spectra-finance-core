# Spectra Finance: product completion inventory

**Status:** Initial repository-backed inventory, 2026-11-07  
**Purpose:** Preserve the full Spectra product scope while completing the real application. This is an engineering status record, not a claim that every subsystem is production-ready.

## Product identity and operating boundary

Spectra Finance is the product. Condor is the analysis/agent layer; Hummingbot is an execution dependency that may be replaced or deeply internalized later. The interface, user-facing terminology, navigation, logs, and operator controls should present Spectra Finance.

The intended authority chain is:

1. Condor may research, analyze, backtest, and propose.
2. Spectra Core / Capital Governor validates policy, accounting, scope, and available authority.
3. The execution adapter may submit only the exact action authorized by Core.
4. The operator retains final authority over enabling any live-capital mode.

Paper mode is the first end-to-end verification environment. It is not a reduced product scope and must not be used to imply profitability.

## 1. Advanced-module recovery inventory

### Explicitly named by the Architect

| Module | Repository evidence checked | Current finding | Required action |
|---|---|---|---|
| DTTP | Core and API default-branch code search; Core branch names; Core commit search | No exact acronym match found in the checked repositories/search surfaces | Recover exact source, definition, owner, inputs/outputs, tests, and integration contract from historical branches, local files, or the governor blueprint |
| DTSL | Same search surfaces | No exact acronym match found | Same; do not infer an expansion |
| PSSM | Spectra agent doctrine in `agents/spectra/AGENT.md` names PSSM as a deterministic alternative when recovery economics deteriorate; no implementation or expansion found in the checked source | Mentioned as an intended decision path, implementation/definition unverified | Recover exact specification and implementation; do not infer an expansion |
| VCAP | Core package exports, runtime, lifecycle, planner, profit-exit, restart, and MCP tool paths | Strong repository evidence; several modules and tests exist | Complete the lifecycle/recovery integration audit and end-to-end verification |
| MIR (possible match for the additional remembered module) | Spectra agent doctrine in `agents/spectra/AGENT.md` names MIR alongside PSSM; no implementation or expansion found in the checked source | Candidate identity only; not confirmed to be the module the Architect is remembering | Verify with historical source and the Architect's governor blueprint; do not infer an expansion |
| Additional remembered module | No reliable identifier beyond the MIR candidate | Unresolved | Identify from historical commits, project documents, and Architect-provided governor file; do not guess |

**Important search limitation:** the exact-acronym searches did not find DTTP, DTSL, or PSSM in the two checked repositories. This is not proof that the work is lost. Search indexing, private/archived repositories, old local folders, uploaded blueprints, and unmerged historical branches may contain it.

### VCAP source map found in the Core repository

The current Core integration candidate exports and/or contains these VCAP-related modules:

- `spectra_core/recovery_fingerprint.py` — recovery fingerprint decisions and VCAP recovery planning.
- `spectra_core/vcap_tranches.py` — tranche planning.
- `spectra_core/vcap_execution.py` — next-tranche carving and sequence replanning.
- `spectra_core/vcap_profit_exit.py` — fee/cost-aware profit-exit decisions.
- `spectra_core/vcap_state.py` — durable state and restart reconciliation.
- `spectra_core/vcap_lifecycle.py` — lifecycle planning and execution checkpoints.
- `spectra_core/vcap_runtime.py` — durable runtime-cycle state and decision trace integration.
- `mcp_servers/hummingbot_api/tools/vcap_execution.py`
- `mcp_servers/hummingbot_api/tools/vcap_lifecycle.py`
- `mcp_servers/hummingbot_api/tools/vcap_profit_exit.py`
- `mcp_servers/hummingbot_api/tools/vcap_runtime.py`
- Tests include `tests/test_vcap_execution_loop.py` and `tests/test_vcap_profit_exit.py`, plus lifecycle/runtime coverage in the wider suite.

This establishes code presence, not complete production activation. Each VCAP path still needs a feature-by-feature map from pure planner through Governor authorization, dispatch, exact fill reconciliation, restart, and operator visibility.

## 2. Core capability inventory

The integration candidate PR #44 is based on the protected production checkpoint and is still draft/unmerged. Its current description records 892 passing focused tests on its then-current head. That is a focused regression result, not a full release or Bazzite installation certification.

Capabilities visible in the candidate's source and changed-file set include:

- Capital Governor and policy decisions.
- Cash authority and execution policy.
- Accounting health and breaker-related checks.
- Economic position / lot ledger.
- Economic exit manifests and owner-attributed allocation.
- Fill reconciliation and executor provenance bridge.
- Position mutation ownership and dispatch guards.
- Append-only decision traces.
- Cycle reconciliation and conservation audit.
- VCAP runtime, lifecycle, execution, and profit-exit integration.
- Partial-attempt assessment (default-off).
- Selected-recovery shadow accounting (default-off).

### Known incomplete or separately staged Core work

- Native selected-recovery runtime and ledger integration.
- Sealed opening-basis Governor integration.
- Dust lifecycle integration.
- Fully coordinated asynchronous partial-attempt runtime.
- API-side Core-issued, short-lived, exact-scope dispatch receipts and trusted image provenance.
- Full integration/verification of every strategy and advanced module in a single end-to-end environment.

The separate `support/decision-accounting-dust-20261012` branch contains selected-recovery runtime/ledger and dust-lifecycle source plus tests. A new draft port PR #46 has brought the selected-recovery native ledger/runtime onto the PR #44 composition candidate and added the required Governor schema/wrappers plus the sealed-gross-basis exposure adjustment. The latest focused CI passed 924 tests. The first run exposed missing schemas and incompatible dust dependencies; dust was removed from the port and remains deferred. It is not safe to call dust complete until it is composed with the newer cash/retirement changes. Prior asynchronous partial-attempt work caused protected-baseline regressions and was reverted from the candidate.

## 3. Execution/API boundary inventory

The Hummingbot API repository already contains executor/order provenance endpoints and corresponding tests. The API audit PR #5 is documentation-only and remains draft/unmerged.

The audit found direct REST mutation surfaces (including executor creation, trading orders, leverage/position mode, cancellation, Gateway writes, bot start/deploy, and Docker deployment) whose inspected paths do not visibly require a Spectra Core authorization receipt. Basic Auth, loopback binding, or a restricted Condor MCP tool profile is not equivalent to Core capital authorization.

The Docker deployment path and mutable image/dependency references are also trust boundaries. A caller-controlled image can be especially dangerous where the deployment service mounts configuration, scripts, data, logs, Docker socket, or host networking. This is a source-level risk finding, not evidence of compromise.

**Required integration design:** a signed, one-time, short-lived, exact-scope Core dispatch authorization, validated at the API mutation boundary and bound to connector/account/pair/side/amount/order type/price constraints and the intended execution identity. Every alternate REST/MCP/Gateway/bot-deployment path must either enforce the same authority or remain unavailable to the Spectra product.

## 4. Existing application and branding findings

The Core repository already contains a substantial React 19 + Vite frontend under `frontend/` and a FastAPI web application under `condor/web/`. The frontend includes portfolio, bots, executors/trade, DEX, agents, routines, and settings surfaces. It is not necessary to invent a separate dashboard from zero.

However, the current app identifies itself as **Condor** in `frontend/index.html` and in the FastAPI application title. The current route tree includes live-capable trading surfaces. Therefore, simply changing the logo/title and calling it Spectra would be unsafe and misleading.

Branding work must be paired with product-mode and authorization work:

- Spectra Finance name and identity throughout the interface.
- Condor represented as the analysis/agent subsystem, not the product name.
- Hummingbot represented as an internal execution provider.
- Clear system-health, policy, decision, approval/rejection, paper-balance, positions, orders, fills, fees, PnL, and audit/event views.
- Paper mode visibly distinguished from any future live mode.
- No live mutation controls in the first Spectra paper-only product shell.
- No API credentials or wallet secrets exposed to Condor or the frontend.

## Additional paper/research implementation found

A separate branch, `paper/btc-eth-crash-short-beta-20261012`, contains a substantial default-off, isolated SQLite paper price-path engine (`crash_short_model.py`, `crash_short_signal.py`, `crash_short_paper.py`, `crash_short_runtime.py`) with synthetic-input tests and frozen research evidence. It is explicitly a hypothetical fractional BTC/ETH spot-price short proxy: no exchange client, credentials, order dispatch, live route, margin model, or Hummingbot integration. It must remain a separate research/paper book and must not be treated as the general Spectra execution simulator or merged into spot/VCAP capital balances.

## 5. Paper-trading status

A complete, verified paper-trading route has **not yet been established** from the repository evidence inspected for this inventory. The exact Hummingbot version and its supported paper-trading/simulator mechanism must be inspected before choosing the adapter.

The first vertical slice must use an explicit simulator or verified supported paper mode, a separate simulated ledger/balance, deterministic sample or clearly labelled market data, and a hard boundary preventing simulated orders from reaching live connectors. It must exercise:

Condor proposal → Core policy decision → paper-only authorization → simulated execution → exact fill/accounting reconciliation → Spectra dashboard and durable audit trace.

No real exchange credentials, wallet keys, live orders, or capital are required for this milestone.

## 6. Delivery sequence

1. Recover and document all advanced-module definitions and source locations, including DTTP, DTSL, PSSM, VCAP, and the missing remembered subsystem.
2. Finish the Core integration in isolated, testable slices. Keep PR #44 separate until the full regression surface is understood.
3. Close the API mutation-authority gap and establish a verified paper-only execution adapter.
4. Rebrand and adapt the existing frontend as Spectra Finance, beginning with a paper-only shell and read-only/approved views.
5. Package and verify on the Architect's Bazzite machine only after the repository-side implementation is testable. Do not alter the immutable base OS or perform live execution.

## New integration work opened 2026-11-07

- Core PR #46: native selected-recovery runtime integration; focused CI passed **924 tests**. Draft/unmerged; selected recovery remains default-off. Dust lifecycle remains deliberately deferred because it needs coordinated position/retirement changes.
- Hummingbot API PR #6: `SPECTRA_PAPER_ONLY` API mode. It restricts connector discovery/creation to `*_paper_trade`, permits only paper executor/order routes and paper connector provisioning without secrets, skips bots/Gateway/Docker, filters legacy live executor/portfolio state, and exposes authenticated `GET /spectra/runtime`. Latest focused API CI passed. Exact Hummingbot core revision remains unpinned, so connector compatibility and local execution are not yet certified.
- Core PR #47: first Spectra Finance UI branding slice. Browser title, shell wordmark, login, package metadata, and PWA manifest are rebranded while preserving Condor as the analysis subsystem. Frontend production build and Core focused CI both passed. Existing live-capable pages are not yet safe to expose as a Spectra installation until runtime status and paper-mode integration are connected.
- Core PR #49: governed paper-execution integration candidate. It adds a distinct paper state directory and binds Core proposals, capital snapshots, protected BUY checks, and executor authorization to `kraken_paper_trade`. Focused CI is running. Its end-to-end tests use synthetic market/portfolio evidence and a fake Hummingbot client; actual local Hummingbot paper connector compatibility and fill reconciliation remain to be verified.

## 7. Evidence and guardrails

- Core PR #44: https://github.com/andredepaulnash-cmd/spectra-finance-core/pull/44
- API audit PR #5: https://github.com/andredepaulnash-cmd/spectra-hummingbot-api/pull/5
- Core integration branch: `integration/protected-recovery-composition-20261107`
- Selected-recovery/dust support branch: `support/decision-accounting-dust-20261012`
- API audit branch: `audit/spectra-integration-readiness-20261107`

Never equate code presence with operational integration, passing focused CI with release readiness, a paper simulation with real market execution, or backtest results with expected future returns. Preserve existing operator-local Compose edits and backups during any future local synchronization.
