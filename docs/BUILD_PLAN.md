# Spectra Finance Alpha 2.0 — Build Plan and Evidence Register

## Development rules

- The sole Core development base is `spectra-finance-alpha-2.0`.
- The sole API development base is `spectra-finance-alpha-2.0` in `spectra-hummingbot-api`.
- Desktop/release branches are packaging candidates only and must be synchronized with the dedicated development branches before release.
- Older Alpha/Condor branches are reference libraries only. Reuse code only after review against this blueprint.
- Preserve local data, configuration, source changes, named volumes, and unrelated services. No destructive upgrade path.
- Paper-only remains mandatory. No live trading credentials or live execution mode.

## Phase 0 — Strategy and policy freeze

### Confirmed
- [x] 74 concurrent position hard ceiling.
- [x] $25,000 simulated equity; $6,250 initial pool; $18,750 recovery pool.
- [x] $20 initial entry; $1,000 cumulative per-asset gross buy ceiling.
- [x] ENTRY, PRESS, ROTATION share the initial-entry pool; RECOVERY is separate.
- [x] 0.40% buy and 0.40% sell fee assumptions; ordinary TOP_GAINER fixed take-profit is +1.74% net after fees.
- [x] Every purchase is MARKET BUY; LIMIT orders are for exit sells only.
- [x] Primary discovery is quality-filtered 24-hour gainers; $10M 24-hour quote-volume entry floor.
- [x] Separate top-loser research pathway: $1M research floor; $10M entry liquidity; flexible RSI/BBWP reversal evidence.
- [x] DTTP/DTSL applies only to TOP_LOSER_REVERSAL positions.
- [x] VCAP is independent shard recovery, not DCA; maximum two capital-bearing/in-flight shards per asset.
- [x] -24% outer boundary stops automatic patching and flags manual review.

### Still requires Architect-approved policy detail
- [ ] Exact VCAP shard-selection, batch-sizing, sequencing, and recycling policy.
- [ ] Exact DTTP/DTSL activation, ratchet, and stop-tightening formulas.
- [ ] Full top-loser reversal candidate scoring and evidence persistence.
- [ ] Any PRESS/ROTATION admission mechanics beyond sharing the initial-entry pool.

No code may silently fill these gaps with assistant-invented thresholds. Related execution stays disabled until the missing policy is supplied and tested.

## Phase 1 — Core safety, ledger, and authorization

- [x] Strict paper-only settings and fail-closed startup checks.
- [x] Separate initial and recovery capital pools.
- [x] Atomic initial reservation and 74-position hard ceiling.
- [x] Per-asset gross-notional guard.
- [x] Single-use, expiring, idempotent Governor authorization receipts.
- [x] Strategy-origin persistence on positions and reservations.
- [x] MARKET BUY construction with signed maximum execution-price ceiling.
- [x] Core and API reject BUY LIMIT requests and unbounded/oversized MARKET BUY requests.
- [ ] Complete restart/replay and concurrent reservation acceptance.
- [ ] Prove all fill, fee, partial-fill, cancel, and uncertain-order states end to end.

## Phase 2 — Primary gainer discovery and Condor

- [x] Read-only Kraken paper ticker/trading-rule access.
- [x] Public hourly candle client for true 24-hour change measurement.
- [x] Positive 24-hour gainer filtering.
- [x] $10M 24-hour quote-volume entry floor.
- [x] Freshness, quote-asset, exchange-rule, and spread checks.
- [x] Condor recommendation-only ranking with capped momentum, liquidity, and spread scoring.
- [ ] Verify the public Kraken candle endpoint in clean-install CI and actual Bazzite runtime.
- [ ] Add data provenance, stale-candle, missing-candle, and exchange outage acceptance.
- [ ] Implement separate top-loser research/evidence pathway without enabling entries prematurely.

## Phase 3 — Execution and ordinary gainer lifecycle

