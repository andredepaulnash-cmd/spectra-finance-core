# Spectra Finance — Canonical Project Ledger

**Status:** living continuity record  
**Updated:** 2026-11-15 (America/New_York)  
**Repository:** `andredepaulnash-cmd/spectra-finance-core`  
**Companion API repository:** `andredepaulnash-cmd/spectra-hummingbot-api`

This ledger is the short operational source of truth for reconstructing the project after a conversation, model, or workstation session changes. It points to deeper historical artifacts rather than replacing them. Update it when a decision, verified implementation state, or operational fact changes.

## 1. Mission and authority

Spectra Finance is an adaptive capital allocator and trading system, not merely a signal bot. Its objective is repeatable, fee-aware, capital-efficient compounding through opportunity discovery, recovery, rotation, profit harvesting, and redeployment.

**Authority boundary**
- Architect owns the mission, capital authority, hard bounds, and reserved decisions.
- Spectra Core is the deterministic policy, capital-governance, accounting, and authoritative position-state layer.
- Condor is the intelligence, research, scanning, ranking, orchestration, and bounded-adaptation layer.
- Hummingbot is the exchange connectivity, market-data, executor, and order-mechanics layer.
- Canonical flow: **Condor proposes → Core validates and authorizes → Hummingbot executes → Core reconciles actual exchange evidence → Condor receives authoritative state.**
- Autonomy means initiating investigation and pursuing approved objectives, not exceeding authorization.
- Paper and live modes remain selectable capabilities. Live execution requires separate explicit Architect authorization and existing governance controls. No live authorization is implied by this ledger.
- Do not wipe/reset paper or live state, alter credentials, enable live execution, merge PRs, or change strategy policy without the appropriate explicit authorization.

## 2. Current approved paper portfolio specification (November 2026)

These are the latest explicit project decisions and supersede older September bootstrap assumptions:
- Total simulated portfolio: **25,000 USDT**.
- Initial-entry allocation: **25% = 6,250 USDT**, distributed evenly across eligible initial entries.
- VCAP/recovery allocation: **75% = 18,750 USDT**.
- Concurrent asset-position target: **50 or 83**; not yet selected as one default.
- Maximum **two active VCAP shards per asset position**.
- No arbitrary global count cap on assets undergoing VCAP, provided the shared recovery pool, per-position ownership, and Fat–Skinny–Fat ladder rules are obeyed. Portfolio-wide risk aggregation remains an identified gap.
- Minimum VCAP shard: **20 USDT**.
- Net take-profit target: **+1.74% after Kraken buy and sell fees**.
- Fees and slippage are execution expenses. Buy fees affect current P&L immediately; estimated sell fees belong in projected net P&L. Keep realized and projected P&L distinct.
- Use only actual Kraken-supported USDT pairs; verify current market metadata. Never assume appending `-USDT` creates a valid pair.
- Reinvest realized profits promptly, subject to the current allocation policy and governance.
- Historical Fat–Skinny–Fat bands remembered from the latest discussion: FAT from 0% to -11.74%, SKINNY from -11.74% to -17.77%, FAT from -17.77% to -24%. The exact shard-sizing formula must be recovered from the Governor/implementation before use; do not infer it.
- A full enforcement model for the 6,250 initial-entry envelope must be verified in code. PR #126 adds 25,000/18,750 policy defaults; that alone does not prove the 6,250 initial-entry allocation is enforced end-to-end.

Older September handoff values ($200 bootstrap, $50 VCAP reserve, 75% productive / 25% reserve routing, 15 × $10 initial positions) are historical scenarios, not current production defaults.

## 3. Historical architecture and strategy recovery

### Canonical design sources located
1. **Spectra Finance Blueprint: Hummingbot V2 + Condor + Spectra Core** — 22 pages.
2. **Spectra Finance Production Alignment Handoff** — 14 pages, dated 2026-09-28.
3. **Spectra Core and Finance Layout** — 8 pages.
4. **Spectra_Canonical_Continuity_v13_pending.md** — 701 lines, detailed historical continuity and design doctrine.
5. **Pasted text(4).txt** — earlier Spectra Intelligence operating instructions and tool/readiness transcript; not a verified Governor configuration.
6. **Pasted text(5).txt** — 823-line local doctor/market-data log capture from 2026-11-15.

The PDFs and continuity files are in the user's project Library. They are not yet all mirrored into this repository.

