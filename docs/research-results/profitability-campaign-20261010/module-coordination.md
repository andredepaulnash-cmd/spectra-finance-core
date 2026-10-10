# Module coordination: research contract and integration limits

This is an offline research admission contract. It does not activate trading,
DTSS, DTSSR or PSSM, modify production financial rules, certify an API image,
inspect the sealed October–November holdout, or establish profitability.

The campaign is bound to PR #37 commit
`51385b5ef71a25b38e82cf3927aebc671c6939c7` and the frozen campaign protocol
SHA-256 `a10def9591d19bc8c775d136b5c845383d42778f274e824ad1d77af88552e374`.
The separately frozen owned-cash supplement is SHA-256
`29f5abe77e7ed27aec6a63393411c7e9b0875cec7b1537d85ad5cb13ee9464f9`.
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

For the generic arbiter, the campaign's 25% reserve target, 20% floor, bootstrap floor, 5% pair cap,
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

## True owned-cash paper ledger

The supplemental implementation in `research/danger_room/owned_cash.py`
separately models the exact cash owners instead of asserting that a fungible
replay proves ownership. At $2,000 it starts with $1,500 PRODUCTIVE and $500
VCAP. It preserves native source lot quantities, gross basis and entry fees.

`reserve_buy` locks an all-in limit in the proper owner before accepting a
proposal. `confirm_buy` spends that same hold for the actual quantity, gross
and fee, and binds a provisional ENTRY parent to its confirmed identity.
Positive partial fills leave unused cash held. Confirmed terminal no-remaining
order proof releases only the unfilled hold; queued cancellation and an
unbacked FILLED status cannot do so. Duplicate receipt replay never adds
inventory, spends cash or charges fees twice. A pending recovery claim blocks
both another patch and new entry identity for the same coin.

Owner funding is rechecked before the native fill mutates quantity. Both the
25% pretrade-equity target and the 20%/$40 hard floor apply, including all
pending and parked holds. The historical dependent-entry reserve exception is
not used. No expected sale proceeds fund a pending replacement. `health`
reports subsequent target/floor pressure without allocating cash or pretending
a mark is settled money. Exchange minimums and the native 5% pair limit remain
in the engine's source-bound execution/planning path.

`settle_sale` selects exact lot IDs from one parent and pair. It checks native
fill totals, a common sale price and proportional exit fees, retains remaining
quantity/basis/entry fees, and refuses duplicate disposal. Fee-inclusive
principal returns to the originating lot owner. Each disposed lot's positive
realized profit, after its entry and exit costs, is allocated 75% PRODUCTIVE
and 25% VCAP; a negative result stays with its original owner. Mixed profitable
and losing lots remain visible in account and owner attribution.

Control partial proceeds remain nondeployable under their established owner.
The explicit `release_confirmed_partial` research arm can release only an
already recorded confirmed partial-sale receipt. It never moves principal
between owners or creates cash. A productive share of actual positive profit
is available only through the separately frozen 75/25 allocation; recovery
principal is never renamed as productive cash. Whole proceeds and candidate
partial deployability follow the supplemental control/arm specification.

The public `allocate_sale_proceeds` helper preserves native selected lot order,
uses archived precision-28 HALF_EVEN quantity proration, and assigns the final
decimal remainder to the last lot so gross proceeds and exit fees sum exactly
to the confirmed fill. The source-compatible allocation comparison tolerance
is $1e-18; it does not clamp or rewrite any actual amount. `all_in_quote`
reserves the exact source gross-plus-fee components, including a possible
native decimal ULP, without changing the native quantity or fill.

The ledger enforces cash plus remaining fee-inclusive basis equals initial
capital plus realized P&L, independently for each owner and the account.
Entry fees live in basis and sale P&L; reporting fees does not subtract them
again from equity. `checkpoint` exports full reservations, original requests,
lot state, used identities, receipts, held sale proceeds, owner allocations and
fees. Restore requires an externally bound SHA-256 plus the frozen protocol,
checks state/invariants, and retains exact replay behavior. A checkpoint hash
is an integrity link, not exchange provenance or live approval.

Thirty-nine synthetic owned-cash tests additionally cover mixed owner losses
and profits, actual partial buy fills, positive-profit allocation, no pending
cash borrowing, fee accounting, delayed proof, lot scope, common sale price,
exact source fill totals, tiny Decimal claims, final residual proration and
checkpoint/restore replay. These are monetary and quantity invariants, not
historical profitability evaluations.

## Native adapter boundary and matched comparisons

The exact archived historical bridge has a shared fungible cash ledger and
colored parked recovery proceeds. It does not independently prove the
separate cash owners modeled by this helper. Native partial recycled parcels
are recovery-owned. Therefore, unlocking those proceeds cannot establish
funding authority for new productive slots. The campaign's
`confirmed_principal_redeployment` and `combined_full_supported` evaluations
require this true owner ledger plus source-bound native hooks and independent
reconciliation before valid results. Their unmatched fungible form remains
blocked. An absent evaluation is not a completed run or evidence of an
economic win or loss.

The native rotating-fleet source also has a historical replacement dependency
exception to its shared reserve check. Preserving the exact archived control
does not authorize carrying that exception into a new owner-preserving
release model. An approved research policy would have to state exact owners,
principal, profit, held proceeds and protected floors explicitly.

The source-bound adapter must preserve the following continuous journal
interfaces, in addition to the implemented owner ledger:

1. Explicit opening PRODUCTIVE/VCAP/PARK ownership that matches total cash.
2. Owner-tagged entry and recovery reservations, pending burns and confirmed
   fills, including fees once and canceled funds retained until proof.
3. Exact lot-specific disposal allocation and confirmed proceeds returned to
   their declared owner, with protected money never made deployable.
4. Conserved residual units/basis/fees, terminal identity release, reserve and
   concentration guards, and reconciliation after every replay step.
5. Exact unchanged-control reproduction and independent mixed-owner,
   partial-fill, cancellation, dust, replay and month-boundary audits.

The original native controls remain unchanged. The supplement supplies a
separate `owned_control_174` for matching ladder/redeployment/full combinations.
The owner adapter does not preauthorize dependent ENTRY while fifty old parents
still hold their slots. It first records the exact confirmed close, then plans
refill from observed eligible candidates for the next observed execution bar.
The matched owner control includes this latency. An expected exit creates
neither a loan against future owner cash nor a promise that the slot is open.
Comparisons against the fungible control disclose the architectural changes;
they cannot be called an isolated ladder effect. The higher-cost 80 bps fee
and 15 bps slippage arms are fixed present-cost sensitivities, not claimed
historical January–June fees or proven account-specific execution costs.

The read-only source hook audit binds sixteen exact anchors in the archived
continuous body SHA-256
`e18adaad068b4b950d972c0330651bdca17755f1d48f408ced8b59fa3acb217a`.
It identifies proposal creation, pending acceptance, future-bar FOK rejection,
pre-fill owner validation, actual buy identity binding, and snapshots of
native selected economic lots before partial/whole sale consumption. The
owner journal must not debit the native cash or fees again. At each five-minute
observation, reconciliation compares actual native cash, remaining quantities,
gross basis, entry fees, realized P&L and total fees, with the source's $1e-18
tolerance. The exact run receipt must state which hooks were exercised and
which product modules remain outside this paper engine.

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