- [x] Every purchase is MARKET BUY, including the shared builder and signed dispatch path.
- [x] Signed max-execution-price estimate and quote reservation guard.
- [x] Strategy origin is bound to reservation and request body.
- [x] Fixed +1.74% net take-profit builder for TOP_GAINER only.
- [x] Governor enforces fixed take-profit minimum after 0.40% sell fee.
- [x] LIMIT SELL execution path is connected to the portfolio supervisor.
- [ ] Verify sell fills, fee settlement, partial exits, position closure, and replacement admission in CI.
- [ ] Prove no resting sell order is submitted before current bid reaches the fee-aware target.
- [ ] Prove TOP_LOSER_REVERSAL cannot enter the fixed TOP_GAINER exit path.

## Phase 4 — VCAP and top-loser reversal exits

- [x] Independent VCAP shard ledger primitives and two-slot structural limit.
- [x] -24% manual-review boundary.
- [x] DCA ladder removed from the active Alpha 2.0 policy path.
- [x] VCAP recovery authorization fails closed while shard policy is unconfigured.
- [ ] Implement only after approved shard-selection, sizing, batch, and recycling policy is supplied.
- [ ] Implement top-loser reversal evidence and persistent `TOP_LOSER_REVERSAL` origin.
- [ ] Implement DTTP/DTSL ratchets exclusively for `TOP_LOSER_REVERSAL`.
- [ ] Test that ordinary gainers never activate DTTP/DTSL and reversal entries never use the ordinary fixed target.

## Phase 5 — End-to-end acceptance and scale

- [ ] Full clean Compose paper cycle: candles → gainer scan → Condor → Governor → MARKET BUY → fill → fee/ledger.
- [ ] Full ordinary TOP_GAINER exit cycle: target reached → LIMIT SELL → fill → fees → close → capital reconciliation.
- [ ] Restart/replay and idempotency under submitted-but-uncertain orders.
- [ ] Staged 10-position test.
- [ ] Staged 25-position test.
- [ ] Staged 50-position test.
- [ ] Staged 74-position test.
- [ ] Concurrent admission and reservation race tests.
- [ ] Continuous paper soak test with a defined duration and invariant pass threshold.

## Phase 6 — Bazzite packaging and upgrade

- [x] Isolated Alpha 2.0 install directory: `$HOME/SpectraFinance-Alpha2`.
- [x] Separate Core and Hummingbot API checkouts.
- [x] Installer no longer removes the legacy install, files, or volumes.
- [x] No Compose `down -v` in the upgrade path.
- [x] Installer paths, wrapper paths, and service names aligned.
- [ ] Synchronize release candidate branches with latest dedicated Alpha 2.0 code.
- [ ] Pass clean-install workflow including full `run-once` paper strategy cycle.
- [ ] Verify install, in-place upgrade, backup, restore, rollback, and scoped uninstall.
- [ ] Verify the complete process on the Architect's actual Bazzite machine.
- [ ] Confirm existing Alpha services, PostgreSQL, EMQX, and volumes remain untouched.

## Evidence register

Record exact run IDs, commit SHAs, logs, and counts. A cancelled or superseded run is not a pass.

| Evidence | Required proof | Current status |
|---|---|---|
| Domain tests | All `tests/test_alpha2_*.py` pass | In progress after strategy and lifecycle corrections |
| API receipt tests | Signed MARKET BUY and strategy-origin checks pass | In progress |
| Clean Compose | Isolated API/Core services healthy | Prior candidate passed; rerun required after current changes |
| Full paper cycle | Public candles, gainer scan, signed MARKET BUY, reconciliation | Newly added to clean-install workflow; pending |
| TOP_GAINER exit | +1.74% net LIMIT SELL and reconciled position closure | Newly added; pending |
| VCAP | Approved policy and end-to-end shard recovery | Blocked pending policy |
| DTTP/DTSL | Strategy-origin-gated reversal exit | Blocked pending ratchet specification |
| Bazzite local | Actual install/upgrade and preservation checks | Not run |

## Reporting rules

- Distinguish unit, API, Compose, full paper-cycle, soak, and local Bazzite tests.
- Report exact pass/fail/skip counts and commit SHA.
- Cancelled, skipped, flaky, or unavailable tests are not passes.
- CI success does not prove local installation.
- Never claim VCAP or DTTP/DTSL is complete while its approved policy remains unconfigured.
- Never claim upgrade readiness until clean-install, upgrade preservation, backup/restore, rollback, and local Bazzite checks have evidence.
