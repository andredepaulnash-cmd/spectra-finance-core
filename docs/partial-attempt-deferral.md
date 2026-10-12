# Opt-in partial-sale feasibility deferral

`PartialAttemptPolicy.enabled` defaults to `False`. Existing callers retain
the baseline lifecycle behavior. No standard MCP tool or runtime entrypoint
enables this policy automatically.

When explicitly enabled in `plan_vcap_lifecycle_from_ledger`, the planner
assesses only the exact persisted open partial-recycle manifest. Fresh known
price/pending-order evidence, unchanged owner provenance, still-present
selected lots, venue support, exact quantity steps, minima and native
fee/slippage profit geometry are required. Unknown pending state fails closed.

Infeasible price/notional cases return `HOLD` with an explicit deferral reason.
The original manifest, sale intent, owner scope, remaining units/basis,
reservations, pending state and ladder checkpoint stay intact. There is no
fallthrough to a recovery purchase. A later call must assess current evidence
again; improved price can make the same manifest eligible. True base minimum,
rounding and consumed-lot problems require explicit reconciliation and are
not mislabeled as temporary price deferrals.

The actual `run_vcap_runtime_cycle` integrates this gate when
`VCAPRuntimePolicy(partial_attempt_deferral_enabled=True)` is explicitly supplied.
It fetches exact pending-order evidence (including ambiguous committed same-asset
claims), preserves the existing capital/accounting/breaker guards, and rechecks
the manifest after the sell adapter's own asynchronous lookups. The final clock
includes elapsed time across awaits; both market and accounting evidence must
still be fresh at the send boundary.

A durable per-position mutation lock and single-dispatch claim protect the
exact manifest before the external create. Unknown creates remain locked for
reconciliation rather than being blindly retried, even through a later caller
that omits the feature flag. Only exact reconciled terminal order/trade totals
release a completed attempt. Duplicate/missing/overfilled evidence fails closed.
Confirmed recovery releases use the sale's exact per-lot allocation journal;
original manifest proportions cannot substitute after asymmetric consumption.

The pure assessment itself grants no execution or capital authority and cannot
guarantee a market fill. A price-aware deferral can miss a favorable subsequent
gap; the default remains off until that behavioral tradeoff is reviewed. The
gate uses the native additive fee-plus-slippage convention, not the research
harness's compounded cost approximation. No future bar/open is an input.

Tests exercise default equivalence, repeated no-op deferral, same-manifest
reactivation, stale/future/invalid observations, pending priority, changed
owners, partial fills, exact rounding, native fee-floor boundaries, and
preservation of authorization/rung state.
