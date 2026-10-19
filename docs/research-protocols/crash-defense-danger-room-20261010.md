# Crash Defense and Portfolio Growth Danger Room — 2026-10-10

This separate research campaign asks whether a bounded benchmark hedge can preserve more **total marked account equity** than unchanged PARK during a systemic decline, without losing the benefit during the rebound or consuming uneconomic collateral. It also describes asset-specific inventory growth and the quality of retained ownership. More units, a profitable hedge book or more realized trades alone are not a successful account result.

The current deliverable includes descriptive saved-history inventory analysis, current official capability/data audits, a research design and a missing-input register. No strategy simulation, hedge result or live readiness is established. The companion `research/danger_room/protocol.json` is **BLOCKED** until a complete immutable, machine-checkable run manifest and independent preflight exist. `research/danger_room/run-manifest.template.json` uses schema `spectra-danger-room-run-manifest-v1`; its explicit missing fields are not permission to substitute guessed values. A JSON specification is not itself an implemented execution guard.

## Preserve the earlier result and the prospective holdout

PR #26's July 1–30 comparison is complete: PARK ended at $1,903.42; SHARED_RAW $1,898.71; SPLIT_RAW $1,893.58; SHARED_QUALITY $1,883.08; SPLIT_QUALITY $1,892.41. The four experimental policies failed their predeclared account-equity, drawdown and terminal-open-loss criteria. Their increased activity did not establish a better account outcome. Preserve the negative result and keep PARK as the reference. July was previously viewed; it is not an untouched performance holdout.

PR #26 is closed without merge. The retained research head is `931d296775425fbea26140ecf2a654767b691874`, branch `research/parked-cash-workflow-20261009`. Its saved financial summary and independent audit receipts remain the evidence, rather than rounded values repeated here. Closing the completed July comparison does not terminate its separate prospective collection or fresh-cash follow-up.

The separate prospective comparison begins **2026-10-09T15:36:31.317254Z** and ends exclusively **2026-11-09T00:00:00Z**. Frozen protocol SHA256: `ef3148ec5b896aded5e812b71d687ea64eddc931c5e86533bfca6f0d08c37134`. Public collection continues in parallel. Do not inspect its prices, evaluate performance, rank its assets or retune its five frozen arms before the endpoint. Operational capture health and opaque artifact metadata may be checked. Its fresh-cash replay adapter remains separate unfinished work: it must reproduce the frozen five-arm fresh-$2,000 rules and receive an independent audit before November scoring. Hedge findings do not change those rules. November 9 is a research endpoint, not a production launch prerequisite.

New material belongs to `research/crash-defense-danger-room-20261010`, based on the retained PR #26 head, under new Danger Room paths. This does not merge or modify PR #26. The corrected execution source in PR #33, `fad5f320b2f87cc7c089bea2750dd8344aa1b661`, is a separate reference, not a validated derivatives implementation. Production `spectra-core-phase2` remains at `6e894fa9c2b55384d0f6120f41ca91892e1fe207`.

## First batch: audit before a strategy run

The authorized first batch can proceed without placing orders or using account credentials:

1. Reconcile saved July account, cash-owner, acquisition-purpose and lot evidence. Preserve uncertainty rather than manufacturing missing units or an asset-level report from aggregate totals.
2. Audit current official Kraken spot-margin and futures/perpetual routes. Record instrument identifiers, specifications, geographic and account eligibility, actual API availability, cost schedules, margin changes, liquidation mechanics and collateral/legal recourse. A public support page is not evidence that this specific account is admitted to that route.
3. Inventory historical spot data, point-in-time listing identities, native adapter inputs, fills/lot histories, historical instrument availability, funding and margin terms. Separate available inputs from unavailable history.
4. Prepare ownership, concentration and inventory report schemas. Any retrospective quality classification must use only information published and available at the decision timestamp.
5. Complete the missing-input register and propose the first content-hashed H1 manifest. Give Architect the rough simulation ETA before starting the run, once workload and native runtime are known.

Initial assessment estimate: **60–90 minutes**, excluding external-access delays. There is no defensible strategy-run estimate until the dataset, route, funded initial state and adapter are fixed.

## Separate accounting books do not isolate venue collateral

`SPOT_BOOK` owns tokens and runs unchanged ENTRY/VCAP/recovery/PARK decisions. `HEDGE_BOOK` records bounded derivative exposure, costs and margin. `ACCOUNT_EQUITY` consolidates them without counting cash or collateral twice. Native PSSM, DTSS and DTSSR remain off. No derivative controller may compete directly with VCAP.

This is internal accounting separation. It does **not** prove Kraken cannot liquidate or seize same-account spot holdings. A spot-margin route may expose existing spot balances to venue collateral or closeout rules. The exact eligible collateral, cross-margin relationships, legal recourse and liquidation waterfall must be verified and simulated. A statement that the hedge uses cash collateral is insufficient. If owned spot can be seized, the simulator must debit those actual assets and disclose the loss; it cannot confine liquidation to an imaginary hedge ledger.

