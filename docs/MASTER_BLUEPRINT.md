# Spectra Finance Alpha 2.0 — Master Blueprint

- Status: **Authoritative build specification**
- Mode: **Paper trading only**
- Supersedes: prior 50–83 position range and all earlier VCAP ladder drafts
- Branch: `spectra-finance-alpha-2.0`

## 1. Decisions and scope

### 1.1 Position-count resolution

**Decision: 74 is the configured operating target and the absolute hard ceiling.**

The earlier 50–83 range is retired. The system may run fewer than 74 positions when market eligibility, liquidity, risk, or capital availability does not justify another admission. It must never open or retain more than 74 concurrently open asset positions. The cap is enforced in Spectra Core and independently checked by CapitalGovernor at reservation time. It is not a target that overrides eligibility or capital controls.

### 1.2 Portfolio parameters

| Parameter | Alpha 2.0 value |
|---|---:|
| Simulated portfolio equity | $25,000 USDT |
| Initial-entry budget | $6,250 USDT (25%) |
| VCAP recovery pool | $18,750 USDT (75%) |
| Maximum concurrent positions | 74 |
| Initial entry per asset | $20 USDT |
| Maximum gross position notional per asset | $1,000 USDT |
| Maximum concurrently occupied VCAP shard slots per asset | 2 |
| Estimated Kraken paper buy fee | 0.40% |
| Estimated Kraken paper sell fee | 0.40% |
| Net take-profit target | +1.74% after estimated fees |

The $6,250 initial-entry allocation is a budget ceiling, not an instruction to spend it immediately. At 74 positions and $20 initial entries, initial deployment is $1,480. Unused initial budget remains uncommitted. Portfolio-wide capital authorization remains limited by the $25,000 simulated account and the separate initial/recovery pools.

### 1.3 Per-asset exposure reconciliation

The user-specified VCAP additions total $920 per asset:

- FAT 1: $20 + $40 + $80 + $160 = $300
- SKINNY: $30 + $30 + $40 + $40 = $140
- FAT 2: $160 + $320 = $480
- Total VCAP = $920

To make the $1,000 gross-notional cap mathematically enforceable while preserving the existing $20 initial-entry design, Alpha 2.0 assigns **$20 to each of the three conventional DCA additions** at -2.74%, -4.44%, and -6.74%.

Therefore, the complete maximum scheduled gross-notional path for one asset is:

`$20 initial + (3 × $20 DCA) + $920 VCAP = $1,000`

This DCA sizing is an explicit Alpha 2.0 engineering resolution, not a value supplied in the user's ladder table. It must be represented in configuration and tests. Fees are accounted separately from gross order notional and must also be reserved before submission. The $1,000 per-asset cap applies to cumulative gross buy notional across initial entry, DCA, and VCAP; reservations and submitted-but-unfilled orders count against it immediately. No order may cause the cap to be exceeded.

### 1.4 Portfolio-wide recovery capital

The $18,750 recovery pool is shared across the portfolio. A theoretical full $920 VCAP ladder for every one of 74 positions would require $68,080, which is more than the recovery pool. The system must never assume every asset can receive every ladder addition concurrently.

CapitalGovernor must reserve funds atomically, account for outstanding reservations and estimated fees, and deny or defer a ladder step when funds are unavailable. A denied step is not silently skipped or resized. It remains pending for reevaluation while the position remains inside its permitted phase. At the -24% boundary, all automatic patching stops and the position is flagged for manual review. No capital is fabricated, borrowed, or taken from another pool without an explicit policy.

## 2. Component architecture and authority

### 2.1 Research scanner
- Reads public market metadata and public market data only.
- Applies configured universe, quote-asset, liquidity, spread, volume, market-age, and exclusion filters.
- Emits timestamped, reproducible candidate records with source and data-freshness metadata.
- Cannot create positions, reserve capital, or submit orders.

### 2.2 Condor
- Consumes scanner candidates and market context.
- Scores, ranks, explains, and recommends eligible assets.
- Emits a recommendation with symbol, venue, market identifiers, score, rationale, timestamp, and expiry.
- Cannot reserve capital, call an order endpoint, or submit/cancel orders.
- A recommendation is advisory and never constitutes execution authorization.

### 2.3 Spectra Core
- Owns the deterministic portfolio lifecycle and canonical position state.
- Enforces the 74-position hard ceiling, duplicate-asset rules, position state transitions, DCA/VCAP thresholds, take-profit evaluation, and replacement admission.
- Converts accepted recommendations into proposed actions for CapitalGovernor.
- Does not bypass CapitalGovernor.

### 2.4 CapitalGovernor
- Is the sole authority that may authorize capital reservation/deployment.
- Validates paper-only mode, portfolio and per-asset budgets, 74-position ceiling, active VCAP shard slots, idempotency key, market freshness, action eligibility, and fee reserve.
- Creates a durable, single-use authorization/reservation before execution.
- Denies by default on missing data, stale authorization, conflicting state, insufficient funds, or failed invariants.
- Every authorization is scoped to one asset, one action, one amount, one order side/type, one expiry, and one idempotency key.

