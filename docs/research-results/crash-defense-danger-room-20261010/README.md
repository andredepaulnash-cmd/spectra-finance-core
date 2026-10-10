# Danger Room initial evidence — 2026-10-10

PR26's historical July comparison is completed and closed without merge. All four redeployment arms were worse than PARK under the frozen financial criteria. This separate campaign has started with exact ownership reconciliation and current official capability/data audits. **No hedge strategy matrix has run; no hedge-profit claim is available.** Production `spectra-core-phase2` remains at `6e894fa9c2b55384d0f6120f41ca91892e1fe207`.

## Genuine PARK ownership and portfolio growth

These are audits of saved historical modeled records, not a new backtest or live account balance. June-to-July holdings and fills were reconstructed from the SHA256-verified baseline archive and reconciled to the previously frozen PARK endpoint. Actual historical market marks from the independently checked tape receipt were applied to PARK's own quantities; no other arm's inventory was substituted.

| Measure | July endpoint / window |
| --- | ---: |
| Owned assets, start → end | 37 → 36 |
| Surviving lots | 57 |
| Modeled fills | 29 |
| Starting assets with units increased / decreased / unchanged | 1 / 3 / 33 |
| Newly held assets | KTA, RAILS |
| Fully disposed starting assets | ESPORTS, FORTH, PACT |
| Ending inventory mark | $195.7626 |
| Ending inventory all-in basis | $503.2265 |
| Ending cash | $1,707.6595 |
| Ending consolidated marked equity | $1,903.4221 |
| Window fees | $1.1680 |
| Inventory-value HHI | 0.0377351 |
| Effective holdings, normalized inventory marks | 26.5005 |
| Five largest marked holdings | 29.2317% of inventory value |
| Assets with marks older than 300 seconds | 18, totaling $91.7488 |

Dollar accounting reconciles within $1e-18; asset quantities and native rounding residuals remain individually exported. Units remain asset-specific. HHI weights normalize owned inventory value, with cash separately disclosed. This does not establish low correlation, risk diversification, current liquidation value, exchange execution finality, dust classification or sellability. The per-asset and per-lot CSVs preserve exact quantities, owner and uncertainty.

The saved January–June prefix separately reconciles 1,630 fills, 224 acquired assets and 37 ending assets, $61.7491 fees and $1,898.2772 ending marked equity. This historical accounting audit establishes neither a new replay nor future recovery. Unelapsed or unavailable future checkpoints remain censored.

**Cash constraint:** July ordinary cash is only about $2.19e-14; nearly all cash belongs to PARK. The proposed H1 does not take PARK/VCAP cash implicitly. A meaningful hedge budget needs a separately declared, feasible allocation or an equal additional-capital experiment across all controls. Collateral is not extra equity.

## Current venue capability audit

The [official-source snapshot](capabilities/kraken_capabilities.json) separates public product rules from actual account entitlement and historical availability. No private request or live validation order was made.

| Route | Verified public finding | Remaining restriction |
| --- | --- | --- |
| Kraken spot | Pinned Hummingbot connector implements spot | Selling owned inventory is not a short position |
| U.S. retail spot margin | Official REST/WebSocket routing uses venue pairs such as `XBT/USD:BTNL`; current public catalog returned 29 advertised short-enabled pairs | Account approval, borrow availability and historical rules unknown; pinned connector does not implement this route |
| U.S. Bitnomial BTC perpetual | Public product 5614 / `PBTCUCZ50`: 0.01 BTC per whole contract, $5/BTC price increment, first trading day May 16, 2026 | Kraken-mediated U.S. order/account API, account entitlement, margin histories and execution data unverified |
| International Kraken derivatives | Separate international API/product family | Does not establish U.S. account access |

