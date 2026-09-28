# Rotating 50-asset fleet replay

`spectra_core.rotating_fleet` models the portfolio requested by the strategy:
one shared quote ledger, 50 simultaneous economic positions, and exactly one
position per normalized base asset.  A completed position receives a new,
never-reused position ID and a replacement from an inactive asset.  Replacement
orders are linked to the exact exit that releases their slot; failed exits cancel
their replacements, and fallback candidates cannot create a 51st position.

The default research configuration starts with 2,000 quote units, opens positions
with a 10.25 all-in budget (never less than 10 basis), retains a 25% portfolio
reserve, and caps each asset at 5% of equity basis.  Production fingerprint,
recovery-sizing, tranche, and protected-profit exit planners are reused.  The
simulation owns its separate cash, reserve, identity, and pair-cap controls; it
does not invoke the live CapitalGovernor, lifecycle persistence, or exchange
dispatch.

## Recorded bars and estimated execution

`spectra_core.binance_kline_import` strictly imports official Binance Public Data
daily spot one-minute kline ZIPs.  Every retained asset must contain every minute
of the same three consecutive UTC dates.  Missing rows, duplicate timestamps,
nonconsecutive dates, mixed quote currencies, BTC/ETH, stablecoins, or a universe
too small to supply an inactive replacement fail validation.  Nothing is forward
filled.

Klines record OHLCV and trade counts.  They do **not** record bid, ask, spread,
order-book depth, queue position, historical symbol filters, or an account's fee
tier.  The fleet result is therefore bar-based evidence with estimated execution,
not a top-of-book replay:

- A decision is made only after a minute closes.
- Its order participates in the following complete minute.
- Estimated price is that following minute's recorded VWAP plus the configured
  adverse slippage.
- A fill is all-or-none and cannot exceed the configured fraction of recorded
  quote volume.
- Fees are charged separately in quote currency.
- No end-of-test liquidation is fabricated; open inventory is marked at the last
  recorded close.

These assumptions are intentionally visible in every report.  They test portfolio
and strategy mechanics but cannot prove that the same fills were available on
Kraken or in a live order book.

## Run

After acquiring the fixed archives in the protocol:

```bash
uv run python -m spectra_core.rotating_fleet raw/*.zip \
  --fee-bps 40 --slippage-bps 5 --output fleet-result.json
```

The report records the raw-file source fingerprint, exact configuration, engine
and importer source hashes, every fill, rejection aggregates, ending inventory,
realized and unrealized P&L, drawdown, fleet occupancy, rotations, recovery fills,
and a reproducibility fingerprint.

The checked-in protocol and first result are:

- `docs/research-protocols/rotating-fleet-v1.json`
- `docs/research-results/rotating-fleet-2026-09-24_26.md`

Passing mechanics tests or a profitable three-day path is not strategy
validation.  Promotion still requires many untouched periods and regimes,
recorded quote/depth replay where available, paper execution, and reconciliation
between assumed and observed fills.
