# Default-off native next-entry admission contract

This is a new paper integration over the pinned PR40 native primitives. It is not
production deployment, a historical result, an external account connection, or
permission to place an order. Only synthetic receipts have been exercised here.

## API and authority

`NativeEntryAdmission(adapter, *, allocator, snapshot_id, cash_bridge=None)` lives
in `research.weekly_reversal.entry_admission`. `helper_identity()` and the instance
`identity()` return concrete source SHA-256 identities. V2 sets `cash_bridge` to
its reviewed `NativeEntryFundingBridge` instance. No caller-provided booleans or
arbitrary approval callback can establish a guard.

- `prove(order_id, *, now, state=None, paper_connection=None)` returns a detached,
  JSON-safe immutable proof with `proof_id`, `proof_sha256`, `helper_identity`,
  `order_id`, `entry_id`, `source_pool`, `entry_pool`, `snapshot_id`, `permit`, and
  exact `evidence`.
- `validate(proof, order=None, *, now, replay=False,
  reconciliation_only=False, state=None, paper_connection=None)` returns the same
  proof bytes/values after ordinary validation. Cleanup-only replay instead
  returns nonauthorizing identity metadata, never a permit.
- `validate_activation(proof, order=None, *, now, state=None,
  paper_connection=None)` verifies the exact atomically activated PREPARING
  state before its first native executor binding, including crash retries.
- `validate_frozen(proof, order=None, *, now, state=None,
  paper_connection=None)` verifies a durable staged proof for cleanup only.
- `paper_connection` is the bridge transaction connection; otherwise the helper
  opens a read-only snapshot. A supplied detached state must equal the SQLite
  state. In-memory adapter state is never admission authority.
- The existing adapter writer lock must remain held. Proof creation never writes,
  advances the actual adapter clock, selects a partial, latches profit protection,
  opens a position, reserves capital, consumes liquidity, or submits an order.

## Prepared order

The order must already exist in paper state with `status=FUNDING_PREPARED`,
`side=BUY`, no terminal result, no fills, and no frozen executable quantity. It is
not executable. Required immutable fields include `id`, `entry_id`, `pair`,
`position` (including a planned successor position), `cohort`, `kind`,
`cohort_generation`, `previous_cohort_generation` for reentry, `source_pool`,
`pool=next_entry:<entry_id>`, `funding_owner`, native `proposal` and `executor`,
`intent`, `target`, `principal`, `base_budget`, `profit_budget`,
`decision_quantity`, `decision_reference`, `decision_at`, `created_at`,
`decision_bar`, `capacity`, `snapshot_id`, `allocation_id`,
`allocation_owner_id`, `allocation_role`, and `repair_rung` for repair.

Initial mutable `quantity/reference` must equal `decision_quantity/reference`.
The unchanged delayed-open model can later freeze different actual quantity and
reference. Replay verifies actual quantity against the full unchanged target,
actual fee rounding, step/tick, minimum, and execution delay/expiry; it never
rewrites the frozen decision fields or increases the approved cash budget.

The exact native proposal must be COMMITTED, with the matching pair, intent,
principal and 1x leverage. This is necessary but insufficient for a permit. An
execution receipt/provenance is not required before funding, because no executor
has been opened; any existing provenance must match. Existing native source
inventory and receipts always require their real native provenance and exact
reconciliation.

## Exactly two entry paths

### REENTRY

The original slot remains `<pair>:seed:<ordinal>`, origin SEED, and must be SOLD
with zero remaining native cohort lots. All predecessor sale orders and their
terminal audits are exact and settled. Another selected partial blocks this
slot. The order references predecessor generation N and proposed generation
N+1; the cohort stays at N until funding succeeds.

Base budget is the lesser of the configured seed ticket and the exact same-root
principal available in the single latest source pool (the explicit pre-run
ordinary-reseed amendment). Profit is the entire
spendable entitlement for that asset in this membership snapshot. Spendable
means the exact integer number of allocation quanta in the available bucket;
released sub-quantum amounts remain in that same bucket. This quantizes order
allocation, not realized profit. Evidence retains raw available, spendable and
remainder separately. The fee-inclusive target is base plus spendable profit.
No held-position top-up is allowed.

Base cash comes from the exact predecessor ENTRY acquisition pool: initial seed,
or a previously funded dedicated next-entry pool with reviewed source lineage.
No other PSM, asset, snapshot, or prior entry pool can be borrowed.

