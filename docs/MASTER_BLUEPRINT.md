# Spectra Finance Alpha 2.0 — Master Blueprint

- Status: **Current Alpha 2.0 specification and implementation boundary**
- Mode: **Paper trading only**
- Development branch: `spectra-finance-alpha-2.0`
- Older Alpha/Condor branches: **reference and code-mining only; not development bases**

## 1. Confirmed portfolio and execution policy

| Parameter | Confirmed value |
|---|---:|
| Simulated portfolio equity | $25,000 USDT |
| Initial-entry pool | $6,250 USDT (25%) |
| Recovery pool | $18,750 USDT (75%) |
| Concurrent position hard ceiling | 74 |
| Initial entry | $20 USDT |
| Maximum cumulative gross buy notional per asset | $1,000 USDT |
| Concurrent capital-bearing VCAP shards per asset | 2 |
| Kraken paper buy fee assumption | 0.40% |
| Kraken paper sell fee assumption | 0.40% |
| Ordinary TOP_GAINER fixed take-profit | +1.74% net after fees |
| Automatic patch outer boundary | -24% from immutable reference; manual review |

The $6,250 initial-entry ceiling is shared collectively by ENTRY, PRESS, and ROTATION. RECOVERY is outside that pool and uses the separate recovery allocation. The pool ceiling applies to aggregate reservations and spending; strategy labels do not create additional capital.

The initial-entry pool is a ceiling, not an instruction to spend all available funds. The system must not open a position solely to fill the 74-position target. It may operate below 74 when market quality, eligibility, liquidity, or available capital do not justify another entry.

### Purchase and sale order types

- **Every purchase is a MARKET BUY**, including initial entries and any future VCAP recovery/patch purchase.
- LIMIT BUY and resting price-targeted buy orders are prohibited.
- LIMIT orders are reserved for authorized exit sells.
- MARKET BUY base quantity must be sized against a signed maximum execution-price estimate and the exact Governor quote reservation. The actual fill plus fee is independently checked during settlement.
- A MARKET BUY request must not contain a LIMIT price.
- A recommendation is never an execution authorization.

## 2. Discovery strategy

### Primary entry pathway: quality-filtered 24-hour gainers

The primary discovery strategy is to identify reasonably solid assets among 24-hour gainers, then evaluate their market quality before entry. It is not indiscriminate buying of low-quality tokens.

Confirmed entry constraints include:
- USDT spot markets on the keyless Kraken paper/data integration.
- At least $10,000,000 in 24-hour quote volume for entry eligibility.
- Fresh ticker data.
- Exchange trading rules compatible with the $20 initial order.
- A configured maximum top-of-book spread.
- Positive 24-hour price change, calculated from public hourly candle history rather than today's opening price or volume rank.
- Condor ranks and explains candidates; it cannot reserve capital or submit orders.

The $10M figure is the entry-liquidity floor. It is not a research-only threshold.

### Separate research pathway: 24-hour top losers and possible reversal

A distinct top-loser research pathway may identify assets with at least $1,000,000 in 24-hour quote volume. That lower floor is for research, not entry authorization. Entry from this pathway requires stronger liquidity, including the $10M entry-liquidity floor, and flexible reversal evidence using indicators such as RSI and BBWP. Do not convert those indicators into rigid, unapproved thresholds.

A top-loser candidate is not automatically an entry. The pathway must preserve its evidence, source, timestamps, and strategy origin.

### Strategy origin is mandatory provenance

Every position, reservation, buy request, fill, and later exit decision must retain its origin:
- `TOP_GAINER`
- `TOP_LOSER_REVERSAL`

This distinction is a policy boundary, not a display label.

## 3. Exit strategy

### Ordinary TOP_GAINER positions

Ordinary 24-hour gainer entries use the confirmed fixed +1.74% net take-profit after the estimated 0.40% buy fee and 0.40% sell fee. The target is calculated from fee-inclusive lot cost basis. Do not report gross price appreciation as net profit.

The fixed target is a LIMIT SELL. It must not be submitted as a resting order before the current bid reaches the fee-aware target.

### TOP_LOSER_REVERSAL positions

Adaptive DTTP/DTSL applies **only** to positions whose entry originated from the 24-hour top-losers reversal pathway and showed possible reversal evidence. It is not a global exit trigger for ordinary entries.

The position's strategy origin must be checked at authorization time. TOP_LOSER_REVERSAL positions must not be routed through the ordinary fixed take-profit path. The precise DTTP/DTSL ratchet formulas, activation conditions, and state transitions must be implemented from the approved strategy specification; they must not be guessed.

## 4. VCAP — independent recovery strategy, not DCA

VCAP is an independently governed, reactive shard-selection and recovery strategy. It is not conventional DCA and must not be implemented as a DCA ladder followed by VCAP.

Confirmed structural constraints:
- Recovery uses the separate $18,750 pool.
- No more than two capital-bearing or in-flight shards per asset may exist concurrently.
- Shards require independent identity, reservation, order, fill, cost-basis, fee, realized-P&L, lifecycle, and recycling records.
- The drawdown reference remains immutable; fills, partial exits, and recycling do not reset it.
- The -24% outer boundary stops automatic patching and flags manual review. It does not automatically liquidate a position.

