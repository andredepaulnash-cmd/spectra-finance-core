# Native next-entry funding bridge V2.1

Status: default-off, synthetic paper integration. No live order routing and no historical run. Final independent review of the complete caller remains required before admission. This module extends the preserved V1 verifier; it does not replace or alter its evidence.

## Files and authorities

- `next_entry_profit_bridge.py` and `next_entry_profit_bridge_v1.py`: frozen V1 SHA256 `9e675449f12d771c8825af08ecc591e513e4b5f10ebc3fa892f285e724aefc6a`.
- `next_entry_funding_bridge.py`: V2.1 cash journal and allocator saga. The original V2 is preserved in `next_entry_funding_bridge_v2.py`, SHA256 `8d5ad71dda60176f85a0663b87217cdb015e99f34c331446fec37a91b51206dd`, with its original contract in `NEXT_ENTRY_FUNDING_BRIDGE_V2.md`.
- `next_entry_settlement_proof.py`: read-only actual BUY/terminal verifier. Its source identity and consulted native sources are frozen into bridge identity.
- `research/weekly_reversal/entry_admission.py`: concrete reviewed admission helper; subclasses and instance replacement of its authority methods are rejected. Original evidence-derived proof/permit IDs are anchored by the accepted allocator request.
- `research/weekly_reversal/psm_profit_partition.py`: canonical PSM ownership/distribution helper, SHA256 `ce45a65b8d893a9be4c2db91effdf48b805c6a73e7d1d6a1d7342055e2cfa4dd`.
- `next_entry_allocator.py`: frozen separate allocator, SHA256 `fcc7d78c8117c0df63951e52c3a778632846e4a0fa016a09c5ec6b777446b3d4`.

Bridge identity includes exact V2/helper/native implementation hashes, account, connector, controller, database paths, allocator policy, paper identity and enable flags. Identity changes require an explicit reviewed migration or fresh research authority. There is no implicit V1→V2 database migration. Both `enabled` and `funding_enabled` default to false.

## Public API

```python
bridge = NativeEntryFundingBridge(
    admission=NativeEntryAdmission(adapter, allocator=allocator, snapshot_id=snapshot_id),
    funding_enabled=True,
    paper_db=paper_db, native_db=native_db, allocator=allocator,
    account_id=account, connector=connector, owner_id=controller,
    enabled=True, fault_hook=None,
)
proof = bridge.prepare_entry(order_id=order_id, now=now)
readiness = bridge.fund_entry(entry_id=entry_id, now=now)
result = bridge.sync_entry(entry_id=entry_id, now=now)
```

V1 `stage`, `dispatch`, `cash_projection`, `before_new_spending` and `integration_callbacks` remain available. `funding_enabled=False, enabled=True` allows only reviewed PSM sale staging; any allocator spending remains forbidden.

`prepare_entry` persists the exact admission proof and deterministic RESERVE outbox before calling the allocator. It returns the original immutable proof. The durable order must remain `FUNDING_PREPARED` and non-executable.

`fund_entry` dispatches the exact staged RESERVE, verifies the actual allocator operation, then atomically writes balanced FUND legs and its acknowledgement. It returns `funding_ready=True`, `order_permission=False`, the entry/order IDs and original reservation receipt. It does not create a position, authorize an executor, submit a purchase, change order status, refresh a permit, or consume liquidity.

`sync_entry` verifies every exact applied paper/native BUY, freezes execution context at the first actual receipt, commits actual profit cash use and reconciles terminal unused release. Results have `order_permission=False`; status is `FUNDED`, `AWAITING_NEWER_NATIVE_CLOSURE`, or `CLOSED`. The later-health result is an explicit pending state, not success or permission.

## Caller requirements and checkpoint order

The caller owns a single account writer lock across adapter, bridge and allocator actions. Database transactions serialize only the paper side; the allocator is a distinct durable store. Caller state must be reloaded from the paper database after bridge writes, especially PSM staging. The adapter's original cash calculation is insufficient: use the validated bridge cash projection and its entry-block callback everywhere cash/holds/capital claims are consulted.

