# Spectra research workflow

The immediate research goal is to deploy recovered quote cash into qualified new
entries and selected recovery purchases without recreating the declining-market
losses caused by immediate unrestricted reuse. A passing engineering test proves
that money can flow correctly; it does not prove a profitable trading policy.

## Preserve the control

The current production branch is `spectra-core-phase2`. The branch was read from
GitHub on 2026-10-09 and remained at PR #25 merge commit
`6e894fa9c2b55384d0f6120f41ca91892e1fe207`, with tree
`af3b20918fd7733d865cecfb313ecd71e873b3c7`.

The six-month `hard_100_park` control exists in a separately archived research
runner. It is not identical to the checked-in rotating-fleet replay or the live
runtime. Its whole-position and recovery-partial execution floors are both 100
net basis points, its partial-sale proceeds remain quarantined, and PSSM,
DTSS and DTSSR are off. Preserve its original source, policy, input hashes and
June checkpoint. Do not modify the control to improve a comparison result.

The exact prior evidence references are in
[`research-protocols/activation-evidence-2026-10-09.json`](research-protocols/activation-evidence-2026-10-09.json).
Large input tapes and checkpoint archives remain external, content-addressed
inputs; they do not belong in Git. A machine-specific hydration adapter remains
archived research code and is not a production persistence API.

## Separate the work

| Change | Location | Required evidence |
| --- | --- | --- |
| Evidence verification and experiment controls | `research/` | Tests for identity, integrity and bounded failure behavior |
| Frozen protocol and findings | `docs/research-protocols/`, `docs/research-results/` | Input/source hashes, conditions, limits and verdict |
| Experimental parked-cash allocation | Explicit offline research setting | Grants, claims, actual fills, disposal and accounting |
| Production allocator or exchange execution | Separate implementation change | Validated strategy, protected execution, restart and paper behavior |

An engineering commit preserves code. It does not enable a new live strategy.
Keep experimental policies disabled outside the explicit experiment entry point.
Do not change recovery ladders, exits, asset-selection features and cash routing
in the same matched comparison. Execution adapters must remain unable to spend
an unclaimed dollar or dispose of an already-sold lot.

## Spend compute in stages

1. **Freeze the experiment.** Record the objective, exact policy parameters,
   source hashes, data hashes, checkpoint identity, time window, runtime budget,
   output paths and promotion criterion before observing the new result.
2. **Prove activation.** On a bounded window, require a real modeled grant,
   claimed capital, a subsequent buy fill, an owned recovery/entry lot and its
   correct disposal or visible terminal inventory. An authorization Boolean or
   a hypothetical gate check does not count as a fill.
3. **Check accounting.** Track physical cash separately from its authority
   buckets and pending claims. Preserve exact partial ownership and ensure
   every fill consumes or releases capital once. Stop on the first violation.
4. **Compare the five predeclared arms.** PARK control; qualified entries only;
   selected recovery only; coordinated entries and recovery; tighter combined
   budget. Keep their data, state, execution assumptions and unchanged strategy
   parameters identical. Freeze numerical budgets in the executable protocol.
5. **Decide before extending.** Publish one comparison table. Extend only an
   activated, audited candidate to the full six months, later data and higher
   execution costs. A blocked arm ends with its decision evidence rather than
   another expensive replay of the same blockage.

The Governor is the single authority for capital and inventory. Entries and
recovery compete through requests and reservations; they cannot independently
claim the same funds. A dedicated experimental permission to spend parked cash
must specify eligible purposes, limits, expiry/cancellation and return routing.
Total wallet cash is not the same as deployable cash. A profitable child sale
returns its released principal and profit to the declared bucket, once.

## Required run receipt

Each run must distinguish **planned**, **started**, **completed**, **blocked** and
**failed**. Its receipt includes:

- Frozen trial identity and parent control identity.
- Executed source hashes, including the exact executed copy if source changed
  after a process started; data and starting-state hashes.
- Wall-clock start/end and elapsed time, frames completed and cutoff reached.
- Proposed opportunities, grants, claims, orders, fills and exact denial reasons.
- Realized P&L, marked unrealized P&L, total equity, fees, drawdown, exposure,
  remaining authority buckets and pending claims.
- Accounting/ownership checks, activation checks and any truncated telemetry.
- Comparison verdict and a concrete next decision.

Save completed results before starting another stage. Preserve failed and blocked
trials as well as successful ones. An archived rolling digest is not reproduced
merely because selected account fields match. State explicitly which fields and
timestamps were checked, and preserve the validator source and receipt.

## Verified discovery on 2026-10-09

The original candidate resumed from its exact June state and replayed 8,640
five-minute frames through July 30. All 29 modeled fills matched the archived
continuation, including three recovery purchases and one recovery-partial sale.
All 720 archived sample points matched on cash, basis, marked inventory and
equity (2,880 comparisons). The simulation itself took about three minutes;
restoring and verifying inputs required additional time.

This reproduced an existing policy. It did not demonstrate adaptive profitability.
The later experimental overlay introduced three separate constraints: its
history-coverage rule rejected all three known native recovery purchases, its
wallet-wide freshness check failed at every sampled state, and its parked-cash
grant was restricted to new entries. These barriers must be measured separately.
Passing all listed shadow conditions is still not an authentic authorization:
pending claims, native planning and conservative valuation can independently
prevent a trade.

The evidence utility checks the saved diagnostic ZIP without extracting or
executing source:

```bash
python research/verify_diagnostic_evidence.py /path/to/Spectra_Activation_Diagnostic_Evidence_2026-10-09.zip \
  --sha256 79287402a3f211427706a837998496b534c9d033e5deb27a7372bf6d42be7feb
python -m unittest discover -s research/tests -p 'test_verify_diagnostic_evidence.py' -v
```

Its PASS proves that pinned archive and all declared payload bytes match. It is
not another historical replay or a production-release decision. The utility uses
only the Python standard library and never contacts an exchange.

## Reporting and promotion

Report completed work and unresolved limits before the next experiment. The main
comparison is total marked account value after modeled costs, alongside drawdown,
exposure and useful capital availability. More turnover, higher realized profit,
or a larger count of winning exits can coexist with worse total equity.

After profitable redeployment survives extended validation, produce a separate
production implementation delta. Profit-protected exit semantics, durable
accounting, point-in-time exchange constraints and restart/idempotency behavior
still need their own evidence. A research commit does not establish those claims.