### Historical strategy lineage
- MoneyPenny / early Polygon work → Freqtrade strategy experiments → Condor + Hummingbot + Spectra Core.
- Original strategy artifacts located include `SpectraVCAPFillUSDVeryLoose.py` and duplicate copy `SpectraVCAPFillUSDVeryLoose(1).py`. The handoff also names `SpectraVCAPFillUSD.py`, `dcahvy.py`, and very-tight DCA variants; those additional original files still need to be located and reviewed.
- **Python duplicate-method rule:** when a class defines the same method multiple times, the last definition is effective. Historical strategy files contain layered experiments; do not treat every code block as simultaneously active runtime behavior.
- Historical VeryLoose variants include immediate VCAP, deep ladders, variable patch multipliers, broad entry/hotlist behavior, stoploss-off experiments, and velocity exits. One variant documents a 9-level ladder at -1.24%, -2.24%, -4.44%, -7.77%, -12.74%, -17.77%, -24.44%, -33.33%, -44.44% with multipliers 0.25, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50, 0.75, 1.00. This is historical experimental code, **not** the current Fat–Skinny–Fat production formula.
- Canonical historical record reports actual VCAP activity in Feb 2026 logs: 863 patch signals in one segment and 3,042 in another, with roughly 347 successful dry-run patch fills in the latter and progression to at least level 21. The old runtime used 50 max trades, 1-minute candles, a broad dynamic universe, and at one point a 200-adjustment override. This proves the old VCAP machinery executed in dry-run; it does not prove profitability or validate the current Hummingbot implementation.
- Historical Freqtrade failure: many positions independently requested patches until shared free cash was exhausted. Production VCAP must be globally arbitrated.
- Historical January 23 incident: a web/API process could respond while the trading process was not healthy; port 8081 was already occupied and meaningful endpoints returned 502. **Process/HTTP health is not semantic trading health.**
- SQLite `.sqlite-wal` and `.sqlite-shm` are sidecars, not databases. Preserve main DB + sidecars as a consistency set; never point a runtime at a sidecar as the database.

### Deeper inspection of the recovered VeryLoose strategy
The 1,500-line `SpectraVCAPFillUSDVeryLoose.py` has **seven** definitions of `adjust_trade_position` inside one class. The final definition wins under Python class semantics. The last active method is the later `SPECTRA_MEGA_VCAP_LADDER_V2` block, not the earlier immediate/19-level variants:
- Final method uses a 47-threshold `VCAP_LEVELS` list from -1.24% through -47.77% and a 47-element descending `VCAP_WEIGHTS` array.
- The class-level `max_entry_position_adjustment = 19` is set in the preceding block and is not overwritten later in the file. Historical runtime configuration/logs must be checked because the handoff separately reports an effective override of 200 at one point and runtime patch logs reached at least level 21.
- Final method's comments say a single position may use up to 6× initial stake, but the actual `target_budget` expression clamps it to the 1× `VCAP_BASE_MULT` baseline (subject to `max_stake`); the 6× `VCAP_MAX_MULT` is therefore not, by itself, evidence that the code deployed a 6× budget.
- The file sets `PATCH_BANK_RATIO = 0.75`, so its intended entry bank is 25% and patch bank 75%, matching the broad split concept later carried forward.
- Its only `custom_stake_amount` implementation computes entry-bank / max-open-trades, but then replaces any calculated stake below 99 with the entire remaining entry-bank amount whenever at least 99 remains. For a $1,000 wallet and 50 slots, that path can turn the nominal $5 target into a $250 first stake rather than evenly spreading the bank. This is a source-level inconsistency that must be compared with historical runtime overrides and logs; do not transplant it into the current engine.
These findings reinforce the handoff's instruction: source text, effective Python method, runtime configuration, and actual logs are four separate evidence layers.

### Additional historical handoff and local update logs
- **Spectra Core / Spectra OS / Spectra Finance — Master Handoff Plan** — 9 pages, dated 2026-01-30. It documents the earlier Bazzite/Kinoite immutable-host model, Spectra Core control plane, old Freqtrade finance chassis, tiered permissions, backup/no-wipe doctrine, and a proposed local-first agent runtime. It is earlier historical context; the later Condor + Hummingbot + Spectra Core blueprint is the current finance architecture.
- **Pasted text(6).txt** and **Pasted text(7).txt** contain the 2026-11-15 local API-update transcript. It records a successful rebuild of the local Hummingbot API image, preservation of existing PostgreSQL/EMQX volumes and credentials, and a running `spectra-alpha-hummingbot-api` container. The same transcript records a failed connection to the dashboard on port 8088 at that moment. This is historical runtime evidence, not a fresh current health check.

### Universal asset state and ranking doctrine
One shared observation/ranking engine feeds specialized behavior profiles. Lists are views into common state, not disconnected scanners:
- Top Opportunities; Hot / Repeat Winners; 24h Gainers / Continuation; 24h Losers / Reversion; Hyper-Volatility Watchlist; VCAP Recovery; PSSM/MIR; Cooling; Weak/Demotion; Dead-Watch; Reactivating; Capital Queue.
- Global lifecycle concept: UNRANKED → SCANNED → WATCH → PRIORITY → PROBE → ACTIVE → HOT, with deterioration through COOLING / WEAK / PSSM-MIR / DEAD-WATCH and possible reactivation.
- Top gainers earn priority inspection, not automatic buys. Top losers earn reversion inspection, not automatic bargain buys.
- Strength Hunting and Dislocation Hunting feed the same capital queue.
- Persist component scores and reasons: Opportunity Score, Execution Score, Capital Efficiency Score, Behavior/Risk Class, and Overall Priority.
- Relevant shared inputs include RSI, MFI, BBWP, EMA/trend, momentum/ROC, VWAP/deviation/reclaim, normalized ATR, volume acceleration, order-flow imbalance, relative strength, spread, depth, price-for-volume, expected slippage, fill probability, capital velocity, reserve pressure, VCAP burden, correlation, and opportunity cost.
- Ranking requires hysteresis/anti-thrashing and auditable promotion/demotion. Every decision should preserve feature snapshot/config version and later compare higher-ranked vs lower-ranked alternatives net of costs.
- Capital goes to the highest expected productive use after costs and hard reserve/ownership/risk rules; empty slots and legacy ownership do not automatically earn priority.

