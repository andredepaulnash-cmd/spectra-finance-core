# Native archived-PSM root cash bridge V2.3 (isolated draft)

Default-off research integration. No historical data, external order, publication,
or live-source replacement is authorized by this artifact. The complete caller
still requires independent synthetic audit before integration admission.

## Authority and immutable history

`NativePSMRootFundingBridge` extends the frozen V2.2 funding saga and requires
`root_cash_enabled=True` as well as the existing two enable flags. Its identity
pins the base bridge, V1/native/allocator/admission/settlement implementations, the
new root bridge and helpers, both partition helpers, database paths and account.
`psm_root_cash.py` is a pure archived rebuild/ordinary-sale binding verifier;
`psm_root_partition.py` preserves the V2.1 partition implementation as a separate
extension for later PSM handoffs of root-backed funded/retained acquisitions.
Neither can authenticate a Boolean or confer database authority.

The root bridge independently rereads actual paper receipts, exact native BUY and
SELL reconciliations, original lots/cost/fees, aggregate native realized events,
immutable disposal allocations, terminal audits, original allocator operations,
and frozen funding/return evidence. Pure archived ownership binds exact original
configured root, successor cohort and actual acquired generation, seed/repair
owner, original rebuild allocation/action/child, native position/proposal/executor,
archived complete journal and native handback audit. A rebuilt-in-place cohort's
actual acquired generation comes from its immutable receipt-bound anchor history,
not blindly the decision order's earlier generation.

Archived reducer journals and kernels are never rewritten. A later ordinary
COHORT_EXIT is not inserted into an old PSM episode. It returns actual proceeds
to the same physical `psm:<pair>` pool and the same original root|funding owner.
Only full actual positive fee-net profit leaves that subcash for escrow. Loss and
zero-profit sales still require the ordinary immutable outbox reconciliation.

## Public read-only source APIs

- `root_source_proof(root_cohort_id, *, now, paper_connection)` returns
  `{source_owner, ownership_proof, available_base_cash}`. The connection must be
  the actual authoritative paper SQLite transaction. It requires current paper
  time, complete actual root receipt/disposal history, zero native root units
  including dust, and settled outboxes. Admission separately proves that no other
  root order, native authority or logical PSM action remains pending, excluding
  only the exact prepared candidate being checked.
- `source_owner` is exactly schema, pair, root_cohort_id, funding_owner=`seed`,
  source_pool=`psm:<pair>`, latest_generation and ownership_proof_sha256.
- `ownership_proof` binds every frozen root acquisition and disposal, complete
  relevant archive/handback receipts, latest actual generation, latest seed
  acquisition cohort IDs and latest_sold_at. Canonical sorted compact JSON uses
  Decimal strings. The digest excludes its own proof_sha256 field.
- `available_base_cash` is exact physical root|seed cash after original PSM
  distributions, post-handback profits and prior FUND/RETURN transfers, before
  the candidate's own hold. Generic pair cash and root|repair cash never qualify.
- `validate_root_source_proof(ownership_proof, *, now, paper_connection)` rereads
  those exact frozen source IDs and returns the unchanged proof. It grants no new
  permission, refreshes no timestamp and does not repeat today's latest/no-held
  predicate. Ancestry cycles fail closed.
- `psm_funding_cash_adjustments(pair)` returns validated signed root|funding-owner
  deltas to add to the adapter's existing receipt-minus-original-distribution
  balance. The adapter must retain its exact physical-pool conservation check.

Historical source verification does not recursively project a newer descendant
entry. It still performs complete native cash coverage and actual allocator
operation validation, and audits the particular CLOSED funding ancestor with the
same receipt, deterministic base-first COMMIT, native closure, RELEASE and exact
FUND/RETURN checks as the pinned V2.2 verifier. This is source scoping, not a skip
or Boolean permission. Normal account projection continues to audit all entries.

## Durable terminal health membership

