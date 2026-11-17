# Spectra Finance Alpha 2.0

A paper-trading-only portfolio automation system.

**Current status:** Blueprint and architecture phase. This branch is intentionally a clean Alpha 2.0 workspace; Alpha 1.x source remains in repository history and existing branches for audit/reference. No application runtime is certified or installable yet.

## Non-negotiable safety mode

- `SPECTRA_PAPER_ONLY=true`
- `SPECTRA_ALLOW_LIVE_EXECUTION=false`
- No private exchange credentials are requested, stored, loaded, or wired into this project.
- Public market data only; all order execution is simulated.
- Hummingbot must reject order requests unless they carry a valid CapitalGovernor authorization.
- A missing, malformed, stale, or contradictory safety setting fails closed.

## Product shape

1. Research scanner: discovers eligible public markets.
2. Condor: analyzes and recommends; never submits orders.
3. Spectra Core: coordinates the lifecycle and enforces portfolio policy.
4. CapitalGovernor: authorizes and reserves capital before any order can be sent.
5. Hummingbot paper executor: executes only authorized simulated orders.
6. Ledger/reconciler: records fills, fees, shard cost basis, realized P&L, and restart state.

See [the master blueprint](docs/MASTER_BLUEPRINT.md) and [the phase plan](docs/BUILD_PLAN.md).
