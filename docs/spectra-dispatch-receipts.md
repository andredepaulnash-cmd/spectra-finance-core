# Spectra Core–API dispatch receipts

## Boundary

Spectra Core remains the only capital authority. Before a Hummingbot executor-create request is sent, Core commits the exact Governor proposal, then signs a short-lived Ed25519 receipt for the exact HTTP request. The Hummingbot API verifies the receipt before its executor route can run.

The receipt binds:
- Unique single-use receipt ID and proposal ID
- Intent, account, connector, trading pair and executor operation
- Positive quote-capital amount and policy version
- HTTP method and normalized route
- Canonical full request-body SHA-256 and executor-config SHA-256
- Issue and expiry times (30 seconds by default; API rejects lifetimes above 60 seconds)

The API stores consumed IDs in its durable receipt ledger. Replays, expired receipts, altered bodies, wrong paths, wrong account/connector/market, unsupported policy versions, missing keys, and ledger failures are rejected before route dispatch. API Basic Auth is checked before receipt consumption.

## Local key setup

Generate one key pair on the local machine. The Core private key stays in the Core repository's local `.env`; only the public key is written to the API repository's local `.env`:

```bash
python scripts/generate_spectra_dispatch_keys.py \
  --core-env .env \
  --api-env ../spectra-hummingbot-api/.env
```

If the two repositories are not siblings, supply their actual local `.env` paths. The script preserves other environment variables, sets both files to mode 0600, and explicitly sets `SPECTRA_PAPER_ONLY=true` and `SPECTRA_ALLOW_LIVE_EXECUTION=false` in both processes (plus `CONDOR_AGENT_SLUG=spectra` for Core). The Spectra runtime rejects a paper-mode disable unless a separate future live-mode authorization is configured. Never commit either `.env`; never copy the private key into the API container, logs, GitHub, or chat.

The shared policy version defaults to `spectra-policy-v1`. If it is changed, update both local environment files and deploy the same reviewed version on both processes.

## Current scope and limitations

The Core MCP executor-create path signs only the current governed grid/DCA executor request. The API receipt middleware blocks writes under executors, trading, Gateway, bot orchestration, Docker, controllers, and scripts unless they carry a valid receipt. Until Core adds operation-specific receipts for cancel/close, direct orders, leverage, Gateway, and deployment operations, those alternate writes remain deliberately unavailable.

The API receipt ledger records one-time consumption. If a request times out after consumption, do not retry the same receipt; reconcile the original proposal and Hummingbot executor state. Full persisted dispatch-outcome reconciliation and image attestation remain additional hardening work.

## Review status

The API verifier and Core signer are draft pull-request changes and have not been merged or installed locally. The end-to-end signed request path must pass both repositories' CI and then a local paper-only smoke test before it is considered operational. No live trading is enabled by this work.
