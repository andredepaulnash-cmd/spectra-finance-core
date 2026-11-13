# Spectra VCAP: fat / skinny / fat recovery zones

## Purpose

This policy translates the Architect's current VCAP drawdown doctrine into the
existing one-tranche-at-a-time recovery planner. It controls parcel geometry,
not capital authority. Every request remains bounded by the existing recovery
plan and CapitalGovernor authorization.

## Drawdown zones

Drawdown is measured from the position's current average entry price:

`drawdown_bps = max(0, (average_entry_price - current_price) / average_entry_price * 10,000)`

| Zone | Drawdown | Tranche geometry |
|---|---:|---|
| Early FAT | 0% through 11.74% | Front-loaded parcels |
| Middle SKINNY | Above 11.74% through 17.77% | Five equal parcels |
| Deep FAT | Above 17.77% through 24.00% | Front-loaded parcels |
| Outside VCAP band | Above 24.00% | No new VCAP patch; retain capital and continue lifecycle/exit monitoring |

Boundary comparisons are inclusive at 11.74%, 17.77%, and 24.00% as shown.
The values are represented in basis points in code (1174, 1777, 2400).

## Parcel construction

- FAT zones use the existing descending-weight fracture: larger first parcel,
  progressively smaller later parcels. When fresh reversal evidence supports
  aggressive recovery, the existing adaptive planner may reduce the desired
  count toward its configured minimum.
- The SKINNY zone always requests the configured maximum of five parcels and
  keeps their weights equal, even when the behavior signal is otherwise
  aggressive.
- Exchange minimums, increments, sellability headroom, and quote rounding can
  reduce the actual parcel count. A parcel that cannot independently meet
  venue exit minimums is never created.
- After every fill, the planner recomputes from current position geometry,
  market evidence, remaining authorization, and current exchange rules.

## Safety and non-goals

- This zone policy does not increase the reserved recovery envelope.
- It does not change the 9% portfolio reserve, 50-position limit, $20 initial
  entry target, pair exposure limits, or other CapitalGovernor constraints.
- It does not authorize a new order; the exact next tranche still requires the
  existing Governor carve/commit path and protected execution controls.
- Beyond 24% drawdown, the VCAP patch planner stops proposing additional buys.
  The position remains owned and monitored by the lifecycle; this is not an
  automatic liquidation instruction.
- Historical strategy files contain several different VCAP ladders. Those
  historical multipliers are not silently imported into this policy. The
  current implementation uses the Architect's explicit three-zone thresholds
  and the existing tested front-loaded/equal tranche geometries.

## Verification

The unit tests cover every exact boundary, all three tranche shapes, the
24% stop boundary, exchange sellability, and Governor-envelope conservation.
These tests prove deterministic mechanics only, not profitability.
