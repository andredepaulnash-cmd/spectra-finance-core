# Alpha 2.0 Market Discovery and Condor Selection

## Data source

The scanner calls only the local Hummingbot API's public-data endpoints, using the fixed `kraken_paper_trade` connector name:
- `GET /market-data/tickers?connectors=kraken_paper_trade&refresh=true`
- `GET /connectors/kraken_paper_trade/trading-rules`
- `POST /market-data/order-book`

The Hummingbot API's public adapter obtains public Kraken market data. The Core does not call private exchange/account endpoints and does not accept exchange credentials.

The Core-to-API HTTP Basic Auth values are locally generated service credentials for the internal API, not exchange credentials.

## Scanner filters

The initial scanner requires:
- USDT quote asset;
- non-stablecoin base asset;
- fresh ticker (no more than 60 seconds old);
- positive price and 24-hour base volume;
- at least $100,000 USDT 24-hour quote volume;
- Hummingbot limit-order support;
- exchange minimum notional no greater than the $20 initial order;
- a valid top-of-book snapshot;
- top-of-book spread no greater than 0.30%.

The scanner checks up to 120 highest-volume preliminary candidates and inspects order books with bounded concurrency. An individual order-book failure removes that candidate; it does not crash the whole scan.

## Condor

Condor is a deterministic recommendation layer, not an execution agent. It ranks screened candidates using 65% normalized log quote-volume score and 35% tight-spread score, and returns a human-readable rationale for each candidate. The portfolio target is at most 74 recommendations/admissions; eligibility and capital governance can result in fewer active positions.

The initial score is deliberately simple and explainable. Additional indicators are deferred until the end-to-end lifecycle is proven.
