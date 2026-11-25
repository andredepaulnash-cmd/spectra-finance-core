# Frozen BTC/ETH paper short beta

## What this adds

A **default-off, explicit local PAPER price-path entrypoint**, a separate SQLite
book, original causal daily signals, delayed observed-open matching, and durable
account/decision evidence. It is suitable for the one-person synthetic beta and
for engineering reproduction of the already-frozen January–June research.

This is a **hypothetical fractional short on spot prices**. It is not executable
BTC/ETH perpetual or futures performance, a venue-margin simulation, or a live
trading recommendation. There is no exchange client, account credential, live
mode, network data feed, order dispatch, scheduler registration, or MCP tool.
The existing spot/VCAP balances, owners, adapters and scheduler are unchanged.

Base: `spectra-core-phase2` at
`6e894fa9c2b55384d0f6120f41ca91892e1fe207`. This change has **no dependency on draft
PR40**, its decision-trace helper, or any other unmerged PR. Its single existing
file change appends tests to the focused CI workflow. Merge, installation and
activation require separate decisions. Actual Bazzite installation/startup is
not verified by this source-level deliverable.

## Fixed policy and boundaries

- Frozen candidate protocol SHA256:
  `bef20f04281c40fe8a23de7955080505db6c358f28ba5d8d6082745e127e426b`
- Initial modeled equity **$2,000**, target reference entry notional **$750 BTC +
  $750 ETH**, with **$500 undeployed by policy**. Fixed quantity `750 / entry_open`
  until exit; at most one position per asset per original CRASH episode.
- This is a standalone modeled book, not a second claim on real spot funds. Do
  not add its $2,000 to a real-account balance. Reference notional is exposure,
  not collateral, money spent or a leveraged margin deposit.
- Original BTC daily midnight-UTC label: completed valid five-minute candles
  only; seven-day close drawdown <= −10% OR trailing 24-hour return <= −5% gives
  CRASH. Preserve seven-day history/anchor requirements and ten-minute freshness.
  The exact frozen binary64 threshold calculations are retained for signals;
  Decimal accounting uses an explicit deterministic 34-digit context.
- An entry also requires the same scheduled daily trailing-24-hour return <= −5%.
  No intraday refresh or additional indicator. No compounding, repairs, reserve
  deployment, weighting changes, additional assets or added exposure.
- Earliest entry is the original episode start **plus five minutes**. Require a
  fully valid immediately preceding candle and a finite positive observed current
  open. The current candle's future high/close/volume never decides that fill.
  Unavailable legs stay unallocated; no redistribution to the other leg.
- Original gross known-at-open equity must be > $500 to admit a new entry. Costs
  and funding remain parallel attribution views and do not change frozen timing.
  This gate is not a stop, collateral test, reserve guarantee or loss limit.
- The first daily label away from CRASH, including UNKNOWN, irreversibly queues
  an exit. Match at the first eligible open at least five minutes later, even if
  CRASH returns before that fill. No same-bar reentry. A false fresh24h entry gate
  alone does not exit. Terminal open positions stay open, with explicit mark age.
- The extra five-minute delay used in a separate sensitivity is **not** the
  default. That sensitivity and whole-contract research do not alter this module.
- Unknown actual contract metadata returns `BLOCKED_UNKNOWN_METADATA`; even a
  populated schema returns `UNSUPPORTED_MODEL`. No contract-engine or live route
  exists. Published contract sizes cannot turn fractional quantities into orders.

All January–June episodes preceded the June 15 U.S. perpetual launch noted in the
frozen protocol. This postselected development sample is not independent evidence
of performance and does not establish historical contract availability. The
separate whole-contract sizing research is not an implemented executable mapping.

## Accounting and missing observations

`gross_cash = 2000 + realized_short_pnl` and
`gross_equity = gross_cash + unrealized_short_pnl`. Short PnL is quantity times
(entry price minus current/exit price). **Short-sale proceeds never become cash.**

All three views are reported together:

1. Gross: zero costs/funding, for attribution only.
2. Cost: hypothetical **45 bps per side** on transaction notional (40 bps fee plus
   5 bps adverse slippage). An observed-open transaction and separate cost debit,
   not a fabricated bid/ask or historical fee schedule.
3. Funding: the same cost view plus hypothetical **10 bps/day adverse debit** per
   held five-minute interval, on prior completed valid close or the new entry
   open. Funding source price and age are recorded. This is not actual funding.

Cash and marked equity remain signed; losses can exceed initial equity. Each
view exposes the modeled equity shortfall as `*_unfunded_liability`. Margin and
liquidation are `NOT_MODELED`; no margin amount is inferred as zero.

Carried valid closes are allowed only for transparent valuation (and the frozen
known-at-open fallback/funding convention), never to manufacture fills. Account
receipts include exact mark ages, stale-position counts, marked notional and
simultaneous observed-high stress equity. The latter is a price-path sensitivity,
not evidence that those highs were contemporaneously executable.

