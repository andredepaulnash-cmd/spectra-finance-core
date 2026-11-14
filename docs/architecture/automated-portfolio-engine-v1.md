# Spectra Finance — Automated Portfolio Engine v1

**Status:** Reconstructed baseline specification; implementation and runtime verification pending  
**Purpose:** Restore the intended autonomous portfolio-selection and trading workflow from surviving Spectra source. This document is a working baseline. Strategy thresholds and portfolio parameters may be tuned later; they must not be silently replaced with generic defaults.

## 1. Product objective

Spectra Finance is an automated portfolio-management and trading system, not a chat interface that requires the user to submit each order manually.

The system continuously:
1. Reads account balances, available quote capital, open positions, active orders, and portfolio equity.
2. Discovers and evaluates eligible markets across a candidate asset universe.
3. Ranks assets and strategy opportunities using Spectra's asset-fingerprinting and trading logic.
4. Selects, sizes, admits, and allocates positions subject to the Capital Governor and portfolio constraints.
5. Deploys and supervises strategy controllers through Hummingbot.
6. Manages entries, exits, stop conditions, take-profit, recovery, and capital recycling.
7. Repeats the process as market data, balances, positions, and opportunities change.

The user's role is to define objectives, configure policy, and retain reserved authority. The system may autonomously investigate, rank, propose, and execute within the explicitly authorized mode and governance envelope. It must never infer permission for live-capital execution from paper-mode authorization.

## 2. Recovered portfolio baseline

The surviving rotating-fleet research protocol documents the following starting design:

| Parameter | Recovered baseline |
|---|---:|
| Candidate universe | 83 assets |
| Simultaneous fleet target | Up to 83 unique asset positions; target is to keep all 83 slots occupied |
| Initial position entry | $20 per position |
| Paper account starting equity | $25,000 USD-equivalent |
| Portfolio reserve | 25% |
| Maximum equity basis per asset | 5% |
| Selection | Causal momentum / opportunity ranking |
| Portfolio model | Shared account balance and shared capital constraints |
| Lifecycle | Replace eligible exited assets from the candidate universe |

These are the user's confirmed working baseline for the reconstructed engine. Preserve them as explicit configurable policy defaults and expose the effective values in the UI and runtime state. The paper account starts with $25,000 USD-equivalent simulated equity. Each newly opened position starts with a $20 initial entry allocation; this is the initial tranche, not a hard cap on the total position. Additional DCA/VCAP tranches and lifecycle capital must be planned separately by the Capital Governor against the shared account ledger, reserve, and risk limits. The fleet target is 83 simultaneous unique positions, with an immediate governed replacement cycle whenever a position closes or a slot becomes vacant. Never silently change these parameters.

The fleet must not allocate the same asset twice through competing controllers unless the portfolio policy explicitly permits strategy-level aggregation. Position sizing must use the shared account ledger, not independent per-controller copies of the same balance.

## 3. System responsibilities

### Spectra Core — orchestration and governance
- Own the authoritative portfolio state and policy decisions.
- Coordinate market discovery, candidate evaluation, ranking, admission, sizing, deployment, monitoring, and replacement.
- Enforce mode, account, connector, risk, reserve, exposure, and authorization boundaries.
- Maintain durable decision, order, fill, position, and lifecycle records.
- Reconcile Hummingbot-reported execution state against the authoritative ledger.
- Expose actionable health and failure states rather than presenting a green status when a required dependency is unavailable.

### Condor — intelligence and strategy selection
- Analyze market and portfolio context.
- Apply the user's trading logic and asset-fingerprinting criteria.
- Rank eligible assets and strategy opportunities.
- Select among supported strategy families and recommend or initiate governed lifecycle actions.
- Explain each selection with its inputs, score, exclusions, and rationale.
- Intelligence is not a separate authority over capital policy; Core governance remains authoritative.

### Hummingbot — execution and strategy runtime
- Run the approved controllers and strategy processes continuously.
- Operate exchange connectors and supported Gateway/DEX integrations.
- Create, track, and manage orders and executor lifecycles.
- Report fills, cancellations, failures, fees, positions, and balances.
- Support controller start, stop, status, restart, and recovery as real runtime operations.

