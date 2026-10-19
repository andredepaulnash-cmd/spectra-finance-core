# Research-only PSM profit partition, schema 3

This is an offline accounting bridge, not an exchange reconciliation service or an
order generator. It has no network, account or historical-data entrypoint. The
original reducer is preserved exactly in `psm_kernel_legacy.py`, SHA-256
`f5e9ffd8c456cd338bdcc361a74b138d2e9776992dc15eb5dfb60a35c85150c2`.

## New event and unchanged invariants

`REALIZED_PROFIT_DISTRIBUTION` is the only new reducer event. It transfers the
entire strictly positive fee-net native profit of one uniquely identified native
trade out of `psm:<pair>` into `next_entry_profit_escrow`. It is accepted only after
the native logical PSM sell action is completely terminal and reconciled, with no
pending same-asset native or reducer action. A canceled action with actual fills
may qualify; rejected/unfilled actions cannot manufacture profit.

The event binds the native trade and realization, allocator event, membership
snapshot and cash backing IDs; account, pair, owner, root cohort and immutable
seed/repair funding owner; exact journal fill and reconciliation events; native
proceeds, returned principal, entry and exit fees, actual positive profit; and
exact settled cash before and after. It retains a SHA-256 of the entire verified
native source. The amount must equal actual positive profit. Losses, zero profit,
partial-profit withdrawals, principal withdrawals and insufficient cash fail
closed. Event IDs replay idempotently only with identical bytes. Reusing a native
trade, realization, allocator event or cash-backing proof under another event ID
fails. Timestamps remain monotonic.

Snapshot units and lifetime basis, retained dust, account/asset claims, original
pro-rata diagnostic basis and repeated-cycle state are preserved. Distributions
are recorded in cycle accounting deltas. There is no incoming cross-asset cash
event, no forced order, and no change to the original buyback unit-gain rule.
Taking profit may consequently leave too little own cash for a qualifying buyback.

## Pure helper API

`identity()` returns schema, current kernel/helper hashes and the legacy hash.
The native bridge must freeze this identity in its persisted restart identity.
The helper verifies the expected kernel and legacy bytes on every proof call.

`verify_transition(state, sale_order, acquired_order, trade_id, *,
acquisition_funding=None)` returns a detached
canonical dictionary with `proof_sha256`. It is a narrow exception for an exact
`PSM_EXIT` from the proven acquisition pool to the same pair's PSM pool. It is
called for every acquisition, including a previous PSM-funded acquisition. It
verifies campaign, archived or current episode, immutable original lineage hash,
source snapshot and handoff, native action owner, every terminal sibling child,
original acquisition lot/proposal/executor/account/pair, root/funding owner, and
journal fill/reconciliation binding. Missing or malformed evidence fails closed.

The original `seed`, `repair` and exact same-pair `psm:<pair>` acquisition paths
are unchanged and reject any non-null `acquisition_funding`. A `next_entry:` pool
is accepted only with this exact envelope, supplied after V2 independently
reverifies the actual paper/native/allocator evidence:

```
{
  "schema": 1,
  "kind": "VERIFIED_DEDICATED_ACQUISITION",
  "acquisition_order_id": "...",
  "entry_pool": "next_entry:...",
  "pair": "...",
  "cohort": "...",
  "cohort_generation": 2,
  "funding_owner": "seed or repair",
  "native_position": "...",
  "proposal_id": "...",
  "executor_id": "...",
  "activation_sha256": "...",
  "lineage": {"schema": 2, "kind": "..."},
  "lineage_sha256": "...",
  "proof_sha256": "..."
}
```

`lineage_sha256` hashes canonical JSON of the complete existing V2 lineage;
`proof_sha256` hashes the complete envelope without its own digest. Canonical
JSON uses sorted keys, compact separators, no NaN, and decimal amounts as strings.
Hashes are lowercase 64-character hexadecimal SHA-256 values. Hashes bind bytes;
they do not authenticate a caller assertion or demonstrate database verification.

The envelope must match the actual persisted acquisition order and its activation
proof, including native position, proposal/executor, pair, cohort, generation and
immutable seed/repair funding owner. Generation must match the source episode's
immutable handoff cohort, not a later mutable cohort generation. Actual PSM child
sales need not carry generation; a generation supplied on one must agree. Existing
root-cohort, lot ownership and source-pool handoff checks still apply.

Only two lineage kinds are permitted:

- `CLOSED_SAME_ROOT_ENTRY_POOL` is the existing schema-2 V2 proof for a terminal
  funded acquisition. The pool must be exactly `next_entry:` plus its bound
  `entry_id`; the order's source pool must match the lineage and its funding
  owner. `admission_sha256` must equal the actual activation's `proof_sha256`.
  Only `REENTRY` / `ENTRY` / `seed` or `REPAIR` / `RECOVERY` / `repair` is allowed.
- `RETAINED_ROOT_BASE_ENTRY` is the existing schema-2 proof for a base-only
  `REENTRY` / `ENTRY` / `seed` acquisition using an explicitly retained predecessor
  pool. The pool must exactly match `root_entry_id` and the acquired order's source
  pool. `base_entry_proof_sha256` must equal the actual persisted base-entry proof
  and activation; the base proof's own digest, acquisition binding and zero-profit
  increment are checked. Predecessors must be nonempty, unique, same pair/cohort,
  pool, funding owner and previous generation, and cannot reuse the current sale
  trade ID. V2 independently rederives their full funding and settled-sale evidence.