### Recovery and exit concepts
- **VCAP:** Volumetric Cost Averaging and Batching; split authorized recovery into independently tracked shards, re-evaluate after confirmed fills, and never blindly continue a stale ladder.
- **Fat–Skinny–Fat:** current bands are in §2; exact shard-sizing formula still needs authoritative recovery.
- **MIR:** rotate capital only after an authorized source exit is reconciled; if destination opportunity disappears, keep cash liquid rather than force a replacement.
- **PSSM:** planned pseudo-short/exit-weakness corridor before MIR; it must own the position exclusively while ordinary VCAP is suspended. Preserve shadow-position and recovery-equity lineage. A below-basis sale remains a real realized loss; never rewrite it as profit.
- **DTTP/DTSL:** fee-aware profit floor, high-water/giveback, stagnation and opportunity-cost exits. 1.74% net DTTP was a September controlled-replay champion, not proof of production edge; current November specification keeps +1.74% net target.
- **Hyper-Volatility Watchlist:** separate lifecycle and thresholds; breakout/volume/flow/depth confirmation, probe→press, fast profit protection. Do not mix its risk profile with normal mean-reversion inventory.
- Adaptive intelligence may propose bounded parameter changes; hard capital, reserve, permissions, and kill-switch controls remain deterministic.

### Current implemented scanner: exact scope and ranking (PR #118 branch inspection)
The current `spectra_core/market_discovery.py` implementation is a **research-only Kraken USD scanner**, not yet the complete canonical universal ranking engine:
- Default scan universe is `quote_assets=("USD",)`; it ranks the top 100 by 24h quote volume, analyzes up to 25 candidates, requires at least 90 completed daily candles, and does not place orders or authorize capital.
- Default guardrails: 24h quote volume at least $10m; 30-day median daily quote turnover at least $1m; active-range day ratio at least 0.65; median daily range at least 0.8%; current move from last completed daily close no more than 15%.
- Rejections include persistent 30-day decline of at least 25% while below the 20-day average, within 5% of the 90-day low while the 30-day trend is negative, weak turnover, sporadic volatility, low daily range, excessive current-session move, and pump-dominated history.
- Mean-reversion score (75% of final score): 90-day VWAP discount up to 35 points; 90-day high drawdown 20 points for 15–65% or 10 points for 8–80%; RSI 15 points for 30–55 or 7 for 25–65; positive 7-day recovery 10; price above 20-day SMA 10; 30-day stability 10 or 5.
- Volatility-consistency score (25% of final score): active-day ratio up to 60 points, median daily range up to 25, median daily quote turnover up to 15. Final selection score is 75% mean-reversion score + 25% volatility-consistency score.
- This is a transparent initial shortlist, not a complete implementation of the universal Opportunity / Execution / Capital Efficiency score stack. It does not yet prove order-flow/depth/correlation/capital-queue ranking or strategy edge.

**Critical quote-asset mismatch:** the current PR #118 path is USD-only in more than one layer:
- `scan_kraken_universe()` defaults to USD.
- `asset_authority.py` validates only `BASE-USD`, uses policy `kraken-usd-mean-reversion-v1`, and writes/loads USD-only approved/discovered lists.
- `kraken_authorization.py` explicitly rejects non-USD quote pairs.
- The current CI paper acceptance fixture and local doctor capture use BTC-USD.
- Although the standalone history analyzer defaults its `quote_asset` argument to USDT and has a unit test with a synthetic TEST-USDT symbol, that does **not** establish end-to-end USDT support.
This conflicts with the latest Architect requirement to use actual Kraken-supported **USDT** pairs. Before paper portfolio deployment, the scanner, asset-authority schema, authorization path, pair metadata, paper connector, and tests must be aligned to verified Kraken USDT pairs. Do not simply change the regex or quote default without end-to-end pair/rule verification.

**Asset-list discovery contract:** `asset_authority.py` checks an operator-provided `approved_assets.json` first; absent that, it can load a fresh `discovered_assets.json` (maximum age 300 seconds) when automatic asset selection is enabled. The scanner returns candidates but the exact runtime call that persists the discovered list still needs verification. The local doctor log's missing-list warning means the live local state did not have a usable list at that time.

## 4. Historical GitHub archaeology — PRs #1–#25

The first 25 Core PRs form a deliberate progression, not 25 unrelated features. All were closed at the last repository review; the September handoff treats #1–#24 as aligned infrastructure/research and #25 as a provisional research prototype. Do not discard this work or rebuild it from scratch.

