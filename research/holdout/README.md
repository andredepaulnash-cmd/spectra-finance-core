# Prospective public-data holdout

This folder captures public Kraken data for a sealed paper comparison. It has no production autoimports, credentials, account endpoints, order endpoints, live trading, strategy P&L reporting or scheduled process. The workflow activates only when a dated request is pushed to the named research branch; its deployment is a separate action.

`protocol.json` is the unstarted collection template. `frozen-protocol.json` is generated once after final source pins and defines the real UTC start. Earlier October 9 candles are excluded from holdout evaluation. The window closes at **2026-11-09T00:00:00Z**, when comparison may begin. No outcome inspection or parameter tuning using these captures is permitted before that time. Collection health, hashes, request failures and timestamps may be checked throughout.

The prospective five-arm comparison starts each arm with fresh **$2,000 cash**, no parked cash or inventory. It includes PARK/OFF and all four predeclared raw/quality and shared/split variants. This initial condition differs from the prior July continuation. `financial-spec.json` freezes these economics; `frozen-universe.json` preserves the original **619** canonical identities. Daily snapshots capture extra markets only as provenance; extras cannot enter this comparison. Existing fixed identities can start at the first full 5m bar at or after freeze. Unavailable or gapped identities stay in the audit rather than being removed after outcomes.

Only collection is implemented here. The fresh-cash holdout replay adapter has not been built or executed. The pinned July runner restores a June checkpoint; it cannot be reused unchanged for this fresh $2,000 initial state. Before any November score, an independent audit must verify the new adapter against these already fixed rules and the exact external native archive/member pins in `native-source-manifest.json`. Mutable native imports are forbidden.

## Source freeze

From the repository root, after the financial files are final:

```bash
python3 research/holdout/collect_public_ohlc.py freeze \
  --protocol research/holdout/protocol.json \
  --output /path/to/holdout-freeze \
  --source-root . \
  --candidate financial-spec=research/holdout/financial-spec.json \
  --candidate fixed-universe=research/holdout/frozen-universe.json \
  --candidate native-manifest=research/holdout/native-source-manifest.json \
  --candidate quality-protocol=research/capital_quality/protocol.json \
  --candidate quality-clarifications=research/capital_quality/protocol-clarifications.json \
  --candidate quality-gate=research/capital_quality/quality_gate.py \
  --candidate quality-adapter=research/capital_quality/release_adapter.py \
  --candidate quality-runner=research/capital_quality/run_comparison.py \
  --candidate quality-matrix=research/capital_quality/run_matrix.py \
  --candidate base-adapter=research/parked_cash/release_adapter.py \
  --protocolout research/holdout/frozen-protocol.json
```

The collector hashes itself and every supplied financial source. It refuses modified sources or an overwritten freeze. The financial protocol also pins the external native strategy archive; that archive and historic tapes are required for later simulation, and are not included here.

## Bounded capture

```bash
python3 research/holdout/collect_public_ohlc.py poll \
  --protocol research/holdout/frozen-protocol.json \
  --output /path/to/daily-capture --source-root . \
  --max-requests 800 --max-seconds 1200
python3 research/holdout/collect_public_ohlc.py verify \
  --output /path/to/daily-capture
```

Every daily capture makes one public Time request and a fresh AssetPairs currency snapshot, then requests online USD currency OHLC pages sequentially, at least 1.1 seconds apart. It requires no exchange account or API key. Capture is bounded by 800 total attempts and 1,200 seconds. A network timeout is a failed request, not a strategy loss. Throttle, maintenance and invalid-market errors remain saved failures; the collector does not run a retry storm. A same-directory retry resumes cursors and deduplicates committed rows. Independent daily outputs overlap by up to 48 hours and require no previous artifact to initialize.

Kraken's OHLC endpoint returns at most 720 recent entries and always includes the uncommitted current candle. Raw original responses retain that candle for provenance; it is excluded from stored evaluation rows. Raw 48h pages may include pre-freeze prices: only the predeclared last 24h prefix may initialize causal warmup, separately labelled and excluded from holdout returns. No pre-freeze row enters the evaluation database. New data values are never printed.

The output contains `frozen-protocol.json`, `freeze-receipt.json`, `capture.sqlite3`, hashed `raw/*.json.gz` objects and operational `status.json`. Each HTTP intent is recorded before dispatch; interrupted processes preserve pending requests as interrupted on resume. Raw bodies survive HTTP or schema failure. Candles with the same identity/time cannot silently change. Universe observation times and gaps are saved. Missing bars are **unobserved**: sparse/no-trade bars and unavailable data cannot be distinguished from OHLC alone. No synthetic candles or fills are invented.

At close, assembly must verify every daily artifact, deduplicate overlaps without overwriting conflicts, apply the fixed universe and timestamp cutoff, preserve gaps, and separate warmup. It must establish actual coverage before any financial replay. The current collector does not implement that replay or certify coverage from a successful process alone.

## Durable request workflow

The additive `.github/workflows/research-holdout-capture.yml` listens only to pushes of `research/holdout/requests/*.json` on `research/parked-cash-workflow-20261009`. It checks out the exact push SHA, validates source pins and a dated request, collects public data with a read-only GitHub token, and uploads raw evidence even on partial failure. Artifact retention requests 90 days, subject to repository policy. The workflow does not merge production or install a default-branch cron.

Each request has exactly these fields; insert the actual frozen protocol's SHA256 and request timestamp:

```json
{
  "schema": 1,
  "action": "public_ohlc_capture",
  "capture_date_utc": "2026-10-09",
  "requested_utc": "2026-10-09T16:00:00Z",
  "protocol_sha256": "ACTUAL_FROZEN_PROTOCOL_SHA256",
  "no_strategy_evaluation": true
}
```

Name it `research/holdout/requests/2026-10-09.json`. Request validation rejects unknown authority, naive timestamps, a different protocol, and requests more than 48h old. The date above is an example, not a request already executed. Daily triggers and artifact retention need monitoring; outages longer than Kraken's recent buffer can cause irreversible gaps. See [deployment options](../../docs/research-holdout/deployment.md).

## Verification and current access evidence

```bash
python3 -m unittest discover -s research/holdout/tests -p 'test_*.py' -v
```

The mocked checks cover source freeze, no private API authority, UTC request validation, pre-freeze/current/end exclusions, clock skew, raw and orphan-object integrity, pair identity, sparse gaps, interrupted requests, timeout handling, and idempotent resume. They do not make financial claims.

`access-probe.json` records one actual harmless public Time request returning HTTP 200 at 2026-10-09T15:19:06Z. That proves public network access for that request; it does not prove a market capture or an active schedule.

Official references checked October 9, 2026:

- [Kraken OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data): last current row, 720-entry recent limit and incremental `since`.
- [Kraken AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs): currency versus tokenized asset classes and symbol identities.
- [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows): push branch/path filters and default-branch cron limitations.
- [GitHub artifact retention](https://docs.github.com/en/actions/tutorials/store-and-share-data#configuring-a-custom-artifact-retention-period): requested retention cannot exceed configured policy.
