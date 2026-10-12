# Selected original-child recovery: gated runtime integration

## Status

This is an **optional, disabled-by-default correctness path**, implemented by:

- `spectra_core.selected_recovery`: durable original-child partitions, source
  ownership, ladder budgets, exit manifests, and owner cash accounting
- `spectra_core.selected_recovery_runtime`: explicit internal
  `run_selected_recovery_cycle` entrypoint and `SelectedRecoveryRuntime`
- `spectra_core.selected_recovery_ledger`: exact native economic-ledger exit
  quotas, allocation receipts, and sealed recovery opening-basis evidence
- `spectra_core.position_mutation`: shared per-position mutation ownership

The enabled helper performs governed dispatch and reconciles actual core ledger
state. It is not registered as a new MCP tool and is not automatically selected
by the legacy VCAP strategy. Both the book and runtime must be explicitly enabled
for a new dispatch. Tests enable the helper against a fake exchange and real
SQLite/core accounting components. No real exchange execution was performed.

Disabling rollout prevents **new orders**. Exact reconciliation of a previously
checkpointed authorized attempt remains available, including after an ambiguous
send, so disabling cannot strand confirmed executions or source settlements.

This fixes mechanics; it does not establish a profitable strategy. Historical
sequential doubling failed its economic objective. No asset ladder, recovery
probability, sizing rule, or trading configuration is economically approved by
these tests. A profitable selected child can coexist with a losing portfolio.
No protected holdout data or running regime experiment is touched.

## Exact original-child behavior

1. Import one ACTIVE parent's exact remaining original ENTRY lots. The read-only
   bridge requires lot identity and scoped governor proposal/executor provenance.
   It rejects wallet-balance ownership, unscoped/manual/unattributed lots, and an
   already aggregated recovery position. Account, connector, controller, source
   proposal/executor, base, gross basis, and entry fees remain attached.
2. Name proposed equal-size original children. Initialization and planning do
   **not** split or select inventory. Healthy positions, declined plans, unfilled
   orders, and cancellations without fills remain unsplit.
3. Only a positive, already-reconciled exact first patch trade atomically creates
   the durable child partitions. These are exact source quotas; original ledger
   fill identities are preserved rather than destructively duplicated. Original
   membership/amounts/fees/owners and the execution rung are rechecked.
4. Lock one selected original child. Each successive explicit ladder rung adds
   that child's **current fee-inclusive invested basis**: an original 20-basis
   child requests 20, 40, and 80, reaching 40, 80, and 160. Partial fills and
   terminal retries finish the same frozen remainder. An unaffordable rung is
   blocked whole. Rungs cannot skip/repeat, the ladder cannot change, and another
   child cannot replace the selected one.
5. An exit manifest contains the entire remaining selected original child **and
   all its patch slices**. It must satisfy exact exchange increments/minimums and
   the configured net return after expected exit costs. The runtime sends a
   full-manifest limit order. The default book floor is 174 bps; that is a
   mechanics setting, not a strategy recommendation.
6. Native exit scopes retain each selected source's base/basis/fee quotas plus
   expected whole-lot remainders. Confirmed partial fills consume only those
   quotas. The original source lot's untouched sibling portion remains intact.
   Internal proportional allocations use 24 decimal places with explicit final
   residual closure; no quantity, cost basis, or fee residual is discarded.
7. Partial sales retain the selected-child lock and prevent further patches to
   that child. Another child becomes eligible only after every selected original
   and patch unit sells, source settlements finish, and terminal order totals
   equal the exact imported trades. Fully closing the parent releases its
   mutation lock, allowing a new normal parent for that asset.

## Capital and dispatch boundary

`plan_patch` and `plan_exit` are pure reads. They cannot reserve capital, select
inventory, or submit orders. The runtime requires an **existing exact RESERVED
RECOVERY proposal** and consumes it through `CapitalGovernor.commit_if_matches`.
It creates no additional reservation, broad permission, leverage, or account
access. Kraken spot at 1x leverage is the only admitted dispatch scope.

The all-in claim includes an explicit buy fee reserve. The submitted DCA gross
amount is the all-in budget less that reserve. Actual confirmed gross plus fees
must fit the exact frozen budget. A partial or fee-underfilled rung retains its
unspent remainder; it is never silently declared doubled. A remainder below
venue minimums defers rather than borrowing money or shrinking a new rung.