| PR | Recovered capability | Principal source paths |
|---:|---|---|
| #1 | Phase-1 CI for Governor, Kraken authorization, executor accounting, position-hold and recovery exposure | `.github/workflows/spectra-phase1.yml` |
| #2 | Exact account/connector/controller-scoped PositionHold provenance; fail-closed recovery exposure | `mcp_servers/hummingbot_api/tools/executor_create.py`, `spectra_core/capital_governor.py` |
| #3 | CI efficiency: avoid duplicate feature-branch Actions runs | workflow |
| #4 | Kraken market fabric: normalize tickers/rules, intersect live tickers with rules, opt-in order books, spread/depth/imbalance/microprice, per-pair rejection reasons; partial stack lock | `spectra_core/kraken_market_fabric.py`, `spectra-stack.lock.json` |
| #5 | Advance locked Hummingbot API revision and exact PositionHold contract | `spectra-stack.lock.json` |
| #6 | Per-asset behavioral fingerprints across FAST/SHORT/MEDIUM/LONG horizons, time decay, freshness, cold-start confidence | `spectra_core/asset_fingerprint.py` |
| #7 | Fingerprint-shaped VCAP recovery planning; exact Decimal position geometry, recovery-to-break-even math, stale-data/depth throttles | `spectra_core/recovery_fingerprint.py` |
| #8 | Dynamic VCAP tranche planner; up to five normal equal parcels, fewer/heavier reversal-supported parcels, venue-rule quantization and independent sellability | `spectra_core/vcap_tranches.py` |
| #9 | One-tranche-at-a-time execution/replan loop; deterministic child IDs; atomic parent-envelope carve; EXECUTE/HOLD/STOP | `spectra_core/vcap_execution.py`, `mcp_servers/hummingbot_api/tools/vcap_execution.py` |
| #10 | Profit-oriented VCAP exits: 174 bps net target, fee-aware gross exit, profit floor/high-water, giveback and stagnation controls, confirmed flat before recycle | `spectra_core/vcap_profit_exit.py` |
| #11 | Durable VCAP restart continuity; exact proposal/executor ownership; RESUME/RETIRE/HOLD fail-closed | `spectra_core/vcap_state.py` |
| #12 | Unified VCAP lifecycle; profit exit outranks new recovery; durable checkpoint before external mutation; receipt-required idempotent progression | `spectra_core/vcap_lifecycle.py` |
| #13 | Economic position ownership ledger with ACTIVE_TRADE / STRATEGIC_RETAINED / UNATTRIBUTED lots and exact basis/fee geometry | `spectra_core/economic_position.py` |
| #14 | Confirmed Hummingbot trade fills mutate economic ledger; exact order ownership, idempotent trade IDs, no fuzzy matching or wallet-balance inference | `spectra_core/fill_reconciliation.py` |
| #15 | Automated executor order/fill provenance bridge; exact executor-owned order and trade evidence; pinned API contract | `spectra_core/executor_provenance_bridge.py`, API provenance tools |
| #16 | Accounting Health supervisor HEALTHY/STALE/HOLD/CONFLICT; preflight all orders/trades before mutation; freshness watermark | `spectra_core/accounting_health.py` |
| #17 | Restart-safe closed-loop VCAP runtime supervisor; one capital mutation per position cycle; reconcile before and after dispatch | `spectra_core/vcap_runtime.py`, `spectra_core/runtime.py` |
| #18 | Unified economic-position exit manifest covering exact ENTRY + RECOVERY lots; retained/unattributed inventory excluded; dust and minimum sellability explicit | `spectra_core/economic_exit.py`, `spectra_core/economic_position.py` |
| #19 | Multi-position VCAP scheduler/watchdog; per-position cycles, sequential bounded fleet ticks, per-position fault isolation | `spectra_core/runtime_scheduler.py` |
| #20 | Partial recovery-lot recycling; exact recovery ownership, proportional/idempotent reserve release, one recycle mutation per position cycle | `spectra_core/partial_recycle.py` |
| #21 | Adversarial strategy laboratory and capital auction; transparent opportunity scoring, portfolio/correlation stress, research integrity statistics | `spectra_core/strategy_lab.py`, `strategy_lab_scenarios.py`, `strategy_lab_validation.py` |
| #22 | Causal offline multi-asset replay with shared cash, later-frame execution, exact lot ownership, fees/slippage/depth/partial fills, comparable baselines | `spectra_core/strategy_replay.py` |
| #23 | Offline adapter to production fingerprint/recovery/tranche/profit planners plus dataset provenance and auditable train/evaluation campaigns | `spectra_core/strategy_dataset.py`, `strategy_replay_vcap.py` |
| #24 | Recorded Kraken Tardis ticker/instrument importer; capture-time causal rules, exact Decimal values, no forward-fill | `spectra_core/kraken_tardis_import.py` |
| #25 | One shared rotating 50-altcoin fleet replay with 83-asset universe and linked replacement rotations; valuable but provisional | `spectra_core/rotating_fleet.py`, `binance_kline_import.py`, protocol/results docs |

### PR #21 research auction defaults — explicitly not production policy
The lab's default `CapitalAuctionPolicy` uses: reserve fraction 50%; minimum recovery reserve 25 quote units; target fleet min/normal/max 10/20/40; minimum viable position 10 quote units. Its score is:
- capital-time edge (expected net edge bps / expected holding hours) × 2;
- reversal score × 40;
- liquidity score × 25;
- marginal recovery efficiency × 35;
- urgency × 10;
- correlation penalty × -30;
- dead-money penalty × -45;
- expected execution cost bps × -1.
The auction is research-only: it ranks and allocates a simulated proposal but does not reserve or dispatch live capital. These weights and fleet targets are old research defaults, not the November 2026 $25,000 policy or a verified current production ranking formula.