`next_entry_terminal_health_scope` freezes exact native bindings/fills, immutable
paper order identities, the original terminal source digest and an audit-prefix
sequence/digest in the same paper transaction as terminal RETURN. Its digest joins
the original terminal payload and any RELEASE outbox. This is required because
later legitimate native authority can share the original closure timestamp.
Replay rereads every frozen native row and audit prefix, validates exact healthy
counts and native chronology, and rejects missing/orphan membership. Additional
native rows at that same timestamp require a matching later durable admission or
receipt audit; timestamps alone cannot expand the original closure membership.
Missing scope cannot be reconstructed silently on an old closed entry.

## Atomic cash bookkeeping

`next_entry_psm_root_cash(trade_id, payload, payload_digest)` is in the paper
receipt database. Each POST_HANDBACK_REALIZED_PROFIT row binds actual native trade,
original source digest, root/owner/pair, amount, allocator event, cash backing and
membership snapshot. It commits atomically with the immutable profit outbox and
V1 source-pool debit/equal escrow credit. Deletion, reassignment, changed amounts,
changed native source, duplicate trade or an orphan outbox fails closed.

A funded ordinary entry stores source_owner on the frozen admission and on both
FUND and RETURN legs. FUND can debit only sufficient original root|seed cash;
profit still comes separately from the allocator escrow. Terminal RETURN restores
only its own unused base to that same root and unused profit to escrow. Unknown
allocator reserve/commit/release outcomes remain pending and block new spending.
Nothing enters an active PSM rebuild from another asset's entitlement or owner.

A base-only REENTRY may retain the PSM pool only through its exact original
zero-profit base-entry proof, independent audit anchor, actual native execution
and source_owner/root-source proof. Later sale/reentry recursively revalidates
that predecessor chain. Funded dedicated-pool descendants continue to use their
exact closed funding source; old PSM balances are not reused merely because a
historical successor exists.

## Caller integration boundary

The isolated caller constructs the new bridge explicitly, freezes source_owner
on every candidate and applies independent same-root no-held/dust/pending guards.
The source's latest actual generation advances by exactly one; a newly completed
positive signal hour must begin after the latest actual root disposal. Successor
histories and original archived episodes remain unchanged.

The explicitly approved pre-run ordinary-reseed amendment uses base cash equal
to `min(configured original seed ticket, exact same-root available principal in
its current eligible source pool)`, plus its available allocated realized profit.
Admission independently derives and freezes initial seed allocation, own actual
fee-inclusive receipts, own profit escrow debits and exact FUND/RETURN rows. Replay
recalculates those frozen totals. Other same-root pools remain unavailable to this
entry; other roots, recovery owners and global seed/PSM cash cannot subsidize it.
The complete fee-inclusive order still must satisfy venue minima, rounding, fees,
account/pair/recovery caps and protected reserve. Zero available base fails closed.
REPAIR remains its independently due unchanged full-double or exact residual
retry target; this amendment does not clip or expand repair authority.

The adapter's PSM cash projection adds the bridge's validated signed adjustments
before its existing sum-equals-physical-pool check. The caller reloads persisted
state after bridge changes and retains the single-account writer lock. No arbitrary
callback, unchecked envelope, database identity rewrite or implicit migration is
supported.

## Compatibility and review status

The isolated draft now pins the reviewed optimized schema-3 kernel
`7e53ef0f2da3229a5fb5158655d090a14c2073b608ebb654f603b92acd7f0585`,
the original partition helper
`6441b0d99b135a9b483f6722b7b72416b869428a49ed10811d72e40a73521cd2`,
and base funding bridge
`bea46fb6054c111da212471dd98a4b217bbf2022c3d0486ed9f0f612d15ea38a`.
Before/after source snapshots preserve the earlier original-kernel evidence.
Merge only reviewed changed code and preserve these exact transitive pins.
Existing databases cannot silently adopt a changed source identity.

Focused actual-native accounting, malformed-source and crash/restart tests are in
`tests/test_psm_root_funding.py`; caller generation/base/funded/zero-fill coverage
is in `tests/test_psm_root_reentry.py`. Final counts, hashes, proof sizes/timings and
remaining limitations belong in the release receipt once all components freeze.
