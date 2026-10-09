# July parked-cash comparison

All five fixed July 1–30 continuations completed from the exact June checkpoint. PARK finished highest. No release policy is promoted by this result.

| Policy | Ending equity | Versus PARK | July gain | July max drawdown |
| --- | ---: | ---: | ---: | ---: |
| OFF | $1,903.42 | $0.00 | $5.14 | 1.57% |
| ENTRY_ONLY | $1,897.66 | $-5.76 | $-0.62 | 1.97% |
| RECOVERY_ONLY | $1,894.34 | $-9.09 | $-3.94 | 2.50% |
| BOTH | $1,898.71 | $-4.71 | $0.44 | 2.46% |
| BOTH_TIGHT | $1,899.22 | $-4.20 | $0.94 | 2.04% |

Initial capital was $2,000; the inherited June-ending state was worth $1,898.28. The table measures the selected July continuation, including retained unrealized losses and modeled costs. It is not a newly executed January–June or all-module test.

Independent audit passed all 43,200 account rows, 302,400 field comparisons and 719 actual modeled fills. Cash, owned lots, source claims and caps conserved exactly. Each fill also passed recorded-candle price, fee, slippage, quantity, minimum and volume-capacity checks.

The release mechanics activated. ENTRY_ONLY circulated $827.09 from PARK while never exceeding its $100 outstanding cap. More completed profit was outweighed by additional open losses. In BOTH_TIGHT, the $50 shared cap funded recovery only from PARK; it did not fund parked-source entries. Native ordinary-funded entries continued. This is an observed consequence of recovery-first priority, not evidence of balanced funding between purposes.

Both 420-second attempts were preserved: 6,355 frames, then 8,542 frames. The first overlap and the second overlap (including 159 fills and 4,492 source events) matched the final run exactly. Only the runtime budget changed to 600 seconds; no signals, caps, targets, tape or financial kernel changed.

The final repository logging follow-up is separate from the historical observer. Fourteen standard-library checks (eight archive checks and six failure injections) passed; 16 native constructed-money mechanics tests passed. Local production pytest was unavailable; the existing GitHub Phase 1 check passed on the prior research commit. Check final commit CI separately.

Keep PARK as the control. Use saved asset-level losses and successes to define one causal capital-admission experiment, with unchanged targets, ladders and release limits. Compare account equity, costs and drawdown before increasing exposure. Test any improvement over a longer continuous period and a fresh holdout before proposing live authority.

No production source, merge or live trading activation is included. Exact figures and input/output hashes are in the companion JSON. Detailed journals and independent receipts are preserved in the separate evidence bundle.

Terminal mark audit also passed. Marked inventory older than five minutes totaled $91.75 for OFF, $128.53 for ENTRY_ONLY, $107.99 for RECOVERY_ONLY, $102.59 for BOTH and $100.65 for BOTH_TIGHT. These historical valuations are not executable liquidation quotes. Candidate-specific freshness/liquidity remains an open issue. Test entry/recovery budget competition separately from asset-quality changes so their effects remain measurable.
