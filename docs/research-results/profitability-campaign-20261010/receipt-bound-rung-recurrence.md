# Frozen research specification: receipt-bound rung recurrence

Status: root-approved experimental semantics, frozen before implementation and
before this author reads historical candidate outcomes. This is a new research
hypothesis. It changes no existing source or protocol and does not authorize
production activation. The authoritative machine-readable specification is
[`receipt-bound-rung-recurrence-spec.json`](receipt-bound-rung-recurrence-spec.json).
The source limitation motivating it is recorded in
[`ladder-lifecycle-source-audit.md`](ladder-lifecycle-source-audit.md).

## One fixed hypothesis

Allow at most **three confirmed purchase episodes per rung over one economic
parent's lifetime**, including the original purchase. A renewal must follow
complete profitable disposal of that rung's previous exact RECOVERY inventory,
actual owner settlement and explicit same-owner release, and a fresh
above-threshold then below-threshold price crossing. The old base lot, entry
anchor, profile and seed remain owned and unchanged.

This is one fixed rule, not a search over recurrence counts, cooldowns, thresholds
or profit allocations. Three episodes allow two observable renewals while
placing a small finite bound on repetition. That is a design rationale; it is
not a claim that three is economically optimal.

For initial actual all-in entry seed `S` and frozen profile seed multiples
`m[j]`, define original nominal profile authorization `N = S * sum(m[j])`.
Lifetime **confirmed all-in RECOVERY purchase turnover** is capped at `3 * N`.
It includes every original and renewed purchase's actual gross quote plus its
charged buy fee, exactly once. Pending buy reservations also occupy the
remaining turnover authorization until terminal confirmation. A sale returns
cash but never subtracts from lifetime purchase turnover.

| Frozen profile | Original rungs | Original nominal `N` | Lifetime turnover cap |
| --- | --- | --- | --- |
| Shallow | 3%, 6%, 10%; seed multiples 1, 1, 1 | `3 * S` | `9 * S` |
| Medium | 3%, 7%, 12%, 18%; multiples .75, 1, 1, 1.25 | `4 * S` | `12 * S` |
| Deep | 4%, 10%, 18%, 25%, 30%; multiples .5, .75, 1, 1.25, 1.5 | `5 * S` | `15 * S` |

These are cumulative turnover ceilings, not permission to commit that much cash
simultaneously. Every buy must still satisfy current finite owner funding,
physical reserve, pair exposure, minima and source execution gates.

## Original priority stays intact

First ask whether the **next unused original sequential rung** is eligible by
its original price threshold. If so, select it exactly as the existing ladder
does. Its funding, quality, minima or other admission failure does not permit
fallback to a cheaper renewed rung. Do not skip unused original rungs.

Only when no next unused original rung is price-eligible may renewal be
considered. Among eligible previously used rungs, choose the deepest rung by
its fixed drop fraction. Reserve at most one proposal per economic parent per
completed bar. If the selected renewed rung then fails admission, do not try
another renewed rung in that bar.

First-pass behavior remains source-faithful to the matched control, including
its existing `native_dttp` flag and hard-target behavior. The new strict
armed-profit-floor veto applies to **renewals**, even in a hard-target arm.
An armed current native profit state always blocks renewal. Never clear,
reset, rebase or bypass profit-floor/peak memory to make recurrence eligible.
Whole-parent exit and protection planning keep their existing priority.
The confirmed RENEWAL buy path must also preserve the existing native
`profit_states` entry; it must not execute the original RECOVERY pop/reset.
Original first-pass RECOVERY retains that original V2 execution behavior.

## Exact proof before renewal

Each purchase episode has an immutable key `(parent_id, rung_index,
episode_ordinal)`, where ordinal is 1, 2 or 3. Its exact accepted order,
confirmed buy receipt and confirmed RECOVERY lot IDs must be recorded. A
positive confirmed purchase consumes one episode even when native limits make
it smaller than the rung maximum. No unused fraction can be filled under a
second episode identity. A zero-fill FOK rejection consumes neither episode nor
turnover.

Renewal requires all of the following proofs for the immediately preceding
episode of that rung:

1. Every exact RECOVERY lot's complete purchased quantity has been disposed;
   native remaining quantity, gross basis and entry fees are exactly zero. A
   residual, dust balance, unfilled sale or cancel request fails this condition.
2. The actual confirmed sale allocations bind those same lot, parent, pair and
   fill identities. For **each** episode lot, cumulative sale gross minus
   charged exit fees minus its original gross purchase basis and charged buy
   fees is strictly positive. Another profitable lot cannot subsidize proof for
   a losing episode lot.
3. The owner journal has settled those exact sale allocations once and contains
   explicit `RELEASE_PARTIAL` proof referencing the corresponding settled sale
   receipt IDs. Released VCAP principal stays VCAP-owned. The existing per-lot
   positive-profit split remains 75% PRODUCTIVE / 25% VCAP; it supplies no new
   authority to borrow the PRODUCTIVE portion.
4. The complete current economic-parent commitment inventory is empty:
   no accepted buy, pending/partial sell, cancel-pending request, reservation or
   unresolved terminal receipt remains. Unknown or incomplete inventory blocks
   renewal.
5. The economic parent is still active with its exact original BASE_ENTRY lot.
   Whole-parent EXIT permanently retires this lineage; it never rearms old
   rungs. A later new ENTRY uses a new parent and new frozen anchor.

Sale snapshots must be captured before the native bridge consumes their lots.
Settlement and release are financial events, not booleans invented from a
planner signal. Replaying an identical proof is idempotent. A conflicting
receipt or lot-to-episode reassignment fails closed.