Additional admission checks include:

- Existing modeled VCAP cash less an explicit protected cash floor
- Maximum outstanding recovery basis, including fees
- Maximum whole-asset remaining basis, including every untouched sibling
- Current exact venue minimums/increments and a priced limit order
- Fresh fully reconciled accounting and market evidence
- Breaker state and current selected-book/native-source consistency
- Exact passive source executor orders and persisted trade ownership
- No competing same-asset RESERVED/COMMITTED claim or unowned unresolved executor
- Shared durable position ownership excluding partial/whole-parent exits

Freshness, book/native consistency, claim conflicts, and lock ownership are
checked after awaited source reads and again after capital commit, immediately
before the send. Unknown executor lookup results never become absence proof.

Before any network mutation, the runtime records a unique durable action and a
deterministic executor identity. It never retries an uncertain create. Restarts
query that exact executor's authoritative order/trade surfaces and reconcile it;
missing or contradictory evidence leaves the action pending/blocked.

## Owner settlement and governor recycling

Each source retains exact sale quantity, released gross basis, released entry
fees, allocated gross proceeds, exit fee, net cash, and realized P&L. Original
ENTRY sources settle to the local PRODUCTIVE bucket and recovery sources to the
local VCAP bucket, within their same account/controller scope. Proposal and
executor identity remain on every source allocation. Productive proceeds are
never silently reassigned to VCAP.

Those cash buckets model source-owned funds; they are **not exchange deposits or
independent governor authorizations**. Initial VCAP cash is an explicit opening
balance, not new money. There is no top-up/reset API. A next child uses existing
VCAP proceeds and still needs its own exact governed claim. Multiple books must
not duplicate the same cash allocation.

Confirmed native recovery gross-basis releases feed the existing idempotent
`record_recovery_recycle_release` mechanism. A selected-only sealed opening-basis
receipt addresses the all-in claim's fee component without inventing a fee
release or a PositionHold: terminal order totals, exact persisted trades, native
lots, book sources, scoped provenance, and passive executor gross/fee counters
must all agree before sealing the actual gross opening basis. For that proven
selected source only, the governor uses gross opening basis minus the existing
confirmed gross releases. A 20 all-in claim containing 19.92 gross and 0.08 fee
therefore tracks 19.92, then 9.96 after half sale, then zero after full sale.
Ordinary unsealed POSITION_HOLD behavior remains conservative and unchanged.
Missing/inconsistent scope or counters do not qualify for the override.

## Durability and fail-closed behavior

The child book journals state and receipts atomically. Native economic exits
journal source allocations, source remainders, and economic events atomically
in the economic ledger. These and governor releases are separate transaction
domains, coordinated with immutable event IDs and ordered idempotent replay.
Tests cover interruption after native settlement but before child-book/owner
settlement, without duplicate selling or release.

Position locks and the final synchronous pre-send checks fence local dispatch;
they are not exchange-side atomicity. The exchange may change after the last
snapshot or accept an order whose response is lost. Such ambiguous attempts
retain their durable identity and lock until exact reconciliation.

Every book transition checks global, per-owner, and exact source/child
conservation. Canonical plans bind the complete state hash. Changed plans,
original manifests, source ownership, order identities, trade identities,
quantity totals, or terminal geometry fail closed. FILLED requires the full
order amount; duplicate and unknown-order evidence is rejected.

An actual execution cannot be undone by rejecting an import. Conflicting source
facts leave the path blocked for investigation; no replacement order is sent.
A confirmed sale below its planned fee-aware floor is accounted for and durably
flagged, blocking further patch proposals. It is never relabeled profitable.

Only confirmed quote-denominated fees are supported. Exact rational planning
avoids loss during subdivision, but the execution adapter rejects amounts that
cannot be represented exactly as Decimal or violate venue increments. There is
no implicit dust write-off, approximate sell-all, or unsupported fee conversion.

## Explicit lifecycle entrypoint

`run_selected_recovery_cycle` takes a `SelectedRecoveryRuntime`, explicit child
and ladder, current market/rule/accounting evidence, remaining authorization,
and an already-reserved proposal ID for a proposed BUY. It resumes pending work
first, otherwise prefers an eligible whole-selected-child exit, then considers
the next patch rung. It does not choose the strategy, child, ladder, fee estimate,
or capital budget for the caller.

