# Recorded three-day rotating-fleet result

This is the first corrected shared-account fleet run.  The earlier isolated
DOGE/PEPE/WIF trials are superseded as portfolio evidence because they assigned a
separate 2,000-unit account to each coin.

## Dataset

- UTC period: 2026-09-24 through 2026-09-26
- Source: official Binance Public Data daily spot one-minute kline archives
- Universe: 83 distinct USDT-quoted altcoins; BTC, ETH, and stablecoins excluded
- Coverage: 4,320 synchronized minutes per asset; 358,560 rows total
- Raw archives: 249 ZIP files verified against publisher SHA-256 sidecars
- Forward fill: none
- Dataset source fingerprint:
  `ea82256ffcf319310e5db42bb14e7cf6de1354a73b012c2f6642364b4855d270`

## Results

All runs started with one shared 2,000-unit account and targeted 50 unique active
assets.  Slippage was 5 bps per side; the fee column is per side.

| Fee | Ending equity | Total P&L | Realized | Open P&L | Fees | Max drawdown | Rotations | Recovery fills |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 bps | 2,095.40 | +95.40 | +110.20 | -14.80 | 25.82 | 2.13% | 374 | 441 |
| 40 bps | 2,093.01 | +93.01 | +110.95 | -17.95 | 49.87 | 2.25% | 331 | 477 |
| 80 bps | 2,065.07 | +65.07 | +88.71 | -23.64 | 87.34 | 2.76% | 245 | 500 |

At 40 bps the fleet first reached 50 positions five minutes into the data, used
77 different assets over the run, finished with 50 positions, and had no duplicate
asset identities or sub-10 position identities.  After initialization it was
below 50 at only three one-minute observations; simultaneous execution failures
briefly produced a minimum of 46.  Fleet position-minute occupancy over the whole
window was 99.9694%.

The 40-bps audit fingerprint is
`6dd40788e510d6f836c88890a29ae8337b67dac7d32b9bed739894bed289fe48`.
The engine source hash is
`6a49c674e3b353e389b61818090a6851db2a81cd3f70dbeca307417ad84fadb6`;
the importer source hash is
`e97d371eb424a2fed47d95af238f8ef50784fb9b44f0dcdf884b28bf25ed4813`.

## Interpretation

This path is encouraging and shows that the corrected portfolio mechanics can
rotate and recover capital while keeping the requested diversified fleet nearly
full.  It does **not** establish a durable edge.  Execution is estimated from
minute VWAP and recorded volume because the archives contain no bid/ask or order
book, the venue differs from Kraken, the period is only three days, symbol rules
are simulated, and no untouched multi-regime campaign or paper-fill reconciliation
has yet been completed.
