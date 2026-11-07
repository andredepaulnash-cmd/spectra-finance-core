# Spectra Core recovery-safety integration map

Status: documentation-only integration plan. This file does not change runtime behavior, enable default-off features, merge branches, or authorize deployment or trading.

## 1. Immutable branch facts

All comparisons below were read from the published Git refs on 2026-11-07.

| Candidate | Head | Base | State and evidence |
|---|---|---|---|
| Protected cash / LIMIT integration (PR #31) | `24b152457d9a8b3e007e69e7879bab38d1beb254` | `spectra-core-phase2` at `6e894fa9c2b55384d0f6120f41ca91892e1fe207` | Open draft; 55 files; PR description reports 788 full-checkout tests and 65 adversarial probes passed |
| Recovery correctness (PR #40) | `5f276798c4c465deb894daf1d5d8e11997126418` | same `6e894fa...` base | Open draft; 35 files; scoped 475-test CI passed |
| Weekly reversal / next-entry research (PR #42) | `51f240ba184a0ecf40a7299aaed8b6977c19fadd` | PR #40 head | Open draft; 99 files; scoped 1,723-test CI passed |

PR #31 and PR #40 are **diverged**, not a simple stack: comparison reports PR #40 is 5 commits ahead and 6 behind PR #31, with the common base at `6e894fa...`. Do not merge one branch into the other or cherry-pick an entire branch blindly. Both candidates modify the same financial and dispatch boundaries. PR #42 is stacked on PR #40 and must be treated as a separate research/next-entry layer, not as a replacement for PR #31's protected execution/cash authority.

## 2. Invariants that must survive composition

The integration target is the union of the following invariants, not a preference for one branch's file version:

1. **Capital authority:** exact account, connector, pair, quote asset, intent, proposal and amount scope; productive cash only; PARK remains segregated; unknown venue cash never grants authority.
2. **ENTRY identity:** canonical position identity and durable once-only submission claim; ambiguous create remains held across restart.
3. **Protected BUY:** explicit price-slippage and fee allowances; fresh exact Kraken pair rules/book; immutable config and policy digest; bounded fixed-price LIMIT; confirmed orders/fills/fees reconciled against the original quote budget.
4. **Protected SELL:** immutable lot/owner parcel, fee-aware price floor, exact quantity, durable once-only dispatch, no market fallback or blind retry.
5. **Cash settlement:** sale purpose determines destination; pending proceeds block new spending; confirmed fills and inventory/journal evidence must agree before cash credit; exact replay never credits twice.
6. **Terminal truth:** retirement requires terminal executor/order state plus complete confirmed trade coverage and durable fill journal. Dust/residual retains quantity, basis and fees; no invented write-off.
7. **Recovery ownership:** one durable position mutation owner at a time; selected-child, partial-recycle and whole-position exits cannot overlap or consume another owner's lots.
8. **Uncertain outcomes:** timeout, empty response, server error or post-claim 404 is ambiguous, not proof of absence. Reconcile known state; do not issue a second create.
9. **Default-off research:** partial-attempt deferral, dust automation, selected-child recovery and weekly reversal remain explicitly disabled until separately reviewed and tested. Tests do not grant capital.
10. **No live authority:** passing CI is not profitability, installed-image, Bazzite, exchange-fill or live-readiness evidence.

## 3. File-by-file port map

| Shared path | PR #31 contribution to preserve | PR #40 contribution to port | Required composition rule |
|---|---|---|---|
| `spectra_core/capital_governor.py` | Durable cash authority, PARK/productive segregation, ENTRY identity/submission claims, protected BUY config/policy, scoped commit/reconciliation, exact Decimal handling | Decision traces and recovery/accounting observability | Port trace writes into the PR #31 transaction boundaries using the same SQLite connection. Preserve PR #31 schemas, cash holds, claim uniqueness and replay semantics. Do not replace its larger Governor with PR #40's older base implementation. |
| `spectra_core/economic_position.py` | Expanded owned-lot, residual, fee/basis and retirement accounting used by protected BUY/SELL and cash settlement | Durable selected-recovery position lock, proposal scope and mutation-owner checks | Add lock ownership to PR #31's inventory mutations without splitting its inventory/fill/cash transaction. Every write must prove the exact lock owner; reads and reconciliation must remain possible while policy is HOLD. |
| `mcp_servers/hummingbot_api/tools/executor_create.py` | Exact Spectra capital commit, protected BUY conversion, fresh book/rules, immutable config/policy and ENTRY/recovery claim before external create | Guarded admission against selected-recovery locks and proposal scopes | Keep PR #31's protected BUY path as the sole authorization path. Add PR #40's lock/scope checks before dispatch; no generic create or alternate executor type may bypass the claim. |
| `mcp_servers/hummingbot_api/tools/partial_recycle.py` | Protected SELL floor, durable manifest, source passivity, order/trade snapshots, pending cash, fill reconciliation and purpose-based cash settlement | Durable position lock, partial-attempt deferral, pending-order priority and exact single-dispatch/reconciliation behavior | Preserve the complete PR #31 cash/price/fill lifecycle. Port PR #40's deferral as a default-off policy around fresh dispatch only; known executors must still reconcile. One canonical durable dispatch claim must cover both layers. |
| `spectra_core/partial_recycle.py` | Immutable partial-sale owner/lot manifest, protected price and fee geometry, durable dispatch identity | Cross-feature position-lock compatibility and dispatch claim safeguards | Unify manifest identity, lock owner and dispatch claim in one transaction. Never permit a second claim table to create divergent notions of “already sent.” |
| `spectra_core/economic_exit.py` | Protected whole/partial exit floors, actual-fill journal, cash-purpose handoff and terminal proof | Exact exit-allocation receipts and selected-owner constraints | Preserve protected price, cash and terminal semantics; add allocation receipts as evidence, not as an alternative source of inventory truth. |
| `mcp_servers/hummingbot_api/tools/economic_exit.py` | Protected SELL dispatch and cash settlement | Exact owner allocation and mutation-lock gates | Lock and allocation checks must run before new dispatch; known SELLs remain reconcilable after HOLD. |
| `spectra_core/vcap_runtime.py` and `mcp_servers/hummingbot_api/tools/vcap_runtime.py` | Durable VCAP cycles, mutation identity and restart-safe reconciliation | Append-only decision traces and opt-in partial-attempt deferral | Add trace events atomically with cycle transitions. Keep deferral false by default; never let a trace or retry marker authorize spend. |
| `spectra_core/vcap_lifecycle.py` and `mcp_servers/hummingbot_api/tools/vcap_lifecycle.py` | Protected VCAP exit/retirement lifecycle | Dust and partial-attempt state evidence | Dust classification must preserve exact basis/fees/ownership and must not settle cash or imply a sale. |
| `spectra_core/kraken_authorization.py` | Exact authorization and venue rules used by protected buys/sells | Guarded successor admission through existing authorization | Add new admission checks without weakening price, amount, account or connector bounds. |

### New PR #40 modules to integrate after shared-path composition

- `spectra_core/decision_trace.py` and `spectra_core/cycle_reconciliation.py`: useful durable observability and read-only conservation evidence. Keep trace writes append-only and transactionally coupled to the state change they describe.
- `spectra_core/position_mutation.py`: shared exclusive lock for selected, partial and whole-position mutations. Lock acquisition/release must be restart-safe and tied to exact owner IDs.
- `spectra_core/partial_attempt.py`: default-off deferral policy and exact pending attempt evidence. No new spend authority.
- `spectra_core/dust_lifecycle.py`: explicit evidence-bound dust classification and guarded re-entry. Classification cannot manufacture proceeds, erase inventory, or release basis without a confirmed event.
- `spectra_core/selected_recovery.py`, `selected_recovery_ledger.py`, `selected_recovery_runtime.py`: selected-child ownership, current-basis rung mechanics, native exact-source sale and capital recycling. Require an already-existing exact Governor reservation; no helper may create new capital permission.

## 4. PR #42 boundary

PR #42 is based on PR #40 and adds weekly reversal, owned-spot, PSM profit partition, next-entry funding/settlement bridges, checkpoint/retention and associated tests. Its own description says the work is default-off and does not establish derivative shorts, leverage, margin, liquidation, profitability or capital-preservation claims. It also records a native Decimal precision mismatch that remains fail-closed.

Do not fold PR #42 into the first protected-execution integration. Keep it as a separate research/strategy review after the PR #31 + PR #40 safety composition is stable. Its passing scoped CI is software regression evidence, not proof of favorable economics or permission to enable the weekly caller.

## 5. Cross-branch regression gate

Do not add branch test counts together. The composed tree needs one fresh full-checkout run against its exact commit, plus independent adversarial review.

Minimum required new cross-branch cases:

- Protected BUY: missing/invalid policy, stale book/rules, config digest mismatch, late/over-budget fills, duplicate proposal, ambiguous create and restart.
- Cash: PARK cannot fund BUY or recovery; recovery-partial proceeds park; whole-sale proceeds remain productive; pending receipts block spend; exact replay cannot double-credit; unknown deposits do not mint authority.
- Exit ownership: selected-child lock excludes partial and whole exits; partial lock excludes selected and whole exits; whole lock excludes new entry; wrong owner/proposal/lot is rejected before dispatch.
- Partial deferral: disabled-by-default parity; pending known SELL reconciles before freshness/dispatch checks; a claimed or ambiguous send never creates a successor blindly.
- Dust/residual: classification preserves quantity, owner, basis and fees; no cash credit; only exact evidence can permit guarded re-entry.
- Restart/finality: crash after claim, after exchange create, after partial fill, after cash receipt and before journal/terminal proof; every replay is idempotent and uncertain states remain held.
- Decimal/ownership: repeated partial fills, tiny residuals, mixed owners and fees reconcile without precision loss or cross-owner disposal.
- Observability: decision trace append is atomic with the state transition and never changes the decision or authorizes an action.

All external exchange I/O in this suite must be synthetic. No real account, wallet, API credential, order or deployment is required.

## 6. MCP launch and API authorization are separate controls

The supervised Condor launcher computes the agent profile and mutes, passes `--profile`, muted-tool arguments and host-generated `CONDOR_AGENT_SLUG` to the Hummingbot MCP subprocess. The Spectra agent definition is narrow and excludes raw orders, position/LP executors, swaps, liquidity writes, controller/bot deployment, leverage and server administration.

However, the inspected default-branch `.mcp.json` also contains a standalone `uv run python -m mcp_servers.hummingbot_api` launch with no profile or agent slug. `mcp_servers/hummingbot_api/settings.py` deliberately defaults the standalone server to profile `full`; the server registers tools from that profile at import/startup. This is a real alternate launch path in the repository, not merely a hypothetical possibility. It may be useful for developer setup, but it must never receive Spectra production credentials or be mistaken for a governed Spectra seat.

Separately, MCP tool mutes do not protect the API's REST routes. The API audit (separate repository, draft PR #5) found inspected direct write paths for order placement, executor creation, leverage/position mode, swaps/liquidity, bot deployment and Docker container start without a visible Spectra Core receipt at the API dispatch boundary. Production must either deny agent access to these routes or enforce a Core-issued, exact-scope, short-lived, one-time authorization receipt at every write boundary.

## 7. Runtime and image blockers

PR #38's dependency packet is incomplete: legacy `eip712-structs` extra and overlapping msgpack package files remain blockers; the final API image, native dependency lock and Bazzite runtime are not attested. The current five-overlay wheel identity cannot be silently changed; the sixth packaging overlay requires a new artifact identity and full verification.

The API repository also has a build/deploy gap: Compose deploy can use mutable `latest` without first building the checked-out source, and its Dockerfile/environment still contain mutable or unpinned inputs. Pin source, dependency hashes, base image and final image digest; require deployment to verify the exact attestation. Do not run deployment as part of this integration.

## 8. Safe order of work

1. Freeze PR #31 and PR #40 heads and their exact workflow receipts; no force-push or branch rewrite.
2. Create a new integration branch from PR #31's exact verified head.
3. Port PR #40 non-overlapping modules first, then compose each shared file using the table above. Keep each feature disabled by default.
4. Add cross-branch tests before enabling any new behavior; run focused tests after each port.
5. Run the full-checkout regression and independent adversarial review on the exact combined head.
6. Review PR #42 separately for economics, precision and caller/launch boundaries; do not promote it in the safety merge.
7. Complete API authorization, image provenance and Bazzite offline verification before any paper/shadow activation.
8. Keep live capital, wallet access, exchange credentials, deployment and production merge out of scope until separately reviewed and explicitly authorized.

**Current decision:** no safe combined code head has yet been built or validated. PR #31 is the stronger protected-cash/LIMIT base; PR #40 contributes valuable recovery ownership, deferral, dust and observability mechanisms, but its shared files must be ported deliberately. PR #42 remains separate research.

## 9. Source-level test review completed

The named suites were inspected, not inferred from workflow totals:

- PR #31 `test_integrated_entry_cash_authority.py`: 27 test functions. They explicitly cover PARK/unknown-cash exclusion, wrong-scope reservation, pending-sale restart holds, atomic identity/cash rollback, concurrent alias claims, exact protected config, fee and overrun reconciliation, fresh venue minima, stale book rejection, multi-level refusal, lost create response and durable policy breach.
- PR #31 `test_protected_buy_reconciliation.py`: 7 test functions covering actual financial truth after policy breach, lagging endpoints, cumulative quantity, direct fill bypass, exact Decimal comparison and restart health tags.
- PR #31 `test_integrated_partial_exit.py`: 7 test functions covering exact funded source, below-floor/fee-breach truth, zero-fill cancellation, cash/PARK behavior and partial-fill ownership.
- PR #31 `test_integrated_retirement_exit.py`: 8 test functions covering pending cash after crash, lagging terminal volume, held protection breach, late owned inventory, residual basis/fees and protected dust retirement.
- PR #40 `test_partial_attempt_runtime.py`: 12 test functions covering default-off behavior, pending/unknown order evidence, post-await rechecks, durable claims, lock retention and final dispatch clock expiry.
- PR #40 `test_dust_lifecycle.py`: 59 test functions covering default-off, exact rounding, ownership/basis/cash preservation, stale evidence, checkpoint invalidation, concurrency, successor admission and restart-safe single-use claims.
- PR #40 `test_selected_recovery_runtime.py`: 26 test functions covering default-off, exact reservation, competing locks, stale-after-await, timeout/restart, ambiguous dispatch, exact native partial fills, owner settlement and lock release.
- PR #40 `test_cycle_reconciliation.py`: 14 test functions covering read-only reports, exact owner conservation, incomplete legacy evidence, impossible chronology and non-finite/corrupt evidence.

These tests are strong source-level evidence for each branch's own feature set. They do **not** prove the cross-branch combination. The missing combined tests are specifically the seams where PR #31's protected cash/BUY/SELL/retirement transactions meet PR #40's shared position locks, dust checkpoint and selected-child mutation owner. The integration gate in section 5 is therefore mandatory; separate successful CI runs cannot substitute for it.


## 10. Composition progress — 2026-11-07

A separate code-bearing draft, [PR #44](https://github.com/andredepaulnash-cmd/spectra-finance-core/pull/44), now starts from the exact PR #31 protected-execution head and adds only the independent append-only decision trace component:

- Added `spectra_core/decision_trace.py` and its focused tests.
- Added transaction-coupled trace writes to PR #31's existing `CapitalGovernor.reserve` decision and VCAP runtime cycle creation/transition. The protected cash, reservation, BUY and SELL code was not replaced.
- Added observability documentation.
- Added the trace test file to the existing focused CI command.

Validation: the first workflow attempt ran the original 788 tests successfully but exposed a missing shell continuation in the newly extended test command; no trace tests ran in that attempt. The command was corrected. The subsequent exact-head workflow completed successfully with **802 passed in 43.13 seconds**. This is a focused CI result for PR #44's current head, not a full all-project, runtime-image, Bazzite or live-exchange validation.

The next code composition remains position mutation ownership and recovery-specific ledger changes. Do not port `cycle_reconciliation.py` unchanged: PR #40 expects an `economic_exit_allocations` schema with per-allocation owner fields, while PR #31 currently stores `economic_exit_lot_allocations` with a different column layout. A schema-aware read-only adapter and matching tests are required before that module can be included. Selected recovery, dust lifecycle and partial-attempt deferral remain unported.
