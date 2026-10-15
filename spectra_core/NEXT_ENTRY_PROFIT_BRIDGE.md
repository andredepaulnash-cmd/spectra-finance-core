# Native settled-profit bridge contract (grant-only v1)

## Status and boundary

`NativeSettledProfitBridge` is default-off. It is a production-forward **paper
integration component**, not a live trading service. It does not authorize or
submit orders, mutate native lots, change governor policy, alter a reserve floor,
or manufacture cash. It imports the frozen local `next_entry_allocator` API.

Authority inputs are the PR40 native economic-position, exact-fill reconciliation
and governor provenance tables, plus the copied owned-spot adapter's SQLite paper
receipt/state/audit schema. Merely having a native realized event does not establish
settled cash. The bridge is intentionally restricted to this schema's USD cash
genesis (protected 500, seed 1,000, repair 500), exact quote-currency fees, and
whole UTC seconds. Other genesis, deposits, inherited/manual lots, third-currency
fees, sub-accounting-atom amounts and undocumented pool migrations fail closed.

This file is the grant-only boundary. **Entry reservation/debit/release routing is
not implemented here.** An allocator reservation or commit without a reviewed
native cash-funding/debit bridge blocks this projection rather than silently
assuming the escrow was spent correctly.

## Public API

Construct with existing, separate database paths:

```python
bridge = NativeSettledProfitBridge(
    paper_db=paper_path,
    native_db=native_path,
    allocator=allocator,
    account_id=account,
    connector=connector,
    owner_id=controller_id,
    enabled=False,
)
```

An omitted/false enable flag creates no paper bridge schema and cannot stage,
dispatch or return spending callbacks. Enabling binds the bridge persistently to
the paper identity, account/connector/controller, native path, allocator path and
allocator policy. An identity mismatch is not an automatic migration.

- `stage(trade_id=..., snapshot=MembershipSnapshot(...), now=...)`: verifies one
  complete applied trade receipt, freezes membership and original attestation,
  and atomically stages its cash-backed outbox. Returns the durable outbox row.
- `dispatch(trade_id=..., now=...)`: revalidates exact original sources, sends the
  frozen payload to `Allocator.apply_profit`, then acknowledges durably. Returns
  the allocator receipt, or `None` for a nonpositive realization.
- `cash_projection()`: returns `CashProjection` with `cash_pools`,
  `available_base_pools`, `profit_escrow`, `total_physical_cash`, `protected_cash`,
  `pending_trade_ids`, and `new_spending_allowed`.
- `before_new_spending()`: revalidates all frozen sources and blocks any unresolved
  discrepancy, unprocessed terminal sale, pending outbox, backing deficit or
  inconsistent allocator state. Returns the checked cash projection.
- `integration_callbacks()`: returns these last two bound callbacks explicitly.

The callbacks are **not installed by construction**. Existing adapter
`cash_pools()` does not know these earmarks and must not remain a second spendable
balance authority in an enabled integration.

## Required adapter integration

1. Retain the adapter's single-account writer lock while calling bridge methods.
   All paper/native mutation, terminal reconciliation, bridge staging and dispatch
   must be serialized under that lock. SQLite serializes receipt-domain writes;
   it is not a distributed lock over independently mutable native sources.
2. Retain pinned-source verification, native accounting health checks, ordinary
   eligibility, reserve, minimum, liquidity, sizing and risk guards. This is an
   additional accounting gate, not a replacement for those guards.
3. After native reconciliation and the paper receipt's `applied=1`, complete the
   existing terminal-order reconciliation. Stage every terminal SELL receipt,
   including losses, before further spending. Then drain the outbox.
4. Freeze the approved membership interpretation before the first event. Pass a
   full immutable snapshot; no runtime membership inference occurs here.
5. Use `available_base_pools` for existing base-budget sizing. Do not include
   `profit_escrow` as ordinary base cash and add its entitlement again. Retain
   `total_physical_cash` for aggregate physical accounting; escrow is part of that
   total, not new money. The protected bucket is never debited.
