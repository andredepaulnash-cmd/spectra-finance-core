# Recovery correctness promotion: review and rollout map

Base: `spectra-core-phase2` at
`6e894fa9c2b55384d0f6120f41ca91892e1fe207`.

This change promotes accounting evidence and separately gated correctness
mechanics. It is not a claim of profitable recovery, a deployment, or a grant
of capital. The standard MCP server registration and existing policy defaults
are unchanged. No research ranking, cooldown, exposure-cap experiment, or
historical price data is promoted.

## Review order

1. [Decision evidence and reconciliation](support-observability.md): append-only
   reservation/runtime traces, exact exit allocation receipts, read-only
   conservation and explicitly identified cycle reports. No new strategy gate.
2. [Partial-sale deferral](partial-attempt-deferral.md): opt in with
   `VCAPRuntimePolicy(partial_attempt_deferral_enabled=True)`. Existing runtime
   and sell-adapter calls then enforce current evidence, exact manifest ownership,
   pending-order priority and durable single-dispatch/reconciliation behavior.
3. [Dust lifecycle](dust-lifecycle.md): explicit post-fill classification API
   flow plus guarded existing entry authorization/create. The environment flag
   alone does not classify an old wallet or schedule evidence collection.
4. [Selected-child recovery](selected-recovery.md): explicit disabled-by-default
   runtime, current-basis rung mechanics, native exact-source sale adapter and
   evidence-bound capital recycling. The caller must already possess an exact
   existing reservation; no helper creates new spend permission.

The shared durable position lock excludes overlapping selected, partial and
whole-parent exits and guarded new executor admission. Unknown external outcomes
retain ownership and require reconciliation. Local transaction fences are not
exchange-side atomicity or fill guarantees.

## What was adapted from research

The research was inspected rather than copied wholesale. Its partial-deferral
prototype was synchronous paper-only and used compounded costs; the permanent
adapter uses the current core's additive fee/slippage convention and adds exact
venue rounding, live pending evidence, elapsed-time freshness and durable send
claims. It does not add the prototype's hypothetical recovery-buy diagnostics as
spend authority.

The cycle audit motivated preserving unsold original/recovery inventory and
reporting complete versus incomplete cycles separately. Realized recovery-sale
profits alone do not establish parent recovery.

The matched historical selected-child treatment lost more than its baseline.
The new selected-child tests establish implementation fidelity and conservation,
not favorable economics. Funding ceilings, caller-supplied existing capital
claims, and executable fee-aware exits remain mandatory. Sequential doubling
stays behind explicit opt-in gates pending separate strategy validation.

Inspected research-artifact identities (paths are relative to their original
research evidence packages, not files added by this pull request):

- `causal_rotation/research/partial_deferral_prototype/partial_deferral.py`:
  SHA-256 `42e0518fbaa8e313e68dac065183bd086b9cb7b68cb229862a25068681f55732`
- `causal_rotation/research/partial_deferral_prototype/source_integrity.json`:
  SHA-256 `245cbb18aee70fe778b6ceb057c974a1e2116691cd41dab22bde80dd30ac6fe6`
- `causal_rotation/cycle_audit/FINDINGS.md`:
  SHA-256 `21eff26eccc6cf4bbaeb896506fc58d098e2d4399392213867b83c6d4757138c`
- `ten_step_research/historical_child_recovery/independent_postrun/matched_pair_receipt.json`:
  SHA-256 `c17af365215c989839344fdb2fa0d5555ddd993f75ee9172991c6ba010541c7a`
- `ten_step_research/historical_child_recovery/frozen_protocol.json`:
  SHA-256 `7eae84517180b1dd82634e3f7ea108d1a4fb02d11e60506a99b53348cf73a806`

No campaign was rerun for this promotion. The running regime experiment and the
protected October 9–November 9 holdout were neither modified nor used for tuning.

## Verification boundary

The existing focused CI matrix is extended with the new observer, allocation,
deferral, dust and selected-runtime tests. Enabled integration cases use real
core ledgers, governor and execution helpers with synthetic exchange I/O. They
cover partial fills, source-owner conservation, rejected and uncertain attempts,
post-await/post-commit races, restart/idempotence, exact terminal evidence,
fee-inclusive capital authority, native source allocations, full-child recycling
and repeated dust/reentry. They are not live exchange tests.

Review the feature-specific invocation sequences before enabling anything. A
human-reviewed deployment and strategy/economic validation remain separate
steps; this draft change does not perform them.
