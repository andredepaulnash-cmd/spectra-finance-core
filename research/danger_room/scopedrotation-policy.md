# Scoped rotation policy

This research helper implements the pre-outcome rotation clarification in
`rotation_protocol.json`, SHA256
`6b6b3a83a159e09d0df8d9407d169cd85bf1b800c6462644a3c30d6a06e91da2`.
The frozen selector source receipt is
`db4878d392738b11df75f42fec0e779b24fcfed95b79020a2111acf3883bb305`.
Production, the original selector and active v1/v2 adapters remain separate.

## Interface

```python
tracker = RotationTracker(pairs)
tracker.update(completed_at, bars)  # chronological, aligned 5m CLOSE; shape (n, 6)
mean, forecast = tracker.daily_forecast(pair)  # Fraction or None; JSON evidence
allowed, evidence = tracker.stale_gate(pair, opened_at)
allowed, evidence = fee_hurdle(quantity_decimal, completed_close_decimal,
                               old_mean_fraction, new_mean_fraction)
```

Rows contain open, high, low, close, base volume and trade count. The tracker
receives one completed frame at a time; it has no access to a future tape suffix.
Inputs are copied where retained. Missing calendar times and invalid rows reset
price troughs, matching the exact selector's validity and float comparisons.
Recovery completion timestamps persist across UTC midnight and include the
current partial day. A completion exactly seven days old is outside the
lower-exclusive stale interval; a later completion vetoes the stale gate.

The stale gate requires at least fourteen days of inherited parent age, a fully
tracked prior seven-day interval, all latest seven closed UTC days with at least
274 selector-valid rows, no observed completion in the specified interval, and
strictly negative signed return between exact current and 24-hour-old positive
finite closes. The first tracked candle interval begins one bar before its close
timestamp. Gaps remain known missing observations; coverage cannot be inferred
from price carry-forward.

The daily forecast requires eight exact UTC boundary closes spanning seven
closed intervals, each with at least 274 selector-valid rows. Boundary close
availability requires a positive finite exact close; full-row validity is a
separate coverage test. A missing boundary denies the forecast even if its day
otherwise has enough rows. Every signed simple daily return contributes,
including losses and flat days. Current-day partial observations never enter
this mean. Completed-day evidence is cached without allowing callers to mutate
the cache.

Means and dollar signs use exact `Fraction` arithmetic over decimal encodings of
observed numeric closes. Diagnostics provide exact numerator/denominator
receipts plus deterministic fifty-significant-digit Decimal display strings.
Ambient Decimal precision, rounding, flags and inexact traps cannot change
admission or display. JSON contains no nonfinite numbers.

## Dollar comparison

The helper uses the frozen all-in forecast budget `B=10.25`, fee `f=40/10000` and
slippage `s=5/10000`. With current remaining-parent mark `M=quantity*close`,
`E=M*(1-s)*(1-f)` and `k=1/((1+s)*(1+f))`, both conditions must hold strictly:

```text
new_gain_USD = B*k*(1+mean_new) - B > 0
delta_USD = E-B+B*k*(1+mean_new)-M*(1+mean_old) > 0
```

The comparison charges old exit and replacement purchase friction, values
unused sale cash without interest, and compares one-day marked wealth with
keeping the entire old parent. Old cost basis is not an input because its
existing unrealized loss is already in current marked wealth. The execution
adapter must separately show the full signed realized disposal loss and actual
fees, prices, delay and replacement result. This estimator establishes neither
obtainable profit nor a future fill.

## Required adapter constraints

The helper does not enforce fifty occupied slots, candidate ranking or normalized
asset identity. It does not alter native priority or an armed profit floor,
authorize money, release owner receipts, dispose lots, retire ladders or open a
replacement. The v3 integration owns those constraints and records their denials.
It preserves the frozen immediate-next-actual-open expiry and conservative 1%
FOK capacity. Only the exact tagged research whole-parent exit may bypass the
174bps execution profit floor. Confirmation and exact owner settlement precede
receipt-scoped release, slot availability and a separately authorized replacement
whose fill can occur only at a subsequent modeled open. A rejection leaves the
economic parent and owner principal intact. Rotation-disabled behavior must match
the reviewed v2 financial body and receive independent matched-control admission.

The synthetic tests cover prefix causality, UTC boundaries, losing and flat days,
274 versus 273 row coverage, timestamped episodes, gaps and reset boundaries,
strict age and interval gates, exact friction oracles, zero/tiny dollar signs,
finite serialization and immutable protocol/selector receipts. They do not read
strategy outcomes or holdout data.
