# BTC hedge replay preparation — 2026-10-10

Status: **MECHANICAL_SYNTHETIC_ONLY**. Historical input acquisition is deferred
at Architect's instruction. No historical pilot, holdout evaluation, venue
request, order, production edit or deployment is performed by this harness.

The retained PR #34 protocol remains unchanged and historical H1 remains
blocked. The code prepares a small constructed bookkeeping model; it does not
complete the empirical Danger Room experiment. Passing the PR #34 manifest
validator or these tests does not grant historical execution or live approval.

## Implemented mechanics

`research/danger_room/hedge_replay.py` runs exactly PARK_OFF and BTC protective
ratios 25%, 50%, 75%, 100% against identical initial spot units, all-in basis,
source owners, spot decisions and equal consolidated equity. Each constructed
fixture adds the same explicitly declared ordinary research cash to every arm;
the OFF arm retains it. This is a distinct conditional capital experiment, not
proof that the saved historical account could fund a hedge.

The model uses exact Decimal inputs with explicit 60-digit calculation
precision. It floors whole fictional 0.01 BTC contracts, caps new target
notional by marked spot exposure and applies the separate funding feasibility
module's conservative reserve test. Free cash and locked margin partition the
same money. Margin transfers are not income; perpetual entry does not credit
short-sale principal. Fees, signed funding, realized/unrealized P&L and unpaid
liquidation liabilities enter consolidated equity once.

Spot basis is all-in: buy fees join basis, sell fees reduce net realized spot
P&L, and collateral conversion fees also enter net realized spot P&L. The
separate reported conversion-fee amount is informational and is not subtracted
a second time. Per-asset units and basis reconcile after every event. The dust
asset remains owned unless the explicitly declared adverse liquidation
waterfall actually seizes it; heterogeneous asset units are never summed.

Completed aligned returns estimate rolling OLS beta with an intercept. Spot
returns use the previous observed asset quantities and successive marks, so a
cash-funded purchase is not mistaken for a market gain. Zero benchmark
variance and insufficient observations remain UNKNOWN. New hedge exposure is
denied until observations exist; an unknown beta requests reduction at the
next eligible event. Negative beta is bounded by the explicit nonnegative
range. An economically capped target prevents a high beta from becoming net
short speculation at order admission.

The independently specified negative entry and positive exit conditions use
confirmation, dwell and cooldown rules. Hedge execution follows a completed
observation and occurs only at a later eligible bar opening after latency.
An obsolete pending increase is cancelled when a subsequent causal decision
returns to the current position; an unchanged pending target retains its
original readiness. No fill, outage and fractional whole-contract capacity
are explicit outcomes. Terminal pending orders are cancelled and existing
contracts are marked, rather than silently forced closed.

Two fictional recourse boundaries are deliberately different. Cash-only
recourse retains an unpaid liability if losses exceed available hedge money;
negative consolidated equity is visible. Same-account recourse sells actual
owned spot units, retires their basis, charges conversion fees, then seizes
the original SPOT_ORDINARY, VCAP and PARK cash owners in that declared order.
This records an adverse divergence of the matched spot trace. It does not
authorize pledging those owners, and neither fixture verifies real Kraken
segregation or liquidation rights.

## Frozen constructed pilot

Five shipped JSON manifests contain every fixture parameter used here:

| Fixture | Mechanical question |
| --- | --- |
| `hedge-synthetic-base.fixture.json` | Decline, rebound, causal entry/exit, spot ownership and cost reconciliation |
| `hedge-synthetic-no-fill.fixture.json` | Orders do not create invented positions, profit or costs |
| `hedge-synthetic-partial.fixture.json` | Contract rounding, partial execution and a genuinely open terminal short |
| `hedge-synthetic-gap-cash-only.fixture.json` | A gap can exceed finite hedge cash; unpaid liabilities remain visible |
| `hedge-synthetic-gap-same-account.fixture.json` | The same gap can consume actual spot assets and protected cash owners |

The input prices, funding, margins, thresholds and assets are fictional. Their
January timestamps merely keep constructed observations outside the sealed
holdout; they do not represent January exchange availability. All asset names
are explicitly fictional alternatives or dust. BTC and ETH remain excluded
from spot fixtures. The contract size is a geometry test and does not prove
actual instrument or account eligibility.

The CLI reads only these five shipped filenames beneath this document's
directory. It rejects an arbitrary or holdout filename before opening it.
Direct Python callers remain responsible for supplying constructed objects;
declarations and hashes cannot authenticate provenance. Any historical
experiment class is unconditionally rejected by this version.

Run from the repository root:

```sh
python3 -m unittest discover -s research/danger_room/tests -p 'test_hedge_replay.py' -v
python3 research/danger_room/hedge_replay.py docs/research-results/danger-room-preparation-20261010/hedge-synthetic-base.fixture.json --output /tmp/spectra-constructed-hedge.json
```

The pilot receipt records raw fixture/source hashes, deterministic result
hashes and measured wall time for five 12-observation, five-arm matrices. That
tiny constructed workload cannot estimate runtime for an actual native
historical replay or support a profitable-hedge claim. Every output retains
the scope label and denies economic-edge, historical-execution and live
approval claims.

## Deliberate limits and remaining work

The event model is one declared opening price, one adverse short mark and one
closing mark per bar. Funding settles after discretionary opening execution
using the opening notional. Forced liquidation assumes an immediate cover at
the adverse mark plus declared slippage, even during an outage; this is an
explicit fictional stress assumption, not demonstrated venue behavior.
Historical depth, queues, real partial fills, evolving margin schedules,
listing histories, executable basis/marks, liquidation finality and contract
removal are not implemented. This version does not model ETH/SOL hedges,
profit redeployment, production DTSS/DTSSR/PSSM, or hedge feedback into spot.

Drawdown is explicitly **maximum observed closing-frame drawdown**. It is not
the full intrabar account maximum. Locked cash-bar sum is a descriptive
occupancy measure, not an economic opportunity-cost estimate. The current
fixture portfolio has constant identities and no migration or redenomination;
it cannot establish complete lot provenance or point-in-time real universe
membership. The aggregate per-asset disposal convention is proportional
all-in basis, not a tax or native lot-policy assertion.

| Historical prerequisite | Current state |
| --- | --- |
| BTC benchmark price history with independent provenance and usable timing | Acquisition deferred by Architect; no new history downloaded |
| Then-available contract, fee, funding, margin and trading-status history | Acquisition deferred; fictional fixture values cannot substitute |
| Actual account route, eligibility and collateral/legal recourse | Unverified; internal book separation cannot establish isolation |
| Common cash budget and unchanged native spot-trace feasibility | Separate cash audit; constructed equal extra cash is conditional only |
| Native spot replay adapter and exact trace/source freeze | Separate adapter work; this prototype uses constructed spot events |
| Empirical causal estimator, state machine and rebalance parameter freeze | PR #34 empirical fields remain unresolved; toy values cannot be promoted |
| Full execution, adverse sensitivity and numeric success criteria | Unresolved for empirical H1; no winner or successful-candidate claim |
| Independent source/input semantic review and complete H1 run manifest | Still required before a historical experiment |
| Fresh-$2,000 prospective holdout adapter | Separate frozen experiment; no collecting holdout outcomes used here |

Recommendation: retain this prototype as auditable preparation, complete the
parallel cash/adapter work, and wait for Architect to resume item 3 before
acquiring historical hedge inputs or estimating/running a historical pilot.