### Strategy-lab stress and promotion gates
The research lab also defines five explicit stress hypotheses:
- Fee shock: fees ×1.75.
- Liquidity crunch: spread ×3, depth ×0.20, volatility ×2.
- Correlated crash: spread ×2.5, depth ×0.30, volatility ×3, return shock -20%.
- Token collapse: spread ×5, depth ×0.05, volatility ×5, return shock -65%.
- Pump/dump: spread ×4, depth ×0.15, volatility ×4, return shock -35%.
These are deterministic adversarial test settings, not calibrated forecasts.

Its research promotion gate requires adversarial, out-of-sample, and shadow checks to pass, Deflated Sharpe probability ≥0.95, PBO ≤0.20, parameter-neighborhood stability ≥0.70, and max drawdown ≤30%. Even a pass returns only `CANARY_ELIGIBLE`; the laboratory cannot authorize live capital or bypass CapitalGovernor. This is a useful validation framework to preserve, but its thresholds remain research policy until explicitly adopted.

### PR #22–#25 evidence and limitations
- **PR #22 replay:** five research baselines (cash, fixed DCA, recovery, recovery recycling, capital-time variant). Uses later executable quotes, shared cash, exact Decimal accounting, fee-inclusive basis, spread/slippage, depth and partial-fill caps. It is a research proxy, not a production-controller replica.
- **PR #23 planner adapter:** invokes production AssetFingerprint, recovery sizing, tranche and profit-exit planners offline. It does not mirror production Governor reservations, full lifecycle/restart behavior, or partial-recycle manifests. Synthetic/estimated data is explicitly adversarial; provenance is caller-declared.
- **PR #24 Kraken importer:** a one-day BTC/USD pipeline check converted 989,539 messages to 1,434 observations, max gap 68 seconds, excluding 230 tickers without contemporaneous rules. Its 36 fixed-policy trials produced no sells and zero realized P&L. It proves ingestion/causality mechanics, not strategy profitability.
- **PR #25 rotating fleet:** one shared $2,000 account, 50 target positions, $10.25 entries, 83 non-BTC/non-ETH USDT altcoins, 2026-09-24–26, Binance one-minute data. At the 40 bps fee scenario it reported ending equity $2,093.01 (+$93.01 total), +$110.95 realized, -$17.95 open P&L, 331 rotations, 477 recovery fills, and 2.25% max drawdown. Execution was estimated from next-minute VWAP plus slippage and volume caps; the dataset had no bid/ask, order-book depth, queue position, Kraken rules, or the user's fee tier. It is a three-day Binance proxy experiment, **not proof of a durable edge or Kraken fill behavior**.
- The exact rotating-fleet result and protocol are preserved in `docs/research-results/rotating-fleet-2026-09-24_26.md` and `docs/research-protocols/rotating-fleet-v1.json`.

### Current portfolio-supervisor defaults versus the November allocation
The PR #126 code currently defaults to `target_active_positions=83`, `max_new_entries_per_tick=3`, scan interval 60 seconds, supervisor tick 10 seconds, discovery top-N 150/show-N 100, and VCAP seed setting 20 quote units. The 83 target is a **code default**, not a settled Architect decision; 50 vs 83 remains open.
- More importantly, `spectra_core/vcap_entry.py` defines `PAPER_VCAP_SEED_QUOTE = Decimal("20")` and `reserve_vcap_candidate_entry()` always reserves exactly $20 for each new ENTRY. The supervisor logs this as its initial-entry amount. The configurable `SPECTRA_VCAP_SEED_QUOTE` is passed into VCAP servicing, but it does not replace the fixed $20 initial-entry reservation.
- At 83 positions, that entry path would nominally reserve $1,660 total; at 50 positions, $1,000. It does not deploy the approved 6,250 initial-entry pool, nor distribute that pool evenly ($75.30 at 83 or $125 at 50).
- The 20-unit constant is close to the current **minimum VCAP shard**, but that does not make it the approved initial-entry size. Initial entries and recovery shards need separate budgets and sizing logic.
- Current paper policy initializer has `max_total_committed_quote=25000`, `max_pair_quote=25000`, `max_position_quote=25000`, `max_recovery_quote=18750`, and `reserve_floor_quote=100`. It has no explicit 6,250 initial-entry envelope. The max-pair/max-position defaults also do not themselves enforce even initial-entry sizing.
This is a material integration gap. Do not reset or start the $25,000 paper portfolio on the assumption that the current supervisor already implements the November 25%/75% allocation.