**Not yet approved/configured:** exact VCAP shard-selection criteria, batch sizing, drawdown triggers per shard, sequencing, concurrency-release conditions, and recycling/re-entry policy. Until those rules are supplied and tested, CapitalGovernor must refuse VCAP recovery authorizations. No DCA purchases are part of Alpha 2.0.

Any older VCAP ladder tables or DCA calculations are provisional historical material, not active execution policy.

## 5. Architecture and authority boundaries

1. **Research scanner** — public market data only; discovers and timestamps candidate markets.
2. **Condor** — scores and explains; recommendation-only, no execution methods.
3. **Spectra Core** — owns portfolio lifecycle, strategy origin, position state, and deterministic orchestration.
4. **CapitalGovernor** — sole authority for capital reservation and authorization; fail closed on invalid or missing state.
5. **Hummingbot paper executor** — executes only signed, single-use, unexpired Governor-authorized simulated orders.
6. **Ledger/reconciler** — records reservations, order IDs, fills, fees, shard/lot basis, realized P&L, and restart state.

The assistant/controller may coordinate research and engineering, but it must not directly control live capital. The Architect retains final authority. Alpha 2.0 has no live execution mode and no private exchange credential workflow.

## 6. Paper-only security invariants

Every service and order boundary requires:
- `SPECTRA_PAPER_ONLY=true`
- `SPECTRA_ALLOW_LIVE_EXECUTION=false`
- `SPECTRA_PAPER_CONNECTORS=kraken_paper_trade`

Missing, malformed, contradictory, or unsafe values fail closed. No private exchange credentials are requested, stored, loaded, mounted, or accepted. Public Kraken candle data may use the public, read-only `kraken` candle feed; all order execution remains on `kraken_paper_trade`.

Every buy authorization is:
- single-use and idempotent;
- scoped to one position, strategy origin, asset, action, base quantity, quote reservation, and signed maximum execution-price estimate;
- expiring and bound to the exact order body;
- reconciled against actual fills and fees before capital is released.

Every sell authorization is scoped to an active lot, quantity, minimum authorized price, strategy origin, and paper connector. A missing or mismatched authorization must be rejected by both Core and the API boundary.

## 7. Persistence and restart safety

Persist:
- strategy origin and immutable reference price for every position;
- capital pools, reservations, and authorization consumption;
- orders, order IDs, fills, fees, and reconciliation state;
- per-lot cost basis, remaining quantity, and realized P&L;
- VCAP shard lifecycle and generation history when VCAP policy is approved;
- reversal evidence and DTTP/DTSL state for TOP_LOSER_REVERSAL positions when that pathway is enabled;
- event history, policy version, and health status.

On startup: validate paper-only mode, load and integrity-check state, reconcile previously submitted orders, restore reservations and open positions, validate exposure invariants, and only then resume scanning. Any unresolved order or invariant failure halts new admissions.

## 8. Current implementation boundary

Implemented or actively being validated:
- paper-only Core/API separation and signed order receipts;
- $25,000 initial/recovery pool accounting and 74-position ceiling;
- $20 initial entry and $1,000 per-asset gross cap;
- gainer-first scanner using public hourly candles, a $10M entry-liquidity floor, freshness, spread, and exchange rules;
- Condor momentum/liquidity/spread ranking;
- MARKET BUY construction and Governor/API enforcement;
- strategy-origin persistence;
- fixed +1.74% net LIMIT SELL path for TOP_GAINER positions;
- lot-level fill/fee accounting and reconciled position closure;
- -24% manual-review boundary.

Intentionally blocked pending approved rules:
- automatic VCAP shard selection, batching, sizing, and recycling;
- TOP_LOSER_REVERSAL discovery/entry and its DTTP/DTSL ratchet engine;
- any unapproved DCA pathway;
- live execution (not part of Alpha 2.0).

## 9. Release acceptance criteria

A release is not complete until evidence demonstrates:
1. Deterministic domain tests pass.
2. Clean Compose install passes with the actual full paper strategy cycle.
3. Paper-only negative tests reject unsafe configuration and unsigned/mismatched orders.
4. MARKET BUY contract is enforced end-to-end.
5. Gainer discovery uses true 24-hour price change and the $10M entry-liquidity floor.
6. Fixed net take-profit is applied only to TOP_GAINER positions and reconciles fees/lot basis.
7. TOP_LOSER_REVERSAL positions cannot enter the ordinary fixed take-profit path.
8. -24% boundary flags manual review and prevents automatic recovery.
9. VCAP remains disabled until its approved policy is implemented and tested.
10. Restart/replay, partial fills, order uncertainty, and idempotency tests pass.
11. Staged 10/25/50/74-position paper capacity tests pass.
12. Clean Bazzite install, upgrade, backup/restore, and rollback are verified on the Architect's machine.

A passing unit suite is not a completed release. CI, clean-install integration, end-to-end paper cycle, soak test, and actual Bazzite verification are separate evidence categories.
