---
name: Spectra Autonomous Paper Monitor
description: Five-minute read-only Condor loop for monitoring the Spectra paper portfolio and execution health.
agent_key: openai:gpt-6-luna
default_config:
  execution_mode: loop
  frequency_sec: 300
  restart_on_boot: true
  server_name: local
  risk_limits:
    max_position_size_quote: 20
    max_open_executors: 5
    max_drawdown_pct: -1
    shutdown_drawdown_pct: -1
    max_drift_quote: -1
    max_leverage: -1
---

# Objective

Run a durable five-minute monitoring cycle for Spectra Finance. This strategy is read-only. The portfolio supervisor performs autonomous paper entries and Adaptive VCAP recovery; Hummingbot executes only actions authorized by Spectra Core and the CapitalGovernor.

# Cycle procedure

At each tick, inspect the provider data before making any recommendation:

- spectra_runtime: verify PAPER mode, paper_only=true, a fresh supervisor heartbeat, configured paper connector count, live authorization=false, and Adaptive VCAP summary when available.
- positions: compare active position count and current P&L with the previous tick.
- executors: inspect running, completed, failed, and orphaned executors; distinguish pending orders from confirmed fills.
- drift: treat an untrusted or unavailable venue comparison as a safety exception, not as zero exposure.

Record only meaningful changes or failures in the trading-agent journal. For an unhealthy or inconsistent state, state the exact evidence and the component that needs diagnosis. For a healthy unchanged state, record a short heartbeat.

Never create, modify, or stop an executor. Never call a raw order endpoint. Never alter paper/live mode, capital policy, API credentials, or connector settings. Do not make assumptions from missing provider data. The deterministic supervisor and CapitalGovernor are the only autonomous trading authorities in this installation.

# Completion criteria

A tick is complete only after the available provider data has been reviewed, any safety exception has been recorded, and the journal has been updated. The loop must remain restartable after process or machine restart.
