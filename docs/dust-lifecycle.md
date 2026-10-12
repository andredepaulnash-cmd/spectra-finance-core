# Evidence-bound dust lifecycle

## Scope and rollout

This is an opt-in accounting and entry-admission correction. It is disabled by
default. The runtime enables it only when `SPECTRA_DUST_LIFECYCLE_ENABLED=1`.
The flag does not grant trading authority, register a new tool, place an order,
change capital policy, or enable a deployment. No live account activity is
required by the tests.

The implementation is connected to existing production helpers:

1. `EconomicPositionLedger.open_position` accepts the optional
   `dust_lifecycle` and `dust_checkpoint_ids` gate.
2. `reserve_kraken_capital` checks dust admission after the portfolio await,
   binds the exact candidate proposal to a successor, then uses the existing
   Capital Governor reservation.
3. `create_grid_executor` and `create_dca_executor` share `create_executor`.
   That existing boundary validates the durable admission and exact capital
   claim before dispatch. Dust entry uses a fresh, uncached, exact-pair venue
   rule response.
4. The returned authorization's `economic_position_id`, and the successful
   create result's `spectra_position_id`, identify the successor for existing
   exact order/fill reconciliation.

Normal callers cannot evade an existing dust predecessor by omitting the gate
or disabling the flag. With the flag disabled and no persisted dust/admission,
normal pre-existing behavior is retained. Enabling the flag does not schedule
an autonomous dust classifier or a price/order-evidence refresh job. The
classification service must receive and persist the required evidence before
ordinary entry authorization can use it.

## Complete caller sequence

The caller is the existing execution/reconciliation application that owns the
position's exact proposal/executor manifest. **A successful sell alone does
not classify its remainder.** The application must run the following sequence
explicitly; flipping the rollout flag alone does not repair an existing wallet.
A background scheduler is not required.

Before this sequence, retrieve complete actual order/trade snapshots for every
original position owner, including owners whose lots are already fully sold.
Retrieve a fresh terminal executor observation for each and a fresh exact-scope
sell-reference price and full venue rule. Missing owners, pending orders,
unvalued fees or an unresolved external outcome must stop the sequence. Do not
turn a cached object into fresh evidence by restamping its time.

The existing public fetch helpers are
`fetch_executor_trade_snapshot(client, *, executor_id)` and
`fetch_executor_order_snapshot(client, *, executor_id)` in
`mcp_servers.hummingbot_api.tools.executor_provenance`. Read trades before
orders. Their strict parsers check identity and response counts. The sequence
below uses their returned snapshots, a validated executor observation, and the
caller’s complete exact-owner manifest; it is not an instruction to submit any
unapproved order.

