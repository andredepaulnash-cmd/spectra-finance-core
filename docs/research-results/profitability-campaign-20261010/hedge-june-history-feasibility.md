# June US hedge history: public path feasibility

**There is no callable Kraken or Bitnomial market-data connector in this thread. Public endpoints are documented, but sufficient US intraday history remains unverified.** Exact Kraken plugin search returned no result, and `kraken` is absent from the local command path. This does not establish whether the user has an app installed elsewhere. No account or permission probe was made.

This documentation-only review covers **June 15–June 30,2026**, represented as `2026-06-15T00:00:00Z` through the exclusive end `2026-07-01T00:00:00Z`. No new market payload was downloaded. The sealed holdout stays unread.

| Data path | Bounded history documented | Remaining limit |
|---|---|---|
| Bitnomial product funding | `begin_time`, `end_time`, product filter and cursor | Actual June records/completeness not checked; funding snapshots are not an intraday price path |
| Bitnomial product price chart | Exact begin/end | Daily granularity; unstable draft API |
| Bitnomial product specs/data | `day` filter | Dated version retention not checked |
| Kraken global derivatives trade/mark candles | `from`, `to`, resolutions including 5m | No verified mapping to US PBTCUC/BTNL contracts |
| Kraken global funding analytics | `since`, `to`, `interval` | Bucket semantics/cashflow and US instrument identity unverified |
| Kraken global public execution/mark events | `since`, `before`, cursor | Same global-versus-US instrument boundary |
| Kraken historical-funding-rates | Only `symbol` documented | Avoid unbounded fetch while holdout is sealed |
| Standard Kraken Trades/OHLC | Trades since/count; OHLC720 recent rows | BTNL venue support unverified; no documented server end |

## Concrete US Bitnomial requests for a later dated data check

The owned current product metadata identifies BTCUC product 5614, `PBTCUCZ50`,0.01 BTC per contract. It does not certify June rules. The [product API](https://bitnomial.com/exchange/docs/api/rest/products/) documents a date filter:

```text
https://bitnomial.com/exchange/api/v1/prod/product/spec/5614?day=2026-06-15
https://bitnomial.com/exchange/api/v1/prod/product/spec/5614?day=2026-06-30
```

The [public funding API](https://bitnomial.com/exchange/docs/api/rest/funding-rates/) supports product-specific historical intervals and cursor pagination. Its overview gives a production base while funding examples omit `/prod`; both are documented spellings, and endpoint resolution must be recorded when checked:

```text
https://bitnomial.com/exchange/api/v1/prod/funding-rates/?product_id=5614&begin_time=2026-06-15T00:00:00Z&end_time=2026-07-01T00:00:00Z&order=asc&limit=100
```

The [chart API](https://bitnomial.com/exchange/docs/api/rest/charts/) documents daily product OHLC/settlement observations:

```text
https://bitnomial.com/exchange/api/web/charts/price/5614?begin_time=2026-06-15T00:00:00Z&end_time=2026-07-01T00:00:00Z
```

These are possible acquisition paths, not receipts proving records were returned. Daily OHLC cannot resolve5m fill timing, bid/ask spread, market depth or intraday maintenance-margin/liquidation order. Funding marks must not be expanded into fabricated candles. Contract rules, June fees and cash settlement timing need independent dated evidence.

## Why global Kraken endpoints do not clear the US gate

[Kraken Market Candles](https://docs.kraken.com/api-reference/candles/market-candles) documents trade/mark types and explicit epoch-second `from`/`to`. [Market Analytics](https://docs.kraken.com/api-reference/analytics/market-analytics) provides bounded `funding` buckets; [public executions](https://docs.kraken.com/api-reference/market-history/get-public-execution-events) and [mark events](https://docs.kraken.com/api-reference/market-history/get-public-mark-price-events) accept epoch-millisecond `since`/`before`.

Those global-series routes do not establish that `PF_BTCUSD` or `PF_XBTUSD` is the US Bitnomial `PBTCUC` instrument. [US perpetual documentation](https://support.kraken.com/articles/us-perpetual-futures) identifies Bitnomial-listed contracts and broker daily cash funding settlement. Global contract prices/funding cannot be substituted without verified venue/instrument equivalence.

The separate [historical-funding-rates endpoint](https://docs.kraken.com/api-reference/historical-funding-rates/historical-funding-rates) documents only a symbol query. Fetching all history and filtering locally could already ingest sealed-period records; leave it uncalled until server bounds are verified. The official [Kraken CLI/MCP documentation](https://docs.kraken.com/llms.txt) describes a built-in MCP server, but no server was installed, connected or started here.

## US spot-margin and the six-month boundary

US margin executes on a [separate BTNL order book](https://support.kraken.com/articles/margin-order-book). The standard [Trades](https://docs.kraken.com/api-reference/market-data/get-recent-trades) and [OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) documentation does not explicitly establish BTNL history support. OHLC is limited to720 recent rows, roughly 60 hours at5m. A venue-metadata filter or order suffix alone does not prove historical market-data coverage. This route needs historical borrow costs, separately from perpetual funding.

Kraken [launched US perpetual access June 15,2026](https://blog.kraken.com/product/kraken-derivatives/announcing-cftc-regulated-us-perps). A full January–June Kraken US perpetual study is therefore unavailable on that route. A verified postlaunch study could be useful; it must retain its shorter date range. CME and US spot margin remain distinct instruments with separate historical gates.

Recommendation: perform a small, bounded June product/funding/daily-data readiness check, then obtain authentic USvenue intraday trade/mark and executable-liquidity records. Keep actual hedge profitability arms gated until those requirements are met. The existing authorized spot study can continue.
