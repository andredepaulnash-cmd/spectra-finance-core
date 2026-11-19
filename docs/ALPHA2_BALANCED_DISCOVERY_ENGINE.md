# Alpha 2.0 Balanced Discovery Engine

## Purpose

This update separates broad market research from entry eligibility while preserving
mean reversion as the core strategy. It makes gainers, losers, volatility, and
recovery evidence visible without turning those lists into trade permissions.

## Independent research lists

- **Mean-reversion research:** every market with sufficient completed daily history
  and a calculated mean-reversion score, including rows rejected by an entry
  guardrail. The rejection reasons remain attached to each row.
- **Eligible mean-reversion:** only rows that pass the existing entry-liquidity
  floor and all existing historical quality/downside guardrails. This remains the
  sole list consumed by the existing discovered-asset authority writer.
- **Recovery watchlist:** losing markets with at least two independent reversal
  observations. It is research-only and does not enable VCAP, DTTP, DTSL, or any
  order path.
- **24h gainers and losers:** measured from hourly candles. The nearest hourly
  candle close at or before the 24-hour reference is used. Missing or stale
  references are omitted; no percentage is guessed.
- **3d losers:** current price compared with the close three completed daily
  candles earlier.
- **24h volume and volatility leaders:** context lists, not entry lists.
- **Rejected candidates:** analyzed rows with explicit eligibility rejection
  reasons. Data/history failures remain separately reported in rejected_data.

## Liquidity boundary

The existing **$10,000,000 24h quote-volume entry floor is unchanged**.

A separate **$1,000,000 research floor** allows the scanner to study a broader
set of Kraken USD spot markets. Markets below the existing entry floor can appear
in research and loser/gainer lists, but they are marked ineligible with
weak_24h_turnover and cannot enter mean_reversion_candidates.

The 90-day completed-history requirement, $1,000,000 30-day median daily quote
volume floor, 0.65 active-day ratio, 0.8% median daily range, 15% maximum current
move guardrail, and existing downside/pump guardrails remain unchanged.

## Recovery evidence

The scanner records:

- 14-day RSI change versus the preceding 14-day window;
- current 3-day return versus the preceding 3-day return;
- measured 24-hour rebound;
- price relative to its 5-day average;
- distance above the recent 3-day low.

A recovery-watchlist row must be a 24-hour or 3-day loser and show at least two
of those observations. These observations are descriptive research evidence,
not a buy signal or a bypass around existing rejection rules.

## Safety and compatibility invariants

- The flat candidates key remains the eligible-only mean-reversion shortlist.
- The lists.mean_reversion_candidates key remains eligible-only.
- write_discovered_kraken_assets is unchanged and consumes only that list.
- Research, gainer, loser, volatility, recovery, and rejected lists never grant
  Governor approval or capital authorization.
- Every research row has capital_authorized set to false.
- The scan remains research-only and creates zero orders.
- Capital policy, portfolio sizing, VCAP, DTTP, DTSL, and live-dispatch settings
  are not changed by this discovery update.
