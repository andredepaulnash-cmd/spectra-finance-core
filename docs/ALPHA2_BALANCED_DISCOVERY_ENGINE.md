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

## Top-loser reversal pathway research

A distinct `top_loser_reversal_candidates` list is now produced for measured
24-hour losers only. A row must pass the existing historical quality and
downside guardrails, the existing $10,000,000 24-hour entry-liquidity floor,
and show at least two reversal observations. A negative three-day return alone
does not qualify a market as a 24-hour loser.

Reversal observations include RSI/price context and BBWP (20-period Bollinger
bandwidth percentile plus bandwidth direction). BBWP is descriptive context:
compression and expansion are observations, not a standalone numeric entry
gate. The candidate list remains informational and sets
`reversal_pathway_eligible=true` while keeping `capital_authorized=false`.

The portfolio supervisor now routes this shortlist through a distinct
`TOP_LOSER_REVERSAL` source, and VCAP admission rechecks the 24-hour loser,
reversal evidence, and $10M liquidity requirements. The paper asset-authority
writer includes only candidates that pass those same pathway checks; this
allowlist is not capital authorization.

The entry strategy source is persisted in the capital reservation, protected
entry submission, economic position, dust-successor admission, and position
snapshot. Recovery submissions inherit the source from the exact economic
position. This makes the source available after process restart.

The adaptive DTTP/DTSL exit manager and its protected SELL execution contract
are position-scoped to confirmed top-loser reversal positions. Their durable
state and exit authorization are covered by the strategy-specific tests below;
they do not globally enable DTTP/DTSL or affect ordinary positions.

## Order execution policy

All purchases use MARKET BUY execution for immediate matching, including
initial entries and every authorized VCAP recovery patch. VCAP still plans and
authorizes one recovery tranche at a time; its tranche sizing and recovery
sequence do not turn the BUY into a resting DCA limit order. The protected
market-buy adapter sizes the base quantity from the exact reserved quote budget
and a fresh, depth-checked Kraken ask book, while retaining venue-minimum and
paper-only safeguards. Live market execution remains separately gated until
quote-denominated spend bounds can be enforced by the connector.

LIMIT orders are reserved for adaptive DTTP/DTSL SELL exits after the
corresponding position-scoped exit trigger activates. Those protected sell
prices are dynamically recalculated from the adaptive exit decision and fresh
market evidence. No ordinary position inherits the top-loser-specific exit
behavior.

## Safety and compatibility invariants

- The flat candidates key remains the eligible-only mean-reversion shortlist.
- The lists.mean_reversion_candidates key remains eligible-only.
- write_discovered_kraken_assets includes ordinary eligible candidates and separately qualified top-loser reversal candidates; it grants no capital.
- Research, gainer, loser, volatility, recovery, and rejected lists never grant
  Governor approval or capital authorization.
- Every research row has capital_authorized set to false.
- The scan remains research-only and creates zero orders.
- Capital policy and portfolio sizing are unchanged. The new source metadata does not change VCAP recovery policy or enable DTTP/DTSL/live dispatch.