The signal is explicitly **NEW_NEXT_ENTRY_POSITIVE_HOUR_ABOVE_EMA8**. It uses
persisted actual completed `HourBar` dictionaries in
`state['next_entry_completed_hours'][pair]`. The native `IndicatorHistory`
recomputes the EMA8 and positive close-to-close return; no RSI hard gate is
added. At least two contiguous observed hours are required, the latest remains
current, and the qualifying hour begins after sold-slot completion. No signal
flag supplied by the caller is accepted as evidence.

### REPAIR

The original `OwnedSpotAdapter.repair_feasibility` is invoked on a detached
read-only view. Exact selected/cursor-selected cohort, generation, next rung,
post-acquisition bar, native provenance, profit protection, prior rung completion,
and full liquidity must pass. Only its old base-owner-cash shortfall may be
replaced by the explicit base-plus-profit cash proof. Other native blocks remain
blocks.

The fee-inclusive target equals current native cohort basis exactly: the full
double remains unchanged. Profit is the entire spendable VCAP entitlement; base
is target minus profit. If spendable profit is at least target, the positive-base allocator
contract cannot be satisfied; the order fails with
`PROFIT_EXCEEDS_REPAIR_TARGET`. There is no clipped doubling or arbitrary penny
base. Venue minimum applies to the complete order.

## Independent shared guards

The helper derives all permit booleans only after checking:

- Frozen native and configuration identity; $2,000 initial cash; $500 protected;
  $1,500 total basis/cash-claims/holds; $750 per asset; $500 recovery; actual 80bps
  fee rate, current fee quantum, step/tick and minimum rules.
- Full paper/native cash history coverage, exact applied fills, native ownership,
  no quarantine, and fresh whole-account native accounting health.
- Durable original seed barrier; no pending action or exit manifest; no active PSM
  ownership for the asset except a verified completed archived REPAIR handback.
- A fresh actual completed five-minute decision bar, exact causal decision time,
  full un-clipped target against prior completed-bar shared participation.
- Exact same-owner base cash plus cash-backed profit escrow, protected reserve,
  total/per-asset/recovery caps including native inventory, cash claims and holds.
  Every `next_entry:*` cash balance is a same-asset capital claim even after an
  order becomes terminal or sells. Its hold is not counted twice. Repair-owned
  dedicated balances count toward recovery. When a predecessor dedicated pool
  funds the next entry, its exact base transfer is subtracted from the existing
  claim before adding the new full target. Uncommitted profit escrow is counted
  as deployment only when funded.
- Exact frozen complete membership: each configured pair has its pair allocation
  and owner `<config.owner>:<pair>`; VCAP is `VCAP`/`<config.owner>:VCAP`.
  Entitlements never cross snapshots or allocation owners, and authorization
  follows the actual grant strictly.

Every blocked predicate raises `AdmissionBlocked` with a stable `code`. In
particular all NEW ordinary signal blocks are separately countable
(`NEW_ENTRY_HOUR_MISSING`, `NEW_ENTRY_HOUR_INVALID`, `NEW_ENTRY_HOUR_STALE`,
`NEW_ENTRY_HOUR_PRE_SALE`, `NEW_ENTRY_HOUR_NOT_POSITIVE`,
`NEW_ENTRY_BELOW_EMA8`, `NEW_ENTRY_HOUR_FUTURE`). The helper itself remains pure;
the orchestration layer records gate-blocked counts.

## Saga and replay

1. Prepare a non-executable order and commit its exact native governor proposal.
2. Freeze the admission proof and immutable RESERVE outbox in paper SQLite.
3. Recompute current guards before the first allocator reservation. The helper
   ignores only its own exact PREPARED marker after checking its proof digest,
   order binding, and matching RESERVE payload. All other unresolved sagas block.
4. Reserve exact allocator entitlements; fund the dedicated pool with balanced
   base/profit cash legs; verify all journal/allocator/native identities.
5. Only then may the adapter create the planned position, transition the cohort,
   consume liquidity and make the order executable under native guards.

First-reserve retries preserve original IDs and approval/expiry times. They check
current accounting health and unchanged decision authority; no newer bar or
larger order can reuse the proof. Accepted-reservation replay additionally
requires an actual identical allocator reservation, original native proposal,
unchanged membership, original source events, and original receipt bytes.
Replay never turns an expired FUNDING_PREPARED order executable. Existing funded
order reconciliation may use its original proof after expiry; settlement and
closure remain separately verified by the native settlement helper.