### 2.5 Hummingbot paper executor
- Uses an isolated paper-trading connector with public market data.
- Accepts order commands only when a valid, unexpired, unused CapitalGovernor authorization is attached and verified.
- Cannot mint or broaden authorizations.
- Cannot load private exchange credentials. No private API key/secret/passphrase fields or credential discovery flow are included in Alpha 2.0.
- Reports exchange-simulated order IDs, fills, fees, and terminal states to the reconciler.
- If the paper connector cannot satisfy an interface or safety requirement, execution remains disabled until a verified alternative is implemented.

### 2.6 Ledger and reconciliation
- Maintains an append-only event history for reservations, submissions, fills, fees, cancellations, releases, shard transitions, and realized P&L.
- Reconciles execution reports idempotently.
- Separates gross notional, buy fees, sell fees, net proceeds, realized P&L, and unrealized P&L.
- Persists sufficient state to recover after restart without duplicate orders or lost reservations.

## 3. Mandatory paper-only security invariants

Every service and executable entry point must require:

- `SPECTRA_PAPER_ONLY=true`
- `SPECTRA_ALLOW_LIVE_EXECUTION=false`

Both settings must be validated at startup and at every order boundary. Missing or invalid values fail closed. A contradictory setting, live connector name, live order route, or credential-bearing configuration causes startup or order submission to be rejected.

Additional invariants:
1. No private exchange credentials are requested, accepted, persisted, mounted, or loaded.
2. Public market data is read-only.
3. The only configured execution connector is a verified paper connector.
4. Condor has no order-submission capability.
5. Hummingbot requires CapitalGovernor authorization for every simulated order.
6. Authorization is single-use, amount-bounded, action-bounded, expiring, and idempotent.
7. Paper mode is enforced in the CLI, API, Core, Governor, executor, and deployment configuration.
8. No "switch to live" feature or live-execution environment variable is implemented.
9. Tests must prove negative behavior: missing flags, false paper flag, true live flag, absent authorization, expired authorization, reused authorization, and mismatched amount all reject.
10. Logs and diagnostics must never print credentials; the Alpha 2.0 product must not contain credential fields in its config schema.

## 4. Position lifecycle and limits

- One canonical asset position per venue/market identity unless an explicitly reviewed future design permits otherwise.
- Maximum 74 concurrently open asset positions.
- Initial order notional: $20.
- Maximum cumulative gross buy notional per asset: $1,000.
- Every pending reservation and submitted-but-unfilled order consumes the relevant portfolio and per-asset budget.
- A market recommendation must be revalidated for freshness and eligibility before admission.
- Position closure releases exposure only after execution and ledger reconciliation.
- Replacement admission happens only after the prior closure is reconciled and capital is actually available.
- Restart recovery must restore open positions, outstanding reservations, orders, fills, shards, and pending actions before new admissions are permitted.

## 5. Conventional DCA phase

DCA applies from 0% down to, but not including, -7.77% drawdown from the position's immutable reference price.

| Trigger drawdown | Gross addition | Phase |
|---:|---:|---|
| -2.74% | $20 | DCA 1 |
| -4.44% | $20 | DCA 2 |
| -6.74% | $20 | DCA 3 |

Each threshold may trigger at most once per position reference-price epoch. Additions are separately identified ledger events and count toward the $1,000 per-asset gross-notional ceiling. DCA never resets the reference price used to calculate drawdown. If a step cannot be funded, it remains pending; later steps cannot leapfrog it. VCAP activates at -7.77%.

## 6. Exact VCAP ladder

Drawdown is measured against the position's immutable reference price. Adding capital, filling a shard, recycling a shard, or realizing a partial exit must not reset that reference price.

| Drawdown | Gross addition | Phase |
|---:|---:|---|
| -7.77% | $20 | FAT 1 |
| -9.74% | $40 | FAT 1 |
| -10.74% | $80 | FAT 1 |
| -11.74% | $160 | FAT 1 — final step before SKINNY |
| -12.74% | $30 | SKINNY |
| -14.24% | $30 | SKINNY |
| -15.74% | $40 | SKINNY |
| -17.24% | $40 | SKINNY — final step before FAT 2 |
| -19.24% | $160 | FAT 2 |
| -21.74% | $320 | FAT 2 |
| -24.00% | No addition | Outer boundary: stop all automatic patching and flag manual review |

At -24.00% or below:
- Stop all automatic patching for that asset.
- Flag it for manual review and surface the reason and current exposure.
- Do not reset the reference price.
- Do not bypass the two-shard concurrency limit.
- Do not liquidate automatically solely because the boundary was reached.
- Do not silently discard pending ledger events or outstanding orders; reconcile/cancel according to their existing authorization and record the outcome.

No additional ladder steps, percentage interpolation, doubling beyond -21.74%, or automatic additions below -24% are permitted.

## 7. VCAP shard model

Each VCAP addition is represented by an independently tracked shard. A shard record includes at minimum:
- immutable shard ID and position ID
- ladder step ID, phase, and generation/recycle number
- target drawdown and authorized gross notional
- reserved amount and reservation ID
- submitted order ID and execution venue
- filled quantity and average fill price
- gross cost basis, buy fees, and remaining quantity
- realized proceeds, sell fees, realized P&L, and unrealized P&L
- status, timestamps, idempotency keys, and full event history

