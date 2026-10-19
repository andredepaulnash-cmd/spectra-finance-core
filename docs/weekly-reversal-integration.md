# Default-off weekly reversal integration

PR42 is stacked on PR40's accounting baseline at
`5f276798c4c465deb894daf1d5d8e11997126418`. Existing application, live executor,
scheduler and native accounting source remain unchanged. This is an additive
paper-research integration. Source publication does not authorize deployment,
merging, external accounts, borrowing, leverage or exchange orders.

## Implemented source

The original component commit supplies causal complete-hour BBWP20/252 and soft
RSI observations, the five-state adaptive-reversal reducer, equal-share realized
profit entitlements, and the default-off settled-profit grant bridge. The recovered
integration adds:

- Actual cash-backed next eligible reentry and repair, with executable permits
  derived from native ownership, health, liquidity and capital evidence.
- Exact PSM snapshot and original-funding-owner partitions, including retained
  principal and profit across archived root generations.
- Durable prepare/reserve/fund/activate/commit/return/release records and recovery.
  A prepared funding intent is not an executable order.
- Native-backed owned-spot sell/rebuild accounting, causal tape/warmup, a bounded
  weekly caller, explicit launch/admission validation, and completed-boundary
  checkpoints of all three SQLite stores and their journals.
- Genuine successor accounting-health refresh after a new ordinary position is
  authorized or a complete PSM rebuild batch is durably sealed. Existing-position
  behavior and all accounting-health guards remain intact.

The optional repair-evidence accessor now uses a local immutable value type
recovered exactly from its original research source. It no longer depends on an
unpublished `spot_campaign` package. This changes no monetary calculation, field,
reason mapping or execution guard.

`WeeklyCompoundingAdapter` requires explicit `enabled=True`; the grant, funding
and root extensions retain separate default-off boundaries. The lower-level
`OwnedSpotAdapter` is a local paper-account primitive and creates local stores
when explicitly instantiated. No published application path automatically imports
and instantiates the new weekly integration.

See the contracts in `spectra_core/NEXT_ENTRY_*` and
`research/weekly_reversal/*CONTRACT.md` for actual authority and cash proofs.
Historical bridge copies are retained as provenance; `psm_kernel_legacy.py` is
also an active source-identity dependency and must not be removed or reformatted.

## Comparison and strategy limits

The held matched comparison varies only BBWP minimum 50 versus 20. Both arms use
soft RSI ranking, the same owned-spot strategy, costs, capital caps, eligible
input and admitted source. The intended capital envelope is $2,000 cash/equity,
including a $500 protected allocation, $1,500 capital-at-work ceiling and $750
per-asset ceiling. The reserve is an accounting allocation, not a market-loss
insurance promise. Equal profit shares use explicit immutable asset membership
plus VCAP; no 75/25 split or account-high-water gate is substituted.

The adaptive policy has the five states SHORT_RIDE, REVERSAL_WATCH,
REVERSAL_LIKELY, COVER and COOLDOWN. State names do not prove derivative execution.
The weekly caller sells owned spot and later rebuilds owned inventory. It creates
no derivative short, borrow, margin, liquidation model, funding payment or
leveraged short profit. Derivative venue eligibility, dated contract liquidity,
funding, margin and actual execution remain unverified by this integration.

The June 3–10, 2026 episode has been examined before and is development evidence,
not untouched out-of-sample data. The roster is BTC, ETH, SOL, BCH, LTC, DOGE,
AVAX, LINK, DOT, SHIB, XLM, HBAR, ADA, AAVE and XTZ. Complete-bar warmup initially
admits BTC/ETH/SOL; XLM becomes ready later. Other missing-history exclusions must
remain explicit, and missing candles are not fabricated. ATR14, EMA8 and the
confirmation details are documented research implementation choices.

No incomplete checkpoint, signal count, synthetic pass, or five-state reducer
establishes profitability, capital preservation or an operational derivative
system. Final metrics must include marked inventory, open losses, fees, ownership,
pending actions, incomplete marks and drawdown.

## Numeric limitation retained

A valid partial-sale reproducer exposes an existing native Decimal(28) versus
strict bridge precision mismatch: persisted realized P&L can differ from exact
subtraction of its persisted components by 1.3E-28. The strict bridge blocks it.
The successor-health repair does not change this arithmetic, relax its guards or
establish general partial-fill support. An alternate dust fixture does not resolve
that failure. The saved weekly full-fill assurance is conditional on its exact
input, fixed bounds, full fills, ownership, grids and caps; it is not universal
Decimal safety, including for extreme prices outside those bounds.

## Saved campaign boundary

At recovery on 19 October 2026, both current arms were saved at 1,003 of 2,016
five-minute bars, June 6 at 11:30 UTC. Continuations 241/242 were complete; 243/244
had not started. Fresh authority version 126 supersedes version 125 embedded in
older evidence. This is a historical status record, not launch authority.

The scoped campaign had consumed 4 of 10 starts and 4 of 60 overall starts.
Continuations of the same frozen strategy run do not create a new strategy start.
The separate older 27-of-28 campaign is not combined with these counts. Original
failed runs remain consumed diagnostics and are not matched comparators for a
changed-source arm. Publishing a portable research overlay must not silently
replace the held campaign's 95 pinned source files.

## Retention and validation

The existing full-copy retention design does not fit the measured available disk
at the observed continuation cadence. The new bounded archival component is
separate from the runner. It retains exact checkpoint bytes and immutable
per-generation manifests using content-addressed chunks, rejects unsafe paths and
corruption, and stops at a configured capacity boundary. It does not prune source
evidence, authorize execution or automatically retire operational directories.
Exact round-trip verification and external source/authority/exclusivity checks
remain required before any saved-run continuation.

See `docs/weekly-reversal-verification.md` for the exact current checks and limits.
