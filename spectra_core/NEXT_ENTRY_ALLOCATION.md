# Equal-share profit allocation for later eligible entries

`next_entry_allocator` is a deterministic entitlement ledger, not a wallet or
order service. A verified settled-profit bridge must reserve the actual cash.
The native bridge is disabled by default and, in its grant-only version, rejects
every spending reservation or debit. Nothing is automatically installed into an
executor or scheduler.

Each immutable membership snapshot has named position-allocation beneficiaries
and exactly one VCAP beneficiary. Every member gets an equal share. Allocation
uses exact integer accounting atoms and carries indivisible residuals within the
same membership snapshot. No 75/25 split or account-high-water gate is imposed.

Eligible profit is the positive fee-net realized result of a whole confirmed
execution receipt, including all disposed lots, attributable entry fees and exit
fees. A profitable lot cannot be extracted from an overall losing receipt.
Deposits, returned principal and open unrealized gains do not qualify. A losing
receipt leaves its loss with its source owner.

Allocation is immediate once the confirmed settlement is admitted. Acquisition
is deferred to the next independently eligible native entry. Subminimum balances
remain earmarked; they do not create penny orders or automatic held-position
top-ups. Native risk, owner-cash, reserve, minimum, sizing and signal guards remain
necessary. A reservation needs an approval strictly later than its profit grant;
ambiguous same-second ordering fails closed.

Operations and economic proof IDs are idempotent. Reserve, actual-fill commit and
terminal release conserve every source atom. Exact operation replay may recover a
previous commit after its original proof expires; a new expired operation cannot
create authority. Component crash tests cover process exits at SQLite transaction
boundaries. They do not by themselves prove a multi-store executor integration.

The research proposal uses fifteen fixed logical asset allocations plus VCAP,
sixteen equal shares. This membership granularity is an explicit configuration
choice rather than an assertion that each partial lot is a separate beneficiary.