```python
from mcp_servers.hummingbot_api.tools.executor_create import create_grid_executor
from mcp_servers.hummingbot_api.tools.executor_provenance import (
    synchronize_executor_economic_ledger,
)
from spectra_core.executor_provenance_bridge import synchronize_executor_evidence
from spectra_core.fill_reconciliation import LiveFillLedgerReconciler
from spectra_core.kraken_authorization import reserve_kraken_capital
from spectra_core.models import DecisionStatus
from spectra_core.runtime import (
    get_capital_governor,
    get_dust_lifecycle,
    get_economic_position_ledger,
)

ledger = get_economic_position_ledger()
governor = get_capital_governor()
dust = get_dust_lifecycle(governor=governor)
reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)

# 1. Apply actual confirmed fills; retain all original owners and receipts.
orders_for_checkpoint = []
for owner in exact_position_owners:
    report = synchronize_executor_evidence(
        reconciler=reconciler,
        position_id=position_id,
        proposal_id=owner.proposal_id,
        order_snapshot=owner.order_snapshot,
        trade_snapshot=owner.trade_snapshot,
        now=reconciled_at,
    )
    if not report.fully_reconciled:
        raise RuntimeError("complete ownership reconciliation is required")
    governor.reconcile_executor_accounting(owner.terminal_observation)
    orders_for_checkpoint.append(owner.order_snapshot)

# 2. Persist the fresh venue/price evidence, then capture and use the checkpoint.
# checkpoint_observed_at is after completing the actual evidence batch above.
dust.observe_market(
    market_id=market_id,
    account_name=account_name,
    connector_name="kraken",
    price=observed_sell_price,
    rule=observed_venue_rule,
    observed_at=market_observed_at,
    now=checkpoint_observed_at,
)
dust.capture_checkpoint(
    checkpoint_id=checkpoint_id,
    position_id=position_id,
    market_id=market_id,
    order_snapshots=tuple(orders_for_checkpoint),
    observed_at=checkpoint_observed_at,
    now=checkpoint_observed_at,
)
dust.classify(checkpoint_id=checkpoint_id, now=checkpoint_observed_at)

# 3. Use the existing authorized entry path; it discovers and checks all
# persisted predecessors and opens the exact successor through open_position.
authorization = await reserve_kraken_capital(
    client,
    account_name=account_name,
    trading_pair=pair,
    intent="ENTRY",
    amount_quote=approved_entry_amount,
    governor=governor,
)
if authorization.decision.status != DecisionStatus.RESERVED:
    raise RuntimeError("capital authorization did not reserve the entry")
result = await create_grid_executor(
    client,
    connector_name="kraken",
    trading_pair=pair,
    account_name=account_name,
    spectra_proposal_id=authorization.proposal.proposal_id,
    spectra_intent="ENTRY",
    **approved_grid_config,
)
if "error" in result:
    raise RuntimeError(result["error"])  # reconcile; do not retry blindly
successor_id = result["spectra_position_id"]

# 4. Later, fetch actual confirmed successor fills through the existing bridge.
await synchronize_executor_economic_ledger(
    client,
    position_id=successor_id,
    proposal_id=authorization.proposal.proposal_id,
    governor=governor,
    ledger=ledger,
    now=successor_reconciliation_time,
)
```

`approved_grid_config` supplies the existing required side, start/end/limit
prices and total quote amount; its amount must match the exact authorization.
It must come from the separately authorized entry decision. The DCA helper
uses the same guarded create boundary. All timestamps must describe actual
fresh observations, not a fabricated later as-of value.

For a fresh market/rule change, call `observe_market` again with a new market
ID and actual observation time. This is the explicit reclassification
entrypoint. Obtain new evidence and use a new checkpoint ID before attempting
any later admission. Do not call `open_position` directly without the gate as
a workaround.

The full-flow regression is
`test_full_flow_confirmed_sell_dust_admission_create_and_successor_fill` in
`tests/test_dust_lifecycle.py`. It runs the confirmed BUY/SELL reconciler,
market observation, checkpoint capture, dust classification, existing capital
reservation and grid creation, and confirmed successor-fill reconciliation.
It checks that the old residual still belongs to its original position and
proposal throughout.

## What classification changes

`ACTIVE → DUST` releases an active scheduling slot. It does **not** close an
economic position or dispose of inventory. Every economic lot keeps its exact:

- Position, proposal, executor and ownership identities
- Original and remaining base quantities
- Original and remaining cost basis and entry fees
- Existing confirmed sale and realized-PnL history

No cash, fictional sale, capital release, basis deletion, ownership transfer or
buy-to-clear is created. Strategic-retained and unattributed holdings cannot
satisfy the owned residual test. Wallet balances are used by the existing
capital envelope, never as proof of dust ownership.

`list_active_position_ids()` counts active slots.
`list_inventory_position_ids()` includes `ACTIVE`, `DUST` and `DUST_REVIEW` for
inventory and exposure consumers. `snapshot()` and `list_active_lots()` retain
the original position's residual inventory. `DustLifecycle.valuations()` marks
residuals separately without treating their value as cash or realized profit.

New confirmed exits also append `economic_exit_allocations` in the same
transaction as their lot and event changes. Each row records the exact source
lot, original owner, sold base, allocated basis and allocated entry fees.
Legacy events are not assigned guessed allocation histories.

## Required checkpoint evidence

`DustLifecycle.capture_checkpoint` persists an evidence-bound checkpoint, not
a caller-supplied “fully reconciled” boolean. Capture and use require:

