# Parked-cash runner observation revision

This is a runtime-observation and reporting correction. The funding policy,
native selectors, recovery planners, exit targets and frozen experiment protocol
are unchanged. It does not establish a new performance result.

The original ENTRY_ONLY attempt reached its 420-second simulation budget after
6,355 of 8,640 frames. Its actual rows and activation evidence remain preserved
externally. It did not complete the month and must not be compared against a
completed control as a full-window result. Missing source journals also prevent
claiming that this attempt can safely resume from its last saved CSV row.

The runner previously rebuilt full owned-lot snapshots and rescanned every
parent on every candle, including candles with no fills. The observation revision
retains account identities, nonnegative settled source balances, parked-source
cap checks and account CSV output on **every frame**. Exact native/source lot
checks occur on the first frame, each fill batch, daily boundaries and the final
boundary. Source ownership changes through the native fill methods; claims
retain their original funding checks.

Daily progress output now prints compact counters, and activation detection scans
new release events only. Maximum and mean position counts come from observer
accumulators. The actual `FleetSample` dataclass has `active_positions`; that field
was valid. The earlier OFF reporting exception concerned the different field
name `FleetResult.rejections`, rather than `rejection_counts`.

The executed runner and adapter bytes are captured before hydration. A bounded
failure additionally exports actual fills, source events, current lots, positions
and a source snapshot. Such failure evidence is not a resumable checkpoint or a
complete-window performance result. Snapshot export currently validates source
ownership and can itself fail following an ownership-corruption exception; the
observed timeout path is the supported case. Do not interpret a missing failure
export as evidence that the underlying account passed checks.

| Source | Frozen SHA-256 |
| --- | --- |
| Revised runner | `4d1a36c1ced979d128acd589150d0702924527491dfd3bbd29b6935a88fd710d` |
| Unchanged funding adapter | `b9e7fcd4c0f3ebff508da90e87c7ee1ba19abacb7d6796da3166bf32d8b4cbfb` |
| Unchanged protocol | `c187a8f4793c016b82ab39d6e2c5bbaba9fd9f45543036a49be796b85eabc09b` |

Independent aggregation of the completed OFF account CSV covered all 8,640 rows
and matched the reported maximum of 37 positions and mean of approximately
36.818287 positions. This checks reporting against saved observations; it is not
a second execution of the strategy. The existing 16 allocator mechanics checks
still concern the unchanged funding adapter. Original executed sources and
receipts remain external evidence rather than additional source archives in Git.