Every arm starts with identical consolidated equity `E0`, spot units/basis, lot provenance and cash ownership. Declare a finite hedge budget `B` **within E0**, with a verified authorized source. Default source is unencumbered ordinary cash; no implicit VCAP or PARK transfer is permitted. Reserve the same `B` outside spot decision authority in every arm: the control holds it as cash, hedge arms divide it among free cash, locked collateral and paid costs. Locked collateral remains an asset with its own risks; moving it is not profit.

The initial overlay also requires that the unchanged PARK spot trace remains cash-feasible after the common budget reservation. If it does not, the overlay is blocked; test a separately frozen native budget-allocation replay later. Do not keep infeasible spot fills and subtract collateral afterward.

If the original account has no suitable cash and a separate experiment uses extra capital `B`, **all controls receive the same additional B** and the reported starting equity is `E0+B`. Label that a conditional capital experiment. Never add capital only to the hedge arm and call its improvement an account edge. Any output that cannot reproduce finite cash and margin is an illustrative scenario, not account performance.

## H1 is a five-arm BTC-only comparison

All arms use the same unchanged PARK spot decision/fill/lot trace, timestamps and marks. The overlay has no feedback into spot budgets or capital-governor inputs. Hedge profit is not recycled into purchases. This first test isolates hedge and collateral economics; a fully interacting native account test is a later experiment.

| Arm | Benchmark hedge ratio |
| --- | ---: |
| PARK_OFF | 0% |
| PARK_BTC_25 | 25% |
| PARK_BTC_50 | 50% |
| PARK_BTC_75 | 75% |
| PARK_BTC_100 | 100% |

Estimate portfolio BTC beta from rolling, timestamp-aligned completed observations strictly preceding each decision. Do not fit beta over the whole crash or use current surviving asset membership as the old universe. The estimator, return interval, lookback, minimum sample size, bounds, missing-data policy, mark-age limit and rebalance cadence remain **BLOCKED** until explicitly frozen.

The intended bounded target is:

`min(ratio × max(0, causal beta) × verified marked spot exposure, verified marked spot exposure, funded route risk cap)`.

Round down to the verified contract increment; an amount below the minimum produces no hedge. The target is protective: no net-short speculation and no hidden credit. Initial/maintenance margin, leverage limits and liquidation buffers still require route-specific frozen values. Unknown exposure or beta denies a new/increased hedge; the existing-exposure response must be separately defined before a run.

Preserve the pinned PARK/native breadth and cap rules and the user doctrine of broad spot ownership; this campaign changes no active-slot limit. Exclude BTC/ETH from spot tests. A BTC hedge instrument is a separate benchmark exposure, not permission to add BTC spot positions. BTC+ETH, then BTC+ETH+SOL, are contingent **later matrices**, with separately frozen weights and rules. Matched-asset shorts, dynamic baskets, native account feedback and hedge-profit accumulation are later research stages, not additional H1 factors.

## Freeze causal triggers, hysteresis and execution

Prepare a deterministic state machine: `NORMAL`, `RISK_OFF_WATCH`, `HEDGE_ACTIVE`, `CAPITULATION`, `REVERSAL_WATCH`, `HEDGE_EXIT`. Entry and exit conditions differ to avoid repeated short/cover churn. Complete the precise signal expression, closed-bar timeframes, threshold values, confirmation count, dwell, cooldown, transition precedence and stale-data handling before inspecting any H1 outcome. This design chooses no numeric signal threshold.

Observation precedes state transition, target sizing, route/cash admission, submission, latency and future eligible execution. Freeze the event order and prevent same-bar close fills from using a price observed only after the decision. Include partial/no fills, spread, slippage, capacity, contract increments, stops, gaps, adverse rebounds, index/mark/basis divergence, funding, borrow/rollover, intraday margin changes, API outages and instrument suspension/removal. A modeled liquidation is retained as an adverse economic outcome; an unmodeled liquidation mechanism invalidates the result.

No cost, latency, margin, liquidation or account-availability number is invented. Missing values remain **BLOCKED**, and sensitivity ranges must also be frozen before outcomes. Exact contracts and eligible routes are snapshot inputs, not a permanently hard-coded universe.

## Keep two different historical questions separate

**Historical-realistic:** use only assets, listings and short instruments that existed and were actually available in the chosen event, with then-applicable eligibility, terms and costs. Reconstruct delistings, renames, migrations and dead assets. Current listings cannot stand in for past membership. Missing critical historical funding, margin or availability prevents a historically executable account claim.

**Current-system crash-shape:** apply current eligible contracts/specifications to explicitly transformed crash-shaped paths. Document transformation, basis and funding assumptions. This is a current-architecture stress scenario, not a claim that its contracts existed in 2018, 2020 or 2022.

Select and verify event dates, start/end rules, warmup and universe before running. Slow bears, flash crashes, multi-leg declines, high-volatility sideways paths, failed rebounds and V-shaped recoveries require distinct reporting. Never blend these two experiment classes into one profitability number. Sealed Oct. 9–Nov. 9 prices are excluded from both.

## Inventory and diversification remain asset-specific

