# Alpha 2.0 Build Plan and Evidence Register

## Build sequence

### Phase 0 — Specification freeze
- [x] Resolve 74-position target/hard cap.
- [x] Record portfolio pools, per-asset cap, DCA and exact VCAP ladder.
- [x] Record authority boundaries and paper-only invariants.
- [ ] Review specification against Architect's Governor file/blueprints when supplied.
- [ ] Validate fee-inclusive take-profit and exposure accounting with executable examples.

### Phase 1 — Alpha 1.x technical audit
- [ ] Inventory Core repository and identify reusable modules.
- [ ] Inventory Hummingbot API repository and paper connector behavior.
- [ ] Map Condor startup/lifecycle and scanner capabilities.
- [ ] Record every candidate component as reuse, rewrite, or reject with evidence.
- [ ] Identify dependency/runtime compatibility and licensing constraints.

### Phase 2 — Minimal foundation
- [ ] Define package boundaries and typed domain models.
- [ ] Add strict settings validation; fail closed on any non-paper configuration.
- [ ] Establish database schema and append-only event ledger.
- [ ] Establish deterministic clock, ID, and test fixtures.
- [ ] Add CI for lint, type checks, unit tests, integration tests, and security-negative tests.

### Phase 3 — Capital and portfolio domain
- [ ] Implement position admission and atomic 74-position ceiling.
- [ ] Implement $25,000 account, $6,250 initial budget, $18,750 recovery pool.
- [ ] Implement $1,000 per-asset gross-notional cap including outstanding reservations.
- [ ] Implement $20 initial entry and $20 DCA additions at each configured DCA threshold.
- [ ] Implement reservation/authorization idempotency and concurrency protection.

### Phase 4 — VCAP and accounting
- [ ] Implement exact ladder table and immutable drawdown reference.
- [ ] Implement shard lifecycle and per-asset two-slot semaphore.
- [ ] Implement fee-aware per-shard take-profit.
- [ ] Implement partial fill, cancel, reject, resolve, recycle, and replay behavior.
- [ ] Prove -24% hard boundary and no automatic liquidation.

### Phase 5 — Execution and integration
- [ ] Prove scanner → Condor recommendation → Core proposal → Governor authorization → Hummingbot paper execution.
- [ ] Prove Condor has no order path.
- [ ] Prove Hummingbot rejects missing, invalid, expired, reused, and mismatched authorization.
- [ ] Prove public market data only and no private credential handling.
- [ ] Prove order → fill → fees → ledger reconciliation.

### Phase 6 — Portfolio scale and endurance
- [ ] 10-position acceptance.
- [ ] 25-position acceptance.
- [ ] 50-position acceptance.
- [ ] 74-position acceptance.
- [ ] Concurrent admission and shard reservation race tests.
- [ ] Restart/replay/recovery tests at each scale.
- [ ] Continuous paper soak test; duration and pass threshold recorded before execution.

### Phase 7 — Bazzite release
- [ ] Pin Core, Hummingbot/API, Condor, database, and broker versions by immutable digest/commit.
- [ ] Build clean install, upgrade, backup, restore, rollback, and scoped uninstall.
- [ ] Verify Bazzite/rootless Podman and user systemd lifecycle.
- [ ] Run clean install on Architect's actual machine.
- [ ] Publish operating guide and evidence-backed release report.

## Evidence register

No test result is assumed. Populate only from actual commands and CI logs.

| Evidence ID | Test/command | Result | Evidence URL/log | Notes |
|---|---|---|---|---|
| A2-BOOT-001 | Not run | NOT RUN | — | Clean boot |
| A2-SAFE-001 | Not run | NOT RUN | — | Paper-only negative tests |
| A2-PORT-001 | Not run | NOT RUN | — | 74-position ceiling |
| A2-VCAP-001 | Not run | NOT RUN | — | Exact ladder |
| A2-SHARD-001 | Not run | NOT RUN | — | Two-slot concurrency |
| A2-LEDGER-001 | Not run | NOT RUN | — | Fill/fee reconciliation |
| A2-RECOVERY-001 | Not run | NOT RUN | — | Restart recovery |
| A2-SCALE-010 | Not run | NOT RUN | — | 10 positions |
| A2-SCALE-025 | Not run | NOT RUN | — | 25 positions |
| A2-SCALE-050 | Not run | NOT RUN | — | 50 positions |
| A2-SCALE-074 | Not run | NOT RUN | — | 74 positions |
| A2-BZZ-001 | Not run | NOT RUN | — | Actual Bazzite installation |

## Reporting rules

- Report exact test names and pass/fail/skip counts.
- Distinguish unit, integration, end-to-end, soak, and local-machine tests.
- Cancelled, skipped, flaky, or unavailable tests are not passes.
- A CI pass does not prove local Bazzite installation.
- Do not claim paper-only safety based solely on environment defaults; include negative tests.
- Do not claim completion until every release acceptance criterion has evidence.