A receipt provides eligibility evidence; it does **not** add cash a second time.
The existing owned ledger remains the funding authority. Cash used elsewhere is
not still available merely because its earlier release receipt exists.

## A new price episode must occur

Use the original rung threshold `T[j] = initial entry anchor * (1 - drop[j])`
with the same native Decimal threshold convention as the first-pass ladder.
After disposal, settlement and release are all confirmed, record an observed
completed close strictly **above** `T[j]`. A distinct later completed bar must
close **at or below** `T[j]` to enable renewal.

If disposal settles at execution open in bar `i`, bar `i`'s completed close may
provide the above-threshold reset because it is observed after confirmation.
It cannot also provide the downward crossing. Crossing requires a later bar
index. No intrabar high, low, unknown bar, future price, or execution candle
information used before its completion can satisfy this condition.

A zero-fill FOK rejection leaves that crossing retryable while subsequent
completed closes remain at or below the threshold. Every retry must revalidate
current evidence, pending state, floor protection, capital and minima. A
subsequent above-threshold close establishes a new reset; it does not create
multiple eligible purchases. Retired or conflicting state cannot be retried.

## Funding and numeric boundary

A renewal's requested all-in budget is no greater than the frozen original
rung maximum `S * m[j]`. Its actual approved amount is bounded by native
recovery/tranche authorization, remaining lifetime turnover after pending
holds, current available VCAP cash, strict 25% pretrade-equity cash protection
after all holds, the existing 20% / $40 hard floor, remaining 5% pair exposure,
and source exchange/minimum/capacity requirements. No future sale proceeds,
negative cash, owner transfer, reserve release or extra slot is assumed.

Turnover and nominal-cap comparisons use exact rational interpretations of
finite Decimal components. Native fill arithmetic remains precision 28,
ROUND_HALF_EVEN. For renewed orders only, the proposed ticket may be reduced
toward zero until the native budget-to-gross/fee recipe's exact all-in sum
satisfies every ceiling. If that falls below minima, reject the request. Record
the actual confirmed gross plus charged buy fee and assert the strict cap after
fill. Do not clamp a fill, mutate fees, or widen the turnover cap with a numeric
tolerance. Original first-pass financial arithmetic remains unchanged.

## Module and sizing compatibility

The selector and direction gate govern new ENTRY decisions as in the matched
control. They do not replace the owned original base lot or silently convert
its parent into a new trade. Renewal rechecks the existing causal recovery
fingerprint/native geometry and any enabled independent recovery-quality gate
at the current completed decision bar. Preserve all comparator flags and
parameters; no new ranking or quality threshold is introduced here.

For fixed-ticket experiments, `S` is the actual initial fill's all-in cash. For
the one-percent experiment, the initial ENTRY target remains 1% of current
completed marked equity subject to the existing owner/cap/reserve/minimum
limits; the **actual confirmed** initial all-in fill becomes `S` for life.
Profits, later equity changes and smaller recovery purchases never resize that
parent's seed, original rung maxima or turnover ceiling.

Current-cost experiments use their separately frozen fees/slippage for both
execution and realized episode proof. The recurrence rule does not alter those
cost parameters, market capacity, data coverage, clock or original profiles.
The hard-target policy still differs from complete native DTTP, and the
renewal-only veto must be reported rather than described as whole production
module integration.

## Required audit trail and acceptance checks

The new driver must serialize frozen parent identity, base lot, anchor, seed,
profile, nominal cap, original next-rung index, per-rung episode keys and exact
lot membership, confirmed turnover, pending turnover holds, disposal/sale/release
proofs, reset/crossing completed-bar indices, and terminal retirement identity.
A manifest-bound checkpoint must preserve those values and their replay
identities. This requirement does not claim live venue restart reconciliation.
The passive completed-close observer runs after due fills and marking but
before planners, so an actual post-sale completed close is recorded even when
another native sale decision takes priority in that bar. Renewal admission
reads the current planner's updated profit state; observation alone authorizes
neither a recovery purchase nor a profit-state change.

Before historical runs, synthetic and adversarial checks must prove:

- Neutral recurrence-disabled execution reproduces the admitted V2 first pass.
- Three total episodes is the absolute per-rung limit; smaller fills still count.
- Missing, wrong-parent, wrong-lot, duplicate/conflicting, negative-profit,
  dust, unsettled or unreleased proof cannot authorize reuse.
- A profitable aggregate cannot hide a losing episode lot.
- Whole EXIT retires the parent, and pending/cancel-pending state blocks reuse.
- Same-bar reset/crossing, future/intrabar prices and stale evidence fail.
- Original price-eligible sequential priority survives funding/quality failure.
- Renewals never reset armed floor memory, fund from PRODUCTIVE cash or exceed
  exact confirmed-plus-pending turnover authorization.
- Source native cash, quantities, basis and fees are changed once; owner and
  recurrence journals only reflect exact source-bound receipts.
- Fixed-ticket, one-percent-ticket and current-cost flags retain their frozen
  comparator settings; unknown/unfunded cases report explicit denials.

Report original versus renewed fills, episode ordinals, positive-profit proof,
renewal denials, original-rung-priority blocks, turnover utilization, finite cash
and slot shortfalls. Existing terminal-mark/liquidation/stale-data disclosures
remain required. No profitable or deployable conclusion follows from tests
alone.

Root will separately freeze the evaluation registry/cumulative cap and admit the
new driver and its independent checks before historical outcomes are read or
new scenarios run. No implementation or historical evaluation was performed
by this specification's author.
