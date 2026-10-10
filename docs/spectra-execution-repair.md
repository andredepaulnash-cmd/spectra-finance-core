# Protected execution repair candidate

An uncertain Kraken AddOrder response could previously trigger another POST,
and a canceled or failed fixed LIMIT executor could submit a replacement for
its remaining quantity. Together those behaviors could exceed the authorization
that Spectra committed. This candidate spends one durable executor attempt and
one durable Kraken client-order attempt before submission. Cancellation, failure,
timeout, missing storage, and ambiguous acknowledgment never release either claim.

This is a source correction and an exact-source build recipe stacked on the
runtime verification PR. It does not attest a compiled image or the installed
Bazzite backend. The operational stack lock remains PARTIAL with live approval
false, and protected PR26 research remains untouched.

## Execution and accounting behavior

| Boundary | Candidate behavior |
| --- | --- |
| Spectra protected BUY | Derives the backend executor ID from the committed authorization scope before capital commitment; a protected receipt must match the saved ID. |
| Fixed LIMIT / LIMIT_MAKER | Claims the executor ID before placing its first order. No automatic replacement follows cancellation, failure, or uncertainty. |
| Changed execution strategy | Reusing an already spent protected executor ID cannot acquire new authority by switching to MARKET or LIMIT_CHASER. |
| Kraken AddOrder | Requires the real registered tracker object and matching scope, canonical numeric userref, and one structurally valid venue order ID; sends at most one request. |
| HTTP transport | Disables redirects and rejects redirect responses so a redirect cannot replay a POST. |
| Late fills | Retains protected order references beyond the ordinary tracker cache; deduplicates trades and requires matching venue order identity and pair. |
| Accounting uncertainty | Pending registration, status reports ahead of trade details, foreign signatures, or corrupt recovery evidence are unavailable accounting. Known fills and reported-volume thresholds remain retained. |
| Restart | Restores durable accounting snapshots without registering another live order. Missing or ambiguous evidence preserves the spent attempt and requires reconciliation. |

The existing quantity, price, fee allowance, native quote value, cash scope,
and ledger ownership remain Spectra's authority. This patch does not authorize
market fallback or a new quantity based on a canceled order's apparent remainder.
Fresh unrelated MARKET and LIMIT_CHASER executors retain their ordinary behavior
with verified journal storage; Kraken AddOrder still has one request per client ID.

A previously persisted protected BUY configuration without a valid immutable
executor ID fails receipt reconciliation. The patch does not invent an ID or
automatically migrate that evidence; recovery requires manual review of the
original executor and committed financial authority.

The executor keeps accounting callbacks attached while shutting down, including
after the requested quantity appears fully filled. It does not automatically
terminate or let the API archive a one-time aggregate while venue finality is
unproven. Subsequent valid fills can still update the active API accounting and
the durable snapshot, with further dispatch disabled. A held executor requires
explicit venue and business-ledger reconciliation; this candidate supplies no
automatic finality decision or claim release.

An ordinary delay between a cumulative status report and its trade details is
temporary accounting uncertainty. Matching validated trades can restore
availability once they reach the saved reported-volume threshold. Foreign
identities and malformed financial values require explicit reconciliation.
Unsupported validation, expiry, IOC, or leverage options are rejected before
submission instead of being silently ignored.

## Durable storage

The attempt guard uses an existing private SQLite journal and a private adjacent
`.claims` hash-chain anchor. Unique claims, explicit journal identity, process
binding, full synchronous commits, and anchor locking prevent ordinary concurrent
or restart replay. Both files must survive a container rebuild and be preserved
together in recovery. No normal execution path initializes, resets, repairs,
or releases the journal.

An incomplete commit, unexpected schema, rotated path/inode, missing mount, or
disagreement between the journal and anchor fails closed. A coherent rollback
of both files across a fresh process cannot be detected from those files alone.
That recovery requires an external trusted checkpoint and manual reconciliation
of the venue and business ledgers. The guard explicitly does not establish
exchange finality.

## Source and build identity

The five-file overlay is bound to official Hummingbot core
`9af100d6822da7d2d0291a906c730ef172284ee2`, and the API recipe uses
`andredepaulnash-cmd/spectra-hummingbot-api` commit
`84426ffbf9ba09522b4444664f9770236205ae29`. Each original and corrected source
is checksum checked. The upstream fixtures remain byte-for-byte unchanged so
they continue to demonstrate the original behavior independently of this repair.

See [the build recipe](../deploy/hummingbot_core/README.md) for exact inputs,
private token handling, wheel verification, runtime import checks, licenses,
dependency evidence, and persistent storage prerequisites. The recipe requires
an independently reviewed immutable base-image digest and an explicitly supplied
overlay manifest checksum. No base-image digest, compiled wheel hash, or image
digest is invented by this PR. Upstream runtime dependencies remain incompletely
locked, so an actual build must record and review resolved dependencies.

The standalone image entrypoint checks installed source identity and the existing
journal before starting the API. Direct uvicorn, an overridden entrypoint, other
bot images, and the existing compose deployment are outside that binding.

## Verification and next operational gate

The repair workflow runs the existing finance regression modules, prior runtime
verification modules, and new journal/executor/connector/build/identity tests.
Those tests use temporary journals and inert framework or HTTP doubles. They
exercise corrected Python logic without contacting a venue or submitting orders.
They do not represent a compiled-core integration test or Bazzite installation.

After source review, the next gate is an actual compiled image build followed by
read-only installed-host attestation: exact image and wheel digests, loaded
module locations and hashes, API/core identity, persistent journal continuity,
real fee profile, and complete order/trade reconciliation. Keep live activation
false until that evidence resolves the operational blockers. No merge,
deployment, or live order is part of this corrective draft.
