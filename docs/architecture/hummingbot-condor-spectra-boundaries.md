# Hummingbot, Condor, and Spectra: capability boundaries

This is the ownership map for the integrated Alpha stack. The rule is simple: **reuse Hummingbot for exchange and execution mechanics, reuse Condor for agent orchestration, and keep Spectra focused on deterministic governance and portfolio policy.** Do not build a second exchange client, executor framework, or LLM-agent runtime inside Spectra.

## System responsibilities

| Layer | Owns | Spectra integration rule |
|---|---|---|
| Hummingbot core + Hummingbot API | Exchange connectors, public/private market data, trading rules, order books, order lifecycle, balances, executors, controllers/bots, history, performance, backtesting APIs, Gateway | Call the API/client. Do not reimplement exchange REST/WebSocket clients, order-book tracking, order lifecycle, or connector logic. |
| Condor | Agent runtime, LLM sessions/ticks, skills, routines, memory/journals, specialist-agent prompts, Telegram/web interaction, MCP tool profiles, Hummingbot API fetchers | Use Condor's existing agent framework and Hummingbot MCP tools. Do not create a parallel LLM scheduler or duplicate its memory/routine system. |
| Spectra Core | CapitalGovernor, entry/recovery budget policy, VCAP, economic-position ledger, paper-mode guard, deterministic market selection, portfolio supervisor, dispatch receipts and runtime health | Add only the project-specific controls Hummingbot and Condor do not provide. Spectra is the policy/governance layer, not another exchange engine. |
| Spectra agents | Domain-specific reasoning and research roles (market analysis, directional/grid/DCA, liquidity/DEX specialists, etc.) | Agents reason over prepared snapshots and invoke permitted tools. They do not bypass the CapitalGovernor or paper/live execution boundary. |

## Execution and accounting contract

1. **Hummingbot owns execution mechanics.** A strategy or agent creates and manages Hummingbot executors through the API. Agents should not hand-roll exchange order placement or maintain a second order lifecycle.
2. **Condor owns agent orchestration.** The deterministic providers/routines prepare market, executor, and portfolio snapshots. The LLM decides among bounded actions; its journal and learnings provide continuity.
3. **Spectra owns capital authority.** Every new exposure must pass through CapitalGovernor. VCAP recovery is a Spectra policy layered over Hummingbot executor operations; it is not a replacement for Hummingbot execution.
4. **Use controller/agent identity for isolation.** Executors remain tagged to their originating controller/agent so one agent cannot accidentally manage another agent's positions.
5. **Use the economic-position ledger for durable exposure.** Hummingbot observations and executor state are inputs; Spectra's ledger records exact entry lots, remaining basis, fees, partial exits, residuals, and verified terminal-flat state.
6. **Do not confuse executor completion with position closure.** A terminated executor can leave inventory behind. The existing orphaned/held-position lifecycle and terminal-flat verification must remain in force.

## Paper-mode capability matrix

Paper-only mode is an execution boundary, not a claim that every Hummingbot product has a native paper simulator.

| Capability | Alpha paper-mode status | Required behavior |
|---|---|---|
| Supported spot CEX connectors | Native Hummingbot *_paper_trade path | Use keyless paper connectors and public market data. Current active venue is Kraken, USD quotes only. |
| Kraken USD markets | Enabled | Discover/validate pairs through Hummingbot API trading rules and market data. Never add USDT quote conversion or USDT-quoted Kraken pairs. |
| Perpetual futures | Not verified as a native paper path in this Alpha | Preserve connector/strategy support in the full product, but do not pretend spot PaperTradeExchange simulates perpetual margin/funding/liquidation. Add a dedicated simulator only if required. |
| Gateway, wallet operations, swaps, AMM/CLMM transactions | Disabled in paper-only mode | Keep live DEX capabilities in the architecture behind separate authorization. Do not sign, submit, or mutate wallets in paper mode. |
| Read-only DEX research | Separate read-only data providers may be used | GeckoTerminal and other market-research data are not permission to enable Gateway transactions. |
| Hummingbot V2 controllers and multi-controller bots | Preserved in the full architecture; native paper compatibility is not assumed | Do not disable or delete them. Use only where the selected connector/runtime supports them and the appropriate mode is authorized. |
| Grid/DCA/order executors | Supported through the Hummingbot API, subject to the paper connector and Spectra policy | New exposure must pass Spectra admission and CapitalGovernor. |

## Current Spectra market-selection path

The market-discovery path should continue to use the Hummingbot API:

- KrakenMarketFabric obtains tickers, trading rules, and order-book snapshots from the API client.
- market_discovery applies Spectra-specific eligibility, liquidity, spread, trend, and risk filters to those returned datasets.
- portfolio_supervisor admits fresh candidates through the paper asset allowlist and CapitalGovernor before it creates an executor.

This is not a second Kraken exchange connector. If a future implementation starts calling Kraken REST/WebSocket endpoints directly for data already exposed by Hummingbot, prefer moving that read to the existing API client instead.

## MCP and agent-tool compatibility

The repository vendors a Condor/Hummingbot MCP surface and adds Spectra-specific tools for governance, accounting, VCAP, and dispatch receipts. Preserve those Spectra tools when syncing upstream Condor changes.

Only register a tool when the connected Hummingbot API version implements its route. For example, newer upstream Condor has a resolve_swap tool for reconciling an unverifiable submitted swap, but the current Hummingbot API branch does not expose the corresponding resolve action. Do not register that tool against this API version; add the server route and its tests first, then enable the MCP tool.

Tool profiles and agent prompts are separate controls:
- MCP profiles determine which tools a given agent seat can see.
- Agent prompts explain when and how to use those tools.
- API-side paper guards and Spectra's CapitalGovernor remain the actual enforcement layers. A prompt is never a security boundary.

## Live-mode preservation

Paper-only mode must not remove live connectors, Gateway, controllers, scripts, DEX support, or other Hummingbot capabilities from the product. It must reject live execution while the paper flag is active. Switching to live mode remains a separate, explicit authorization and must continue to pass the API guard, Spectra policy, and any required dispatch receipt checks.

## Operational cautions

- Hummingbot API controls exchange credentials, balances, orders, bots, and Gateway. Keep its API and broker bound to loopback/private networking; do not expose the trading API publicly.
- EMQX's bootstrap file is only imported for accounts that do not already exist. Changing .env does not rotate an existing broker user's password.
- The upstream make emqx-auth-reset operation deletes the EMQX data volume and loses broker state/retained messages. Never use it as a casual health fix. Back up the volume and attempt a supported export before any reset/replacement.
- On the Bazzite/Podman deployment, preserve rootless volumes and the user's existing .env, credentials, Postgres data, bot directory, and paper history during repairs.