For each canonical asset preserve starting units; ENTRY and RECOVERY acquisitions; whole exits and partial recovery sales; conversion events; ending units; total and average per-unit all-in basis; realized/unrealized P&L; source owner; epochs/lot IDs; age; listing/liquidity/sellability; residual status; and mark timestamp/uncertainty. Percent growth from zero starting units is not applicable. Do not add units from different assets into an account total.

Small residuals remain property. `economic_owned_asset_count`, `active_management_asset_count` and `tradable_now_asset_count` answer different questions. Removing a residual from active scheduling or attaching a new ENTRY epoch to the same canonical parent requires proven execution finality. PR #33 does not prove that finality. This campaign changes no production duplicate-position or dust rule.

Restricted, delisted or unpriced ownership is not fabricated zero. Preserve exact units and provenance; report last reliable mark, zero-value sensitivity and any permitted executable external reference separately. Verify renames/migrations and record actual exchange conversions as economic events. A tail-inventory cap is a later research proposal, not disposal permission.

Report top-1/5/10 exposure, HHI and its reciprocal effective holding count, causal correlation/beta, clusters, liquidity concentration and volatility contributions where data support them. Effective holdings uses normalized known-valued spot weights summing to one; disclose excluded unpriced ownership and cash. Report consolidated-equity exposure weights separately; their unnormalized reciprocal HHI is not an effective holding count. Fifty ticker symbols do not establish fifty independent risks.

Quality features must carry point-in-time availability and missingness. No future survival can determine original eligibility. Future +30/+90/+180/+365-day checkpoints are evaluation only: time to regain basis and specified gains, checkpoint value, liquidity persistence, delisting, recovery contribution and opportunity cost. Fix exact observation/tolerance rules and the observed-as-of cutoff. Not yet elapsed, missing history, listing exit and unknown pricing remain distinct censored outcomes, rather than fabricated recovery or failure.

## Account truth and deterministic comparison

Reconcile the change in total marked equity:

`E_end − E_start = net external flows + net realized spot P&L + net realized hedge P&L + change in spot unrealized P&L + change in hedge unrealized P&L + other verified income/expense`.

State gross/net conventions and count trading fees, funding, borrow, rollover and liquidation costs exactly once. Returned principal, internal transfers and collateral movement are not profit. Report both books and their consolidation, free/locked/pending cash by owner, gross/net exposure, margin utilization, liquidation buffers, downside saved, rebound upside sacrificed and collateral opportunity cost. A winning hedge against a larger spot loss remains a losing account when consolidation says so.

Report all four ratios and all declared regimes, including incomplete and adverse results. Do not select the best ratio after the fact and call it untouched. A candidate must have a complete conserved finite account, no liquidation or unapproved recourse, lower consolidated maximum drawdown, higher net terminal equity than matched PARK, bounded rebound drag, adequate cash/buffers, and pass every predeclared adverse cost/execution/mark sensitivity. Numeric rebound, liquidity, buffer and reconciliation tolerances remain **BLOCKED**. A drawdown/equity trade-off is reported as a trade-off, not success. Passing this research criterion never approves deployment.

Paired path differences are descriptive; reused historical paths do not establish universal profitability or statistical significance. Freeze any inferential sampling procedure separately. No outcome-driven threshold adjustment, selective event dropping or erased failed run is permitted.

## Required first-run gate

A run remains blocked until a checked, immutable manifest provides:

- Source/protocol/input hashes, experiment class, verified exact period selection, point-in-time identities, warmup and coverage.
- An audited unchanged PARK trace/native adapter and exact initial spot/cash books with source ownership.
- A verified eligible route/API, account jurisdiction, contracts, costs, funding/borrow, margin changes, collateral rights and liquidation waterfall.
- Finite matched hedge budget/source, initial equity, control allocation and spot-trace feasibility.
- Complete causal estimator, trigger state machine, event ordering and realistic execution/terminal-finality rules.
- The exact five H1 arms; adverse scenario matrix; numeric success bounds; uncertainty and censoring rules.
- Per-arm compute budget, output locations, abort rules, implemented manifest validator and independent preflight receipt.

The validator rejects missing/null/UNKNOWN/TBD/BLOCKED required values, hash mismatches, unexplained not-applicable values, declared prospective market coverage or sealed paths, differing declared initial capital or trace hashes, inconsistent declared budgets, and missing route/recourse declarations. Required market references must be typed `market_data` with coverage ending before the embargo. It checks **declarations and local hash coherence only**; arbitrary text or booleans do not prove genuine provenance, causal contents, account eligibility, cash/trace feasibility or venue isolation. Independent semantic evidence review is separately required. A passing fixture remains `approved_for_strategy_execution=false`, `approved_for_live=false`, and `external_facts_verified=false`. This is an offline preflight tool, not a production runtime lock or strategy runner. Source and input freeze precedes every strategy outcome. A later amendment gets a new run/campaign identity and preserves prior evidence.

Stop on conservation, causal, source or modeling failures. Preserve actual no-fill, outage, suspension and modeled liquidation outcomes. Keep an incomplete run labeled incomplete, including when its predeclared compute budget expires.

All work is research only. No merge, deployment, money movement, credentials or exchange orders are authorized by this protocol. Production and frozen holdout sources remain untouched.
