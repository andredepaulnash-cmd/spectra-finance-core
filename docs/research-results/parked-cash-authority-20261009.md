# Durable PARK cash authority — 2026-10-09

## Purpose and scope

Recovery-partial sale proceeds belong to PARK. Whole-position sale proceeds return to productive capital, including whole sales with recovery lots. Sale purpose, rather than the final inventory event type, decides the destination. Closing the last recovery child therefore still parks its proceeds.

This change builds on the unmerged protected recovery and whole-exit work in PRs #27 and #28. It does not alter targets, reserve percentages, VCAP ladders, PSSM/DTSS settings or historical strategy results. It does not authorize a merge, deployment or live trade. PR #26 remains the separate economic experiment.

## Activation

Existing databases remain unbound until an explicit reviewed activation names the account, connector, quote asset, timestamp, evidence ID, productive opening balance, PARK opening balance and entry-fee allowance. The exchange balance alone never activates authority. Existing unresolved RESERVED/COMMITTED claims and incompatible historical provenance block activation. Opening balances must already include pre-activation fills; those fills cannot be credited again.

Activation is immutable and exact replay is idempotent. A different activation or conflicting scope persists a spending hold. The cash journal is stored in the Governor database and survives restart. No automatic deposit credit or withdrawal workflow is introduced here.

## Spending boundary

Productive authorization and independently observed venue free cash both constrain every reserve, commit and recovery carve. PARK cannot fund either an entry or a recovery buy. Unknown venue balance increases do not grant productive authorization. The existing physical cash floor is preserved; PARK can satisfy that physical floor without becoming spendable. No second productive reserve floor is added.

A committed BUY burns its all-in authorization exactly once. A configured quote-fee allowance provides headroom; exact confirmed BUY fills audit actual cost and fees. An overrun is charged once and blocks further spending while the inventory ledger still records financial truth. Wrong-account, wrong-connector, unscoped and conflicting replay requests cannot reuse the authority.

## Confirmed sale handoff

A new sale manifest durably records its cash route before dispatch. Exact executor, pair, scope and sale purpose are immutable. Confirmed fills first create PENDING cash receipts, which block new spending. Cash is credited only after exact inventory events, fill journals, lot allocations and required recovery-basis releases agree. A SHA-256 proof binds settlement to those journals. Replays do not credit proceeds twice, and order acknowledgements alone never credit cash.

A pending cash hold does not hide healthy source inventory evidence: the runtime must still reconcile the existing synthetic sale. Its saved partial mutation key identifies the exact manifest even if inventory reconciliation completed before a crash and execution_result was never saved. New dispatch and BUY spending remain gated.

## Deliberate operational limits

- This is plumbing for the candidate, not an adaptive PARK release policy. PARK remains quarantined.
- Canceled, unfilled or partially filled BUY authorization is not automatically refunded. It remains conservatively consumed until a separate exact terminal-order refund protocol is implemented and tested.
- A zero default fee allowance is conservative. An activated paper/shadow profile must explicitly set an allowance consistent with confirmed venue fees.
- Legacy sale manifests with no durable route are not silently reinterpreted. They require an explicit migration before dispatch under activated authority.
- Persistent conflicts require explicit reconciliation; this patch does not supply an automatic conflict-clearing or cash-minting control.
- The venue free-cash bound is the last scoped observation, not a guarantee of current availability. Fresh venue checks, exchange rejection handling and paper/shadow validation remain necessary.
- No new six-month replay or profit claim follows from these regression tests. Live latency and fill quality remain unproven.

## Validation

Local validation: 270 tests passed in 4.41 seconds across 17 focused suites (Python 3.12, pytest 8.4.1, subset source mirror with --noconftest). Final GitHub CI validation is pending. The new suites cover cash identity, scope, purpose, fees, concurrency, exact arithmetic and activated runtime crash recovery. The full repository check uses the locked GitHub environment.

## Next recommendation

Keep this stack unmerged. Run a bounded paper/shadow activation and sale cycle, then close exact unused BUY refund/finality before enabling automatic capital reuse. Evaluate PARK release gates separately against the frozen economic control.