6. Call `before_new_spending()` immediately before any new capital mutation and
   retain the original native/adapter guards. Never interpret an allocator receipt
   as refreshed order permission.

If `economic_accounting_health` exists, explicit conflicts, holds, unresolved IDs,
future evidence and evidence older than the native default policy fail closed.
Absence of that table in an isolated synthetic ledger is **not** a verified health
attestation. The production adapter must retain its own mandatory native health
gate, including completeness for every active position.

## Evidence and granularity

The event granularity is one confirmed paper **trade execution receipt**, not a
cumulative position P&L snapshot or an order estimate. All disposal allocations
for that event are aggregated, including losing lots. Profit is:

    gross proceeds - allocated original principal
      - allocated entry fees - actual exit fees

The sum must equal the native immutable event's realized P&L exactly. Only a
positive aggregate creates an entitlement; zero/loss receipts are durably recorded
as `INELIGIBLE` and create no cash journal or allocator event.

Verification checks exact trade ID, order ID, account, connector, pair, side,
quantity, price, fee currency/amount, timestamps, native result ID, position,
proposal, executor and controller ownership. The paper order must be terminal
FILLED/CANCELED with one matching terminal audit receipt. All order fills must be
applied and consistent with the terminal quantity and chronology.

Every disposed lot must have its own exact applied paper acquisition debit,
native acquisition binding and reconciliation. Original basis/fees/quantity must
match that debit; future acquisitions and unsupported inherited inventory are
rejected. Sale cash must return to the exact original acquisition pool; a PSM or
other pool migration needs a separately reviewed transition contract.

All current disposal allocations must belong to the sell's permitted lot scope
and native owner. Historical allocation amounts must be valid and reference real
native events. Remaining lot amounts plus all its disposals must conserve original
quantity, basis and fees. Scoped native fills and paper cash receipts must cover
the same IDs; native lots/events must have corresponding confirmed acquisitions
and sales, with complete event-level allocation aggregation. Physical cash plus
remaining basis/fees must equal original cash plus lifetime realized P&L.

The allocator booleans are set only after this derivation and the planned cash
reclassification is validated in the same staging transaction. No input boolean
from a caller is treated as proof.

## Durable accounting and crash protocol

The new `next_profit_*` tables live in the **paper receipt SQLite database**:

- immutable bridge identity and membership snapshots
- outbox source bytes, source digest, original event payload, payload digest,
  stable operation ID, state and acknowledgement
- one source-pool debit and equal profit-escrow credit per eligible receipt
- append-only discrepancy records

WAL, `synchronous=FULL`, and `BEGIN IMMEDIATE` are used for all staging and
acknowledgement transactions. Staging freezes source bytes, membership, original
proof timestamps and event IDs **before** allocator dispatch. Cash creation is
always zero. A staging rollback leaves neither outbox nor cash reclassification.

Dispatch happens only after the staging commit. If the allocator commits and the
process dies before acknowledgement, retry uses the identical operation/event/
proof/backing IDs and byte-equivalent payload. Freshness is not fabricated by
refreshing proof timestamps. Already accepted events can replay after original
expiry; never-accepted expired staged events remain blocked with their escrow
intact. Acknowledgement never releases escrow.

Each replay rereads native and paper sources and checks the frozen values. Source
changes, changed membership, foreign ownership, malformed cash legs, missing
historical debits, unexplained allocator events, or an allocator reset fail
closed. Original forensic source and attestation bytes are never overwritten.
Discrepancies have no automatic reset or deletion API; deliberate reconciliation
requires a separately reviewed procedure. Digests are consistency checks, not
signatures or protection from coordinated malicious database rewriting.

The allocator's complete source event and snapshot must match this bridge's
frozen cash backing. Acknowledged events must remain present in the allocator;
a pending outbox may be either accepted-but-unacknowledged or not yet accepted.
This makes all three crash states distinguishable without allocating twice.

## Future dedicated entry-pool saga

