# Protected whole-position exits: validation receipt

## Source and scope

Dependency: unmerged draft PR #27, commit
`0ad928fa5d6f51ed2f402bb58c360d8a9f6b4a36`.
Production remains `spectra-core-phase2` at
`6e894fa9c2b55384d0f6120f41ca91892e1fe207` (PR #25).

This draft extends profit-protected execution to unified whole-position exits.
It does not activate PARK allocation, change targets or recovery ladders, enable
DTSS/PSSM, or replay the historical strategy. The existing default remains
174 net basis points; the 100-basis-point research candidate is not installed.
The draft is based on PR #27 but targets phase2 so configured CI checks the full
proposed stack. Neither draft is merged or approved for live operation.

## Execution contract

New manifests persist the configured target, fee/slippage allowance, tick-rounded
LIMIT floor, exact ENTRY/RECOVERY owner scope and deterministic SELL executor ID.
The aggregate floor protects proportional disposition of the whole owned parcel.
STRATEGIC_RETAINED and UNATTRIBUTED inventory cannot satisfy the sale.

Before first submission, all source buying executors must be held passive with
`keep_position=True`. Fresh exact order/trade snapshots must prove terminal source
orders and confirmed coverage of source fills. Inventory geometry must still
match the frozen manifest. A fresh exchange rule is required for first submission.
An OPEN recovery-partial manifest blocks a whole manifest, and vice versa, inside
the shared SQLite creation transaction.

A durable once-only claim precedes external SELL creation. Only an explicit 404
before that claim proves absence. Timeouts, server failures, empty responses or
claimed-but-missing executors require reconciliation; no new MARKET fallback,
price chasing or blind resubmission is permitted. Known SELLs reconcile before
fresh-submission guards, including after a breaker halt or market-price change.
Legacy manifests and the old single-executor profit helper are reconciliation-only.

Confirmed exact SELL fills dispose only frozen lots, journal actual consumed
per-lot basis in the inventory transaction and release RECOVERY exposure from that
actual basis once. ORDER acknowledgements and held source metrics cannot prove a
sale. Protection breaches are accounted honestly and persisted as conflicts.
Post-fill checks detect outstanding orders exceeding remaining inventory.
Completion requires exact durable fill coverage, fee-inclusive profit checks,
zero ACTIVE inventory, and confirmed seller plus order finality. A canceled order
or unsellable residual remains pending; there is no invented dust write-off.

## Finite accounting arithmetic

Repeated proportional fills previously accumulated ambient Decimal rounding in
quantity/basis subtraction. Additions and differences of stored finite journal
amounts now use enough precision to preserve those exact amounts. Division remains
a finite proportional proposal. Each proposal is bounded by remaining quantity and
future owned-lot capacity; full-lot disposal takes the stored basis directly.
Strict coverage and ownership checks remain exact, with no epsilon tolerance.

Independent tests compare fractional stored amounts with Fraction arithmetic,
including repeated mixed-owner fills, replayed Governor releases and a tiny final
lot. This repair does not change strategy admission, targets, fee assumptions,
reserve percentages or economic capital limits.

## Validation

Local environment: Python 3.12, pytest 8.4.1 and Hummingbot API client 1.5.9.
The local restored source mirror excludes the full application conftest. GitHub
CI uses the configured Python 3.13 locked dependencies and the full checkout.

The new independent test file contains 64 scenarios and is added to Phase 1 CI.
Coverage includes profit floors, immutable scope, migration, source passivity,
once-only submission claims, ambiguous lookup, confirmed partial/full fills,
fee/order protection breaches, cross-journal conflicts, exact Governor releases,
crash recovery, finality, dust, mutual manifest exclusion and runtime restart.

Final local regression: **175 passed in 3.44 seconds** across whole exits,
partial recycling, protected recovery exits, economic inventory, lifecycle/runtime,
multi-position scheduling, fill reconciliation and Governor exposure tests.
GitHub validation belongs to the exact draft commit/run and is not predeclared here.

## Remaining release work

1. Durable Governor-owned PARK cash authority and finality-safe cash attribution.
   Routing must use sale purpose: recovery-partial proceeds go to PARK; whole
   proceeds remain productive even when the parcel contains recovery lots.
2. Explicit finality-safe resolution of canceled/uncertain submissions and dust.
   This patch keeps unresolved inventory/capital reserved rather than guessing.
3. Paper/shadow verification with the installed Hummingbot core image, Kraken
   connector, Bazzite runtime and restart scenarios. Passing client-level tests
   does not prove deployed connector execution or net profitability.
4. Strategy validation of any parked-capital release policy remains separate.
   Unit checks do not reproduce 94.91%/99.1% preservation or create a profit claim.

Recommendation: review this draft with PR #27, then implement PARK authority before
paper/shadow capital-reuse trials. Estimated PARK work: 2–4 hours plus 5–15 minutes
for configured CI, excluding external-deposit/BTC basis handling and live deployment.
