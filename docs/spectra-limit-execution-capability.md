# Protected LIMIT execution capability

This integration remains a draft with live approval disabled. Source inspection
and offline tests do not attest an installed Hummingbot core, immutable API image
or Bazzite deployment. The production profit target remains 174 bps.

## Inspected sources

The installed Python client is `hummingbot-api-client==1.5.9`. Its
`ExecutorsRouter.create_executor` forwards `executor_config` as supplied to
`POST /executors/`; decimal prices and amounts must be strings. Its direct
trading API has no time-in-force parameter.

The official Hummingbot core sources were refetched at
[`9af100d6822da7d2d0291a906c730ef172284ee2`](https://github.com/hummingbot/hummingbot/tree/9af100d6822da7d2d0291a906c730ef172284ee2).
The Spectra API source was inspected at
[`84426ffbf9ba09522b4444664f9770236205ae29`](https://github.com/andredepaulnash-cmd/spectra-hummingbot-api/tree/84426ffbf9ba09522b4444664f9770236205ae29).

| Source | Verified behavior |
| --- | --- |
| `order_executor/data_types.py`, `order_executor.py` | `execution_strategy="LIMIT"` uses the fixed positive `config.price`; there is no autonomous conversion to MARKET. `LIMIT_MAKER` clamps to the same-side book quote and becomes post-only; `LIMIT_CHASER` can reprice. |
| `connector/exchange/kraken/kraken_exchange.py` | LIMIT sends `ordertype="limit"` and the explicit price; LIMIT_MAKER adds `oflags="post"`. `buy`/`sell` do not forward extra order options. The request builder does not send `timeinforce`. |
| `dca_executor/data_types.py`, `dca_executor.py` | The supported field is `mode`, with MAKER yielding LIMIT entry and TAKER yielding MARKET entry. Autonomous close orders are hardcoded MARKET. |
| `grid_executor/grid_executor.py` | `triple_barrier_config.open_order_type` selects the entry type, but a crossed BUY grid level is repriced below best bid. A LIMIT setting alone therefore does not make a GRID buy marketable. Whole-grid closure has a hardcoded MARKET path. |
| API `services/executor_service.py`, `services/trading_service.py` | API validates the installed core config class, passes explicit Decimal price through to the connector, and persists the supplied config. This is not a durable idempotency guarantee for duplicate create requests. |

Kraken documents a limit price and native IOC/GTC/FOK options in
[Add Order](https://docs.kraken.com/api-reference/trading/add-order). Native IOC
does not travel through the inspected client/executor/connector path. This
integration must not add invented `timeinforce`, `time_in_force` or
`execution_mode` fields. Supported orders may rest as GTC, and need supervised
cancellation and exact terminal reconciliation rather than an assumed IOC fill.

## Single-parcel adapter

`spectra_core.execution_policy.build_protected_buy_limit_config` converts one
explicit protected BUY parcel to `order_executor` with fixed LIMIT price. DCA
must have exactly one amount and one original reference price. GRID must declare
`min_order_amount_quote == total_amount_quote`, `max_open_orders=1`, and at most
one order per batch. Multi-parcel configs fail closed; no collapse or allocation
change is inferred. Native autonomous barriers are replaced by Spectra lifecycle
coordination, with residual buys held after executor completion.

The caller keeps original capital budget Q, proposal, intent, position, account
and controller identity. It must authorize Q from the native config rather than
the smaller converted principal. No authority is increased. Unused reservation
and any separate governor fee headroom remain reserved until a strict refund
protocol permits release.

The caller supplies explicit activated price/slippage and fee ceilings. Missing
policy ceilings block new protected creates. The fee ceiling is a policy
allowance, not evidence of the account's actual taker fee. Actual fee overruns
must be durably audited and block future cash authorization; a zero or maker-only
ceiling cannot be advertised as an established taker-fee upper bound.

For original reference R, fresh ask A, price tick t, base step s, allowed
slippage b and fee allowance f:

- Price cap: floor-to-tick of `R * (1 + b/10000)`, also constrained by the
  original maximum on native order/GRID scopes.
- Marketable observed price P: ceil-to-tick of A; reject when P exceeds cap.
- Base amount: floor-to-step of `Q / (P * (1 + f/10000))`.
- Maximum principal plus declared fee allowance is at most Q. Venue quantity
  and notional minima must still be satisfied after quantization.

`build_protected_sell_limit_price` rounds the immutable owner/basis/fee-derived
minimum up and a fresh bid down. It rejects when no legal observed marketable
SELL price reaches the minimum. Quantity, ownership, net-profit calculations,
one-shot submission claims and reconciliation remain the dispatcher's duties.

## Trusted venue observation

Client1.5.9 supports `market_data.get_order_book(connector_name, trading_pair,
depth)`, returning `{trading_pair,bids:[{price,amount}],asks:[...],timestamp}`.
At the inspected API pin, the common tracker path assigns `time.time()` when
serializing cached `tracker.snapshot`. That timestamp proves a recent read,
not a recent venue update, so it is insufficient for new protected dispatch.

`mcp_servers.hummingbot_api.tools.execution_book.fetch_protected_kraken_book`
instead performs unauthenticated fresh HTTPS venue reads: filtered
[AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
proves canonical wsname/altname identity, then
[Depth](https://docs.kraken.com/api-reference/market-data/get-order-book)
fetches at most ten levels. Each request has a five-second timeout and observed
duration bound. Scope, ordering, positive finite prices/quantities, nonempty
sides, crossed books, ambiguous mapping and clock regression are checked.
The typed result contains request/receipt times, source and a response hash.
Dispatch must recheck the trusted observation age at its final claim.

The same fresh AssetPairs row supplies exact positive `ordermin` and `costmin`.
Kraken specifies that [volume minimums](https://support.kraken.com/articles/205893708-minimum-order-size-volume-for-trading)
are denominated in the base asset and [cost minimums](https://support.kraken.com/articles/12425041458708-cost-minimum-for-trading)
in the quote asset. The provider requires agreement between the requested pair,
`wsname`, and the row's base/quote identifiers and spot asset classes. It returns
typed Decimal minima, canonical base/quote identity, rules receipt time and the
AssetPairs response hash. Missing, zero, nonfinite or conflicting minimum
metadata blocks a new protected BUY; no hardcoded cost minimum is substituted.

The protected wrapper retains the exact API rule checks, then uses the maximum
of API and fresh venue base/notional minima before fee-aware quantity rounding.
The quantized principal must satisfy the cost minimum; the original budget and
fee allowance cannot be counted toward that threshold. The market observation
time is the older of the rules and Depth receipt, so final freshness covers
both reads. API source inspection confirms its serializer emits the required
increments and LIMIT-support boolean, but the inspected core Kraken formatter
leaves cost minima at zero defaults; those defaults alone are insufficient.

The provider has no credentials or order methods. Other exchanges fail closed
until a trusted provider is implemented. Observation-time marketability does
not guarantee liquidity remains, total fills, fill latency, or realized net P&L.

## Required installed-stack proof

Before live approval, bind an exact Hummingbot core revision and API image
digest, then prove on that installed stack that LIMIT prices persist through
API validation, executor retry/cancel handling and the Kraken request body;
that no MARKET fallback occurs; and that strict terminal orders/trades match
the durable buy/sell journals. Exercise partial fills, cancellation races,
ambiguous creation, outages, fresh book rejection and fee overruns. Current
source inspection and offline fake transports leave those deployment checks
pending.
