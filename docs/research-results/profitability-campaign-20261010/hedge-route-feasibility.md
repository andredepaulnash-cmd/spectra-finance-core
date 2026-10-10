# Hedge route and execution input feasibility

Research date: October 10, 2026. This is public-source preparation for the authorized profitability campaign. It changes no production code and accesses no account, credential, order, or sealed holdout. The structured facts, source identities, conflicts, and gates are in [hedge-route-feasibility.json](hedge-route-feasibility.json).

The spot campaign can proceed. An actual US hedge profitability claim remains incomplete until route-specific execution, financing, collateral, and availability evidence is admitted. More ordinary BTC spot OHLC does not resolve those gaps.

## Which route can actually short?

| Route | Public evidence | Historical and automation limit |
|---|---|---|
| US retail spot margin | Kraken explicitly supports borrowing and selling crypto through Kraken Derivatives US / Bitnomial. Current BTC pair metadata supports `XBTUSD:BTNL`. | Retail launch evidence falls during 2026, and the relevant order book differs from the archived standard spot book. Account entitlement remains unverified. |
| US Bitnomial perpetual | BTCUC / PBTCUCZ50 is a cash-settled 0.01 BTC contract. Kraken announced US access June 15, 2026. | Whole contracts, actual funding, margin and contract prices are required. Exchange API documentation does not establish API access through a Kraken US account. |
| US CME Micro Bitcoin | Kraken US CME access was announced July 15, 2025. MBT represents 0.1 BTC per whole contract. | Potentially date-valid for January–June, but exposure is coarse at the proposed capital levels. Actual contract histories, rolls, fees and session margins are missing. |
| Global Kraken Derivatives | Separate non-US product and API ecosystem. | It cannot silently substitute for a US participant's contract, permission or funding route. |
| Kraken Borrow US | Purchase financing backed by crypto. | The purchase feature alone is not short-route evidence; use the separately documented margin short route. |