1. Persist an exact non-executable candidate and actual completed-bar evidence.
2. Recompute the ordinary same-root REENTRY or independently due REPAIR guards; commit the exact native governor proposal. Do not create an executable order yet.
3. `prepare_entry`: freeze admission and RESERVE payload in the paper transaction.
4. `fund_entry`: allocator reserve may commit before paper acknowledgement. Replay must use the same IDs and payload. FUND moves base from the exact permitted source and profit from escrow to `next_entry:<entry_id>` with equal aggregate credit.
5. Only the separate caller may activate the order, under its unchanged signal, full-budget, fee, rounding, minimum, risk, reserve, ownership and prior-bar liquidity guards. The original permit must still allow that activation. Record immutable decision quantity/reference and actual execution geometry separately.
6. Native-confirmed paper receipts debit only the dedicated entry pool. Persist `receipt_reconciled` audits before applied flags and terminal reconciliation.
7. `sync_entry`: commit exact incremental native fee-inclusive profit usage, base first. A partial fill that has used only base produces no fabricated profit debit. Every later positive commit includes all previously unconsumed native receipts in insertion order; no receipt or fee may be skipped or reused.
8. At terminal cancellation/full fill, prove exact terminal audit, no remaining liability, complete native coverage and actual fresh accounting health. After a positive commit the health observation must be strictly later than that commit's verification time. Wall-clock passage alone is insufficient.
9. Atomically RETURN the exact unused base to its original source and unused profit to escrow; stage RELEASE before dispatch. Pending release/unknown acknowledgement blocks new spending. Only actual accepted allocator RELEASED evidence permits CLOSED.

All historical owned native positions need fresh actual health, not only each pair's current position. The frozen V1 projection deliberately rejects stale/missing/conflicting evidence; do not relax native checks to advance a saga.

## Durable records and proof invariants

The paper SQLite transaction domain contains:

- `next_entry_funding`: exact admission bytes/digest, stable reservation ID, lifecycle and terminal proof;
- `next_entry_funding_outbox`: immutable RESERVE/COMMIT/RELEASE payloads, operation IDs and allocator acknowledgement;
- `next_entry_cash_transfers`: exactly one FUND and at most one terminal RETURN per entry;
- `next_entry_execution_contexts`: original actual execution geometry, allocator reservation time, owner/source/membership scope;
- `next_entry_observed_receipts`: canonical immutable receipt/native evidence, with original bytes retained.

Funding states are PREPARED, FUNDED, CLOSING and CLOSED. Every pending operation or unobserved receipt blocks new spending. Every `FUNDING_PREPARED` order remains an entry block even after funding is known. Unknown outcomes never become available cash merely because time elapsed.

Projection reconstructs all balanced legs, exact accepted allocator operations and current native sources. ACK requires the exact allocator operations-table receipt; paper ACK alone is not evidence. Commit amounts and IDs are re-derived from the complete prefix of native debits. Stored hashes alone do not verify an attestation. Execution context is reconstructed against the exact current order, original admission and actual allocator reservation, then compared byte-for-byte.

Terminal proofs bind native authority, paper audits, original receipt bytes, actual health and coverage. Release proof IDs contain the original closure digest, anchoring those exact bytes in the allocator. Closed replay compares immutable authority and reconstructs the original causal health scope while allowing legitimate later mutable health and inventory observations. A later contradictory fill or changed authority fails closed.

Cash is never created. Protected reserve is neither a source nor a receipt pool. `total_physical_cash` includes escrow; base spending must use `available_base_pools`. All receipts, original basis, original entry fees, exit fees and aggregate native realized P&L remain covered by V1. Deposits, inherited/manual inventory, foreign owners, unmatched fills and unknown journal legs do not become profit.

## Retained roots and PSM

Ordinary sales return to the acquisition's exact pool. A directly funded dedicated-pool acquisition must have its exact CLOSED funding record and immutable native BUY evidence. Later same-root reentry can source that pool; principal is not silently returned to unrestricted seed/repair cash or pooled across assets.

A zero-spendable-profit base-only REENTRY may retain an existing root pool only through the separately audited base-entry admission proof and audit anchor. Its predecessor generation, source pool, cohort, pair and funding owner must match; predecessor sale sources are recursively re-verified against their original durable outboxes. Generation must strictly increase. Base-only orders do not fabricate an allocator reservation. Sub-allocation-quantum entitlement remains allocated; the caller may floor only the spendable increment, never erase the remainder.

