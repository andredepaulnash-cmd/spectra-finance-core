# Protected recovery-partial exits: validation receipt

## Scope

Base: `spectra-core-phase2` at `6e894fa9c2b55384d0f6120f41ca91892e1fe207` (PR #25).

This isolated draft closes the mismatch between profitable recovery-lot selection
and unprotected MARKET dispatch. Newly selected recovery parcels now retain their
configured net target and submit an exact-quantity LIMIT order at a durable price
floor. The whole-position exit path and strategy economics are outside this patch.

The existing default recovery target remains 174 basis points. No target is changed
to 100 basis points by this change. No replay, DCA ladder, parked-cash authority,
DTSS/PSSM policy, production merge, deployment or live order is included.

## Protected price and ownership

Let `B` be the selected base quantity, `C` acquisition basis, `F` entry fees,
`T` configured net-profit basis points and `E` configured exit-cost basis points.
The minimum raw price is:

`(C + F) * (1 + T / 10000) / (B * (1 - E / 10000))`.

The submitted price is rounded upward to a valid exchange tick and also satisfies
the minimum quote notional. Selected quantity must already meet the base step and
minimum; it is never rounded upward into ENTRY or another owner's inventory.
Exit costs retain the caller's existing fee/slippage allowance.

The existing economic ledger disposes of the selected lots proportionally. The
floor protects that aggregate parcel; it does not introduce FIFO disposal or
require every consolidated child to clear a separate target. ENTRY,
STRATEGIC_RETAINED and UNATTRIBUTED units remain outside the permitted partial sale.

## Durable submission and reconciliation

- Persist the target and floor with the immutable lot/owner manifest.
- Acquire one SQLite dispatch claim before the external create request. Competing
  callers cannot both submit, and the claim survives process restart.
- Only an explicit HTTP 404 establishes absence before first submission. A timeout,
  server failure, empty response or 404 after a claimed submission blocks retry.
- Reconcile known executors before guards that apply only to fresh submissions.
  Pending LIMIT orders remain accountable when price, breaker or minimums change.
- Acknowledgements and executor status do not release capital. Confirmed,
  exactly scoped fills update inventory, released basis and realized P&L once.
- Record confirmed financial truth even when exchange evidence violates protection;
  persist the conflict and hold the manifest instead of presenting a successful exit.
- Detect non-LIMIT/below-floor order evidence and oversubscribed outstanding orders.
- Completion independently requires durable fill coverage equal to disposed
  manifest inventory and a fee-inclusive profit check on each protected journal row.
  This closes the crash window between ledger/journal/conflict commits, including
  when a later provider snapshot omits the offending trade.
- Legacy nullable manifests remain reconciliation-only. They cannot newly dispatch
  an unprotected sale or silently inherit a newly invented target.

## Local validation

Python 3.12, isolated pytest 8.4.1 and Hummingbot API client 1.5.9. Source was restored
from the pinned production commit before modification. No exchange connection was
used. The local source mirror does not include the full application's conftest;
GitHub's configured locked-dependency workflow provides the broader environment check.

Final focused run: **103 passed in 5.04 seconds** across:

- `tests/test_partial_recovery_recycling.py`
- `tests/test_protected_recovery_exit.py`
- `tests/test_economic_position_ledger.py`
- `tests/test_unified_economic_exit.py`
- `tests/test_vcap_lifecycle_controller.py`
- `tests/test_vcap_runtime_supervisor.py`
- `tests/test_multi_position_runtime_scheduler.py`
- `tests/test_live_fill_reconciliation.py`
- `tests/test_recovery_exposure_governor.py`

An independent reviewer supplied the new tests and identified the now-covered
restart/conflict crash window. The new file is included in Spectra Phase 1 CI.
GitHub run status belongs to the draft PR; this receipt does not predeclare a pass.

## Remaining release gaps

1. Whole-position exits still require equivalent price protection.
2. Durable parked-cash authority and safe capital reactivation remain separate work.
3. Canceled orders, uncertain submissions and unsellable partial remainders stay
   reserved pending explicit finality-safe resolution. There is no automatic
   cancellation, market fallback, chasing or resubmission in this patch.
4. Excess venue fees or connector violations can still breach the net target;
   confirmed breaches are accounted for and held rather than concealed.
5. The installed Hummingbot core image is not pinned by this patch. Client/source
   compatibility is not proof of actual Bazzite runtime or Kraken paper execution.
6. No profitability, autonomous refill latency or release-readiness claim follows
   from these execution and accounting checks.

Recommendation: keep this PR unmerged, verify configured CI, then give whole-position
exits the same bounded execution treatment before integrating parked-cash authority
and testing restart/finality behavior in paper or shadow execution.
