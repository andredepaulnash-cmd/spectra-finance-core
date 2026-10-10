# January–June historical paper BTC hedge sensitivity

This is a conditional paper comparison on owned January–June 2026 BTC OHLC and the previously saved PARK spot trace. It answers whether fixed BTC index shorts would have improved that recorded spot path under declared costs and capital. It does not alter the original spot strategy, use an actual historical derivatives contract, or authorize production trading.

The protocol was frozen before reading BTC price outcomes. Its SHA256 is `952aa51955da1feaca9e0b09245ce1ef7dd156a97c5f9db714ceec9617a81dd0`. A separate root receipt records acknowledgement of those exact bytes before this economic run. No parameter was selected from results.

## Comparison and capital

All five arms retain exactly the same saved spot account and trades. The ratio is 0%, 25%, 50%, 75%, or 100% of the latest **observed marked spot inventory**. A signal uses that observation and the BTC close completed at the same timestamp. It submits a fractional linear BTC-index short, executable at the exact opening five minutes later. There is no beta estimate, fitted threshold, hindsight coin list, or future-close execution.

Each capital scenario gives every arm the same separate cash reserve: $250, $500, or $2,000. The primary comparison uses $2,000 extra reserve, so every primary account starts with **$4,000 total**: the unchanged $2,000 spot account plus $2,000 research collateral. This is a conditional capital experiment, not a recommendation to add money, and its return is not the return of the original $2,000 account. PARK cash and spot inventory are never pledged, seized, or spent by this overlay. Futures sale proceeds are not credited as collateral.

The synthetic instrument is cash settled. Gains/losses settle from actual BTC observations, fees and adverse slippage apply only to the opening-price nominal value of actual size changes, and constant adverse carrying cost applies once per five-minute interval. Three fee/slippage scenarios (0/0, 10/5, and 40/5 basis points) cross three carrying-cost assumptions (0, 5, and 20 basis points per day). Carry is a hypothetical net funding/borrow expense; no actual historical funding or borrowing rates were fetched. The primary setting is 10-bps fee, 5-bps slippage, and 5-bps daily carry.

A separate reserve must cover 100% of desired notional before an increase. The synthetic maintenance threshold is 80% of current notional. An already-held short first has to survive the actual opening price gap before any pending ordinary size reduction can rescue it. A breach covers at that actual open. Surviving or newly rebalanced shorts then face the full five-minute high; a breach conservatively covers at that high. Both liquidations charge transaction costs and a 10-bps penalty and disable the overlay for the rest of the experiment. The additional opening-gap ordering check was an engineering correction after the first run, recorded separately without changing the original frozen protocol or financial grid. All 135 final financial results remained exactly equal after the correction. Negative balances remain liabilities. These are declared stress assumptions, not verified venue margin rules or an inference about the exact sequence of intrabar trades.

## Data resolution and limits

The saved portfolio export provides hourly observations, beginning January 1 at 00:05 UTC. Its exact July 1 00:00 UTC boundary is the completed June 30 account. The `minimum_hedge_cash_marked` metric records the completed-bar sampled minimum; intrabar high collateral is disclosed separately through `minimum_observed_high_margin_headroom`. The portfolio equity reconciles to physical cash plus marked inventory at every observation. Intrahour spot prices and equity are not reconstructed. Reported portfolio drawdown is therefore **sampled hourly**, and cannot be described as full five-minute maximum drawdown.

The canonical BTC calendar contains 52,128 five-minute intervals, of which six are missing. Missing OHLC are left absent. An execution due at a missing open is rejected, rather than delayed to a future convenient price. A held position retains its last observed accounting mark until the next actual observation; the missing interval's hypothetical carry uses that last observed notional. Open-position gaps are counted. A liquidation within an unobserved interval cannot be ruled out, so results with such gaps are conditional on no unseen breach.

Saved spot inventory may include stale last-observed marks. Those are accounting values, not executable venue liquidation quotes. Historical derivatives basis, order-book capacity, fractional contract availability, real borrowing/funding, account eligibility, and actual venue collateral recourse are unresolved. The funded whole-contract harness's historical/live gate remains unchanged. October 9–November 9 prospective holdout payloads were not opened or scored.

## Reproduction

The raw six owned monthly archives and large benchmark are external evidence, not embedded in this source tree. `prepare_existing_btc_history.py` and `prepare_saved_park_history.py` recreate the canonical inputs from their exact previously recorded source members. The run refuses any supplied file whose hash differs from the declared input pin.

```sh
python -m unittest discover -s tests -p test_paper_hedge_sensitivity.py -v

python research/danger_room/paper_hedge_sensitivity.py \
  --protocol docs/research-results/hedge-paper-20261010/protocol.json \
  --protocol-sha256 952aa51955da1feaca9e0b09245ce1ef7dd156a97c5f9db714ceec9617a81dd0 \
  --btc /absolute/BTC_USD_2026_H1_5m.csv \
  --btc-sha256 a27fbaa17e5a5a92ebb354f8e24aed5faf522db220238765dd1e932527c12114 \
  --portfolio /absolute/PARK_2026_H1_Hourly_Observed.csv \
  --portfolio-sha256 20ab93ef1add5973d6add0743513c3b41c09720fc5a2de7eaf76ef93f021723c \
  --boundary /absolute/PARK_January_June_Saved_Accounting_Summary.json \
  --boundary-sha256 405509794840496df929fd73d9b23ca60b1dd203e475f76dc20d8d8cf73c5e4e \
  --output /absolute/new-paper-results
```

The output directory must be new. It receives all 135 scenario summaries and the five primary arms' observation paths and execution journals. Final shorts remain marked open; no fictitious final closing fill is added. Any future close would add costs and price risk.
