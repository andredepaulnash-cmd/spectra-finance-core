# Alpha 2.0 Governor State Model

The first durable Governor uses SQLite with write-ahead logging, FULL synchronous mode, foreign keys, a 30-second busy timeout, and `BEGIN IMMEDIATE` around each capital mutation.

## Tables

- `pools`: separate INITIAL and RECOVERY budgets, reservations, and settled spend.
- `positions`: immutable position identity/reference price, lifecycle status, and cumulative committed gross buy notional.
- `reservations`: unique idempotency key, authorization ID, pool, amount, fee reserve, expiry, submission state, and actual settled fill/fee.
- `vcap_shards`: per-asset shard slot status linked to its reservation.
- `events`: append-only domain events for reservation, authorization, fill settlement, shard activation/resolution, release, and position closure.

## Atomicity and accounting

The Governor serializes all admission and capital mutations with `BEGIN IMMEDIATE`. A reservation consumes both pool availability and per-asset exposure before the order can be submitted. A settled fill replaces reserved gross notional with actual gross notional and moves actual cost/fee from reserved to spent. A no-fill release requires explicit confirmation; a submitted order cannot simply be timed out and forgotten.

The 74-position ceiling counts both RESERVED and OPEN positions. A partial unique index prevents two simultaneous positions for the same asset while permitting that asset to be considered again after a reconciled close. A position close does not itself credit sale proceeds back to a pool; actual sale settlement must be recorded separately before capital is reusable.

## Scope still outstanding

This is the first durable reservation/governance layer, not the final ledger. Before executor integration it still needs:
- persisted order outbox and order-ID binding before network submission;
- expiration/reconciliation recovery for reservations left behind by process failure;
- schema versioning and backup/restore;
- end-to-end idempotency across API, Hummingbot, and Core.
