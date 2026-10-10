# Fresh validation historical-input metadata feasibility

Status: **blocked; no genuinely unseen period certified**. This read-only check identifies possible old-history sources. It does not acquire candles or trades, score strategies, inspect financial outcomes, alter policy, or open the Oct. 9–Nov. 9 holdout.

The parent reports that current research has no qualifying positive finalist. The acquisition and fresh-validation gate therefore remains closed. The frozen campaign protocol still calls for two finalists across three nonoverlapping new periods without outcome tuning between periods. Its current SHA-256 is `a10def9591d19bc8c775d136b5c845383d42778f274e824ad1d77af88552e374`.

## Three provisional calendar windows

These consecutive quarters were chosen by dates alone, without viewing market behavior. Each lies before the already inspected Jan.–Sep. 2026 tapes and outside the stated 2026 sealed holdout. Other historical seals and prior inspections remain unverified.

| Candidate | Start inclusive, UTC | End exclusive, UTC | Prior inspection |
|---|---|---|---|
| 2025 Q1 | 2025-01-01 00:00 | 2025-04-01 00:00 | Unknown |
| 2025 Q2 | 2025-04-01 00:00 | 2025-07-01 00:00 | Unknown |
| 2025 Q3 | 2025-07-01 00:00 | 2025-10-01 00:00 | Unknown |

Personal Context was attempted and returned “Personal context is unavailable for this conversation.” Metadata-only title searches and both pages of the recursive `/Spectra Finance` listing (225 items) identified no pre-2026 market archive. That does not establish the periods were unseen: 2025-dated configuration backups and older Freqtrade logs exist. Their contents were not read. An authoritative exposure ledger must resolve each window before it can become fresh validation.

The JSON companion records exact Library/file IDs for the seven identified 2026 archives. Those are already inspected by the parent’s history and are excluded from fresh validation. Filename dates are metadata, not independently verified payload coverage.

## Official downloadable sources

| Source | Published coverage and format | Bounded collection implications |
|---|---|---|
| [Kraken OHLCVT](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) | Each pair’s first trade through June 30, 2026; 5-minute OHLCVT supported; no-trade intervals omitted | Full archive includes candidate dates and already seen 2026. No currently verified 2025-only incremental URL. Later extraction must enforce frozen dates. |
| [Kraken time and sales](https://support.kraken.com/articles/360047543791-downloadable-historical-market-data-time-and-sales-) | Public trades by pair through June 30, 2026; timestamp, price, volume, side, order type, misc, trade ID | Can support 5-minute aggregation if completeness is verified. Trades do not prove book depth, queue priority or strategy fills. |

Exact published multipart URLs, advertised reassembled hashes, fields, and source receipts are in the JSON. No archive part was opened or downloaded; `MANIFEST.json` remains unread. The documented June 30 cutoff avoids the sealed holdout, but actual build bounds still require verification before later use.

There is a build-identity discrepancy in the linked checksum metadata. The [OHLCVT checksums](https://assets.kraken.com/marketing/institutions/OHLCVT_Full_PARTS_SHA256SUMS.txt) use undated filenames, whereas the page advertises dated 2026Q2 filenames. The [Trades checksums](https://assets.kraken.com/marketing/institutions/Trades_Full_PARTS_SHA256SUMS.txt) list 14 undated parts (00–13), whereas the page advertises 13 dated parts (00–12). Neither should be treated as verified hashes of the advertised build until the relationship is resolved.

## A strictly dated API alternative

Current [Post-Trade Data documentation](https://docs.kraken.com/api-reference/transparency/post-trade-data) specifies public `/0/public/PostTrade` with `symbol`, `from_ts`, `to_ts` and `count <= 1000`. Explicit past bounds can prevent present overlap. Its documented request shape is verified; availability and completeness of 2025 responses are not. No endpoint was called. Retention, delisted-symbol access, venue coverage and pagination at identical timestamps remain blockers.

[Recent OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) cannot supply these windows: only 720 recent entries are available and the current unfinished candle is always included, even with an old `since`. Calling it risks crossing the holdout boundary.

## Universe and validation limits

The generic archive claim does not establish the exact historical USD pair universe, point-in-time listing/delisting completeness, historical US eligibility, or date-specific minima and costs. The archive member inventory is unopened. [Current AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs) documents country/venue filters without a historical as-of filter. Back-projecting the inspected 619-pair 2026 cohort would risk future-listing and survivorship bias.

Before opening prices, verify exposure history and seals, freeze the finalist and all validation assumptions, resolve source identity, establish the causal eligible universe, and pin exact window/warmup bounds. Source extraction must be quarantined and fail on timestamps outside those bounds. Pre-2026 spot data cannot establish pre-2026 US hedge execution: the separate route report’s launch and instrument-history restrictions still apply.

No fresh period, positive strategy, complete universe, or historical hedge profitability is claimed by this report.