Every validation independently recomputes the proof ID from frozen evidence and
reviewed helper identities, then verifies its derived permit ID and root/permit/
order scope and amounts. A self-consistent changed JSON hash cannot retain an
accepted allocator permit's original proof ID. Concrete helper identities also
pin the compounding coordinator, original adapter/model/PSM code, native signal
implementation and allocator implementation.

`validate(..., replay=True, reconciliation_only=True)` requires the original
actual accepted allocator reservation but may acknowledge that same reservation
after expiry. It returns `order_permission=False` and
`reservation_permission=False`. It cannot grant OPEN readiness. An unaccepted
staged reservation uses `validate_frozen`, which requires its original durable
funding row, exact proof bytes/digest and original native/order/source identity.
Neither method establishes terminal native liability closure; the settlement
helper must separately verify that closure.

## Atomic activation and crash recovery

After verified funding, the coordinator creates only the pinned planned native
position, then atomically persists cohort generation or unchanged repair rung,
the exact liquidity debit, status PREPARING and an independent
`weekly_entry_activation` audit payload containing the order ID and activation
`at`, `proof_sha256`, `liquidity_key` and `liquidity_debit`. Only
`validate_activation` authorizes resuming this first native executor binding.
It requires the original unexpired approval, exact independent audit anchor,
unchanged completed bar and inventory, exclusive pending ownership, reconciled
funding, current capital caps, exact advanced generation/history or repair rung,
and exactly one original liquidity debit. Every historical/current native
position must retain fresh accounting health. The sole exception is the exact
newly planned empty successor, whose first health report follows executor
binding. A normal replay result alone cannot authorize first execution.

Funded activation requires the identical actual allocator reservation and exact
FUNDED journal proof. Base-only activation requires its separate original
admission proof and audit anchor, without fabricating an allocator reservation.
An expired or altered activation blocks recovery rather than refreshing its
proof, advancing its generation again or executing a larger order.

## Pure preflight and zero-profit ordinary entry

`preview(order_id, *, now, base_only=False, state=None,
paper_connection=None)` evaluates the same durable candidate and real guards
without requiring the candidate's native commitment to exist yet. It returns
only diagnostics, `order_permission=False`, `eligible_except_native`, and an
explicit `native_committed`/`NATIVE_RESERVATION_MISSING` result. It never returns a
reusable proof or permit, creates a native reservation, or weakens final `prove`.
The integration can count normal signal/cash blocks before protecting native
capital. Existing conflicting native reservations remain blockers.

An ordinary sold-slot reentry must not be silently disabled because its spendable
profit is zero. `prove_base_entry` and `validate_base_entry` share the
same read-only guards for a durable `ENTRY_PREPARED` REENTRY, positive unchanged
same-root source-principal budget capped by the configured seed ticket,
`profit_budget=0`, and `pool==source_pool`. The
exact spendable asset entitlement must be zero; positive spendable profit cannot
be bypassed with this path. Any positive sub-quantum remainder remains allocated
and unspent. A settled loss still supplies frozen membership from
the bridge's snapshot table even if the allocator has never granted an event.

The distinct result contains `base_entry` with kind `BASE_ONLY_NEXT_ENTRY`,
`permit=None`, and `allocator_reservation_required=False`. It is not an allocator
EntryPermit, never makes an allocator reservation, and needs no dedicated funding
transfer. The adapter persists it as `order['base_entry_proof']` and anchors its
digest independently in the `base_entry_admitted` audit event within the original
approval window. It validates the proof again before ordinary native execution.
`validate_base_entry(..., replay=True)` requires that original audit and the
actual native executor receipt. `validate_base_entry(..., replay=True,
activation=True)` is the narrowly scoped PREPARING crash-recovery branch used by
shared `validate_activation`; it cannot skip the atomic activation checks.
Fresh admission/expiry, immutable source
pool, all native checks, the NEW hour gate, generation, risk, minimum, fee and
liquidity rules still apply. If it spends a predecessor dedicated pool, the same
cash-claim-to-inventory accounting prevents double counting.

## Terminal reentry slot outcomes