A registered tool or visible controller template is not proof of a running controller. Deployment must be confirmed by Hummingbot runtime state and reconciled execution records.

## 4. Automated operating loop

The engine runs as a persistent, restart-safe orchestration loop:

1. **Account snapshot:** obtain connector identity, account identity, balances, equity, available quote capital, open orders, positions, and recent fills.
2. **Market discovery:** retrieve current, timestamped market metadata and price/volume data for the configured candidate universe. Reject stale, missing, malformed, or unsupported data; never substitute fabricated prices.
3. **Eligibility filters:** apply market availability, liquidity, spread, volume, connector capability, asset status, and user-defined exclusions.
4. **Asset fingerprinting:** compute the configured asset features and fingerprint from reproducible, timestamped inputs.
5. **Opportunity ranking:** score and rank eligible assets using the recovered causal ranking/trading logic. Preserve score components and reasons for rejection.
6. **Strategy selection:** evaluate the applicable strategy families, including EMA trend selection and adaptive grid strategies. Strategy choice must be explainable and constrained by market regime and connector support.
7. **Portfolio construction:** select up to 83 unique eligible assets, targeting 83 simultaneously active positions, subject to shared balance, reserve, exposure, concentration, and existing-position constraints. Each new position begins with a $20 initial entry tranche; DCA/VCAP additions are separate governed allocations.
8. **Capital admission:** request a governed allocation from the Capital Governor. No controller may reserve or spend capital outside this admission process.
9. **Controller deployment:** start the selected Hummingbot controller with an unambiguous account, connector, pair, strategy, configuration, and Core-owned controller identity.
10. **Execution supervision:** track executor/order state, fills, fees, realized and unrealized PnL, stop/target behavior, and strategy health.
11. **Lifecycle management:** evaluate exits, protected-profit continuation, recovery/re-entry, partial exits, capital recycling, and replacement candidates through the appropriate VCAP lifecycle components and policy gates.
12. **Reconciliation:** reconcile balances, orders, fills, and positions after each material event and on a periodic schedule. Detect and quarantine discrepancies.
13. **Fleet rotation:** maintain a target of 83 simultaneous active positions. When a position closes or a slot becomes vacant, immediately rescore the candidate universe and start the next eligible replacement through the Governor, without waiting for a manual chat instruction and without violating portfolio limits. If no candidate is eligible or market/account data is unavailable, keep the slot visibly vacant, report the exact blocker, and retry on the next scheduled/event-driven scan; never force a bad trade merely to fill a slot.
14. **Persistence and restart recovery:** persist loop checkpoints and controller/position mappings. On restart, reconstruct state from durable records and exchange/runtime truth before deploying or modifying anything.

The scheduler must support configurable scan, refresh, reconciliation, and strategy-specific intervals. It must prevent overlapping runs from double-allocating capital or deploying duplicate controllers.

## 5. Strategy families

### EMA trend selection and controller deployment
- Evaluate EMA-based trend candidates using the strategy's configured timeframes and parameters.
- Compare candidates using the strategy's documented evaluation metrics and risk constraints.
- Rank and select eligible pairs; do not deploy every candidate automatically.
- Deploy the selected controller configurations through the real Hummingbot controller runtime.
- Monitor controller health and position lifecycle; stop or replace controllers only through governed transitions.
- Retain evaluation evidence and deployment rationale.

### Adaptive grid strategies
- Use the recovered adaptive-grid strategy configuration and its market/regime inputs.
- Support dynamic range and grid adjustment using the strategy's configured volatility/range logic.
- Track grid orders, fills, inventory, realized/unrealized PnL, stale-grid conditions, and risk limits.
- Reprice, take profit, pause, stop, or redeploy only through governed lifecycle operations.
- Do not treat a static grid configuration or a one-time executor submission as an operating adaptive-grid strategy.

### Additional strategies
The architecture must remain extensible to the other existing Hummingbot strategies, controllers, exchange connectors, Gateway/DEX integrations, and research modules. This reconstruction does not authorize deleting or hard-disabling existing capabilities.

## 6. Shared portfolio and capital governance

The Capital Governor is the single admission and capital-authority boundary.