### External verification of Kraken USDT market availability
Checked Kraken's current public documentation on 2026-11-15:
- Kraken Pro's official markets list, updated 2026-10-30, explicitly lists USDT as a crypto-to-crypto quote market and marks multiple assets as tradable against USDT. Not every asset has a USDT market.
- Kraken's API symbols article, updated 2026-10-29, states REST and WebSocket symbols can differ and directs clients to the public `AssetPairs` endpoint to retrieve available markets and symbol variants.
- Kraken's US jurisdiction page, updated 2026-11-04, lists restricted assets for US clients; USDT is not in that US restricted-asset list. Account eligibility and market-specific restrictions still need to be checked at runtime.
Sources:
- https://support.kraken.com/articles/kraken-markets
- https://support.kraken.com/in/articles/360000920306-api-symbols-and-tickers
- https://support.kraken.com/articles/where-is-kraken-licensed-or-regulated
- Counting the official crypto-to-crypto table's USDT column on that page yields **45 listed USDT-quoted markets** (including stablecoins/tokenized assets and major assets). This is fewer than either proposed 50 or 83 distinct active positions, before account eligibility, volume/depth filters, or duplicate-base prevention. Treat 45 as a documentation-table count, not a substitute for a live AssetPairs query; new listings and pair status can change.
- Therefore, a strict USDT-only Kraken universe may not support 50/83 simultaneous distinct asset positions. Before selecting the target, compare the live AssetPairs set against the approved universe. If it remains below 50, Architect must choose between a lower feasible target or a broader quote-currency universe with explicit quote conversion/accounting. Do not silently mix USD and USDT or invent extra markets.
Implication: the requested USDT quote universe is a real Kraken spot-market option, but the scanner must enumerate actual supported pairs and account/connector rules. Never synthesize `BASE-USDT` by suffix concatenation or assume every USD market has a USDT counterpart.

### Architect decision — quote-currency universe (2026-11-16)
Architect selected **USD as the preferred Kraken quote currency if USD offers the broader eligible market universe**. The design should not enforce USDT-only trading. Discover and validate actual Kraken-supported USD and USDT pairs from live market metadata; prefer USD when it expands eligible asset coverage, and retain USDT pairs where useful. Keep portfolio valuation/accounting in the approved common USDT-denominated unit using explicit, fresh conversion rates and conversion-cost accounting. Never synthesize pair symbols, assume every asset has both markets, or mix quote-currency amounts as if they were equivalent. Final eligible universe and pair preference should be determined from live Kraken AssetPairs/connector metadata and account eligibility, not the stale documentation table count.

### Condor source discovery status
The authenticated GitHub repository listing for the Architect's account currently exposes only `spectra-finance-core` and `spectra-hummingbot-api`. No separate Condor repository was discoverable by repository search. Core references a `condor-portfolio` controller and the architecture documents describe Condor as the intelligence layer, but this does not recover the complete Condor application source or its full feature/ranking inventory. Treat Condor-source recovery as incomplete until the local Condor project path, archive, or repository is identified.

## 5. Hummingbot / Core contracts to preserve

- Hummingbot Strategy V2 uses controllers and self-managing executors; do not rebuild order mechanics that already exist without a measured reason.
- The Capital Governor must make atomic reservations so simultaneous controllers cannot spend the same free cash.
- Every order/fill transition needs durable IDs, timestamps, ownership, state, and receipts.
- Exact order lifecycle: proposal → authorization/reservation → dispatch → exchange acknowledgement → zero or more partial fills → terminal order state → fee-aware fill ledger → authoritative position update → Condor state update.
- Fill IDs must be idempotent. Unknown/ambiguous submissions must be reconciled before retry; no blind retry after timeout.
- Actual exchange evidence and the reconciled economic ledger are authoritative; Core must not invent positions from proposals or wallet balance.
- Partial fills, fee currency, order amount/precision, minimum notional, order/trade journal lag, duplicate/out-of-order events, cancellation races, restart recovery, and stale data must be explicitly tested.
- Semantic health includes authenticated API, broker, active bot/controller, fresh market data, reconciled portfolio, persistence, and no unresolved order/ownership anomaly.
- Use WebSocket market-data fan-out and shared cached calculations; avoid independent polling storms.
- Local workstation services should remain loopback/private. Never put exchange secrets, tokens, or config passwords in this ledger, memory, receipts, or ordinary logs.

## 6. PR and repository state (refreshed 2026-11-15 local / 2026-11-16 UTC)

Repositories:
- Core: https://github.com/andredepaulnash-cmd/spectra-finance-core
- API: https://github.com/andredepaulnash-cmd/spectra-hummingbot-api

### Merged
- API PR #18 — executor order provenance writes in `services/orders_recorder.py`; records executor-owned order rows on create/fill/completion/cancel. Already merged; do not redo.

### Open at last refresh
- Core PR #118 — [Governed market-entry path and Alpha validation fixes](https://github.com/andredepaulnash-cmd/spectra-finance-core/pull/118). Market discovery, Kraken minimum order quote, supervisor adapter, governed MARKET BUY entry, position identity and VCAP lifecycle bootstrap. Earlier clean Linux paper acceptance failed; not install-ready.
- Core PR #126 — [Fix governed paper MARKET BUY reconciliation](https://github.com/andredepaulnash-cmd/spectra-finance-core/pull/126). Adds policy defaults for 25,000 total / 18,750 recovery and protected MARKET BUY reconciliation. Latest known head: `0ee633eb03ce61c105e1802a79f91791ec9dfa4d`. Workflow `43798156044` failed clean Linux paper acceptance.
- Core PR #127 — [Restore vertical scrolling on the Overview page](https://github.com/andredepaulnash-cmd/spectra-finance-core/pull/127). Frontend TypeScript and production build passed; its workflow was blocked by the inherited paper-acceptance failure. Not merged.

