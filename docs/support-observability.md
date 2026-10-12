# Decision evidence and cycle reconciliation

These permanent core support APIs do not introduce a trading strategy, spend
permission, or live scheduler. The standard MCP entrypoint remains unchanged.

## Decision tracing

Capital reservation decisions and VCAP runtime phase transitions append their
reasons and allowlisted scalar facts to `decision_traces` in the existing local
SQLite transaction. Capital facts include requested amounts, protected cash,
observed exposure, and policy ceilings. Runtime facts link the position,
envelope, action, mutation identity, and previous phase.

`read_decision_traces(path, subject_id=...)` is a read-only query. It cannot
authorize orders or release cash. An exact reservation retry retains the
original evidence; runtime transitions retain their history instead of losing
earlier reasons when the current phase changes. Unsupported rejected numeric
inputs are represented by safe scalar/type descriptions, never arbitrary
object serialization. No raw connector payloads or credentials are added.

Trace writes commit with their originating decision. A database write failure
rolls back the decision; it cannot leave an unjournaled new reservation. This
is a storage-integrity behavior, not an additional economic rule. Normal
economic decisions are regression-tested against the unobserved code path.

Existing databases acquire the additive table/index when their existing store
is constructed. Historical decisions are not invented or backfilled.

## Exact allocation evidence

New economic exits journal their already-computed per-lot base, cost basis,
entry fees, proposal, executor, ownership, and event identity in
`economic_exit_allocations`. Inserts occur in the same transaction as existing
lot/event updates. The allocation calculations and original monetary amounts
are unchanged. Exact event retries do not append duplicate allocations.

## Read-only reconciliation

`reconcile_position` checks original versus remaining and sold inventory,
basis and fees, per-event profit identities, event residual/closed state,
per-owner allocation bounds and ratios, exact execution provenance, and
recovery releases versus proven consumed owner basis. Dust inventory remains
included with its original ownership and cost basis.

`reconcile_explicit_cycle` combines explicitly supplied position identities
for the same normalized asset and quote currency. Open inventory remains an
uncompleted cycle; realized gains are never substituted for whole-cycle
cashflow. It does not infer that one position's proceeds financed another.
That requires exact cash/reservation attribution at execution time.

The reports distinguish accounting conservation, provenance completeness,
complete allocation-journal coverage, and event chronology completeness.
Same-timestamp buys/sales without an ordering watermark are checked against
valid conservation bounds but remain chronology-incomplete. Legacy events without new per-lot
evidence are reported as incomplete, not reconstructed from wallet balances.
Separate governor and economic databases are read snapshots, not a live
cross-database authorization lock. Reports never mutate either database or
create missing files.

## Verification and limits

Focused tests cover rejection/retry evidence, restart history, observational
economic parity, rollback, malformed inputs, closed/open/reentry identities,
wrong owners, impossible but aggregate-balanced allocations, non-finite data,
and legacy incomplete evidence. No historical optimization, unseen holdout,
live order, deployment, or autonomous scheduler activation is part of this
change.
