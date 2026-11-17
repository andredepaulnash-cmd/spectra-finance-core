# Adaptive VCAP 1.0

## Policy

Adaptive VCAP is an asset-local recovery scheduler. It does not replace the
portfolio-wide CapitalGovernor, exchange validation, fill reconciliation or
user authorization boundaries.

- Conventional DCA remains first: -2.74%, -4.44%, -6.74%.
- VCAP activates at -7.77% (777 bps drawdown).
- FAT 1: -7.77% up to, but not including, -11.74%.
- SKINNY: -11.74% up to, but not including, -17.77%.
- FAT 2: -17.77% through -24.00%.
- Below -24.00%: Deep Drawdown Governor. Ordinary automatic ladder additions stop;
  the position is not automatically liquidated.

The exact boundary ownership is intentional: -11.74% begins SKINNY and
-17.77% begins FAT 2.

## Fragment rules

- Five independently tracked fragments per asset by default; four is configurable.
- At most two fragments per asset may be active at once. This is not a
  portfolio-wide concurrency limit.
- Other fragments remain in a persistent waiting/priority queue.
- Pending executors always retain a reconciliation slot; the scheduler must not
  abandon an external order merely to satisfy a concurrency limit.
- A completed or recovered fragment can release a slot for the next eligible
  waiting fragment.
- Global capital availability, per-position governance, exchange minimums and
  the CapitalGovernor remain authoritative. A fragment budget is not permission
  to spend beyond those controls.

## Initial paper ladder values

The base quote defaults to $20. FAT 1 uses a doubling sequence of 1x, 2x and 4x
the base ($20, $40, $80). FAT 2 continues that sequence at 8x, 16x and 32x
($160, $320, $640), rather than restarting at 1x.

SKINNY uses a graduated, non-doubling sequence of 0.25x, 0.375x, 0.5x and
0.625x the base ($5, $7.50, $10, $12.50). These SKINNY values and the number of
FAT 2 steps are provisional paper-test defaults, not a claim that Architect
has permanently approved those exact amounts. They are configurable and must
be evaluated against paper fills, venue minimums, fees and recovery outcomes.

The current paper portfolio policy is $25,000 total, with $6,250 reserved as
the initial-entry ceiling and $18,750 as the portfolio-wide recovery ceiling.
Each initial asset entry defaults to $20; the number of target positions does
not inflate that seed. There is no arbitrary $1,000 per-position cap.

## Fragment priority

The queue is not sorted by deepest drawdown alone. The initial deterministic
score weights mean-reversion signal × confidence, liquidity, volatility quality,
estimated recovery efficiency and drawdown urgency. Drawdown urgency has only
10% of the score. This keeps a very deep but illiquid or technically weak
asset from automatically outranking a shallower, higher-quality recovery.

## Deep drawdown

Below -24%, no ordinary automatic doubling continues. The governor holds
automatic additions while preserving the position and its accounting. Any
exceptional recovery action must be explicitly represented in policy and pass
the existing capital and execution gates.

## Implementation status

The pure phase, ladder, priority and active-fragment selection policy with unit
tests is in the Core codebase. This policy is not considered operational
Adaptive VCAP 1.0 until the persistent fragment manager, per-fragment
execution/reconciliation state, portfolio-wide recovered-liquidity allocator
and restart acceptance tests are wired into the portfolio supervisor.