VCAP profit replaces part of an independently due unchanged full-double REPAIR budget. It does not create a trigger, expand the ladder target or authorize held-position topups. Both base and profit components remain explicit.

A PSM source-pool transition requires the pinned canonical ownership proof for every acquisition, even a same-pool rebuilt acquisition. On positive sale staging, the helper's detached PSM distribution state and the equal paper escrow earmark commit atomically. Only full exact positive fee-net realized profit leaves PSM cash. Losses still stage and reconcile without an allocation. PSM rebuild uses its own snapshot proceeds; cross-asset entitlements wait for a later eligible ordinary entry. V2.1 admits a dedicated-entry-pool→PSM handoff only through an exact `VERIFIED_DEDICATED_ACQUISITION` envelope produced after independent replay of the actual paper, native and allocator stores. The envelope binds acquisition order/pool, pair, cohort/generation, immutable seed/repair funding owner, native position/proposal/executor and the actual activation proof. Its lineage is the same exact CLOSED funded-entry proof or recursively verified retained-root base-only proof used for ordinary sales. Direct REENTRY/seed and REPAIR/repair origins and retained REENTRY/seed origins are the only accepted cases.

The pinned pure PSM helper checks that envelope against the actual order and immutable source-episode handoff, plus all unchanged PSM owner/action/lot/journal checks. Its hashes bind bytes; they do not independently authenticate database evidence. V2 reopens and verifies exact native BUY evidence, admission/activation, allocator reserve/commit/release acknowledgements, CLOSED terminal batch and balanced cash return before supplying the envelope. It repeats that verification immediately before atomic positive-profit distribution. A well-formed caller envelope, arbitrary `next_entry:` string, missing funding record, unclosed entry, foreign owner or changed allocator operation never grants authority. Source retries rederive identical frozen lineage. No new incoming PSM cash event or broader cross-asset funding permission is added.

Ordinary reentry after a PSM_REBUILD successor remains outside this extension: aggregate `psm:<pair>` cash is not a root-specific seed spending authority. That separate root|funding subcash path must remain blocked until explicitly implemented and reviewed.

## Crash/retry and admitted limitations

Synthetic hooks cover before/after durable prepare, allocator reserve/commit/release before acknowledgement, debit staging, terminal-return staging and acknowledgement commits. A crash after allocator commit replays identical original payloads even after that proof expires, provided its original accepted result exists. An unaccepted stale payload cannot be refreshed silently.

Accepted RESERVE cleanup can reconcile the original paper FUND after execution-permit expiry using the helper's non-authorizing reconciliation path. `fund_entry` still refuses execution readiness and the prepared order blocks spending. If no correctly timestamped native execution/cancellation evidence exists, that expired prepared entry requires audited snapshot recovery; no artificial OPEN, timestamp or terminal proof is manufactured.

A never-accepted expired RESERVE remains explicitly quarantined/pending for this admission. No extra automatic abandonment or cleanup API is implemented. Evidence and cash are preserved for completed-bar snapshot recovery. This is an explicit operational limit, not an implicit release.

The caller must not refresh source hashes, rewrite outboxes, relabel principal as unrestricted cash, debit protected reserve, adopt a new helper identity on restart, or bypass pending states. Native-source edits, historical evaluation and real exchange orders are outside this deliverable.

## Validation and review boundary

The focused V2 suite covers real synthetic native SQLite and applied paper receipts, ordinary REENTRY/VCAP REPAIR, partial/full/zero fills, later loss/base-only retained-root generations, PSM profit/loss atomic staging, duplicate replay, invalid source changes, stale accepted versus unaccepted payloads, and 15 staging/allocator/ack crash boundaries. The settlement verifier has its own 146 focused tests; frozen V1 has 65.

The last completed independent V2 review verified 25 then-current integration tests and confirmed the two reported closed-evidence failures were fixed. Subsequent admission-ID anchoring, exact allocator ACK reciprocity, expired accepted cleanup and retained-root changes are regression-tested by the implementation owner, but final independent review of the complete caller is still pending. A later review turn was blocked by the platform; it was not bypassed or counted as approval.
