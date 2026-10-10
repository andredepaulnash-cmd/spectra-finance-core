# Module coordination: research contract and integration limits

This is an offline research admission contract. It does not activate trading,
DTSS, DTSSR or PSSM, modify production financial rules, certify an API image,
inspect the sealed October–November holdout, or establish profitability.

The campaign is bound to PR #37 commit
`51385b5ef71a25b38e82cf3927aebc671c6939c7` and the frozen campaign protocol
SHA-256 `a10def9591d19bc8c775d136b5c845383d42778f274e824ad1d77af88552e374`.
Production remains outside this research branch. January–June prices are
previously inspected development evidence, not a fresh validation period.

## What the repaired source already requires

The read-only audit matched eleven local source/document blobs against PR #33
tree `6d15736a95b20bbd0e5aee8e34a07217f8143b91`. Relevant source is
`spectra_core/vcap_lifecycle.py`, `economic_position.py`, `capital_governor.py`,
`cash_authority.py`, `vcap_execution.py`, `vcap_profit_exit.py`,
`vcap_tranches.py`, `recovery_fingerprint.py` and `decimal_math.py`.

* Accounting health and exact active economic ownership gate new mutations.
  Strategic retained and unattributed inventory are excluded from recovery.
* Whole-position profit protection outranks partial recovery harvesting; an
  eligible partial recovery exit outranks another patch. Profit protection
  cannot return to recovery after reaching its protected floor. The native
  production target is 174 net basis points, including applicable costs.
* A planned patch is checkpointed before external execution. Without its exact
  receipt, its persisted envelope and sequence do not advance. An exact replay
  cannot subtract its capital twice.
* A close request, an apparent cancellation, or a flat-looking snapshot is not
  terminal retirement. Exact executor/order/trade coverage, immutable scope,
  durable fills and settled cash are required before replacing identity.
* Proven unsellable dust moves to residual inventory with its units, basis and
  fees retained. Its retirement certificate releases zero quote capital. A
  residual cannot generate fictional sale proceeds or erase a marked loss.
* Canceled/unused buy authority remains held until a separately proven refund
  protocol releases it. Parked and unclassified balances cannot authorize a
  productive entry or recovery purchase.

These are observed source behaviors. The new research helper does not replace
their durable journal, receipt validation, live evidence freshness or governor.

## Contract exposed to a research engine

The implementation is
`research/danger_room/module_coordination.py`. Its interface is
`decide_batch(intents, state, budgets) -> DecisionBatch`.

| Input or result | Meaning |
| --- | --- |
| `Intent` | Exact proposal ID, economic parent, canonical `BASE-QUOTE`, action, funding owner, all-in requested cost and optional recovery rung |
| `ParentState` | Active, flat pending, residual or retired state; protected-profit flag; consumed rung identities |
| `PendingOrder` | A still-held parent/asset claim, including cancellation awaiting terminal proof |
| `CoordinationState` | Parent/pending claims, maximum of at most 50, accounting gate, spot risk gate and explicit research hedge admission |
| `CashBudgets` | Finite Decimal cash split among PRODUCTIVE, VCAP, HEDGE and PARK, disjoint held/protected amounts, physical reserve floor and optional observed physical cash reconciliation |
| `DecisionBatch` | Accepted reservations, rejected proposal IDs and typed reasons, reservation-adjusted owner cash and occupied claims |
| `SlotShortfall` | Vacancies and typed capital, reserve, pending identity, liquidity, eligible-universe, stale-data or risk/accounting explanations |

Accepted decisions are **reservations, not fills or running positions**. The
engine must measure confirmed open-position occupancy separately. No proposed
sale changes free cash or releases a slot in the same batch. Only a subsequent
state with authoritative retirement and settled ownership can do so.

Arbitration precedence is EXIT, CANCEL, HEDGE, RECOVERY, ENTRY. Within an action,
the integer priority and immutable proposal ID break ties deterministically.
An arbitrary priority cannot lift recovery above an exit. Each economic parent
gets at most one accepted intent per batch; two whole/partial sale requests
cannot both pass for the same parent. The upstream economic-exit adapter still
owns exact lot/quantity admissibility and native whole/partial precedence.

Canonical base identities are shared across quote pairs. XBT normalizes to BTC.
BTC, XBT and ETH cannot be productive spot entries or recovery purchases in
this campaign. Wrapped exposures remain a disclosed universe limitation.
Terminal-confirmed residual or retired records remain visible but can release
their active identity claim. An unconfirmed residual, flat pending record or
pending buy still blocks replacement. Retired parent IDs are never reused.

ENTRY reserves PRODUCTIVE cash, RECOVERY reserves VCAP cash, and the optional
research hedge reserves HEDGE cash. PARK cannot fund any of them. Held funds,
protected owner cash and the physical liquidity floor survive arbitration.
Negative, non-finite, non-Decimal or inconsistent cash states are rejected.
Exact arithmetic preserves small locks even when total balances span many
decimal orders of magnitude. No balance is transferred between owners.