It must:
- Maintain one authoritative account-level view of total equity, free balances, reserved balances, open exposure, and pending commitments.
- Enforce the configured reserve (baseline: 25%) before new allocations; the paper account begins with $25,000 USD-equivalent simulated equity.
- Enforce the configured per-asset equity cap (baseline: 5%) across all strategies/controllers. A $20 initial entry is the starting tranche only; subsequent DCA/VCAP tranches require fresh governed sizing and must remain within aggregate exposure limits.
- Prevent double-spending, duplicate reservations, and oversubscription from concurrent controller requests.
- Account for fees, slippage, partial fills, pending orders, and unsettled proceeds.
- Apply strategy-specific and portfolio-wide risk limits.
- Record every approval, rejection, reservation, release, and policy reason.
- Fail closed on missing/stale account data or unresolved ledger discrepancies.

All controllers share the same portfolio ledger. A controller's local view is informational and cannot supersede Core's authoritative balance or allocation decision.

## 7. VCAP and lifecycle integration

VCAP is part of the position lifecycle, not a detached dashboard label. Its implementation and integration status must be reported honestly.

The runtime should compose the existing VCAP components for:
- Entry admission and tranche planning.
- Position and tranche accounting.
- Profit-floor / protected-profit continuation.
- Exit evaluation and execution.
- Replanning after partial fills, price movement, or changed market conditions.
- Recovery and capital recycling where policy allows.
- Restart continuity and reconciliation.

Each lifecycle transition must be explicit, persisted, idempotent, and auditable. A module marked partial, unverified, or not implemented must not be presented as fully active. Unverified components remain gated until their contracts and tests establish safe behavior.

## 8. Execution modes and authorization

- Paper mode is the default development and verification mode.
- Paper mode must use an isolated simulated execution runtime/account. The user does not require a particular exchange/connector brand or display name (the connector may be called anything); what matters is that the actual dispatch, balances, fills, and ledger are genuinely simulated and technically isolated from live capital. Resolve aliases to a verified paper backend identity at the dispatch boundary; never infer paper safety from a label.
- Connector identity, account identity, and mode must be validated at every dispatch boundary.
- Live mode remains a supported product mode; it must not be removed or permanently hard-disabled as a shortcut.
- Live execution requires a separate, explicit user authorization and the existing governance checks. Paper authorization never grants live authority.
- The UI must clearly show active mode, connector, account, runtime health, and whether execution is enabled.
- A chat response, tool registration, approval card, or controller template is not evidence of an executed order or running strategy.

## 9. Required runtime and UI capabilities

The application must expose:
- Candidate universe: all 83 configured candidates, eligibility, market-data freshness, and ranking.
- Fleet: target of 83 simultaneous active positions, 83-asset candidate universe, active assets, vacant slots, strategy/controller per asset, initial $20 entries, DCA/VCAP tranche exposure, and immediate replacement queue.
- Portfolio: balances, equity, reserve, available capital, committed capital, exposure, fees, and PnL.
- Intelligence: asset fingerprints, ranking scores, strategy selection, and decision rationale.
- Controllers: deployed/running/stopped/error states, configuration, connector/account, heartbeat, and last action.
- Execution: orders, fills, cancellations, executor state, and reconciliation status.
- Lifecycle: VCAP state, tranches, protective levels, exit/recovery decisions, and capital returned to the portfolio.
- Automation: last scan, next scheduled scan, active loops, failures, retries, and pause/resume controls.
- Governance: policy limits, approvals, denials, authorization scope, and audit trail.

The interface must distinguish **configured**, **available**, **authorized**, **running**, **healthy**, and **verified**. These states are not interchangeable.

## 10. Failure handling and safety

- Missing market data: skip affected markets and report the cause; never synthesize a price.
- Stale account snapshot: block new capital commitments until refreshed.
- Connector mismatch: reject dispatch before order creation.
- Controller crash: mark unhealthy, preserve state, reconcile open orders/positions, then recover according to policy.
- Duplicate or replayed request: reject idempotently.
- Partial fill: update reserved/committed capital and position quantities from confirmed fills.
- Ledger mismatch: pause affected allocations, reconcile from authoritative connector/runtime evidence, and retain an incident record.
- Governor unavailable: no new entries or capital-changing actions.
- Risk threshold breach: execute the configured protective policy and record the transition.
- Restart: recover from persisted state plus connector truth; do not blindly resubmit prior orders.
- All errors should trigger diagnosis, targeted repair, and regression tests where the development workflow permits. Never claim success without verified evidence.