### In-place updater failure: exact root cause and correction
The first passing workflow (`43805192285`) exposed a separate updater defect:
- `scripts/update-alpha-api.sh` defines `API_SOURCES` in this order: unified connector, market-data service, orders recorder, then `deps.py`.
- Its final paper-only bot-status guard check incorrectly searched `${API_SOURCES[2]}`, which is `services/orders_recorder.py`, even though the guard text lives in `deps.py`. The updater therefore stopped with “paper-only bot-status guard was not applied locally” even though the fetched `deps.py` already matched reviewed upstream.
- Corrected on PR #126 in [`98a9e37`](https://github.com/andredepaulnash-cmd/spectra-finance-core/commit/98a9e37d11a5ec764581dd51f6569761cd570fe3) to check `$API_DIR/deps.py` explicitly. - The correction triggered workflow [`43805709781`](https://github.com/andredepaulnash-cmd/spectra-finance-core/actions/runs/43805709781). Static/unit checks have passed; the clean-install job was still running its public installer at the last check. Final updater and paper-acceptance outcome remains pending.
A new CI run is required to verify the in-place updater now completes.
- This was a false-negative verification bug, not evidence that the API guard itself was absent. Keep the guard and fail-closed behavior; do not remove it.

### Follow-up CI result — exact protected BUY mismatch recovered
Workflow `43805709781` completed with failure in the end-to-end paper execution test. The updater correction itself worked: it applied the reviewed API updates, created a consistent isolated paper-state backup, rebuilt the trade screen, and the Hummingbot API passed authenticated readiness after restart. The remaining test failure now reports the exact conflict:
`protected_buy_order_amount_changed: ... authorized=0.00038359:observed=0.000383500000000000`.
There was one confirmed fill and one ledgered fill, with no unresolved orders or trades; the Governor nevertheless correctly held the position because the protected BUY amount changed. This is a venue-precision/amount-normalization discrepancy of 0.00000009 BTC in the Kraken paper execution path. Investigate the precise amount semantics across authorization, connector quantization, order-created provenance, fill event, and ledger. Do not loosen authorization or treat the order as fully reconciled until the permitted venue rounding rule is explicit and tested. The updater false-negative is fixed; the protected BUY reconciliation is still not fixed.