- Positive remaining owned inventory, a real reconciled entry and a real exit
- Exact scoped proposal/executor provenance and execution receipts
- Complete terminal order snapshots matching durable order bindings
- Per-order filled quantities matching confirmed fill receipts
- Each entry lot and exit event matching its actual fill and fee geometry
- Per-owner conservation of base, basis and entry fees
- A durably terminated owner executor whose cumulative turnover and fees match
  the reconciled fills
- Fresh executor evidence and a fresh exact-account, venue and pair market
  observation; the default freshness limit is 30 seconds
- No current position mutation lock
- No pending same-base capital reservation, including aliases and other quotes
- No uncompleted partial-sell dispatch claim, including a separately created
  sell executor that is absent from the original owner's order snapshots
- No orphaned partial dispatch claim with unresolvable ownership
- No unreleased recovery obligation, and no recovery release larger than
  proven disposed basis

Completed historical partial-sell claims remove only the pending-dispatch
blocker. They are **not** proof of a reconciled SELL. The source fingerprint
includes locks, partial dispatch claims and their manifest states. Changes
invalidate an already captured checkpoint.

### Supported SELL reconciliation routes

Checkpoint capture currently supports SELL fills from the original lot-owner
executors, explicitly bound and journaled by `LiveFillLedgerReconciler`, either
directly or through `synchronize_executor_evidence` /
`synchronize_executor_economic_ledger`. Every economic exit event must match an
`economic_trade_fill_reconciliation` SELL result, with matching order bindings,
exact owner provenance, fill geometry and terminal executor totals.

The existing `execute_economic_position_exit` adapter requests stops of those
same owner executors. Its resulting fills can qualify after the caller runs
the reconciliation sequence above; the stop request and
`reconcile_economic_exit_owner_accounting` alone do not qualify.

The separate SELL executor used by `partial_recycle`, and native selected-child
exits, record economic events outside that fill journal. Their native receipts,
per-lot exit allocations and completed manifests do **not** currently bridge
into dust checkpoint evidence. Residuals containing those events fail closed,
even after their dispatch claims complete. Passing the separate SELL executor
snapshot cannot substitute for the original owner's identity. Do not replay
an already-applied native exit through the generic fill reconciler to work
around this limitation: that is not an evidence import and risks a duplicate
economic mutation. Supporting those routes requires an explicit,
owner-preserving native-receipt adapter; none is supplied or scheduled here.

`test_unjournaled_ledger_mutation_blocks_checkpoint` covers the missing SELL
journal refusal. The completed-claim test covers only removal of the pending
blocker with independently valid original-owner fill evidence; it does not
claim native partial-recycle SELL support.

Capital Governor ingestion appends `executor_accounting_refresh_receipts`
bound to the exact observation ID, proposal and executor. An identical fresh
reread therefore updates freshness evidence without changing the original
observation, counter totals or economic deltas. A receipt for another executor
or observation cannot freshen the required evidence.

## Venue geometry and prices

`residual_geometry` floors the exact owned base to the venue's base increment,
floors the reference sell price to its price increment, and compares the exact
resulting notional with both venue notional minimums. Positive increments and
a supported LIMIT route are mandatory; venue unavailability is not dust.

A base-sized LIMIT sell does not submit a quote budget. Its quantity-times-price
notional must not be rounded down as though it were a quote-budget order:
that could falsely classify an executable residual as dust.

`preflight_rounded_sale` evaluates both a proposed sell and its actual rounded
remainder. It defers a planned partial sale that would leave untradeable units.
Actual exchange partial fills can nevertheless leave dust; their confirmed
reconciliation precedes classification.

This geometry is not a liquidity or fill guarantee. It does not prove a
profitable sale, use an optional profit target as an exchange minimum, or
establish exchange-side atomicity. Actual fees must be confirmed. Quote fees
and base-denominated fees follow the existing fill reconciler; unvalued
third-currency fees and incompatible executor accounting totals fail closed.

## Successor entry and the final dispatch fence

A successor must pass every same-base predecessor checkpoint. Duplicate active
positions remain prohibited. Multiple dust predecessors retain separate
identities. Their combined same-account/venue quantity is also checked: repeated
dust cycles cannot hide an executable accumulated old balance. The gate never
pools or sells those lots.

