# Completed funding and source diagnostics — 2026-10-10

Generated `2026-10-10T22:07:29.192152+00:00` from 3 finalized independent accounting-PASS development runs. Every artifact hash was reverified before reading its contents. This descriptive report neither changes policies nor selects coins. No partial run or sealed holdout is included. Amounts below are rounded USD; derived JSON retains exact serialized arithmetic and the complete input bindings.

| Run | Realized net | Open loss | Whole account marked P&L | Paid fees | Gross turnover |
|---|---:|---:|---:|---:|---:|
| causal_category_ladders | 159.39 | -350.20 | -190.81 | 28.82 | 7,204.33 |
| current_cost_owned_control_174 | 217.87 | -370.98 | -153.11 | 74.45 | 9,306.20 |
| owned_control_174 | 203.92 | -367.09 | -163.17 | 36.44 | 9,109.31 |

| Run | BASE realized | RECOVERY realized | BASE marked P&L | RECOVERY marked P&L |
|---|---:|---:|---:|---:|
| causal_category_ladders | 158.79 | 0.60 | -187.44 | -3.37 |
| current_cost_owned_control_174 | 195.22 | 22.65 | -147.96 | -5.15 |
| owned_control_174 | 183.36 | 20.56 | -166.36 | 3.19 |

The source split follows exact disposed-lot allocations and terminal remaining-lot marks. BASE and RECOVERY marked totals reconcile with the independently audited whole account. Positive realized exits cannot establish portfolio profitability while open losses remain.

| Run | Ending usable PRODUCTIVE | Ending usable VCAP | Samples VCAP can fund modeled minimum | Occupied slot time / 50-slot capacity |
|---|---:|---:|---:|---:|
| causal_category_ladders | 1,106.59 | 507.97 | 100.0000% | 99.9693% |
| current_cost_owned_control_174 | 683.48 | 5.04 | 1.9490% | 99.9644% |
| owned_control_174 | 669.31 | 0.00 | 0.4393% | 99.9668% |

Usable cash is the lesser of its owner cash after that owner’s holds and physical cash after all holds plus `max(25% of current marked equity, $40)` reserve, floored at zero. Pending and parked claims are included. Minimum recovery affordability means $5 gross plus the declared fee ($5.02 at 40bps; $5.04 at 80bps), not a profitable or admitted order. Route, pair-cap, identity and selection constraints remain separate. Occupied slot time is the audited sum of occupied slots over total 50-slot capacity, not the fraction of bars with all fifty filled.

In the two completed owner controls, PRODUCTIVE cash could fund the fixed $10.25 ticket at every sample, while VCAP minimum-ticket affordability was scarce. In the isolated ladder arm, VCAP stayed available but profile evidence blocked most ladder eligibility. These observations describe different constraints; they do not identify a counterfactual profitable intervention.

## Frozen ladder evidence

`causal_category_ladders` assigned 365 UNKNOWN profiles among 366 parents; 49 remained UNKNOWN at termination. Categories: `{"UNKNOWN": 365, "deep": 1}`. The observed 5 distinct parent/rung requests totaled $51.25; confirmed rung funding totaled $31.73. There were 8 submitted recovery attempts (including retries), 6 submitted quote reductions of at least one cent, and 3 confirmed rungs at least one cent below their full nominal request. 1 parents reached exhaustion, with 1 still present at termination.

Exact below-request counts also include tiny quote-rounding differences; the one-cent threshold is presentation only and cannot change policy or rung consumption. A confirmed positive fill consumes a rung under the frozen source even if the confirmed ticket is below the nominal request. Rejected attempts consume none. The historical Architect per-coin map remains unrecovered; these assignments are explicitly new research hypotheses.

Repeated gate evaluations: `{"LADDER_EXHAUSTED": 35466, "LADDER_RUNG_ELIGIBLE": 8, "LADDER_THRESHOLD_NOT_REACHED": 1113, "UNKNOWN_LADDER_PROFILE": 773899}`. These counters are not unique denied orders or dollars. Source evidence does not export cap-specific denied rung amounts or per-rung reset histories; no such attribution is inferred.

## Fees and scope

Paid fees and gross turnover sum actual serialized fills. Disposed-lot fee drag adds disposed entry fees and exit fees back to realized net P&L for an accounting comparison; it is not a zero-fee strategy rerun. Fees on open lots remain inside their open basis. No policy thresholds, asset universe or number of trials were changed for these diagnostics.

## Source bindings

Code: `research/danger_room/completed_run_diagnostics.py`; synthetic validation: `tests/test_completed_run_diagnostics.py` (18 tests pass). Full derived output bindings live under the same campaign scratch `descriptive_diagnostics/` directory. Each finalized run’s published accounting receipt, results and artifact manifest remain authoritative.

- `causal_category_ladders`: accounting receipt `64ceb3ea755bfe9b2e9984eb4d15daef56409c39ee3dabc930e3535e8fd63ed1`; results `f9f4fc0877cc76fdb4cba4371df47d6581d0f97ed3fae5f9dd24b571b0d59552`; manifest `f148cf68a363d0724e20a4d9c5b89770de35bb61cf625fef448a1a5e7d0ba9a9`; derived diagnostic `3b313261366abf21088fe6025496a2ad8e6d874782f37e68f9789d92ce2d0849`.
- `current_cost_owned_control_174`: accounting receipt `920804797a65b46cf35db8e0746eecd6e4de7d3689802b2efca138b89557163c`; results `9f28f5a194b2909afd9663d235ff67ded3dc5fc88286691085893c09a4c9fc82`; manifest `8c73b1a3d860987ea7b3756ca5a185e687e8496931653db620fca86759c8c66c`; derived diagnostic `2b1581559d169ebc37177bc75b94b2237175569113d8b8102526345762c6e0fe`.
- `owned_control_174`: accounting receipt `92a9cba9d03d5eadf1e5f00eb0b8f69a8619d157963961da614a86f03be1c04a`; results `dee4efab7626dc2e5d4e1715f32c419e3e5e7b2de700a791315e69265527d6c5`; manifest `b2ba20ffddec2610439862f7537275ad7b2e88ee7f1935b55f80a5408f6d2f71`; derived diagnostic `38f8289e9e790c5086efea4624f6869283b0f2991ebf5499f7c8e75a680d761d`.

Diagnostic source SHA256: `a2a6f0a4cf427a0555b288857218c8faede9ec3f2b43240eab04c59af53504ef`.
