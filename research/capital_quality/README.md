# Capital quality and purpose budgets

This is isolated offline research. It does not change any production strategy,
enable live orders, or establish that a winner selector is predictive.

The prior July comparison showed that bounded parked-cash release actually
funded trades, but every enabled arm ended below PARK. It also showed that
recovery priority could occupy the entire shared authority and prevent parked
cash from funding entries. This follow-up separates admission quality from
competition for that budget.

| Arm | Outstanding parked basis and claims | Qualification |
| --- | --- | --- |
| SHARED_RAW | $100 shared | Native rules |
| SPLIT_RAW | $50 ENTRY and $50 RECOVERY | Native rules |
| SHARED_QUALITY | $100 shared | Fixed causal gates |
| SPLIT_QUALITY | $50 ENTRY and $50 RECOVERY | Fixed causal gates |

The same exact June checkpoint and July 1–30 tape are used. PARK is the saved,
independently audited control. SHARED_RAW must reproduce the previously audited
BOTH run byte-for-byte for account rows, fills, and terminal owned inventory.
The split budgets cannot borrow from one another. They track exact surviving
fee-inclusive parked acquisition basis plus pending parked claims, rather than
lifetime spend or current market value.

New parked-funded entries require strict positive 1h and 24h close movement.
Parked recovery supplements require a positive 15m close move and a higher
rolling three-candle minimum low than the preceding three candles. Referenced
OHLCVT must be finite and OHLC prices positive. Missing actual anchors deny only
the parked supplement. Ordinary capital remains available to native planners.
Replacement qualification stays frozen at its original submission close.

`protocol.json` and `protocol-clarifications.json` describe the exact rules and
the inherited wording correction. Original native ranking, recovery/tranche
planners, 1% whole/partial execution floors, fees, slippage, volume limits,
reserve and pair caps remain in place. There is no per-coin outcome allowlist or
trained model. PSSM, DTSS and DTSSR remain off.

## Replay

Supply the preserved native wrapper, typed hydration module, exact checkpoint,
canonical prepared tape, and independently audited prior BOTH result directory:

```sh
python research/capital_quality/run_matrix.py \
  --wrapper /absolute/archive/continuation_wrapper.py \
  --hydration /absolute/archive/hydration.py \
  --checkpoint /absolute/archive/boundary_state.json.gz \
  --prepared /absolute/archive/prepared \
  --previous /absolute/archive/previous-results/BOTH \
  --results /absolute/new-results
```

Each result preserves executed source, exact source journals, modeled fills,
account rows and qualification records. Interrupted attempts stay incomplete;
thresholds are not relaxed to produce activation. Independently audit every
source transition, purpose cap, actual tape fill and qualification cutoff.

Report both split contrasts, both qualification contrasts and their interaction.
Closed profit alone is insufficient. A July candidate must beat PARK ending
marked equity after costs, have no greater window drawdown or worse terminal
open P&L, and produce real audited parked-funded buys. Any candidate still needs
wider and prospectively sealed validation. No July result promotes production.

## Checks

```sh
python -m unittest discover -s research/capital_quality/tests -v
```

The four native integration checks require the external authentic archived
fixture. Without it they explicitly skip; ten source-budget and seventeen causal
boundary checks still run. Constructed checks prove mechanics, not profitability.

Prospective public-only collection is described separately in `research/holdout`
and `docs/research-holdout`; it uses fresh matched $2,000 simulated accounts,
not the June inventory checkpoint. It does not run trades or score holdout data
before its declared November 9 endpoint.
