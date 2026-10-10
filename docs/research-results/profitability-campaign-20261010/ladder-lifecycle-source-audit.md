# Source audit: single-pass ladders and recovery recycling

This audit was completed without reading candidate strategy outcomes, terminal
positions, financial summaries, market arrays, or the sealed holdout. It changes
no source code, existing protocol, production setting, or trading permission.
The findings describe the exact preserved native source and immutable V2 adapter.
They do not establish the profitability of any policy.

## Finding

The V2 ladder permits one pass through its rungs per economic parent. A confirmed
profitable partial recycle of a RECOVERY lot can return cash and reduce the
remaining parent basis, but it does not restore the consumed rung. The generic
native research replay can subsequently request another funded RECOVERY parcel
under the same still-open parent. Its ability to do so is conditional on current
position geometry, evidence, capital and execution constraints.

Neither behavior should be described as the complete durable production
lifecycle. The preserved durable lifecycle separately advances a receipt-bound
sequence and reduces a finite authorized recovery envelope; recycling does not
automatically renew that envelope.

## Exact source register

The imported-core inventory contains 23 files. Every current preserved file
matched its manifest SHA256 during this read-only audit. Relevant sources are:

| ID | Source | SHA256 |
| --- | --- | --- |
| V2 | `research/danger_room/owned_profitability_campaign_v2.py` | `8acfb0aadf570294fe0855a0ca49cc87dfaa093d728238474d7de031e6b14024` |
| BODY | `campaign-20261008-rotation-followup/baselines/hard_174_park/generated_continuous_body.py` | `e18adaad068b4b950d972c0330651bdca17755f1d48f408ced8b59fa3acb217a` |
| BRIDGE | `campaign-20261008/native/danger-room-20261006/restored/evidence/one-percent-research-20261006/one_percent_test.py` | `87247fd166775c3230445ed06e29f5a4ba2ad0e9253a9a52e3ff5e085f7db6c7` |
| EXIT | Preserved core `vcap_profit_exit.py` | `0803bc2f168704e2b0258a88172e6e24ca15ea942a1ab76361dad9715a0269da` |
| LIFE | Preserved core `vcap_lifecycle.py` | `91795d5ca1de0ccaa280888a77980199334971538c0d34a2207ff4732cb48f86` |
| SEQUENCE | Preserved core `vcap_execution.py` | `80981cb4d4caa84615e79755b159beae4919d0eb9a3c77bd381872c4ef2cddd6` |
| PARTIAL | Preserved core `partial_recycle.py` | `eab2b61bd4c11ad7424fcfed09403a3d397dd6db695841bbd5230b48f9001417` |

BODY and BRIDGE are the recorded restore-relative evidence paths under the
campaign's native prerequisites. The complete imported-core inventory and
restore identities are in
[`native_source_manifest.json`](../../../research/danger_room/native_prerequisites/native_source_manifest.json).
The preserved core lives under
`research/holdout_replay/vendor/Spectra_2000_Recovery_Recycling_Evidence/recovered/original-plan/Spectra_Exit_Strategy_Evidence/Spectra_Exit_Strategy_Evidence/spectra-finance-core/spectra_core/`.
No current-production source was substituted.

## Rung lifetime and lot lifetime are different

The actual V2 class is `LadderBook` (V2 lines 82–147). At confirmed entry it saves
the parent identity, pair, initial actual entry price, initial all-in seed and
selected profile. `requested_budget` indexes the profile with `consumed`;
`confirmed_recovery` advances that integer once after a positive confirmed fill.
There is no sale callback, rung reset, remaining-rung-budget ledger, or
partial-disposal rearm method.

| Event | V2 ladder effect |
| --- | --- |
| New confirmed ENTRY | New parent anchor, seed and profile; consumed count zero |
| Accepted RECOVERY request | One exact pending reservation for the current rung |
| Confirmed positive RECOVERY fill | Consume the entire current rung once |
| Smaller fill caused by native funding/cap limits | Still consumes that rung; its unused maximum does not remain available |
| Confirmed zero-fill FOK rejection | Release reservation; consume no rung |
| Confirmed partial recycle | No change to the consumed rung index |
| Last rung consumed | Further requests return `LADDER_EXHAUSTED` |
| Whole parent exit | Native parent closes; no reset of its historical ladder record |
| Later new ENTRY in that coin | New parent identity can receive a fresh ladder |

These transitions follow V2 lines 89–147 and 845–904. The frozen profiles contain
3 shallow, 4 medium or 5 deep rungs (`profitability_protocol.json` lines 69–117).
Exhaustion applies to the still-open parent, not permanently to the coin.

## What generic native recycling does

