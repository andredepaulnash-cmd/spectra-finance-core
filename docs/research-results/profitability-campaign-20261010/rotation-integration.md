# Source-bound whole-parent rotation adapter

The separately frozen v3 driver implements the two originally gated rotation
hypotheses and one additional matched adapter control. It does not alter the
admitted v1/v2 drivers, preserved native modules, existing selector, owner helper
or protocol bytes. Its disabled rotation control generates the exact same
financial controller body as v2 (`df36360d54e0f000531c46180d9c570df48971e372b31b84f58ab5ccf7d38dce`).

The native controller still decides ordinary whole-profit exits and profitable
partial recovery harvests first. A reached native profit floor vetoes research
rotation even when the hard-target mode would otherwise wait. Each accepted
rotation records a unique immutable order/parent/quantity/basis tag and causal
gate receipt. Only that tagged whole sale can bypass the 174-bps execution
profit floor. It retains the immediate next actual open, conservative native
1% FOK volume, identity and order-minimum checks. Missing/rejected sales retain
the parent's lots, owner principal and occupied slot.

The confirmed whole sale uses the unchanged v2 per-lot owner settlement: exact
principal and losses return to their origin; positive disposed-lot profit is
split 75/25 PRODUCTIVE/VCAP. The receipt-bound release proof reconciles those
owner cash changes to exact executed proceeds. It preserves every unrelated
parked receipt. No future sale cash, anticipated slot or same-open replacement
is admitted. The fee-hurdle forecast's candidate remains an unfunded intention;
after confirmed closure, current native eligibility and owner guards must pass
before its entry order can execute at a subsequent modeled open. Failed
intentions and terminal pending orders are explicit.

`rotation_events.jsonl.gz` records submission/trigger, immutable sale scope,
confirmed signed loss and fees, owner release, terminal rejection, and linked
post-confirmation replacement or expiry. Standard native fill/account/lot and
owner journal/checkpoint artifacts retain their independently audited schemas.

Local validation passed 20 tests, including 11 actual preserved-native synthetic
scenarios. Execution fixtures inject an explicitly labeled deterministic gate
to isolate execution, ownership and arbitration; separate rotation-policy tests
exercise the genuine causal history and exact rational fee hurdle. These tests
read no historical outcomes or sealed holdout. The local receipt is not an
independent historical admission.

Before history, an independent reviewer must issue a source/helper/protocol
bound `SYNTHETIC_ROTATION_INTEGRATION_PASS` admission, then independently audit
`rotation_owned_control_174` before either candidate. The CLI uses the inherited
owner admission flags and additionally requires:

```text
--rotation-protocol research/danger_room/rotation_protocol.json
--owner-adapter-admission <independent-v3-synthetic-admission.json>
--owner-adapter-admission-sha256 <exact-admission-sha256>
```

Candidate evaluations also require the exact independently audited matched
`rotation_owned_control_174` receipt through `--matched-owned-control-receipt`
and `--matched-owned-control-receipt-sha256`. Only the three rotation protocol
arms are admitted by this driver. The total frozen campaign budget remains 40
strategy evaluations. No historical rotation result or profitability claim is
established by this integration note.

Before any v3 historical admission, a runtime-only revision removes unused
candidate ranking in stale mode and caches one immutable native ranking per
completed frame in fee mode. Current position, pending-pair, closed-pair,
normalized-base and liquidity eligibility still apply separately to each
proposal. Next-frame identity/order invalidation and future-mutation tests pass.
Both active generated financial bodies and the neutral v2 body remain exactly
unchanged. The prior `833307c97cda0bf31b3ef4afc4082973cb6aa6621491feaf969c536781286a5d`
source is preserved as `rotation_profitability_campaign_v3_precache.py`; it ran
no historical strategy evaluation. The optimized driver's separate local
receipt requires renewed independent source admission before history.

The optimized driver and terminal-rejection consistency fix were independently
admitted before history at source SHA
`5f6c9b2b2ca0c1863c410e578893168690f77a59c70e9169db21971d7f85214a`.
The reviewer reran all 20 driver tests and verified the unchanged 31 helper
tests and 144 independent rational hurdle cases. The CLI-ready admission is
`rotation_v3_optimized_synthetic_review.receipt.json`, SHA
`4f238a4a426207514d2a3e97920ac479d32198948501d92f0c5b7162ea42ab0c`.
This admits the additional matched adapter control. Candidate history remains
gated on its full independent financial/owner audit and exact v2 financial
comparison. No source or policy was revised after a v3 historical evaluation.
