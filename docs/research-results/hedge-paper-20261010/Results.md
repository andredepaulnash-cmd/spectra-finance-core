# Historical paper results: improvement was conditional; primary account still lost money

The frozen January–June comparison completed all 135 combinations. At the primary cost and capital assumptions, every nonzero BTC hedge improved the saved PARK path and reduced its hourly sampled drawdown, while **every primary account still finished below its starting capital**. This supports further investigation of a hedge; it does not establish profitable production software.

Every primary arm started with the unchanged $2,000 spot account plus the same $2,000 separate research reserve: **$4,000 total**. The spot account itself ended at $1,898.28. The declared primary short costs are 10-bps transaction fee, 5-bps adverse slippage, and 5-bps daily adverse funding/borrow expense.

| Fixed target ratio | Net hedge P&L after costs | Ending combined equity | Combined P&L vs $4,000 start | Hourly sampled max drawdown |
|---|---:|---:|---:|---:|
| 0% | $0.00 | $3,898.28 | −$101.72 | 4.851% |
| 25% | +$14.13 | $3,912.41 | −$87.59 | 4.307% |
| 50% | +$28.26 | $3,926.54 | −$73.46 | 3.801% |
| 75% | +$42.39 | $3,940.67 | −$59.33 | 3.419% |
| 100% | +$56.52 | $3,954.80 | −$45.20 | 3.245% |

The primary 100% target earned $96.13 gross short P&L, then paid $6.77 transaction fees, $3.39 adverse slippage, and $29.45 modeled carrying cost, leaving $56.52. Its total short turnover was $6,772.14 across 4,340 fractional size changes. The final short remained open and marked, with approximately $195.78 notional; no fictitious terminal close was added. A real final cover would add fees, slippage and price risk.

## Carrying expense can reverse the result

With $2,000 additional reserve and a 100% target, the following **net hedge P&L** is conditional on the stated constant carrying-cost assumptions. The same original spot loss of $101.72 must still be subtracted to calculate total account profit.

| Transaction fee / slippage | 0-bps daily carry | 5-bps daily carry | 20-bps daily carry |
|---|---:|---:|---:|
| 0 / 0 bps | +$96.13 | +$66.68 | −$21.67 |
| 10 / 5 bps | +$85.97 | +$56.52 | −$31.83 |
| 40 / 5 bps | +$65.66 | +$36.21 | −$52.14 |

The high carrying-cost case can make the hedge lose money and worsen total equity. Actual historical funding/borrow rates and futures-versus-spot basis were not included, so the positive conditional results cannot be treated as venue profitability.

## Three narrow whole-account positives require zero assumed carry

Only **3 of 135** total accounts ended above their own starting capital. All three used a $500 additional reserve, a 100% target, and **zero assumed carrying expense**:

| Transaction fee / slippage | Combined account profit | Target increases denied by reserve |
|---|---:|---:|
| 0 / 0 bps | +$19.79 | 594 |
| 10 / 5 bps | +$13.79 | 594 |
| 40 / 5 bps | +$1.64 | 602 |

These accounts started at $2,500, not $4,000 or the original $2,000. Their reserve constraint changed the executed hedge path by denying many increases. They are not a scaled copy of the primary path, and the favorable reserve was not selected for deployment. **No tested account was profitable with either 5-bps or 20-bps daily carry.** At 5-bps carry and moderate transaction costs, the $500 / 100% case finished $8.72 below its $2,500 start.

Across all reserves and costs, 81 of the 108 nonzero hedge scenarios had positive incremental net hedge P&L; 27 had negative incremental hedge P&L. These are sensitivity cases on one already viewed development window, not 108 independent market experiments.

## Capital and missing-price limits

No scenario borrowed, pledged or seized spot/PARK cash. Each initial-margin increase had to fit within the separate reserve after costs. Forty-five scenarios denied at least one target increase. No liquidation was triggered by the observed five-minute BTC highs under the declared synthetic rules.

There were six absent BTC intervals, and all four primary shorts were open during those gaps. An unseen adverse high and liquidation cannot be ruled out. The reported results therefore assume no unseen liquidation in those intervals. Exact-time missing execution bars rejected two attempted primary target changes, and one hourly observation lacked a contemporaneous BTC signal close. Missing OHLC were not fabricated; the explicit carry proxy during the gaps uses the last observed notional.

Portfolio observations were hourly, with an exact final June boundary, so the portfolio drawdown column is sampled hourly. Saved spot inventory includes historical accounting marks that can be stale. No executable venue liquidation value, production profitability, original-$2,000 funded hedge, or historical exchange eligibility has been established. The October 9–November 9 holdout was not opened or scored.

## What this suggests

The owned six-month data was sufficient for this first conditional economic test; another OHLC download was not required. The evidence supports examining actual instrument availability, funding/borrow and collateral terms before treating the apparent protection as implementable. It also shows that hedging alone did not repair the primary spot account's loss. No best ratio, reserve size or production policy is selected from this inspected window.