An activated generation remains provisional until it actually acquires native
inventory. `WeeklyCompoundingAdapter` derives one terminal outcome from exact
applied receipts, original native lots and the independent original terminal
audit. A CANCELED/REJECTED zero-fill attempt restores the exact prior SOLD cohort
snapshot and generation from its admitted proof. The failed order, proof,
activation, native position and all audits remain; none are rewritten or deleted.
Expiration/missing-open cancellation uses the original CANCELED status. Funding
closure and native liability reconciliation still gate every fresh admission.

Any actual partial acquisition keeps the activated OWNED generation and receives
its quantity-weighted actual-fill anchor and last acquisition time, even when the
unfilled remainder is canceled. It never becomes an empty/reseedable slot. The
terminal outcome and cohort transition are saved together with one independent
`reentry_terminal_outcome` audit anchor. Repetition and restart verify that anchor
and never append a duplicate price anchor or rewind a later generation. Restart
also finishes a crash between native terminal reconciliation and the slot
transition. Acquisition-source selection includes only actual applied BUY fills,
so a failed zero-fill attempt cannot donate a foreign dedicated pool to a later
successfully acquired generation. Bound post-OPEN REJECTED native executors
remain in exact accounting health coverage; pre-admission rejected candidates do
not acquire native authority.

## Exact same-target repair retries

`repair_retry_feasibility(pair, *, now, state=None, paper_connection=None)` is a
pure diagnostic. Its result always includes `order_permission=False`. The caller
exposes the same method plus `retry_repair(pair, *, now)` for a fresh admitted
attempt. `eligible` means an independently checked candidate, never permission
to reserve, fund or execute. The ordinary `preview`, `prove` and activation APIs
then perform their full current checks and require the exact native commitment.

A retry stays kind REPAIR and carries immutable `repair_retry` scope:
`root_order_id`, `prior_order_ids`, `requested_target`, `start_basis`,
`prior_fee_inclusive_debit`, `remaining` and `level`. Its complete order target is
exactly the original rung target minus every prior exact fee-inclusive debit.
The root target/start basis, rung level and generation cannot change. Native
original lots, receipt coverage, terminal audits and unchanged owned basis must
reconcile; previous funded attempts must be CLOSED before admission. There is no
new full double, no omitted partial fill and no clipped residual.

The original native lifecycle planner runs on actual persisted selected-cohort
state with a capture-only checkpoint store in the diagnostic; native profit exit
and profit protection still block, and neither preview nor proof mutates their
latches. Actual retry orchestration retains the original native selected-cohort
checkpoint before final admission. Current whole-account health, ownership, PSM
handback, completed-bar price, fee rounding, tick/step/minimum, full participation,
protected reserve and all capital caps remain required.

Spendable VCAP profit funds part of the exact residual through the unchanged
funding saga. If it leaves no positive base, the attempt explicitly blocks with
`PROFIT_EXCEEDS_REPAIR_TARGET`. Zero-spendable retries alone may use the distinct
base proof with source/pool `repair`, without an allocator reservation. If no sale
has yet created an allocator snapshot, that restricted base proof pins the exact
persisted `weekly_compounding_membership`; it creates no grant, entitlement or
allocator snapshot. A residual below venue minimum remains an incomplete,
reported rung rather than becoming a smaller or profit-only order.

Activation appends exactly one new order to the existing rung and updates pending
execution geometry while preserving original target, start basis, prior orders,
actual added cash and shortfall. Crash replay verifies this exact append and the
original approval expiry. Only actual cumulative fills can complete the rung.

## Explicitly unavailable PSM-root reseeding path

An original SOLD seed slot is not empty while any same-root PSM successor owns
native lots or a pending action: `ROOT_SUCCESSOR_HELD` blocks admission. A
historical successor that has completely sold still requires reviewed latest-root
acquisition/disposal and cash lineage, which this version does not yet supply.
It fails closed as `ROOT_SUCCESSOR_LINEAGE_UNAVAILABLE`. It never reuses the
original seed cash implicitly or relabels a PSM successor as SEED. This is an
explicit integration limitation, not a completed latest-root reentry path.

## Isolated V2.3: completed PSM-root ordinary reentry

