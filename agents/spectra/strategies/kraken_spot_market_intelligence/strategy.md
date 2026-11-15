---
name: Kraken Spot Market Intelligence
description: Read-only Kraken BTC-USD market snapshot for Spectra Intelligence; no order, executor, or capital authority.
agent_key: null
skills: []
default_config:
  execution_mode: dry_run
  frequency_sec: 60
  total_amount_quote: 0
  max_ticks: 0
  market_connector_name: kraken_paper_trade
  market_trading_pair: BTC-USD
  market_candle_interval: 1m
  market_candle_records: 120
  market_data_max_age_seconds: 180
  market_data_cache_ttl_seconds: 10
  market_order_book_depth: 20
  risk_limits:
    max_position_size_quote: 0
    max_open_executors: 0
    max_drawdown_pct: 0
default_trading_context: |
  READ-ONLY MARKET INTELLIGENCE MODE.
  Analyze only the configured Kraken BTC-USD paper connector market snapshot.
  Do not create, modify, cancel, or manage orders, executors, controllers, bots,
  positions, reservations, or capital proposals. Do not request any capital.
  Report the snapshot status, timestamps, price, SMA20, RSI14, candle return,
  best bid/ask, spread, and quote depth. Only treat status FRESH with
  decision_grade=true as decision-grade. If status is STALE, DEGRADED, or
  NOT_CONFIGURED, explicitly HOLD and explain the missing or stale inputs.
  This strategy is research-only; all trade authority remains outside this
  strategy and exclusively behind Spectra Core's CapitalGovernor.
---
# Kraken Spot Market Intelligence

This strategy supplies Spectra Intelligence with a timestamped, read-only
Kraken BTC-USD market snapshot through the shared market_intelligence provider.

## Operating rules

- Research only. Never call any mutation or capital-allocation tool.
- Use only the configured kraken_paper_trade / BTC-USD feed.
- Require FRESH and decision_grade=true before interpreting the snapshot.
- Treat stale, missing, malformed, or timestamp-less data as a no-trade condition.
- Report observed facts separately from interpretation; never imply profitability.
- The zero budget and zero executor limits are deliberate secondary safeguards.
