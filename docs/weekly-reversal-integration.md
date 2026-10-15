# Default-off weekly reversal integration

This draft is stacked on PR40's accounting baseline. It preserves PR39 and PR41.
The first component commit provides tested building blocks; the full cash-funded
purchase coordinator and historical caller require separate integration admission.
It changes no live executor, scheduler, account setting or order route.

## Components

- `spectra_core/reversal_signals.py`: authentic complete-hour BBWP20/252,
  RSI14 soft ranking, explicit causal timing and default-off signal policy.
- `spectra_core/adaptive_reversal/`: immutable staged reversal reducer, ATR14,
  exact fills/retries, monotone stop policy and reconstruction accounting boundary.
- `spectra_core/next_entry_allocator.py`: equal-share realized-profit entitlement
  allocation, exact residual carry and replay-safe reserve/commit/release ledger.
- `spectra_core/next_entry_profit_bridge.py`: default-off native receipt/lot/
  ownership verification, actual cash earmark and durable grant outbox. This
  first version deliberately keeps spending locked.

## Proposed matched weekly comparison

The initial comparison changes only BBWP minimum 50 to 20. Both arms use the same
soft RSI ranking, adaptive policy, compounding, costs, caps and eligible data.
Per-asset BBWP application and the hourly bearish evidence rule are newly explicit
implementation choices. The recovered old overlay used BTC BBWP globally.

The proposed June 3–10, 2026 window is a previously examined development episode,
not untouched out-of-sample evidence. The reference roster is BTC, ETH, SOL, BCH,
LTC, DOGE, AVAX, LINK, DOT, SHIB, XLM, HBAR, ADA, AAVE and XTZ. Strict complete-bar
warmup makes only BTC/ETH/SOL ready throughout; XLM becomes ready later. The other
eleven must retain explicit exclusions. Missing no-trade candles are not invented.

No historical strategy evaluation has been launched for this new integration.
Synthetic component tests do not establish profitability, executable derivative
history or production readiness. Actual derivative funding, margin, dated contract
liquidity and eligibility remain unverified. Owned-spot sell/rebuild accounting
must be reported separately from any future derivative proxy study.

## Cash and scope gates

The research envelope remains $2,000 total cash/equity, $500 protected reserve,
$1,500 authorized capital-at-work ceiling and $750 per-asset ceiling. The reserve
is an accounting allocation, not a guarantee limiting market losses. All final
metrics include marked inventory, open losses, fees, cash ownership, pending
actions and drawdown; a larger signal count is not a larger fill count.

The subsequent integration must verify cash-backed next entries, PSM snapshot
ownership, atomic outbox/debit recovery, native governor and dust/reentry guards,
then freeze the protocol and registry before historical evaluation. The current
component commit cannot be used to claim that those gates have passed.
