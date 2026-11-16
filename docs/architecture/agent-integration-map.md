# Spectra agent integration map

The repository already contains a multi-agent framework. Do not replace it with a second agent runner or duplicate Hummingbot's strategy/executor tools. Condor provides the agent runtime; Hummingbot API provides market/execution tools; Spectra Core supplies deterministic capital admission and position accounting.

## Agent seats and current paper-mode fit

| Agent | Primary role | Paper-mode use |
|---|---|---|
| Condor / Spectra Intelligence | Main coordinator, delegates research and execution tasks, maintains continuity through journals, skills, and routines | May orchestrate approved paper tasks. Must enforce the global Spectra capital and mode policy before delegating execution. |
| Spectra Finance | Narrow portfolio meta-controller | Governed Grid/DCA actions only. The deterministic portfolio supervisor owns initial one-shot entries. |
| Adaptive Grid Trader | Multi-timeframe adaptive grid strategy | Paper-capable only on a configured native Hummingbot paper connector; all new exposure must pass CapitalGovernor. |
| Directional Trader | Signal engineering, controller development, backtesting, live/backtest comparison | Research and backtesting in paper mode. Controller deployment requires a verified paper-compatible connector and governed admission. |
| Fundamental Analyst | Company/protocol fundamentals, yield, risk, and valuation | Research-only; no execution tools. |
| Market Making Expert | Regime detection, spread calibration, inventory management, PMM strategy work | Research by default. Execution only where the selected controller and connector have a verified paper path. |
| Smart-Money Flow | Derive perpetual directional trading and capital-flow analysis | The current prompt identifies Derive mainnet and leverage. Treat as research-only in paper mode; no mainnet execution or leverage. |
| Meteora Launch LP | Meteora DAMM v2 launch liquidity and fee-yield strategies | Read-only pool research in paper mode. No LP mutation or wallet transaction. |
| Solana DEX LP Expert | Solana CLMM/LP research and position management | Read-only DEX research in paper mode. No LP executor or swap execution. |
| XRPL Market Maker | XRPL CLOB pricing, spread, reserve, and inventory management | Research-only unless an explicit XRPL paper connector is configured and tested. Never fall back to a live connector. |

The table describes current verified behavior, not a permanent removal of capabilities. All specialist agents remain part of the full product architecture. A specialist without a native paper adapter can still research, analyze, backtest, and prepare a recommendation while paper-only mode is active.

## Runtime flow

1. The Condor agent engine selects an agent seat and its configured tool profile.
2. The agent receives market, portfolio, executor, and history context through the existing Condor fetchers and Hummingbot API MCP server.
3. The agent reasons and proposes a bounded action.
4. Any new exposure must be admitted by Spectra Core's CapitalGovernor and carry the exact proposal/claim identity and dispatch receipt required by the API.
5. Hummingbot API creates and manages the Hummingbot executor on the selected connector.
6. Condor fetchers observe the executor and portfolio state; Spectra's economic-position ledger reconciles durable entry basis, fees, exits, residuals, and verified terminal-flat status.
7. Agent journals and routines preserve decisions and learnings for subsequent ticks.

Use controller/agent identity to isolate executors and prevent one seat from managing another seat's positions.

## Global paper/live rules

- Paper-only mode stays enabled during paper validation.
- Only explicitly configured native *_paper_trade connectors may create paper orders.
- The current active Kraken paper universe is USD-quoted only.
- No Gateway wallet transaction, swap, AMM/CLMM mutation, live connector, or mainnet leverage action is allowed in paper-only mode.
- If a venue/strategy has no verified paper adapter, that specialist remains research-only; never silently route it to live.
- Live mode and live orders remain available in the architecture, but require the user's separate explicit authorization and independent live-mode authorization.
- Agents must never change SPECTRA_PAPER_ONLY, SPECTRA_ALLOW_LIVE_EXECUTION, credentials, or deployment settings to enable live execution.
- Prompts and tool lists are not enforcement. API paper guards and Spectra Core capital checks are the security boundary.

## Avoiding duplicate capabilities

- Use Hummingbot connectors for exchange connectivity, public/private data, trading rules, order books, and order lifecycle.
- Use Hummingbot executors/controllers for execution and strategy lifecycle where supported.
- Use Condor for LLM sessions, agent scheduling, delegation, memory, journals, skills, routines, and notifications.
- Use Spectra Core only for project-specific governance: CapitalGovernor, VCAP, economic-position accounting, deterministic candidate admission, portfolio supervision, and dispatch receipts.
- Do not add another exchange REST/WebSocket client, executor manager, order database, or agent scheduler when an existing layer already provides it.
