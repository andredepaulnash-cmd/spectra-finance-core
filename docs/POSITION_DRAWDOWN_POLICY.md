# Position Drawdown Policy

The initial entry stores an immutable reference price. Current drawdown is always calculated as:

`current_price / reference_price - 1`

DCA/VCAP additions, shard fills, partial exits, and shard recycling do not change this reference price.

## Decision order

1. If drawdown is at or below -24%, return MANUAL_REVIEW and authorize no automatic addition.
2. Otherwise, if the next uncompleted DCA threshold has been reached, select that exact $20 DCA step.
3. Only after all three DCA steps have settled as filled can VCAP be considered.
4. VCAP steps are sequential and must match the exact configured step ID, drawdown threshold, and amount.
5. A step cannot be skipped, resized, or repeated after completion.
6. VCAP may have at most two reserved/submitted/filled/active shard slots per asset.

CapitalGovernor recalculates drawdown from the stored reference price and the current price supplied with the proposal. It independently verifies that the requested action is the next eligible step and that its amount matches the frozen ladder. Core planning is advisory; Governor authorization is mandatory.
