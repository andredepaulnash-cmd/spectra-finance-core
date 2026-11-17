# CapitalGovernor Transaction Contract

This document defines the first Alpha 2.0 Governor implementation boundary.

## Reservation rules

- Every initial entry and capital addition reserves gross quote notional plus a 0.40% estimated buy-fee reserve.
- Initial entry and conventional DCA reserve only from the $6,250 INITIAL pool.
- VCAP reserves only from the $18,750 RECOVERY pool.
- A reservation is committed to the per-asset $1,000 gross-buy-notional ceiling immediately, before an order can be submitted.
- A denied reservation changes no position, pool, shard, or ledger state.
- SQLite `BEGIN IMMEDIATE` serializes admissions/reservations so concurrent requests cannot overspend a pool, exceed 74 open/reserved positions, or occupy a third VCAP shard slot.

## Authorization rules

1. Core requests a reservation using a unique idempotency key.
2. Governor persists the reservation and returns an authorization ID.
3. The executor must consume that exact authorization using the same idempotency key and the fixed `kraken_paper_trade` connector.
4. Live connectors, expired reservations, mismatched idempotency keys, unknown authorization IDs, and previously consumed authorizations are denied.
5. Only a confirmed fill can settle a reservation.
6. A submitted reservation cannot be released until the caller confirms that the order was cancelled/unfilled.

## Pool settlement

- Reservation time: `reserved += gross_notional + fee_reserve`.
- Fill settlement: `reserved -= gross_notional + fee_reserve`; `spent += actual_gross + actual_fee`.
- Any unspent difference is released back to the pool.
- Per-asset gross committed notional is adjusted from reserved notional to actual filled notional.
- Fees remain separate from gross position notional and are recorded in the pool spend and later shard/position ledger.

## VCAP shard slots

The database counts `RESERVED`, `SUBMITTED`, `FILLED`, and `ACTIVE` as occupied states. A shard slot is released only by the explicit resolve operation after its complete exit has been reconciled. This is a per-asset constraint.

## Known implementation boundary

The current Governor stores shard identity/status and capital reservations. It does not yet own full per-shard fill/exit accounting or a durable order outbox. Those are separate implementation tasks and must be completed before end-to-end execution is enabled. The present code is domain groundwork, not an executor integration.