Every interval must be submitted contiguously. Represent a missing observation
using six null OHLCVT fields. Do not interpolate or skip a time interval. The
first interval must open at midnight UTC. Signals, opportunities (including
skips), fills, delays, input digests and accounts persist atomically. Receipt-bound
state and retained BTC observation hashes fail closed on incomplete/corrupt
restart evidence. Exact duplicate cycles are idempotent; changed observations or
controls at an already-committed timestamp require reconciliation.

## Run the synthetic beta

From the repository root, in the existing Python environment:

```sh
python -m spectra_core.crash_short_runtime
# {"status":"DISABLED", ...}; no file or database is opened/created

python scripts/make_crash_short_synthetic_fixture.py /tmp/short-synthetic.jsonl
python -m spectra_core.crash_short_runtime --enable-paper \
  --source synthetic --input /tmp/short-synthetic.jsonl \
  --state /tmp/short-demo/crash_short_paper.sqlite3 --run-id synthetic-demo

python -m spectra_core.crash_short_runtime --status \
  --state /tmp/short-demo/crash_short_paper.sqlite3
```

The command prints a standalone JSON status/account receipt. Detailed receipts
and events are accessible through `PaperShortStore.read_receipts()` and
`read_events()` in the same independent book. Paper status is one of DISABLED,
WARMING_UP, PAPER_READY, STALE_DATA, EXIT_PENDING or RECONCILIATION_REQUIRED.
`control` and `signal.unknown_reason` add the exact reason. A disabled call with
an existing store reports its liabilities without changing them.

The callable entrypoint is `run_crash_short_paper_cycle(enabled=True, store=...,
timestamp=..., bars={"BTC": Bar(...), "ETH": Bar(...)})`. It accepts no client.
It processes a completed historical/synthetic interval: the observed opening
price determines any modeled fill; the completed close values the resulting book.
It is not a wall-clock realtime execution feed.

### Stop, resume, pause and close-only

Use an immutable complete fixture. `--until` bounds interval openings (exclusive)
so a run can be stopped and reopened before the end. For example:

```sh
python -m spectra_core.crash_short_runtime --enable-paper \
  --source synthetic --input /tmp/short-synthetic.jsonl \
  --state /tmp/short-controls/crash_short_paper.sqlite3 --run-id controls \
  --until 1768003800
python -m spectra_core.crash_short_runtime --enable-paper \
  --source synthetic --input /tmp/short-synthetic.jsonl \
  --state /tmp/short-controls/crash_short_paper.sqlite3 --run-id controls \
  --resume --control close-only
```

`--resume` verifies the committed prefix with its original controls and applies
the new control only to new intervals. It cannot rewrite history. A PAUSE prevents
entries while existing positions still mark and obey daily exits. CLOSE_ONLY also
irreversibly queues a paper close for an eligible open at least five minutes later.
These explicit operational overrides are recorded and cannot inherit the frozen
unmodified performance claim. Removing `--enable-paper` stops processing; it does
not close, transfer, erase or release a liability. Re-enabling can resume it.

Appending to or replacing the input file changes its immutable source identity
and is rejected for the existing book. Use a separate book for another synthetic
fixture. This release deliberately has no current-market/forward-shadow ingestion.
The October 9–November 9 holdout remains untouched. Do not label market observations
as synthetic to bypass the source boundary.

## Reproduce the frozen acceptance

Large inputs and database files are intentionally not committed. The approved
`ohlcvt_15.npy` must hash to
`5f4b40da6cd468c0d66bce148e1648c71d29f41942d16b137ca55aa32c12eefd`.
The runtime hashes and parses the **same bytes**, accepts exactly the original
52,128 × 15 × 6 array, and uses only BTC/ETH in Jan 1–Jul 1 (exclusive), 2026.

With the approved original input and BTC/ETH reference directories available:

```sh
python -m spectra_core.crash_short_runtime --enable-paper \
  --source frozen-jan-june --input "$INPUT/ohlcvt_15.npy" \
  --state /tmp/short-frozen/crash_short_paper.sqlite3 --run-id frozen-acceptance
python scripts/verify_crash_short_paper.py \
  --state /tmp/short-frozen/crash_short_paper.sqlite3 \
  --reference "$REFERENCE" --receipt /tmp/short-equivalence.json
SPECTRA_FROZEN_SHORT_FIXTURE_DIR="$INPUT" python -m pytest -q \
  tests/test_crash_short_signal.py tests/test_crash_short_paper.py \
  tests/test_crash_short_runtime.py tests/test_crash_short_review.py
```

The verifier pins the reference files using the committed hash manifest before
comparing all 52,128 account rows, all 181 daily signals, 12 modeled fills and
1,154 entry opportunities. These are implementation-equivalence checks against
an existing experiment, not a new strategy candidate or validation trial. The
external historical signal test skips when its approved fixture is absent;
synthetic safety tests always run in CI. See the committed acceptance receipt
and review notes for exactly what passed and what remains unverified.
