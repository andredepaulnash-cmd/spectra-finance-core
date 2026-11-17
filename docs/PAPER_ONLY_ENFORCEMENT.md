# Paper-Only Enforcement Matrix

Alpha 2.0 does not contain a live-trading mode. This is a paper-only product, not a live product with a default-off switch.

| Layer | Required enforcement |
|---|---|
| Process configuration | Require exact `SPECTRA_PAPER_ONLY=true` and `SPECTRA_ALLOW_LIVE_EXECUTION=false`; reject known exchange credential environment keys |
| Scanner | Public market data reads only; no account/private endpoints |
| Condor | Recommendation-only interface; no order or capital tools |
| Spectra Core | Validate paper mode before creating an order proposal |
| CapitalGovernor | Construct only from validated paper environment; re-check immutable safety state on every public operation; authorize only `kraken_paper_trade` |
| Hummingbot adapter | Independently validate paper mode and verify the exact single-use Governor authorization before each submission |
| Deployment | Only paper connector configured; no private credential volumes, files, environment variables, or secrets |
| Tests | Positive paper-mode tests plus negative tests for missing flags, wrong values, credential variables, live connector, absent authorization, expiry, replay, and amount mismatch |

The current implementation has process-setting validation, Governor constructor validation, paper connector restriction, and initial negative tests. The Hummingbot adapter and its independent authorization verification are **not implemented yet**. Do not enable the executor until that boundary is complete and tested.