The helper checks these bindings and PSM ownership only. V2 remains responsible
for reopening and validating funding rows, admission evidence, native acquisitions,
allocator reservations/commits/releases, activation audits, terminal batch,
balanced cash returns and predecessor outboxes. Neither a Boolean nor a merely
well-formed envelope replaces that external evidence. No extra acquisition pool,
funding event, reducer event, trading permission or source of profit is introduced.
The returned PSM proof includes the detached `acquisition_funding` envelope only
when present, so original-path proof structure and bytes remain unchanged.

The returned proof excludes mutable current cash, live journal hash and current
mode/action. Its source episode, source handoff and completed action remain
immutable. Source verification therefore gives identical bytes after later
rebuilds, distributions, archived episodes and a JSON restart. It verifies the
source action is terminal; it deliberately does not gate unrelated later pending
actions during historical source revalidation.

The native bridge freezes the distinct proofs as `source.psm_ownership`, sorted
by `acquisition_order_id`. Source fields are its verified receipt, frozen order,
terminal audit row, native event, original native lots, acquisitions and exact
disposal allocations. Native acquisition cash, fees and source-ordered pro-rata
allocation replay remain independently mandatory in the native bridge; this
helper does not replace them or confer authority from caller booleans.

`stage_distribution(state, source, amount, at, allocation_event_id,
cash_backing_id, snapshot_id)` returns a detached updated whole adapter state.
`source` may be the frozen dictionary or JSON string. It recomputes and compares
all proofs, checks source receipt/scope, native original lots and allocation totals
against the immutable handoff, cross-checks exact journal fill and native
realization economics, and journals the outward transfer. First-time staging is
restricted to the current unarchived PSM episode, with no pending logical/native
same-pair order. Exact replay checks the frozen source and all identity/amount
fields, returns an unchanged detached state, and tolerates later actions or
archiving. An attempted changed-ID replay is rejected.

For dedicated acquisitions, staging extracts the envelope from
`acquisition.pool_transition.acquisition_funding`, rederives the transition, and
requires exact equality with both that frozen transition and `source.psm_ownership`.
It never accepts an envelope found only in a caller's `psm_ownership` array. A
changed but internally rehashed envelope cannot replace the immutable source of
an already journaled distribution.

## Atomicity and adapter responsibilities

The helper does not persist anything. The caller must hold the adapter's single
writer lock and write the returned adapter state, balanced `next_profit_cash`
debit/escrow credit, and immutable allocator outbox in the SAME paper SQLite
transaction. It must replace/reload the adapter's in-memory state after that
transaction and before any further adapter save. A rollback must expose none of
these changes. Frozen outbox replay handles the separate allocator-store boundary.

The native bridge must stage all eligible terminal sale receipts, including losses
as non-grants, before admitting another spend. Only positive receipts call this
partition helper. It must not stage individual positive children until all
siblings and the logical reducer sell action are terminal/reconciled.

`distribution_debits(state, pair)` returns cumulative `root|funding_owner` debit
amounts across current and archived journals, deduplicating current/archive
overlap. The adapter subtracts these debits from exact receipt-derived subcash.
The adapter cash projection subtracts the same debits once from the PSM source
pool and adds them once to escrow. Principal and every other root/funding owner's
cash remain untouched. The reconciled current source pool must equal current
reducer cash plus unsettled source receipts plus prior-episode retained offsets.
Offsets already represent net retained cash and must not be deducted twice.

## Reporting

`marked_usd` and `current_wallet_marked_usd` are current source-wallet value only.
`cumulative_distributions` is a separate outward-transfer metric.
`source_value_including_distributions` and `total_economic_pnl` add those historic
distributions back for source economic performance. `value_vs_holding` uses the
same source-total convention. `realized_sell_pnl` remains the unchanged reducer
pro-rata diagnostic; actual native realization remains the native ledger's
separate authority. Portfolio cash/NAV must not add cumulative distributions
again, since the transferred cash is already in escrow or a later funded asset.

## Verification scope

The tests use real native SQLite acquisition/disposal/reconciliation evidence and
an actual PSM reducer journal, with no market history or external accounts. They
cover positive profit, principal/loss/partial refusal, no-pending gates, source and
native-economics tampering, duplicate proof, exact replay, JSON restart, later
episode stability, archive overlap, insufficient cash, unchanged buyback gain
rules, source reporting, basis preservation and the exact legacy reducer hash.
Dedicated acquisition tests in this pure suite use synthetic envelopes over real
native disposal evidence to check direct seed/repair and retained-base binding,
malformed/missing/foreign evidence, changed digests, kind/intent/owner conflicts,
immutable handoff generation, root/pool identity, predecessor self/duplicate replay,
base-proof substitution, source rederivation and exact distribution replay. They
do not claim to verify dedicated-funding database evidence; that is covered by
V2's actual native integration/caller tests. The prechange helper bytes were
preserved before this extension with SHA-256
`67ca42e69e03a1efef10b25886367bd9d08f863d0bb2925f42604a71ab03a10a`.

Cross-store crash recovery and end-to-end adapter projection remain the integrated
native bridge/caller test responsibility; these helper tests do not certify
production readiness or historical strategy performance.