Spot-margin liquidation can convert collateral elsewhere in the same Kraken account. An internal `HEDGE_BOOK` does not ring-fence owned spot or VCAP from venue recourse. The model must represent the real account boundary and seized assets, or remain a scenario with explicit limitations. See [Kraken's U.S. liquidation rules](https://support.kraken.com/articles/liquidations-us-margin).

Current public cost observations are 0.40% maker / 0.80% taker at the lowest spot volume tier, plus route-specific margin opening/rollover charges; U.S. perpetual charges are $0.15 per contract per side before funding. These are not the user's tier or historical cost proof. Bitnomial exchange funding timestamps and Kraken customer cash settlement need separate reconciliation. Public BTC contract size is confirmed by the [exchange specification](https://bitnomial.com/exchange/rulebook/product/crypto/pbtcuc/).

The dedicated Kraken perpetual article lists 16 launch contracts while its U.S. Futures overview says 17; that discrepancy remains unresolved. Exact symbol matching intersects historical PARK with four current advertised margin assets (ALGO, BCH, HBAR, RENDER) and two advertised perpetual assets (BCH, HBAR). This is a current-catalog comparison, not historical shortability or account approval.

## Data and research prerequisites

All six January–June monthly spot archives and the fixed-universe Q3 archive were located. The standalone 2.34 GB canonical array was not resolved. Prepared spot metadata excludes BTC/ETH and includes SOL: benchmark history must be obtained separately without admitting BTC/ETH into the spot strategy.

The official historical OHLCVT distribution covers through June 30, 2026. The ordinary public OHLC endpoint returns at most 720 recent bars and cannot backfill arbitrary old history. No-trade intervals need explicit missingness handling. Neither source alone proves point-in-time listings, executable spreads/depth, funding, margin, borrow, liquidation or account access. See [official historical downloads](https://support.kraken.com/in/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) and [OHLC API rules](https://docs.kraken.com/api-reference/market-data/get-ohlc-data).

The BTC exchange contract did not exist in January–early May according to verified metadata. Such a hedge cannot be presented as historically executable over those dates. Current and June 30 catalog responses were identical; a date-filtered response does not prove immutable historical terms or Kraken's customer-access inception.

The next data batch should acquire and hash BTC/ETH/SOL benchmark histories, point-in-time identities and eligible contract/funding/margin/cost histories, then establish the finite budget and causal estimator/signal parameters. Begin with the five-arm H1: PARK OFF and BTC hedge ratios 25/50/75/100%, identical capital and feasible unchanged spot traces. BTC+ETH, +SOL, baskets and hedge-profit redeployment remain separate later experiments. Give a simulation ETA after measuring the verified input size and adapter runtime; no performance-run ETA is justified yet.

## Offline checks and holdout boundary

Local checks passed 47 unit tests and 45 independent adversarial probes. The actual template returned BLOCKED with 271 missing-contract findings. The pure-stdlib [validator](../../../research/danger_room/validate_run_manifest.py) and its tests operate without a strategy, connector or network. Acceptance means declaration/local-hash coherence only. It cannot prove causal file contents, genuine provenance, cash/trace feasibility, account eligibility or venue isolation. Independent semantic review is required, and successful declaration checks retain `approved_for_strategy_execution=false`, `approved_for_live=false`, and `external_facts_verified=false`.

The real [run template](../../../research/danger_room/run-manifest.template.json) is **BLOCKED**. Missing inputs and numeric criteria are visible; there is no executable hedge manifest. The [design protocol](../../research-protocols/crash-defense-danger-room-20261010.md) is separate from a completed strategy-run freeze.

The daily public-only November holdout collector remains enabled on the retained PR26 research branch. Its fixed endpoint is November 9 00:00 UTC, equivalent to November 8 7 p.m. Eastern. Scheduled final overlapping capture is clipped to that endpoint. Collection does not depend on PR26 staying open. Sealed market outcomes were neither read nor scored. The fresh-$2,000 replay adapter remains a separate unfinished task against its unchanged financial rules.

No production merge, deployment, live order, account change, DTSS/DTSSR/PSSM activation or financial-rule amendment occurred.