## 11. Acceptance criteria

The automated portfolio engine is not complete until all of the following pass in isolated paper mode:

1. Starts with a $25,000 USD-equivalent simulated paper-account balance and reads/reconciles balances and positions from the selected isolated paper runtime.
2. Loads the configured 83-asset candidate universe and reports the actual market-data coverage/freshness.
3. Computes and displays reproducible asset fingerprints and opportunity rankings.
4. Selects and maintains a target fleet of 83 simultaneous unique eligible positions from the 83-asset candidate universe, subject to policy and available capital; each position begins with a $20 initial entry tranche.
5. Applies the shared reserve and per-asset exposure limits across concurrent strategies.
6. Selects and deploys the appropriate EMA/adaptive-grid controller through Hummingbot's actual runtime.
7. Keeps controllers running and evaluating without requiring a chat message for each order.
8. Creates, manages, and closes paper orders/positions through Hummingbot.
9. Reconciles every confirmed fill, fee, balance, position, and lifecycle transition into the shared ledger.
10. Performs governed exits, DCA/VCAP tranche management, recovery/recycling, and immediate replacement of closed positions to restore the 83-position target whenever eligible candidates and safe account conditions permit.
11. Survives service restart without duplicate orders, lost positions, or double allocation.
12. Shows accurate controller, automation, portfolio, and execution state in the UI.
13. Demonstrates that paper dispatch cannot reach a live connector.
14. Demonstrates that live mode remains available as a separately governed mode but cannot be entered without explicit user authorization.

A single successful market order does not satisfy these criteria. A passing unit test, mocked executor, registered tool, or green UI indicator alone does not prove end-to-end automation.

## 12. Known integration issues to verify during implementation

The following are investigation leads, not claims that the current runtime has been re-tested by this document:

- The Intelligence tool proposal shown in the local UI used connector name `kraken`, while the UI banner requires an isolated simulated connector. The display name is not important; verify that aliases resolve to a truly isolated simulated backend and that no live connector can be reached.
- Prior source review found a mismatch between registered executor tools and Core's governed executor dispatch allowlist. Verify all supported executor types against the actual Hummingbot API.
- Prior source review found executor controller identity being overwritten with a generic `spectra` value. Preserve a stable, Core-owned mapping between conversation/strategy, controller, executor, account, and asset.
- Controller templates/configuration must be distinguished from deployed, running Hummingbot controllers.
- The API paper-runtime specification describes authorization and dispatch work but does not, by itself, establish complete fill, balance, history, and lifecycle reconciliation.

## 13. Implementation sequence

1. **Recover and freeze contracts:** inventory existing implementations and tests for rotating fleet, asset fingerprinting, market discovery, Capital Governor, EMA controller, adaptive grid, VCAP, Hummingbot runtime, and ledger.
2. **Make paper account truth reliable:** connector/account identity, balances, market data, order book, order dispatch, fills, fees, and reconciliation.
3. **Wire the persistent controller runtime:** real start/stop/status/heartbeat/restart and executor lifecycle, not templates alone.
4. **Connect the automated selection loop:** 83 candidates → eligibility → fingerprint/ranking → fleet selection → governor admission → controller deployment.
5. **Connect lifecycle automation:** monitoring, exits, VCAP, capital recycling, and fleet replacement.
6. **Complete UI observability and controls:** show the actual state of each loop, controller, position, and ledger.
7. **Run end-to-end paper acceptance tests:** prove the full loop over time and through restarts before declaring the product operational.

Do not begin by replacing the recovered strategy logic with a new generic strategy. First inventory and compose the existing code. Preserve existing capabilities and make the smallest integration changes that satisfy the acceptance criteria.

---

**Working principle:** Spectra should automate the trading workflow the user designed. The user sets the objectives and retains authority; Condor supplies intelligence; Spectra Core governs and coordinates; Hummingbot continuously executes and manages the approved strategies.
