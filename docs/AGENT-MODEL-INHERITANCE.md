# Spectra agent model inheritance

Spectra's stock agent identities do not pin an LLM provider or model. They
inherit the user's active model preference so the coordinator and specialist
agents use one consistent model configuration by default.

## Resolution order

The runtime resolves a model in this order:

1. Explicit run-level `agent_key` override.
2. Explicit strategy-level `agent_key` override.
3. Explicit agent-level `agent_key` pin.
4. The authenticated user's active model preference.
5. The existing empty-key runtime default when no preference is saved.

The same resolver is used by agent-start preflight and the background
`TickEngine`, so the model checked before a run is the model used by the run.
An unavailable preference store does not invent a model; it preserves the
existing empty-key fallback.

## OpenRouter as the shared provider

Choose the desired OpenRouter model once in the user's active model selector.
Unpinned Spectra agents inherit that selection automatically. The repository
does not contain an API key or a hard-coded OpenRouter model ID. Credentials
remain in the user's local provider configuration.

A deliberate run- or strategy-level override remains possible for experiments,
but it should be explicit and documented. Do not add a model pin to a stock
agent merely to make one local test work.

## Inventory

The ten stock identities are Condor, Spectra, Adaptive Grid Trader, Directional
Trader, Fundamental Analyst, Market Making Expert, Smart-Money Flow, Meteora
Launch LP, Solana DEX LP Expert, and XRPL Market Maker. Their strategy-specific
market and execution roles remain unchanged by model inheritance.
