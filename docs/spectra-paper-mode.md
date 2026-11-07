# Spectra Finance paper-only execution mode

## Purpose

Spectra paper mode exercises the real Core authorization and Hummingbot executor path with simulated balances and paper connectors. It is not a second live account, and it must not share capital databases or credentials with live execution.

## Enablement

Set `SPECTRA_PAPER_ONLY=true` in both:
- the Condor/Spectra Core process that runs the Hummingbot MCP tools; and
- the Hummingbot API process.

The API's paper-only runtime then:
- discovers and initializes only connector names ending in `_paper_trade`;
- rejects live connector names at both trading- and data-connector creation/lookup boundaries;
- allows only the reviewed paper executor/order, portfolio, and market-data routes;
- blocks Gateway, wallet, live credential, bot/MQTT, script, Docker, and unreviewed mutation routes;
- starts the executor control loop and accounts service only against paper connectors; and
- reports the effective mode and initialized paper connectors at authenticated `GET /spectra/runtime`.

The Core MCP execution tool separately requires `kraken_paper_trade` when paper mode is enabled. It refuses live Kraken or any other connector before market-data lookup, then performs the existing protected BUY preflight and commits the exact proposal through the Capital Governor before the Hummingbot executor create call.

## State and ledger isolation

- Live state remains in `SPECTRA_STATE_DIR` or the repository's default `data/spectra`.
- Paper state uses `SPECTRA_PAPER_STATE_DIR` or the separate default `data/spectra-paper`.
- The paper path is rejected if it resolves to the same directory as live state.
- Capital Governor, economic-position, VCAP, scheduler, and decision-state databases are all created beneath the selected mode's state directory.
- If no paper-specific capital policy exists, Core reads the existing live `capital_policy.json` as a read-only policy fallback. It does not copy or mutate the live policy or any live database.
- A paper-specific `breaker_state.json` takes precedence. If it is absent, Core reads the live breaker state read-only; if neither exists or the file is malformed, the breaker remains HALTED.

## Hummingbot paper connector setup

Hummingbot's official paper-trading configuration uses the `_paper_trade` connector suffix and the `paper_trade.paper_trade_exchanges` / `paper_trade.paper_trade_account_balance` settings in `conf_client.yml`.

Reference: https://hummingbot.org/client/global-configs/paper-trade/

The API account must contain a dedicated paper connector configuration such as `kraken_paper_trade`. Paper connector configuration is allowed through the API only when the connector name has the paper suffix and all exchange/wallet secret fields are empty. Do not reuse live credentials.

The current API repository still installs the Hummingbot core package without a pinned revision. Therefore, connector discovery, paper account provisioning, simulated order submission, and fill reconciliation must be verified against the exact local Hummingbot core version before this mode is considered runtime-certified.

## Current scope and limitations

- The tested Core executor-create path currently supports Kraken USD spot grid/DCA in this phase; the broader DTTP, DTSL, PSSM/MIR, VCAP, recovery, rotation, and strategy-lab systems remain part of the full product inventory and must be integrated/tested individually.
- The Hummingbot paper connector consumes public exchange market data. It simulates orders and balances; it is not an offline market-data simulator.
- The initial paper balance (the Architect previously discussed a $1,000 starting simulation) must be set explicitly in the paper connector configuration. This branch does not silently invent or modify the starting balance.
- The Core tests use synthetic portfolio/rule/book evidence and fake Hummingbot dispatch. They prove the Core authorization ordering and connector selection, not a live local Hummingbot container.
- No real exchange API keys, wallet seeds, live orders, or production capital are needed.

## Verification sequence

1. Confirm both processes report `PAPER_ONLY` and the API runtime endpoint lists only paper connectors.
2. Confirm the paper account has its explicitly configured simulated quote balance.
3. Reserve a Core ENTRY proposal against the paper connector.
4. Submit a protected grid/DCA executor through the Core tool.
5. Verify the Capital Governor decision is COMMITTED before API dispatch.
6. Verify paper fills reconcile into the separate paper ledger and the Spectra interface displays the paper account only.
7. Attempt the same route with a live connector name and confirm it is rejected before connector creation.

Any live mode remains unavailable until a separate Core-issued dispatch receipt is implemented and enforced at every API mutation boundary.
