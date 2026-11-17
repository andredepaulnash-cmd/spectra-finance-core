# Alpha 2.0 Implementation Status

## Current branch
- Branch: `spectra-finance-alpha-2.0`
- Latest domain implementation: `b8eaf1a7bae8251bdf5018177144ecf6fc860574`
- Status: early domain foundation; not an installable application.

## Completed
- Clean Alpha 2.0 source tree containing only specification, audit, initial package and tests.
- Master blueprint and build/evidence plan.
- Preliminary Alpha 1.x repository audit.
- Deterministic policy constants for 74 positions, $25,000 capital, 25/75 pool split, $1,000 per-asset gross notional, $20 DCA sizing, exact VCAP ladder, -24% boundary, and fee-aware net take-profit.
- Fail-closed paper settings requiring `SPECTRA_PAPER_ONLY=true` and `SPECTRA_ALLOW_LIVE_EXECUTION=false`.
- Initial independent VCAP shard model with lifecycle transitions, partial-fill accounting, and two-slot counting helper.

## Verified CI evidence

| Run | Commit | Result | Evidence |
|---|---|---|---|
| 44184622202 | `0bf11d0ba2d7ebccfc4972c1edb5278d25dcb175` | PASS — compileall plus 14 unit tests | https://github.com/andredepaulnash-cmd/spectra-finance-core/actions/runs/44184622202 |
| 44184585173 | intermediate edit | FAIL | superseded; malformed intermediate source edit |
| 44184599895 | intermediate edit | FAIL | superseded; malformed intermediate source edit |
| 44184608546 | `7ff1a43232c2806dafa9725ea9ccc72d5a9d438d` | PASS — initial policy/safety tests | https://github.com/andredepaulnash-cmd/spectra-finance-core/actions/runs/44184608546 |

The 14-test passing run predates the shard module. The shard module and `test_alpha2_shards.py` have been committed after that run; their CI status is **NOT YET VERIFIED**. No shard test is counted as passing yet.

## Not yet implemented or verified
- Durable, transactional CapitalGovernor and authorization receipts.
- Position admission under concurrent 74-position races.
- Durable two-shard per-asset semaphore.
- DCA/VCAP orchestration against live market observations.
- Scanner and Condor recommendation integration.
- Hummingbot paper executor integration.
- Order/fill/fee/ledger end-to-end reconciliation.
- Restart/replay recovery.
- 10/25/50/74 staged simulations.
- Bazzite installer, upgrade/rollback, and actual local-machine verification.

## Important limitations
- No real exchange credentials are used or requested.
- No live execution path is implemented.
- The current tests are unit tests only; they do not establish market connectivity, execution, continuous operation, or local installation.
- The preliminary Alpha 1.x audit is not final until the relevant source and Governor documents have been inspected.
