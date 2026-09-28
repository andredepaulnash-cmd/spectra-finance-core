# Offline strategy replay

Run from the repository root:

```bash
uv run python -m spectra_core.strategy_replay quotes.csv --fee-bps 40 --output replay.json
uv run pytest -q tests/test_strategy_laboratory.py tests/test_strategy_replay.py
```

The fee above is an example research input, not a claim about a current Kraken
fee tier. Supply fees appropriate to the dataset/account. Cash defaults to 200
quote units and slippage to 5 bps per side. All fees are paid in quote currency.
The JSON report contains configuration, a dataset fingerprint, fills, exact lot
ownership, remaining inventory, rejection reasons, expired orders and metrics.

## Data contract

CSV columns (all required):

```csv
timestamp,pair,bid,ask,depth_base,min_base,min_quote,step_base
0,BTC-USD,100,101,10,0.001,1,0.001
3600,BTC-USD,101,102,10,0.001,1,0.001
7200,BTC-USD,102,103,10,0.001,1,0.001
```

This tiny example is synthetic and only demonstrates the format. Timestamps are
integer Unix seconds. Supply at least two strictly increasing, synchronized
frames, with exactly one row for each pair at every timestamp. One quote currency
and one pair per normalized base asset are supported. BTC/XBT aliases cannot
create duplicate economic exposure. Missing quotes fail validation rather than
silently carrying forward stale prices. Venue minima and size increments belong
to each historical frame and are rechecked at execution time.

`depth_base` is the available base quantity on the relevant execution side in
that snapshot. This initial symmetric-depth format conservatively uses one
provided quantity for either side; it is not an order-book reconstruction. Use
actual quote/depth records or explicitly label estimated inputs. OHLC prices
alone are insufficient evidence of bid/ask prices or executable depth.

## Event order and accounting

Each frame executes previously queued orders, records bid-marked equity and
remaining fee-inclusive basis, then supplies an immutable history prefix to the
policy. Orders execute no earlier than the next frame (configurable positive
`latency_steps`). There is at most one outstanding mutation per pair. Orders are
IOC: unfilled quantities expire and are never automatically refilled. Orders
still pending at the end are counted as expired. No end-of-test sale is invented.

Buys use the later ask plus slippage; sells use the later bid minus slippage.
Fills are capped by displayed depth and `fill_fraction`, then rounded down to the
base step. Zero liquidity never fills. Exchange minima apply to the submitted
order; a valid partial fill can be smaller. Sell orders select exact lot IDs;
unknown lots and oversells fail without changing inventory. Entry orders require
no active lots, while recovery buys require an existing position. Partial sales
release proportional basis including buy fees. Remaining dust stays visible.

Cash is shared across assets and cannot go negative. Every buy is capped by
spendable cash after the configured equity-based reserve and the pair's remaining
fee-inclusive cost-basis budget. Defaults are 25% reserve and 25% per-pair basis
cap; these are simulation parameters, not a change to production policy.
Sells can release cash for later orders in their deterministic submission order.
No mark-to-market gain is converted into spendable cash.

Every frame checks:

`cash + open fee-inclusive basis = starting cash + realized net P&L`

Reports distinguish realized P&L, unrealized bid-marked P&L, and total equity
change. `net_profit_quote` in the inherited lab metrics means **realized** P&L;
use `total_pnl` for combined profit/loss. Capital-hours integrate the previous
frame's committed cost basis. Open inventory is not reduced by hypothetical exit
fees; its bid value is not a guaranteed liquidation value. Sparse snapshots can
miss intra-frame drawdowns and price paths.

## Comparable research baselines

All five runs start with identical cash, data and execution assumptions:

| Candidate | Rules |
| --- | --- |
| cash | Hold cash, no orders |
| dca | Fixed tickets at a fixed frame interval; net-target full exits |
| recovery | Bounded drawdown-scaled purchases after an observed uptick; net-target full exits |
| recycling | Recovery rules plus profitable recovery-lot sales |
| capital_time | Recycling rules plus age exits and observed-momentum priority when cash is scarce |

These are deliberately simple VCAP-inspired research proxies. They do **not**
invoke the production VCAP planners, CapitalGovernor, fingerprint model, auction,
MIR, or live execution adapters. They do not inherit proof of those contracts.
No module added here can send an order to an exchange. Policy functions receive
only immutable data up to the current frame; callers remain responsible for not
embedding future information in custom policy closures.

## What this PR establishes

Deterministic replay, accounting tests, a CSV entry point and comparable baseline
runs. The tests include hand-calculated round trips and partial sales, reserve
and shared-cash limits, exchange constraints, latency, lookahead checks, and a
seeded synthetic stress path. Synthetic tests validate mechanics, not returns.

Before using results to choose a production strategy, supply versioned historical
quotes and point-in-time venue rules, implement a separately tested adapter to
production planning logic, and run the preregistered train/holdout campaign with
its full trial ledger and cost sensitivity. The runner itself does not perform
parameter selection, walk-forward training, historical-data downloading, maker
queue simulation, delisting valuation, or live shadow/canary promotion.