BRIDGE `record_buy` creates a unique `lot:{order_id}` for each exact ENTRY or
RECOVERY purchase and records source ownership and execution provenance (lines
137–152). Its partial-sale manifest selects RECOVERY lots only. At fill it uses
the selected lots' entire remaining quantity, gross basis and entry fees,
checks execution minima, capacity and any configured net floor, consumes those
lots, completes their manifest, and reduces the surviving parent's quantity and
basis (lines 208–246). It does not retire the old base lot or whole parent.

BODY later calculates the surviving all-in average and calls
`plan_vcap_recovery` and `plan_vcap_tranches` again (lines 469–529). It contains no
lifetime rung count or maximum `recovery_count` eligibility condition. Thus a
new RECOVERY lot can be created after an old RECOVERY lot has been disposed,
provided the current gates permit it. This is conditional repeatability, not
unbounded or automatically funded trading.

PARK explicitly quarantines confirmed partial proceeds. BODY spendable cash is
bounded by both the reserve and nonparked cash (line 361); BRIDGE lines 125–130
define the PARK/reuse distinction. New owner-adapter experiments additionally
require actual VCAP ownership and confirmed release. A profitable disposal is
not permission to transfer PRODUCTIVE cash or protected reserves to recovery.

## Profit-floor memory and priority

EXIT recomputes current net-profit geometry and target price from the supplied
current position and costs. It carries forward the prior floor timestamp,
peak-profit basis-point value and peak timestamp (lines 267–314). A previously
armed floor that is now breached returns EXIT; an armed profitable position
otherwise returns EXIT or EXTEND (lines 335–422).

BODY's partial-fill branch updates the position and cash but neither clears nor
rebases `profit_states` (lines 126–139). A successful whole exit clears it (line
214); a successful RECOVERY purchase also clears it (line 286). Recomputing
current geometry therefore differs from resetting historical floor memory.

Under genuine native DTTP, BRIDGE excludes partial recycling when the parent
profit action is EXIT or EXTEND (lines 175–178). LIFE similarly gives whole
profit protection priority over parcel harvesting and patching (lines 171–193,
441–485). Normally, armed-profit partial harvesting is unreachable through this
priority, so it requires no reset to enable another recovery cycle.

The hard-target research override is materially different. BODY lines 434–439
replace the raw action with WAIT whenever current estimated net profit is below
the hard target, including a raw protected-floor EXIT. The following partial
branch can then run while raw planner memory remains armed. Its later recovery
purchase can clear that memory. This is a declared surrogate policy difference,
not proof of native durable lifecycle behavior.

The separately frozen native-DTTP supplement uses `legacy_174_park`,
`hard_target=false`, and V2 adds an explicit armed-floor recovery veto (V2 lines
292–301, 914–916;
[`native_profit_policy_protocol.json`](../../../research/danger_room/native_profit_policy_protocol.json)
lines 14–24, 28–53). Clearing, resetting or rebasing the floor merely to permit
rung recurrence would violate that protection.

## Imported modules versus invoked lifecycle

Static call-site inspection established these native planner calls:

| Actual replay call | Source location |
| --- | --- |
| `plan_vcap_profit_exit` | BODY line 377 |
| `plan_vcap_recovery` | BODY line 478 |
| `plan_vcap_tranches` | BODY line 508 |
| `plan_partial_recycle` | BRIDGE line 192 |

The replay does not call `plan_vcap_lifecycle`,
`plan_vcap_lifecycle_from_ledger`, `checkpoint_vcap_tranche_execution`,
`checkpoint_vcap_exit_result`, or real `CapitalGovernor.reserve()`.
BRIDGE substitutes `MemoryLedger`, `MemoryGovernor` and `MemoryStore` (lines
44–103); V2 adds a separate research owner journal. Importing the durable core
modules is not evidence that their complete execution/restart lifecycle ran.

In the durable native implementation, SEQUENCE reconsiders current evidence and
geometry one tranche at a time and stops when the remaining authorized envelope
is zero (lines 95–100, 135–147). LIFE consumes authorization and increments its
sequence only after an exact execution receipt; exact replay is idempotent
(lines 262–318). Its partial planning branch saves the existing lifecycle state;
it does not replenish the remaining authorization (lines 459–485). Native
generic replay repeatability must not be generalized to indefinite durable
envelope renewal.

## Research implication

The source limitation warrants a separately bounded recurrence hypothesis:
renew an already used rung only after exact recovery inventory disposal,
positive realized net lot profit, confirmed same-owner settlement and explicit
release, no pending parent commitments, fresh causal price/evidence gates, and
finite lifetime count/turnover limits. Keep the original base lot and entry
anchor. Preserve whole-parent profit protection.

The source audit itself is not a finding that recurrence will improve returns.
The approved fixed proposal is documented separately in
[`receipt-bound-rung-recurrence.md`](receipt-bound-rung-recurrence.md) and
[`receipt-bound-rung-recurrence-spec.json`](receipt-bound-rung-recurrence-spec.json).
It requires a new driver and independent admission before historical testing;
it does not alter the meaning or bytes of the earlier single-pass experiments.
