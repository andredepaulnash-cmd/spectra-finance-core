# Existing six-month hedge research inputs — 2026-10-10

The original January–June spot archives **already contain Bitcoin history** as
the exact `ohlcvt/XBTUSD_5.csv` member. The earlier prepared 619-asset trading tape
excluded BTC and ETH. That exclusion did not prove the original archives lacked
a separate benchmark. No new external OHLC, funding or margin history was
downloaded for this preparation.

All six already-owned monthly ZIPs match the sizes and whole-archive SHA256 pins
retained in PR #34. Only their exact XBTUSD member contents were read. Kraken's
[Bitcoin naming documentation](https://support.kraken.com/articles/360001206766-bitcoin-currency-code-xbt-vs-btc),
checked October 10, explains XBT and BTC refer to Bitcoin. This identity is used
only for the benchmark; the excluded trading universe remains unchanged.

| Prepared historical input | Verified observations | Timing and limits |
| --- | --- | --- |
| Bitcoin/USD benchmark, January 1–June 30 | 52,128 calendar slots; 52,122 genuine five-minute bars | Six missing bars remain explicit, with blank prices and `present=0`; no forward fill |
| Saved native PARK account observations | 4,344 saved hourly observations plus the exact June boundary | First snapshot January 1 00:05 UTC; then hourly; terminal July 1 00:00 UTC |

The benchmark CSV identifies both bar opening and completion. Its OHLCV and trade
count may enter a decision only at opening plus 300 seconds. A subsequent opening
fill may use that opening price, but not that bar's later high, low, close or
volume. The six missing opening times are February 4 at 11:30, 11:35, 11:40 and
11:55 UTC; March 9 at 08:05 UTC; and May 7 at 07:05 UTC. Missing adverse prices do
not prove an existing short stayed safe during those intervals.

The PARK observations use the actual saved control's cash, basis, marked exposure
and equity. Each source matches its previously published PR #34 hash. Every
observed equity equals cash plus marked inventory within $1e-18. The exact June
boundary agrees between the separately pinned summary and owned-lot inventory;
it is appended only at its actual terminal time and never used to initialize a
January decision. Original modeled spot fees already embedded in equity must
not be deducted again.

This control began with $2,000 and zero holdings. Its already-known saved June
equity was $1,898.277183091943957381228996. That is prior development evidence,
not a new hedge result, a venue balance or a prospective result. The small
ordinary cash balance at June or during July cannot establish that every
January-through-June hedge design lacked funds.

These observations support a useful **historical paper comparison** once its
causal rules and assumptions are frozen: compare the saved PARK control with
25%, 50%, 75% and 100% hypothetical Bitcoin protection, counting declared costs
and finite dedicated capital consistently. Added research cash must be identical
in all arms and must not be counted as profit. Full funding, margin and execution
histories are not necessary to calculate explicitly assumed cost sensitivities.
They are necessary before calling those sensitivities an actual historically
executable venue result.

The saved account CSV does not expose every five-minute spot change, outstanding
order obligation or cash owner. A paper study using it must label its hourly
exposure observations and assumptions between snapshots. It cannot certify that
the original $2,000 account could fund a hedge while preserving every native
spot event. This preparation performs no new strategy replay and reconstructs
neither the 1.55 GB spot array nor an event-level pending-cash ledger.

Actual derivative prices, then-available contracts, funding or borrow charges,
historical fee tiers, margin and liquidation rules, execution conditions,
account entitlement and collateral recourse remain separate requirements for a
venue-realistic funded replay. Spot Bitcoin bars alone do not establish those
facts. The previously examined U.S. contract's first trading day is May 16, 2026;
a January start using that instrument would be hypothetical. Its source metadata
remains in PR #34 and was not reinterpreted as January availability.

The existing prototype already ran five fictional scenarios across five arms,
or 25 mechanical runs. Those verified accounting behavior. This preparation
supplies genuine historical benchmark observations for the separate paper
comparison; it does not claim any hedge is profitable.

The two portable preparers require the exact existing pinned source files. They
parse immutable byte snapshots after verification, refuse symlink input files,
and do not download data. Twelve admission tests cover altered pins, missing
genuine observations, wrapped-token substitution, duplicate and invalid bars,
cash conservation, source symlinks and terminal-only boundary use. An independent
review also checked every one of the 52,122 raw Bitcoin rows against the canonical
tape and all six source ZIP hashes.

Reproduce the benchmark with `research/danger_room/prepare_existing_btc_history.py`
and the saved account with `research/danger_room/prepare_saved_park_history.py`.
Both accept `--source-directory` and `--output-directory`. The neighboring
receipts contain exact source, derived-tape and validation hashes. Large source
archives and derived market/account CSVs are retained as research inputs rather
than embedded in these source documents.

No collecting October 9–November 9 holdout outcome was inspected. Production,
financial freeze, holdout protocol and the fixed trading universe remain intact.