This extension is prepared in the isolated V2.3 worktree; the earlier live
milestone remains frozen until the combined extension is reviewed and tested.
`root_reentry_source(cohort_id, *, now, state=None, paper_connection=None)` is a
read-only, nonauthorizing source diagnostic. It returns `order_permission=False`
and the actual latest root acquisition's source pool and generation. A PSM
source additionally returns an exact `source_owner`, immutable `root_source`,
`latest_sold_at`, `latest_cohort_ids`, and `available_base_cash`.

A root comprises the original configured SEED partial and every cohort whose
`root_cohort_id` names it. Actual native lots, including dust and lots hidden by
a changed cohort lot list, unresolved root orders, and competing native
commitments block reentry. Every separate successor must have actual archived
PSM acquisition receipts. PSM generations come from their unique completed
handback/receipt anchor, which also handles a rebuild into a previously live
cohort whose decision order retained the preceding generation. Ordinary entry
generations remain bound to the actual filled order. Empty canceled attempts do
not advance the source generation. A later acquired dedicated entry supersedes
older PSM history and retains its exact dedicated-pool lineage.

The new source owner is exactly:
`{schema:1,pair,root_cohort_id,funding_owner:'seed',source_pool:'psm:'+pair,
latest_generation,ownership_proof_sha256}`. It is immutable in both the prepared
order and admission envelope. The complete bridge ownership proof is frozen in
`evidence.root_source`; its root and generation are independently checked against
actual acquisition receipts and anchors. Every original source ID is reread on
frozen, accepted, base-entry, and activation replay. Replay does not grant new
permission or require the historical source still to be the latest generation.

Fresh admission uses `NativePSMRootFundingBridge.root_source_proof` and separately
checks its exact root-plus-seed available cash. Aggregate asset PSM cash, repair
subcash, and another original root's cash cannot make an insufficient root base
eligible. The configured seed ticket caps the source-specific remaining
principal; the order does not replenish losses from other roots. The later
positive hour above EMA8 must begin after the latest root sale completes, not
the earlier original seed disposal. Generation advances from that latest actual
root acquisition while retaining the original configured slot identity and all
successor histories. The helper and caller never relabel PSM successors SEED.

Positive-profit orders retain the reviewed dedicated funding saga; the transfer
and any unused-base return are bound to the same root owner. Base-only orders
remain in the exact source PSM pool, create no allocator reservation, and recheck
the original root proof plus current root cash at first activation. A current
PSM cash claim already counts under the $1,500/$750 limits, so funding subtracts
only its exact base transfer before adding the full new target. No order hold is
counted twice. Recovery-owned PSM cash remains counted under the $500 cap and is
never borrowed by this seed reentry path.

Zero-fill canceled/rejected PSM rebuild descendants are retained as structural
history, not counted as acquisitions. A separate immutable empty-attempt proof
requires every child to have zero receipts/native lots, exact terminal native
proposal/executor/binding and paper terminal audit, and matching completed
archived PSM allocation/action/reconciliation. Such shells may keep their
original `PSM_PENDING` metadata. Arbitrary empty successor records still block.
These proofs are frozen in `evidence.root_empty_attempts` and independently
reread during replay. The next generation follows actual acquired history and
its handback anchor, not empty-attempt labels.


### Explicit pre-run ordinary principal amendment

Ordinary REENTRY base is `min(configured_seed_ticket, available_base_cash)` from
the one exact latest acquisition source pool. This is a declared new reseed
policy; it does not change REPAIR full-double targets or residual retry scope.
The complete native minimum, tick, fees, shared participation and risk checks
apply to base plus the spendable allocated profit. `BASE_PRINCIPAL_EMPTY`
explicitly rejects zero-base/profit-only orders under the unchanged positive-base
allocator contract. A subminimum full order remains `MINIMUM`; preview includes
base, profit and total budgets so callers can count it separately.

`evidence.root_principal` independently starts each original root with only its
configured initial seed allocation, adds/subtracts its actual native-covered
receipts, subtracts its exact realized-profit cash debits, and applies its own
FUND/RETURN legs. Shared `seed` or PSM pool totals never substitute for root cash.
For a PSM source this independent ledger must equal the bridge's exact root-seed
subcash proof. All receipt/owner, cash-debit and transfer rows are frozen and
reread on replay; accepted identity anchors bind the original balance and base.
Other same-root positive pool balances are reported as
`unavailable_to_this_entry`, without a multi-pool transfer or claim that those
amounts were spent. Base-only activation rederives the exact source ledger.
