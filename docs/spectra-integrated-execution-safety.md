# Integrated execution and cash safety candidate

This draft combines PRs #27–#30 on the production base below. It preserves the
174 bps production profit target. PR #26 and its protected research inputs are
outside this change. No production merge, live deployment, order submission,
strategy promotion or holdout inspection is part of this validation.

| Input | Immutable commit |
| --- | --- |
| Production base | `6e894fa9c2b55384d0f6120f41ca91892e1fe207` |
| #27 protected recovery exits | `0ad928fa5d6f51ed2f402bb58c360d8a9f6b4a36` |
| #28 protected whole exits | `d00dfbd7a266b46593e563e593c370ade84fd66a` |
| #29 parked cash authority | `b5f9ee170dc56b72c8a72404ef5dbf8bc97c4ec4` |
| #30 ENTRY, residual and retirement safety | `8720e5df47ad395eb29c2d4eb0b02e812cc43163` |

#27–#29 are cumulative. #30 was independently based on production. The combined
code explicitly composes their overlapping transactions rather than applying
one branch over the other.

The earlier files under `docs/research-results/` remain historical receipts for
their original PRs. Their test counts and remaining-work checklists are not the
release checklist for this integrated candidate. Use this document and the
candidate's exact-head validation receipt for current status.

## Resulting behavior

- A canonical ENTRY identity claim, exact scoped reservation, protected buy
  submission evidence and productive cash burn commit atomically. PARK and
  unclassified venue balances cannot fund an ENTRY or recovery authorization.
  An ambiguous create retains its claim and cash burn across restart.
- Every new Spectra buy requires explicit price and fee allowances, including
  before cash authority activation. One funded DCA/GRID parcel becomes a
  fixed-price BUY OrderExecutor LIMIT using venue rules tightened by fresh
  Kraken pair minima and a fresh public book. Missing policy,
  unsupported scope, excessive drift or multiple parcels refuse before commit.
  The original approved quote budget remains the authority; conversion does
  not create additional spending authority.
- Exact submitted BUY config and policy are saved durably. Actual orders,
  cumulative fills, fill prices and fees are audited against them. A breach
  records actual inventory and cash first, then keeps reconciliation and new
  spending blocked. Replays do not duplicate fills or erase violations.
- Partial and whole profit exits retain exact ownership, fee-aware minimum
  LIMIT price and approved cash purpose. Pending sale proceeds remain held;
  PARK settlement cannot become productive cash. A floor or fee breach records
  the real sale while refusing a successful terminal certificate.
- Terminal certificates require exact scoped receipt/config, terminal executor
  and orders, cumulative volume, confirmed trade coverage and durable fill
  journal. A lagging trade endpoint cannot certify retirement. Protected BUY
  conflicts and unsettled cash also prevent identity replacement.
- Unsellable remaining units retain their exact basis and fees in RESIDUAL.
  Classification does not credit sale proceeds or erase inventory. Restart
  completes a persisted retirement handoff without dispatching a duplicate
  sale, including while HALTED reconciliation is permitted.

## Validation boundaries

The dedicated Spectra workflow runs the composed baseline and integrated
regression modules with Python 3.13 and `uv sync --dev --frozen`, including the
repository's shared `tests/conftest.py`. The validation receipt identifies the
exact candidate head, local result and GitHub full-checkout result. Independent
adversarial probes additionally cover bounded cash arithmetic, late terminal
volume and actual BUY policy breaches inside the original quote budget.

LIMIT execution preserves a price boundary; it cannot guarantee execution time,
liquidity, full fills or net profit. Native IOC is not exposed end to end in the
inspected Hummingbot transport, so no IOC field or MARKET fallback is invented.
Orders may rest as GTC and require supervised cancellation and terminal proof.

Unused or canceled BUY authorization remains conservatively burned until an
explicit reviewed refund protocol releases it. Partial SELLs support a proven
terminal predecessor and a new submission generation. A canceled whole SELL
with sellable remainder keeps its claimed manifest and inventory; automatic
whole-sale replanning is not introduced here.

If another exact owned BUY fills after a whole SELL was dispatched, the
reconciler records the original SELL against its immutable selected lots and
retains the additional BUY inventory. Cash settlement and terminal proof stay
held because the frozen whole-sale parcel cannot certify that new ownership.
The existing sale is not recreated or expanded to consume those extra lots.

Installed Hummingbot core/API image and Bazzite semantics remain unattested.
Live approval remains disabled. Before activation, pin the deployed core and
API image, exercise protected LIMIT price propagation and cancellation/retry
behavior on that stack, and bind reviewed policy allowances. Source capability
evidence and exact transport limitations are in
[spectra-limit-execution-capability.md](spectra-limit-execution-capability.md).
