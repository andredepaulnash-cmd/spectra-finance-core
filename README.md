# Spectra Finance Alpha 2.0

A paper-trading-only portfolio automation system. Alpha 2.0 is developed on the dedicated `spectra-finance-alpha-2.0` branch. Older Alpha/Condor branches are reference and code-mining sources only.

## Non-negotiable safety mode

- `SPECTRA_PAPER_ONLY=true`
- `SPECTRA_ALLOW_LIVE_EXECUTION=false`
- No private exchange credentials are requested, stored, loaded, or wired into this project.
- Public market data only; all order execution is simulated.
- Hummingbot must reject order requests unless they carry a valid CapitalGovernor authorization.
- Missing, malformed, stale, or contradictory safety settings fail closed.

## Product architecture

1. Research scanner: identifies quality-filtered 24-hour gainers from public Kraken market data.
2. Condor: scores and explains candidates; recommendation-only, never executes.
3. Spectra Core: coordinates position lifecycle and preserves strategy origin.
4. CapitalGovernor: sole authority for capital reservations and order authorization.
5. Hummingbot paper executor: submits only signed, authorized paper orders.
6. Ledger/reconciler: records fills, fees, lot basis, realized P&L, and restart state.

## Confirmed Alpha 2.0 strategy boundaries

- Primary entries are positive 24-hour gainers from Kraken USD spot markets. Alpha 2.0's paper portfolio has a hard ceiling of 74 concurrent reserved/open positions with an initial operating target of 50 (`MAX_OPEN_POSITIONS=74`, `INITIAL_TARGET_POSITIONS=50`), at most 10 new entries per cycle, staged 10/25/50/74, with $30 gross initial MARKET BUYs and automatic replacement after fully reconciled exits. Permissive default paper-test filters still require fresh data, valid exchange rules, positive books, at least $100,000 24-hour USD quote volume, and a maximum 1% spread.
- Every purchase is a MARKET BUY. LIMIT BUY and resting buy orders are prohibited.
- Ordinary `TOP_GAINER` positions use the fixed +1.74% net take-profit after 0.40% buy and sell fee assumptions; exit sells are LIMIT orders and are not submitted until the current bid reaches the fee-aware target.
- `TOP_LOSER_REVERSAL` is a separate, not-yet-enabled research/entry pathway. The discussed $1M research and $10M entry-liquidity targets still need metric/quote-asset semantics. Adaptive DTTP/DTSL is exclusive to that origin and is not applied to ordinary gainer positions.
- VCAP is independent shard recovery, not DCA. VCAP automation remains disabled until its approved shard-selection, batching, sizing, and recycling policy is configured.
- The -24% outer boundary stops automatic patching and flags manual review.

See [the master blueprint](docs/MASTER_BLUEPRINT.md), [the phase plan and evidence register](docs/BUILD_PLAN.md), and [the Bazzite installation guide](docs/BAZZITE_INSTALL.md).

## Release status

The repository contains a working paper-only foundation and deterministic domain/API tests. It is not yet declared upgrade-ready: the full clean-install strategy cycle, restart/soak evidence, VCAP policy, reversal-path DTTP/DTSL, and actual Bazzite install/upgrade verification remain separate acceptance gates.