Lifecycle:

`Available → Reserved → Submitted → Filled → Active → Resolved → Recycled`

Partial fills are represented explicitly; they do not create duplicate shards. Failed/rejected/cancelled submissions return to Available only after reservation release is confirmed and recorded. A shard is not Resolved until its remaining quantity is zero and all proceeds/fees are reconciled. Recycling creates a new generation while preserving the prior generation's immutable history and accounting.

**Concurrency rule:** no more than two shards per asset may occupy a capital-bearing/in-flight state at once. For race safety, Reserved, Submitted, Filled, and Active all consume one of the two slots. Slot release occurs only after a shard is fully Resolved and its reservation/order state is reconciled. This is a per-asset rule, not a portfolio-wide limit.

A shard's accounting is never blended into a single undifferentiated position cost record. The parent position may expose aggregate reporting, but all source data remains attributable to individual shards and the initial entry.

## 8. Take-profit and fee accounting

Net take-profit target is +1.74% after estimated Kraken paper fees of 0.40% on buys and 0.40% on sells.

For each shard, maintain gross purchase notional, buy fee, quantity, gross sale proceeds, sell fee, and net realized proceeds. For a proposed sale, calculate expected net proceeds after the 0.40% sell fee and compare against the shard's fee-inclusive cost basis. A shard is eligible for take-profit only when projected net proceeds meet at least 1.0174 times its fee-inclusive cost basis, subject to rounding, tick size, lot size, and available liquidity. Fees are estimates in paper mode and must be recorded as separate ledger entries. Do not report gross price appreciation as net profit.

The position reference price for DCA/VCAP drawdown remains separate from each shard's cost basis and take-profit calculation.

## 9. Order authorization and idempotency contract

Every order command must include:
- authorization ID issued by CapitalGovernor
- unique idempotency key
- position ID and (for VCAP) shard ID
- exact market identity and side
- exact authorized gross notional/quantity bounds
- paper connector identity
- issue time and expiry

Before execution, Hummingbot verifies the authorization with CapitalGovernor or a signed, verifiable authorization artifact. A command is rejected if any field differs, if the authorization is expired/revoked/used, or if paper-only invariants fail. The executor must not infer authorization from a recommendation or an API caller's identity.

Order submission, fill handling, and ledger updates are idempotent. A repeated request returns the prior result or is rejected; it never creates a second order.

## 10. State, persistence, and restart recovery

Persist:
- configuration and policy version
- simulated portfolio balances and pool allocations
- all positions and immutable reference prices
- reservations and authorization consumption
- all orders, fills, and fee records
- all DCA trigger states
- all VCAP shards and generation histories
- candidate/recommendation provenance
- event offsets/checkpoints and health status

On startup:
1. validate paper-only settings before connecting any execution component;
2. load and integrity-check persisted state;
3. reconcile all previously submitted paper orders/fills;
4. restore reservation and shard concurrency counts;
5. verify capital and exposure invariants;
6. only then allow scanning/admission to resume.

If reconciliation or invariant validation fails, enter safe halted mode: no new orders or admissions. Preserve state and report the exact inconsistency.

## 11. System operations and observability

Minimum operational capabilities:
- start, stop, restart, status, health, logs, doctor, backup, restore, update
- dashboard showing paper-mode status, account equity, free/committed initial capital, recovery pool, open positions (n/74), per-asset exposure, DCA state, VCAP phase/shards, open orders, fills, fees, net P&L, and manual-review flags
- health checks for scanner, Condor, Core, Governor, database, broker, and paper executor
- structured logs with correlation IDs and no secrets
- safe shutdown that stops new admissions, resolves/cancels only as authorized, persists state, and exits cleanly

No additional autonomous agents are required for Alpha 2.0.

## 12. Acceptance criteria

A release is not complete until all are demonstrated:

1. Clean Bazzite installation on the target machine using documented commands.
2. Paper-only settings enforced at every layer; negative safety tests pass.
3. No exchange credential fields or private credential flow exist.
4. 74-position hard ceiling enforced under concurrent/racing admissions.
5. 10, 25, 50, then 74-position staged paper simulations pass.
6. Per-asset cumulative gross buy notional never exceeds $1,000, including pending reservations.
7. Exact DCA and VCAP threshold/amount sequence is tested, including boundary values.
8. No more than two capital-bearing/in-flight VCAP shards per asset, including concurrency tests.
9. -24% boundary blocks all new automatic patching without resetting reference price or auto-liquidating.
10. Order-to-fill-to-fee-to-ledger reconciliation is idempotent.
11. Net take-profit is calculated after buy and sell fees.
12. Restart recovery preserves every position, shard, reservation, order, fill, and reference price without duplicate execution.
13. End-to-end autonomous paper loop runs continuously for a defined soak test with no invariant violations.
14. Reproducible package, pinned dependencies, backup/restore, rollback, and operating instructions are verified.

Passing unit tests alone does not certify the application. Every acceptance item must have named evidence and an explicit pass/fail/not-run result.