### Exact current PR #126 blocker
- Clean Linux acceptance proved the API order was created, the paper exchange confirmed a fill, the ledger recorded it, and order/trade provenance was recognized.
- Position remained HOLD because protected BUY reconciliation recorded `protected_buy_order_amount_changed`.
- Current bridge compares the exact authorized amount from the durable protected submission (`config.amount`) against the amount recorded for the Hummingbot executor-owned order. Exact equality currently fails.
- Do **not** weaken this invariant or assume the cause is rounding/precision. First expose and compare exact authorized amount, submitted executor config amount, Hummingbot order-created event amount, persisted `ExecutorOrder.amount`, and cumulative fill amount. Then reproduce with a regression test and fix the true unit/precision/serialization mismatch.
- The current fill audit separately ensures cumulative confirmed base fills do not exceed the authorized amount and fees stay within the recorded fee allowance.
- A diagnostic-only commit was added to the PR #126 branch: [`be3f098`](https://github.com/andredepaulnash-cmd/spectra-finance-core/commit/be3f0982eb714623f5f5dcc602134487f757c049). It leaves the exact equality check intact and adds the authorized and observed decimal amounts to the conflict reason.
- Workflow run [`43805192285`](https://github.com/andredepaulnash-cmd/spectra-finance-core/actions/runs/43805192285) completed with overall success. Static/unit checks, clean Linux install, dashboard and paper connector readiness, API readiness after the updater attempt, and the focused governed paper order/fill/accounting/restart test passed. The acceptance command reported 1 passed, 5 deselected.
- Important qualification: the in-place updater step itself failed at the bot-status guard check; the workflow intentionally reported this as a warning and continued using the clean installation for paper acceptance. The precise false-negative path and correction are recorded in §6.
- The amount mismatch was not reproduced in this successful run, so the diagnostic values remain unknown. The exact equality check is unchanged.
- The CI log also echoed an ephemeral generated test API password in a step environment listing. No user credential was involved in the reviewed output, but CI should mask generated credentials before exposing step environments.
- No merge or install-ready claim until the complete clean Linux acceptance, restart/idempotency checks, and all required workflows pass.

## 7. Local runtime evidence supplied 2026-11-15

The Library's `Pasted text(5).txt` contains an 823-line capture of the local doctor and market-data checks:
- Paper-only / live-disabled configuration was confirmed.
- Authenticated Hummingbot API confirmed Kraken paper-only mode.
- API, PostgreSQL, and broker were reported running/healthy.
- Dashboard responded on loopback in most checks; one transient connection failure to port 8088 was followed by successful checks.
- Kraken paper connector returned valid BTC-USD order-book levels with bids and asks; this was a read-only check and submitted no orders.
- **No Governor-approved asset list was configured; all paper BUY-side intents were blocked.** This is a real runtime blocker until the Architect-approved list is present and validated.
- Doctor repeatedly reported the latest paper-state backup archive as corrupt. The tar error showed a literal escaped newline suffix in the archive path (`...tar.gz\\n`); investigate quoting/path construction before relying on backups.
- These logs do not establish current paper balances, open positions, or that the full paper trading loop is currently operating.

A separate Library text named `Pasted text(4).txt` contains an earlier agent instruction/transcript. It is not the actual current Governor policy file. No standalone authoritative Governor configuration file or current approved-asset list has yet been identified in the reviewed Library material.

## 8. Current gaps and next work

### Immediate integration
1. Diagnose exact protected MARKET BUY quantity mismatch using the evidence fields listed in §6.
2. Run focused tests before consuming another full CI run; then rerun the clean Linux paper acceptance.
3. Verify that the 6,250 initial-entry envelope is enforced in addition to the 18,750 VCAP/recovery envelope.
4. Recover/validate the approved Kraken USDT asset list; do not invent it or use BTC-USD test fixture as production universe.
5. Fix and verify the paper-state backup archive path; verify backup integrity before any reset/update.
6. Keep PR #118/#126/#127 separate; merge only after acceptance and explicit Architect approval.

### Historical recovery still needed
- Locate and review original `SpectraVCAPFillUSD.py`, all VeryLoose variants, `dcahvy.py`, very-tight DCA variants, and the actual Governor file.
- Locate original `freqtrade.log` and rotated hotlist logs; inspect actual runtime overrides and order/patch outcomes.
- Locate SQLite snapshot/analysis and verify main DB/WAL/SHM consistency set.
- Locate `Spectra_50_Altcoin_3Day_Simulation_Report.md` and `Spectra_Exit_Strategy_Comparison.md`; treat three-day results as exploratory, not profitability proof.
- Review PRs #1–#25, especially #7–#20 production VCAP/accounting contracts and #21–#25 research infrastructure. Preserve research provenance and causal replay.
- The September handoff says PR #25 is provisional and that any future strategy change needs an implementation delta and Architect approval.

### VCAP shard-count compatibility check
The recovered PR #8 implementation defaults `VCAPTranchePolicy.max_tranches = 5` (aggressive minimum 2); each accepted tranche is independently sellable and tracked, and PR #9 executes only the next tranche before replanning. PRs #13–#20 preserve multiple recovery lots under one economic position. The November Architect rule is **no more than two active VCAP shards per asset position at a time**. If a tranche is an active shard (the PR #8 sellability/independent-lot model strongly suggests it is), the historical default of five is not compliant. One-at-a-time dispatch does not automatically enforce a two-active-shard cap when earlier fills remain open. Before using the old planner, explicitly define shard vs tranche vs lot, count active recovery owners, and enforce the two-shard cap at the authoritative Core/Governor layer; preserve the ability to service many different positions concurrently. This is a compatibility gap, not permission to silently change the current rule.

### Fat–Skinny–Fat and shard-minimum compatibility check
The current Core VCAP planner is not yet the remembered November Fat–Skinny–Fat ladder:
- `VCAPRecoveryPolicy` defaults to a 500 bps target rebound, maximum seed multiplier 4×, maximum patch-to-position ratio 1×, minimum rebound improvement 25 bps, and an adaptive reversal-signal threshold of 0.25.
- Recovery size is shaped by position break-even geometry and the fingerprint's reversal score/confidence. No code in the inspected recovery/tranche modules encodes the November drawdown bands 0 to -11.74% FAT, -11.74% to -17.77% SKINNY, and -17.77% to -24% FAT.
- `VCAPTranchePolicy` has max_tranches=5, aggressive_min_tranches=2, and a sellability buffer; it has no explicit 20-quote minimum shard field. It enforces venue sellability/minimums, not the Architect's independent minimum-shard rule.
- The two-shard cap and Fat–Skinny–Fat sizing must be specified and enforced at the authoritative Core layer before the current planner is considered compliant. Do not confuse the older 47-level VeryLoose strategy, the PR #8 five-tranche planner, the current fingerprint-based recovery planner, and the November Fat–Skinny–Fat rules; they are distinct generations.

### Operational and economic validation
- Prove order lifecycle, fill idempotency, fee currency, restart reconciliation, partial fills, cancellation races, unknown timeouts, stale data, exchange minima, and failure injection.
- Add portfolio-wide exposure/correlation/drawdown/recovery budget controls only through a reviewed spec; they are identified gaps, not silently adopted changes.
- Demonstrate strategy expectancy out-of-sample after fees, spread, slippage, and capital lock time. Do not infer edge from closed winners while underwater positions remain unresolved.
- Progressive rollout: unit → recorded replay → walk-forward → parity → shadow → Kraken paper → tiny live only after explicit authorization → controlled scale.

## 9. Operating discipline

- One focused change at a time; preserve backups and exact commit hashes.
- Verify effective runtime config after writes; requested config is not proof of applied config.
- No state reset during recovery without explicit authorization.
- No strategy or architecture expansion merely because an idea is interesting. First make the existing system observable and correct.
- Keep this ledger current with: verified commit SHAs, PR/workflow results, test evidence, runtime config hashes, operational blockers, decisions, and next exact action.