Sources: [US spot-margin launch](https://blog.kraken.com/product/margin/cftc-regulated-spot), [current short-selling description](https://www.kraken.com/pro/short-selling), [US perpetual launch](https://blog.kraken.com/product/kraken-derivatives/announcing-cftc-regulated-us-perps), [US CME launch](https://blog.kraken.com/product/kraken-derivatives/us-regulated-derivatives), and [US contract specifications](https://support.kraken.com/articles/contract-specifications).

## Execution and capital cannot use the earlier synthetic shortcut

Kraken states that US spot-margin orders execute on a separate Bitnomial order book. Standard spot charts supply context and can differ in spread or volume. Consequently, the owned `XBTUSD` five-minute history remains useful for a reference-price sensitivity but is not evidence of actual `XBTUSD:BTNL` fills. See [the margin order book](https://support.kraken.com/articles/margin-order-book).

US retail API orders require the `:BTNL` suffix. A standard pair can produce `Reduce only:Non-ECP`. Kraken also states that `validate=true` checks format without verifying live account routing. This campaign submitted no validation or live private request. See [official API errors](https://support.kraken.com/au/articles/360001491786-api-error-messages) and [AssetPairs documentation](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs).

A 639-byte unauthenticated BTC pair metadata response retrieved at `2026-10-10T20:56:18.266472Z` reports a 0.00005 BTC minimum, eight quantity decimals, $0.50 minimum cost, $0.10 price tick, and 80 / 40 margin-call / liquidation percentages. Its SHA-256 is `2b439aaf8f548460538b73cc73a99258f256f7cd07c778a3a4419b34db99ac98`. These are current metadata, not historical parameters or proof of account eligibility. The response's fee arrays are empty.

The 737-byte public BTCUC specification retrieved at `2026-10-10T20:57:41.765165Z` identifies product 5614, a 0.01 BTC whole contract and a $5/BTC increment, equivalent to $0.05 per contract. SHA-256: `1dcc9632cc349adb5d738b3a33410b8d99180374c181a7614332fec9f5170a4d`. Its exchange first trading day is May 16, 2026; Kraken US access still has a separate June 15 launch gate.

For an explicitly illustrative $60,000 instrument price:

| Instrument | Smallest exposure | Effect on $200 / $1,000 / $2,000 accounts |
|---|---:|---|
| Current BTC spot-margin minimum | $3, subject to cost minimum and precision | Quantity size can fit; costs, collateral and funded reserves still need admission. |
| BTCUC perpetual | $600 | One contract can overhedge a small account or force large steps in hedge ratios. |
| CME MBT | $6,000 | Low posted margin does not make the exposure $200 or $1,000. |

The $60,000 example is neither a current nor historical price. Admission must use actual contract prices, finite funded reserves and route-specific margin requirements. Within every capital cohort, hedge ON and OFF share the same total starting capital. Do not add an invisible hedge reserve or count borrowed proceeds as freely owned cash.

## Cost and ownership findings

The current linked margin schedule quotes borrowed BTC opening and four-hour rollover rates of 0.01–0.02%. Six four-hour intervals imply 6–12 basis points per day if valuation is constant. These costs are additional to entry and exit trade fees; rates can be locked at execution. The prior five-basis-point daily paper assumption is below this current published range, but these observations do not reconstruct January–June rates. See [US margin fees](https://support.kraken.com/articles/us-margin-fees) and the [fee schedule](https://www.kraken.com/features/fee-schedule).

Current starting spot maker / taker rates are 0.40% / 0.80% per side. Kraken's July 9 notice confirms that the tier system and some fees changed. Historical reconstruction needs dated rates; deployment stresses need the applicable causal tier, including trailing turnover. Do not award a future high-volume tier to earlier trades. See [the fee change notice](https://support.kraken.com/articles/cross-platform-fee-tier-changes).

US margin liquidation may close positions and convert collateral assets from the Kraken account. The published liquidation spread is 2%; conversion charges can add 1.5% for cash currencies, 2.5% for BTC/ETH to cash, or 5% for other conversions. An internal VCAP label does not prevent venue recourse. The hedge model must either establish actual protected collateral boundaries or explicitly account for their absence; it cannot certify protected VCAP merely by keeping separate bookkeeping. See [US margin liquidations](https://support.kraken.com/articles/liquidations-us-margin).

## Inputs that still need resolution

1. Date-valid BTNL execution data and financing rates, or actual dated CME / BTCUC contract histories.
2. Actual reference marks, collateral haircuts and initial, intraday and maintenance margin schedules.
3. Contract-specific fees, trading calendars, liquidity and failure behavior.
4. Account-specific route eligibility and an authorized automated order path.
5. Equal-capital reserve ownership and collateral recourse consistent with the VCAP rules.

Bitnomial documents a public historical funding endpoint with product filters, pagination and `begin_time`. No funding payload was downloaded. Its draft public chart endpoint supplies daily prices, which cannot alone verify five-minute executions or liquidation chronology. See [funding documentation](https://bitnomial.com/exchange/docs/api/rest/funding-rates/) and [chart documentation](https://bitnomial.com/exchange/docs/api/rest/charts/).

Three source conflicts remain explicit. The ECP FAQ says US retail margin was introduced in April, while the dated launch announcement says May 6; the earliest date needs resolution. June US futures support describes $0.15 per perpetual contract per side, while current US marketing describes maker/taker pricing. Funding descriptions separate eight-hour accrual from broker daily cash settlement, and product clock fields need interpretation against the correct historical version. Do not resolve these conflicts by selecting the most profitable assumption. Sources: [ECP FAQ](https://support.kraken.com/articles/360061972272-margin-trading-and-eligible-contract-participant-ecp-self-certification-for-u-s-clients), [US futures fees](https://support.kraken.com/articles/us-futures-fees), [US perpetual support](https://support.kraken.com/articles/us-perpetual-futures), and [current perpetual marketing](https://www.kraken.com/pro/perps/crypto-perpetuals).

Proceed with coin selection, coin-class DCA / VCAP ladders, capital use, redeployment, execution stresses and spot-account audits. Hedge sensitivities can continue with explicit hypothetical assumptions. Actual hedge-backed profitability remains a separately gated claim; the missing inputs do not block the authorized spot study.
