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
complete-window performance result. The historical runner's validating snapshot
could itself fail following an ownership-corruption exception. A later
repository-only logging follow-up addresses that failure-reporting limitation;
it is documented below and was not executed in the historical matrix.

| Source | Frozen SHA-256 |
| --- | --- |
| Historical observation runner | `4d1a36c1ced979d128acd589150d0702924527491dfd3bbd29b6935a88fd710d` |
| Unchanged funding adapter | `b9e7fcd4c0f3ebff508da90e87c7ee1ba19abacb7d6796da3166bf32d8b4cbfb` |
| Original 420-second protocol | `c187a8f4793c016b82ab39d6e2c5bbaba9fd9f45543036a49be796b85eabc09b` |

Independent aggregation of the completed OFF account CSV covered all 8,640 rows
and matched the reported maximum of 37 positions and mean of approximately
36.818287 positions. This checks reporting against saved observations; it is not
a second execution of the strategy. The existing 16 allocator mechanics checks
still concern the unchanged funding adapter. Original executed sources and
receipts remain external evidence rather than additional source archives in Git.

## Separate budget and failure-reporting follow-up

The observation revision's next ENTRY_ONLY attempt stopped at its unchanged
420-second budget after 8,542 frames, 98 short of the complete window. Both
incomplete attempts remain archived. The final operational protocol increases
only `execution.maximum_simulation_seconds_per_arm` from 420 to 600; all financial
settings are identical. The matrix driver passes that frozen budget explicitly.
This is an execution-budget revision, not outcome-based strategy tuning.

The subsequent repository runner writes `failure_receipt.json` before closing
the account CSV or attempting validating exports. Receipt amendments use staged atomic replacement to preserve the first valid
receipt even if an amendment fails. Each export failure is recorded
beside the original exception, other available evidence still exports, and the
caller uses a bare re-raise to preserve its original simulation error. Pending
claims are not expired on this path. No full `result.json` is produced. An
unwritable filesystem can prevent the receipt itself being saved; that failure
is reported to stderr without replacing the original exception.

This patch follows isolated research commit
`54bea18a0ffa2427a20a531473f55efe04533329`. The historical matrix continues to use
the observation runner pinned above; do not attribute its results to the later
logging source. Six standard-library injection tests cover corrupt snapshots,
payload serialization, CSV closure, failed storage, interrupted receipt amendments and the runner's original
exception/claim-preservation contract. The eight prior integrity tests also pass.

| Follow-up source | SHA-256 |
| --- | --- |
| Repository logging runner | `495d4b590d99bcb3151db3f6d9dc30c1f0213392d4ff2146821bfd4ad1896f43` |
| 600-second protocol | `3939a3740d697f6c7c587ffd392eef8a2eaf12ec398b516bdc9f9e0c12aa0e2f` |
| Explicit-budget matrix driver | `31ec5a4ef269bca17e8fbc9930047f4c6d1fe7caf7fe2d284a55ae21636849c0` |
