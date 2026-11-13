# Spectra Market Intelligence Provider

The Condor tick pipeline now has a read-only `market_intelligence` core
provider. It composes the existing ticker-pool and candle fetchers with the
Hummingbot API order-book snapshot. Its structured result and compact summary
are injected into every configured agent tick through the existing core-data
provider pipeline.

## Per-strategy configuration

Set these values in the agent/strategy configuration:

| Key | Meaning | Default |
|---|---|---:|
| `market_connector_name` | Hummingbot connector name, e.g. `kraken_paper_trade` | empty / disabled |
| `market_trading_pair` | Exact connector pair, e.g. `BTC-USD` | empty / disabled |
| `market_candle_interval` | Candle interval | `1m` |
| `market_candle_records` | Requested candle count (20–500) | `120` |
| `market_data_max_age_seconds` | Maximum acceptable age for source timestamps | `180` |
| `market_order_book_depth` | Order-book levels per side (1–100) | `20` |
| `market_data_cache_ttl_seconds` | Shared snapshot cache lifetime (1–30 seconds, bounded by source freshness) | `10` |

No market is inferred when the connector or pair is missing. The provider then
reports `NOT_CONFIGURED` without making API calls.

## Output and safety semantics

The provider reports price, 24-hour USD volume, candle history, SMA20, RSI14,
last-candle return, best bid/ask, spread in basis points, and quote depth on
both sides. It includes timestamps, age calculations, timestamp provenance,
missing timestamps, and source errors.

- `FRESH`: all required sources returned usable data and have acceptable
  timestamps.
- `STALE`: at least one known source timestamp exceeds the configured age.
- `DEGRADED`: required data is missing, malformed, or lacks a source timestamp.
- `NOT_CONFIGURED`: the strategy has not selected a connector and pair.

Only `FRESH` is marked `decision_grade=true`. Every other state includes an
explicit no-trade data guard in the agent prompt. This is advisory context,
not capital authorization: CapitalGovernor remains the only authority for
capital reservations and executor dispatch.

Identical agent ticks share one process-local snapshot for the same API server, connector, pair, interval, and depth. A per-key async lock prevents simultaneous agents from stampeding the ticker, candle, and order-book endpoints. Cache lifetime is bounded by the oldest source timestamp, and cached ages/status are recalculated before the snapshot is reused.

The order-book response may not expose a source timestamp on every API version.
When it does not, the provider records `retrieved_at` as its timestamp basis;
it does not mislabel that value as an exchange timestamp.

## Operational limits

This provider is read-only. It does not create orders, executors, reservations,
or exchange credentials. Configure the connector and pair deliberately for
each strategy. Do not enable live execution to test market data; use the
existing paper-only connector and paper-only flags.