The campaign's 25% reserve target, 20% floor, bootstrap floor, 5% pair cap,
exchange minima, price/fee protection and other exposure limits remain
upstream native/adaptor responsibilities. This helper consumes their exact
allowed owner budgets; it does not infer them from OHLC or silently replace
them with a new rule. A $200 account cannot fund fifty $10.25 entries while
maintaining the declared reserve. Vacancies must be reported honestly.

## Account-level hedge and recovery conflict

The optional HEDGE action is a typed research reservation only. It has no
short-sale, exchange, DTSS or DTSSR implementation. It requires an explicit
`hedge_admitted` state and the single portfolio risk parent
`PORTFOLIO_HEDGE`. A second request cannot bypass the one-parent rule by using
a different identifier; pending portfolio hedge claims block replacement.
Hedges do not consume one of the fifty spot slots.

An explicit defensive hedge proposal with `blocks_spot_increase=True`, if
admitted, blocks RECOVERY and ENTRY across every spot parent for that batch.
The authoritative `risk_increase_allowed=False` state independently blocks
spot increases. A normal hedge adjustment without that declared flag does not
silently change recovery policy. Selecting this experimental defensive rule
must be declared in the campaign before measuring its outcomes. Actual hedge
arms remain blocked pending instrument, cost, collateral and eligibility
evidence; successful arbitration is not historical hedge execution proof.

## Rungs and same-owner cash reuse

`confirm_recovery` consumes a rung only after the exact proposal scope has a
positive confirmed quantity and positive confirmed cash expenditure within
its all-in reservation. Missing receipts and zero fills consume no rung.
The first positive partial fill consumes one rung; it cannot authorize a
second purchase at that rung. Exact proposal replay is idempotent and another
proposal cannot claim the same consumed rung. Remaining reserved money is not
refunded here. Actual subsequent fills, overspend violations, quantity and
basis accounting remain the engine/journal's responsibility.

`unlock_confirmed_recycle` is an abstract research-only cash-availability
operation. It defaults to disabled, preserving controls. When separately
declared, it accepts only caller-proven partial recovery proceeds already
recorded as held VCAP cash. The original and recorded owner must both be VCAP;
the operation decreases that owner's held amount without changing total cash,
protected locks, the physical floor or any other owner's balance. Its prior
release journal must persist continuously; exact replay is idempotent and a
conflicting reuse of a receipt ID fails. It cannot refund a canceled buy,
unlock PARK, invent proceeds, or reassign VCAP proceeds to productive entries.

## Current integration boundary and blocked evaluations

The exact archived historical bridge has a shared fungible cash ledger and
colored parked recovery proceeds. It does not independently prove the
separate cash owners modeled by this helper. Native partial recycled parcels
are recovery-owned. Therefore, unlocking those proceeds cannot establish
funding authority for new productive slots. The campaign's
`confirmed_principal_redeployment` and `combined_full_supported` evaluations
remain blocked until a true owner adapter exists. Their absence is not a
completed run or evidence of an economic win or loss.

The native rotating-fleet source also has a historical replacement dependency
exception to its shared reserve check. Preserving the exact archived control
does not authorize carrying that exception into a new owner-preserving
release model. An approved research policy would have to state exact owners,
principal, profit, held proceeds and protected floors explicitly.

A future owner adapter can preserve the frozen policy by maintaining a
continuous, receipt-bound cash and lot journal with:

1. Explicit opening PRODUCTIVE/VCAP/PARK ownership that matches total cash.
2. Owner-tagged entry and recovery reservations, pending burns and confirmed
   fills, including fees once and canceled funds retained until proof.
3. Exact lot-specific disposal allocation and confirmed proceeds returned to
   their declared owner, with protected money never made deployable.
4. Conserved residual units/basis/fees, terminal identity release, reserve and
   concentration guards, and reconciliation after every replay step.
5. Exact unchanged-control reproduction and independent mixed-owner,
   partial-fill, cancellation, dust, replay and month-boundary audits.

This is additional engineering and audit work, not a small cash-toggle. Its
effort should be estimated after the exact owner initialization and protected
source interfaces are mapped. It need not hold up causal selector and frozen
ladder development runs that do not claim this absent integration.

## Verification and interpretation

`python3 -B -m unittest discover -s tests -p test_module_coordination.py` passes
42 adversarial checks at this receipt. They cover deterministic precedence,
cash and physical locks, owner conservation, cross-pair duplicates, queued
cancellation, terminal dust/flat evidence, conditional fifty-slot admission,
portfolio hedge identity, global defensive gates, rung receipts and VCAP-only
cash-release idempotency. Test proposals are abstract unit fixtures; their
small costs do not claim executable exchange minima or profitable fills.

These checks establish helper mechanics only. They do not demonstrate every
product module running together, marker-to-order causality, real limit-order
fills, profitable coin selection, margin safety, a complete API image, Bazzite
compatibility, or an active background trading controller. Those require their
own source-bound integration and execution evidence. No strategy outcome run
was performed to validate this helper.