A separately reviewed purchase coordinator may use `next_entry:<entry_id>` pools:
atomically transfer base-source cash and reserved profit escrow to the dedicated
pool, debit real paper BUY receipts there, return unused funds to their exact
sources and commit/release matching allocator lots. That extension must add a
verified balanced funding/debit journal to this projection and relax the current
reservation/commit rejection only against that evidence. Later sale receipts must
preserve original acquisition pool attribution. Do not implement the saga by
silently crediting unrestricted base pools or manually decrementing escrow.

## Verification

The focused suite uses actual native ledger/governor/reconciliation calls with a
real SQLite paper receipt/state/audit store. It covers positive/mixed-lot/loss/zero
sales, duplicates, deposits, exact evidence mutations, ownership, chronology,
stale/changed evidence, immutable membership/forensic bytes, source-pool balance,
protected reserve, missing acquisition cash, native health conflicts, allocator
reset, partial disposal and ambient Decimal precision.

Five exception-crash boundaries and eight actual subprocess `os._exit` boundaries
exercise pre/post staging commit, allocator pre-write/pre-commit/post-commit and
pre/post acknowledgement. Restarts must grant exactly once with one balanced
cash reclassification. No historical market evaluation, external orders or live
purchase routing is exercised.

## Protected extension hooks (default-deny)

A V2 implementation must be a separately reviewed class/module. V1 exposes these
protected methods so an extension does not duplicate or weaken native evidence
verification:

- `_extension_identity()`: persisted implementation/helper identity. V1 returns
  `{"implementation": "grant-only-v1"}`. V2 must return exact source/helper hashes;
  a different identity cannot silently reopen the database.
- `_source_pool_proof(state, sale_order, acquired_order, trade_id)`: called for
  every actual acquisition. V1 requires identical pools and returns `None`. A
  reviewed PSM override must return independently derived immutable transition
  evidence, including same-pool PSM cases, from its pinned canonical validator.
- `_decorate_source(state, source)`: V1 is an identity operation. V2 may add pinned
  deduplicated transition proofs; it cannot alter verified native amounts or IDs.
- `_stage_earmark(db, source, event, snapshot)`: V1 writes the two equal cash legs.
  V2 may additionally update detached PSM reducer state, but must keep all changes
  inside this same paper-database transaction and preserve both cash legs.
- `_validate_funding_allocator(db, allocator_state)`: V1 rejects any reservation or
  commit. V2 must reconcile every native reservation/debit/release with its durable
  funding journal before allowing these states.
- `_funding_projection(db, state, pools, escrow)`: V1 returns the input pools and
  escrow unchanged. V2 may apply verified balanced source/escrow/dedicated-entry
  transfers. The outer projection independently enforces nonnegative escrow,
  source-pool availability, protected reserve and physical cash conservation.
- `_pending_funding(db, state)`: V1 is empty. V2 must return every unresolved saga
  ID so intermediate/crash states disable new spending.

These are trusted-code extension points, not user-supplied authorization callbacks.
Default behavior remains fail-closed. No V2 subclass or funding journal is enabled
by this module.

## Native arithmetic and storage maintenance restriction

Per-lot basis and fee allocation is independently replayed in native event
insertion order with precision 28 and ROUND_HALF_EVEN, matching the pinned native
execution convention. Entire-owner cross-lot quantity allocation, including the
last-lot residual, is independently replayed in native creation-time/lot-ID order.
Every scoped original acquisition is checked before candidate filtering, so a
losing lot cannot disappear through rewritten timestamps or owner metadata.
Execution under a different undocumented Decimal context is unsupported; no
rounding tolerance is invented to accept it.

SQLite event rowids are recorded as part of this replay-order evidence. Do not
VACUUM, rewrite, delete/reinsert or migrate active native event tables; SQLite may
renumber rowids. Such maintenance can fail closed against frozen proof bytes and
requires an explicit verified migration/reconciliation procedure. The bridge has
no automatic maintenance exception.
