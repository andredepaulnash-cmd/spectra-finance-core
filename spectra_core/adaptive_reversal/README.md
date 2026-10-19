# Adaptive reversal mechanics

This paper-only reducer implements staged trailing protection and emits intent
records. It does not place orders, model exchange margin/funding, or establish
historical profitability. The application must explicitly select a configuration
and supply causal, reconciled evidence.

The recovered design is ride → watch → likely reversal → cover → cooldown.
Trailing distances of 3, 2 and 1 ATR were suggested in that design. ATR14 Wilder,
hourly observations, two evidence families and two consecutive confirmations are
explicit new implementation choices. They are not an exact recovered trading
specification. The generic default research configuration has no arming threshold;
any application needing a fee-net arming target must configure and verify it.

The weekly signal adapter defines two correlated OHLC feature families:

- Momentum: a positive completed-hour return
- Structure: a higher completed-hour low AND a close above EMA8

These are not statistically independent data sources. Multiple flags within a
family count once. Two consecutive qualifying observations are required by the
provisional reducer configuration.

Stops never loosen. A completed bar's high is tested only against a stop known
at the bar's open; a newly computed stop cannot retroactively fill inside that
same bar. All actual fills, partial fills, canceled attempts and late-fill races
must be reconciled. Unknown execution, stale evidence and gaps fail closed.

Owned-spot sell/rebuild is economically distinct from a borrowed derivative
short. Reusing this market-policy component cannot create short proceeds or
funding P&L. The separate reconstruction predicate requires actual accounting,
not a price-derived assumption. It adds no fixed 1% extra-unit rule.

Snapshots include exact reducer and ATR state. Component replay tests do not
establish cross-database transactional integration or live readiness.