The legacy lifecycle remains unchanged unless a caller explicitly chooses this
separate selected-mode entrypoint. There is no automatic live enablement or new
MCP registration in this promotion. Deployment, asset-specific configuration,
and economic approval remain separate decisions.

## Caller setup, invocation, and restart sequence

1. Load the existing `EconomicPositionLedger` and `CapitalGovernor`; establish
   exact original ENTRY provenance first. Choose one stable book database per
   parent and a genuine existing VCAP cash allocation. Do not duplicate that
   cash allocation in another book.
2. On first use only, explicitly enable the book and call `initialize` with the
   native ledger/governor, exact parent/account/connector/controller, child IDs,
   opening VCAP cash, and `RecoveryLimits`. This records proposed partitions but
   leaves original inventory unsplit. On restart, reopen the same book and skip
   initialization; reinitializing progressed state is rejected.
3. Construct `SelectedRecoveryRuntime` with that book, the same native ledger and
   governor, and an explicitly enabled `SelectedRecoveryRuntimePolicy`. Both
   classes default to disabled. There is no environment toggle that starts an
   autonomous scheduler and no new registered MCP entrypoint.
4. The existing capital-authorization workflow must already have reserved the
   **exact proposed all-in rung remainder**. `book.plan_patch` can report that
   amount without modifying state. A read-only plan is not approval. Obtain
   current rule, market timestamp, accounting-health and recovery evidence via
   the existing account/strategy workflow; unknown/stale inputs cannot dispatch.
5. Explicitly invoke the internal coroutine. The names below are caller-supplied
   verified objects/values, not defaults or suggested strategy settings:

```python
from spectra_core.selected_recovery_runtime import run_selected_recovery_cycle

result = await run_selected_recovery_cycle(
    client,
    runtime=selected_runtime,  # separately constructed with explicit double gates
    child_id=selected_original_child_id,
    ladder=explicit_child_ladder,
    current_price=current_execution_price,
    market_observed_at=market_snapshot_timestamp,
    rule=current_kraken_rule,
    health=current_accounting_health,
    remaining_authorized_quote=existing_remaining_authorization,
    proposal_id=already_reserved_exact_proposal_id,
    buy_fee_reserve_quote=explicit_quote_fee_reserve,
    expected_exit_cost_bps=expected_exit_fees_plus_slippage_bps,
    evidence_allowed=current_recovery_evidence_allows,
)
```

6. If pending or a send was ambiguous, call the same coroutine again, or call
   `await selected_runtime.synchronize(client)`. It queries the checkpointed
   executor, validates orders/trades, reconciles the native BUY and partition,
   and imports exact terminal ownership. Never create a new action to bypass an
   uncertain old one. A disabled rollout can still reconcile this existing
   action; it cannot initiate the next one.
7. On a later explicit invocation whose price clears the selected child's net
   floor, the helper constructs and sends its exact full-child limit exit.
   Confirmed SELLs consume native source quotas, settle the same owner buckets,
   and release confirmed recovery principal. Partial/cancelled sales require
   another explicit invocation to complete the same selected child.
8. Only after full terminal completion may the caller select another untouched
   original child and supply a newly authorized exact claim funded from existing
   capital. Tests exercise that second-child step. No callback schedules itself.

A first attempt that fails before any send, or terminates with zero fills, leaves
original inventory unsplit and releases its temporary position admission lock.
An uncertain send retains its lock. If a capital claim was committed just before
a final pre-send check failed, it remains conservatively recorded for explicit
capital reconciliation; lock cleanup does not invent a refund or retry.

## Verification

`tests/test_selected_recovery.py` covers the accounting kernel.
`tests/test_selected_recovery_runtime.py` exercises the enabled internal path
against a fake exchange, real native ledger, governor, persisted order/trade
bindings, terminal evidence, source allocations, and next-child recycling.

Coverage includes delayed splitting, 20/40/80 budgets, sibling/source isolation,
funding caps, partial retries, replay/restart, concurrent receipts, rollback,
full/partial native exits, ambiguous sends, stale post-await/post-commit evidence,
competing capital, feature disable with in-flight work, terminal geometry,
closed-parent reentry, and fee-inclusive capacity reconciliation. These are
mechanical integration tests, not a live exchange certification or backtest.

```sh
uv run pytest tests/test_selected_recovery.py tests/test_selected_recovery_runtime.py
```
