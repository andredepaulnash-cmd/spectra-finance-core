# Governor-approved Kraken paper assets

Spectra Alpha does not infer an approved asset list from market rankings. A research candidate is not trade permission.

For paper BUY-side capital (ENTRY, PRESS, RECOVERY, ROTATION), Spectra requires an explicit source-attributed allowlist. The default location is:

`spectra_core/data/spectra-paper/approved_assets.json`

You may instead set `SPECTRA_APPROVED_ASSETS_FILE` in the Core environment to an absolute path.

## Format

```json
{
  "schema_version": 1,
  "source": "Name/version of the Architect-approved Governor asset list",
  "connector": "kraken_paper_trade",
  "approved_pairs": [
    "BTC-USD"
  ]
}
```

- `source` must identify the actual Governor list or approved document/version.
- `connector` must be exactly `kraken_paper_trade`.
- `approved_pairs` contains explicit Kraken USD spot pairs only.
- `XBT-USD` is normalized to `BTC-USD`.
- An absent file, empty list, malformed file, unapproved pair, or wrong connector fails closed.
- No default assets are populated by the installer or updater.
- The list does not itself authorize a trade. Capital limits, accounting health, breaker state, bounded execution policy, and signed Core dispatch are still required.

The mean-reversion scanner can continue to rank research candidates without this file, but it marks no candidate as Governor-approved and cannot create orders. Exit-side risk reduction remains on its separate authorization path.
