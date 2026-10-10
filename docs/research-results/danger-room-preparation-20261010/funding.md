# Funded hedge feasibility preparation

The specified historical PARK design has **no allocated hedge cash**. No actual
hedge has been admitted or tested for historical profitability. The separate
conditional extra-capital designs below preserve every old spot owner and give
the OFF control exactly the same new cash as all hedge arms.

## Exact saved cash evidence

The previously viewed June checkpoint starts the July continuation with these
balances. All 8,640 saved July account frames were independently reconciled.

| Quantity | Exact USD |
| --- | --- |
| June checkpoint physical cash | 1702.856656155282386623051379 |
| June checkpoint PARK-owned cash | 1701.314697008937781013999490 |
| June checkpoint ordinary cash | 1.541959146344605609051889 |
| June checkpoint marked spot inventory | 195.4205269366615707581776165 |
| June checkpoint marked equity | 1898.277183091943957381228996 |
| July endpoint physical cash | 1707.659514487834020320225698 |
| July endpoint PARK-owned cash | 1707.659514487833998378453361 |
| July endpoint ordinary cash | 0.000000000000021941772337 |
| Maximum ordinary cash in any saved July frame | 14.305981622245502401716007 |

Ordinary cash reaches $10 in only two saved frames. Its minimum is the July
endpoint residue. Therefore any constant withdrawal from ordinary cash that
leaves the saved spot rows unchanged has a necessary row-level upper bound of
that residue, or zero when floored to USD cents. This is **not sufficient proof
of spendable cash**: intra-frame cash events and pending order obligations were
not replayed. Event-level cash admission remains
`BLOCKED_TRACE_CASH_UNKNOWN`. Existing PARK or VCAP ownership is never claimed
as hedge collateral by this analysis.

The January account started with $2,000 cash. These July observations do not
say that every earlier or fresh-account capital design is unfundable.

## Whole-contract geometry

Previously collected public metadata specifies a whole PBTCUCZ50 contract of
0.01 BTC, with first trading day May 16, 2026. No current BTC price was requested.
No January-through-early-May availability is inferred from this instrument.
The following quotes are **invented test values**.

| Constructed BTC quote | One contract notional | Whole contracts at a 100% hedge of saved July $195.7626 spot |
| --- | --- | --- |
| $10,000 | $100 | 1 |
| $20,000 | $200 | 0 |
| $60,000 | $600 | 0 |
| $100,000 | $1,000 | 0 |

The target floors to whole contracts. Increasing cash does not permit rounding
up past the protective notional target. Minimum block-trade size is a separate
venue field; no block-trade execution is modeled.

## Conditional funded designs

The reproducible findings contain 48 explicitly constructed scenarios and 240
arm assessments. Each scenario uses the same extra cash allocation across OFF,
25%, 50%, 75% and 100% arms. This is a different experiment from the original
account: extra deposits increase every arm's initial equity equally and are not
profits. Old spot/PARK/VCAP books remain unchanged.

For an illustrative $1,500 spot exposure and invented $60,000 BTC quote, the
whole-contract targets are 0, 0, 1, 1 and 2. The invented risk assumptions are
20% initial margin, 12% maintenance margin, a 25% adverse BTC rally, and a 1%
aggregate adverse funding bound over the assumed horizon. These are **not actual
margin rates or funding history**. The previously collected current fee snapshot
supplies $0.15 per contract per side and a $10 perpetual liquidation cash buffer;
historical fees and the user's exact terms remain unknown.

| Equal extra cash per arm | Admitted contracts: OFF / 25% / 50% / 75% / 100% |
| --- | --- |
| $0 | 0 / 0 / 0 / 0 / 0 |
| $100 | 0 / 0 / 0 / 0 / 0 |
| $300 | 0 / 0 / 1 / 1 / 1 |
| $600 | 0 / 0 / 1 / 1 / 2 |

The conservative reserve is the larger of initial margin and stressed
maintenance margin, plus the adverse short loss, entry and exit fees, bounded
adverse funding, and a once-only liquidation/fixed cash buffer. One contract
needs $287.80; two need $565.60 in this example. These amounts are reserved cash,
not fees spent twice and not a profitability result. A finite rally assumption
does not guarantee solvency against unbounded short losses.

Actual venue isolation has not been established. An internal HEDGE_BOOK does
not prevent Kraken from accessing collateral held in the same account. Account
eligibility, actual initial/maintenance margin, historical prices/funding/fees,
collateral recourse and event-level spot-trace cash obligations remain unresolved.
The new item 3 data acquisition is deferred as instructed. None of these
preparation results approves live trading or changes production.

## Reproduction

`funding_findings.py` requires exactly five already-viewed SHA256-pinned sources.
It checks hashes before parsing and verifies the exact July frame interval.
The source paths below point to the archived local research inputs, not the
sealed holdout.

```bash
python3 -m research.danger_room.funding_findings \
  --park-rows /path/to/control_PARK/window_accounts.csv \
  --june-summary docs/research-results/crash-defense-danger-room-20261010/data/PARK_January_June_Saved_Accounting_Summary.json \
  --july-summary docs/research-results/crash-defense-danger-room-20261010/data/PARK_July_Inventory_Summary.json \
  --btc-spec docs/research-results/crash-defense-danger-room-20261010/capabilities/btcuc_current.raw.json \
  --capabilities docs/research-results/crash-defense-danger-room-20261010/capabilities/kraken_capabilities.json \
  --output funding-findings.json
python3 -m unittest discover -s research/danger_room/tests -p 'test_funding_feasibility.py' -v
```

All complete input hashes, conditional parameters, cash bounds and arm outputs
are in `funding-findings.json`. The validation receipt binds the code, tests and
generated findings. No network, account, order, funding-history download or
holdout outcome was used.