The durable entry admission binds proposal ID, successor ID, account, pair,
amount, leverage and exact checkpoint IDs. Only that exact candidate's pending
capital reservation is excluded from predecessor fingerprints during dispatch;
other pending claims remain blockers. Admission uses these states:

- `ADMITTED`: slot and evidence are bound; no send has been claimed
- `CLAIMED`: the exact committed proposal consumed its single-use send claim
- `EXECUTED`: an exact Governor execution receipt was observed and recorded
- `CANCELLED`: a proven unsubmitted, empty admission was retired

After asynchronous venue reads, `create_executor` checks selected-owner locks
and dust evidence, uses the existing exact `commit_if_matches`, then checks
both again and durably consumes the admission. There is no intervening await
before entering the client create call. SQLite write locks protect local
checkpoint revalidation; they are not held across HTTP.

Fresh venue constraints must agree with the checkpoint. Incomplete rules,
changed thresholds, wrong accounts, changed owner inventory, pending orders,
changed locks, expired evidence, duplicate claims and unsupported cross-pair
admission fail closed. Cross-pair dust entry currently requires additional
explicit fresh venue evidence; the normal enabled success path is same-pair.

A local fence cannot prevent the exchange or market changing while an HTTP
request is in flight. Such changes require observation and reconciliation.
Concurrent callers cannot consume the same admission a second time.

## Restart, rejection and explicit reconciliation states

A sizing rejection before dispatch cancels only an `ADMITTED` admission with
no receipt, lot, event, bound order or position mutation lock. Its empty
successor becomes `CLOSED`; the original dust and all audit history remain.
A subsequent correctly sized entry can use still-fresh predecessor evidence.
Terminal non-owning rejected/released/expired reservations do not alter the
ownership fingerprint.

Expired RESERVED admissions use the Governor's RESERVED-only release
transition, followed by transactional verification before retiring the empty
slot. A crash between admission and reservation can be recovered only after
the admission lease and proof that no capital claim or execution evidence
exists. `reconcile_unsubmitted_entries` is used by admission/checkpoint paths
and is restart-safe.

These states deliberately require explicit reconciliation:

- COMMITTED capital, including a post-commit safety refusal
- CLAIMED sends with an error, missing executor identity or uncertain outcome
- Receipts inconsistent with the exact admission or source ownership
- Unreleased recovery obligations
- Stale/incomplete order, market, fee or accounting evidence
- Pending partial or selected-child mutation ownership
- Executable accumulated old residuals

The automatic cleanup path never recycles COMMITTED, CLAIMED or EXECUTED
activity and never treats network failure as proof that an order was not sent.

## Reclassification

`observe_market` persists newer exact-scope market evidence. If an existing
residual becomes executable because price or venue constraints changed, its
status becomes `DUST_REVIEW`. Its original owner and inventory remain intact,
even when a distinct successor already exists. Re-entry is blocked.

A new safe checkpoint can classify it as dust again if it becomes genuinely
untradeable. `DUST_REVIEW` does not automatically reactivate or sell the old
position. Cleanup, transfer, aggregation, or a new execution route requires an
explicit owner-preserving reconciliation workflow.

## Verification

Synthetic tests in `tests/test_dust_lifecycle.py` exercise exact rounding,
rounding-induced remainders, confirmed partial fills, repeated sell/dust/reentry
cycles, multiple predecessors, preserved basis and ownership, aliases,
executable accumulation, pending capital and sell markers, mutation locks,
stale/foreign evidence, market reclassification and restart/idempotence.

Enabled integration tests call the existing capital-authorization and grid
create helpers, reconcile a successor fill, inject changes during the venue
await and after commit, check fresh rule changes, reject legacy bypass,
recover a rejected/expired unsubmitted admission, and hold a send across a real
async wait while a competing caller attempts the same admission. Ambiguous
outcomes retain their durable claims.

`tests/test_economic_exit_allocations.py` verifies exact allocations, replay,
transaction rollback and honest legacy coverage. Existing accounting,
recovery-exposure and runtime suites remain regression requirements. These are
mechanics and admission-safety tests, not profitability, exchange fill or
production deployment evidence.
