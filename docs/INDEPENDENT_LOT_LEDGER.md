# Alpha 2.0 Independent Lot Ledger

Every initial entry, conventional DCA addition, and VCAP addition is represented by an independent `position_lots` row. A VCAP lot ID is its shard ID; DCA lots have a position/step identity; the initial entry has its own lot identity.

Each lot stores:
- gross buy notional;
- buy fee in quote currency;
- filled and remaining base quantity;
- remaining cost basis including buy fee;
- gross sell proceeds;
- sell fees;
- realized P&L;
- lifecycle status and its source reservation.

The Core binds a Hummingbot order ID to the submitted CapitalGovernor reservation before it will settle the fill. Fill quantity multiplied by average fill price must reconcile to gross quote notional and may not exceed the reserved notional or fee allowance.

A partial sell allocates cost basis pro-rata to the quantity sold. Only the actual net sale proceeds (gross proceeds minus sell fee) are credited to the originating capital pool after the sell fill is reconciled. A VCAP shard slot is released only when its lot has zero remaining quantity and its sell fill has been recorded. Position closure requires every initial, DCA, and VCAP lot to be fully exited.

The current ledger is durable for completed/terminal order reconciliation. Partial open-order fills remain reserved until the order reaches a terminal state; restart reconciliation and order-ID binding are required to complete settlement. Full outbox/replay recovery is still outstanding.
