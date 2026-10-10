# Fresh-$2,000 prospective replay adapter

This isolated research adapter binds the October 9 financial/source freeze to
the preserved native five-minute replay. It starts each of the five predeclared
arms with $2,000 ordinary cash, $0 PARK cash, no inventory, no pending orders and
no deposits or withdrawals. It never hydrates the historical June checkpoint.
Production source and the frozen collection protocol remain unchanged.

The comparison remains `OFF`, `SHARED_RAW`, `SPLIT_RAW`, `SHARED_QUALITY` and
`SPLIT_QUALITY`. The native financial freeze owns its 50-asset cap, $10.25 entry,
25% physical reserve, 5% pair basis cap, 40-basis-point fee per side, five-basis-
point adverse slippage, generic $5 minimum and 1% modeled volume participation.
Shared PARK authority is at most $100 outstanding acquisition basis plus
claims; split authority is at most $50 ENTRY and $50 RECOVERY without borrowing.
These are this frozen experiment's rules, not a change to broader portfolio
breadth policy or a claim about current venue fees/minimums.

All 11 frozen candidate/collector sources are checked against the immutable
protocol SHA256 before they are consumed. Twenty-seven recovered Python
members are individually checked against its native source manifest. Only
verified bytes are copied into a private source snapshot before any native
imports; mutable replacement modules and extra recovered files cannot enter
that snapshot. The rebuilt native kernel must equal the exact frozen
`9260c013c5c85088f0098d76d39f754b456d581d93e8dda98c5bbf0711d6b838`
body. The vendored source receipt preserves original archive/member identities;
fixture CI needs no network or source archive downloads. Eventual genuine
scoring additionally re-hashes both original whole source archives.

The adapter makes three explicit changes to the unchanged native kernel before
the already-frozen PARK overlays: passive warmup without financial activity,
an accounting/ownership observer after native intent scheduling, and a sample
index correction so the native initialization metric addresses evaluation-only
samples. Exact before/after source edits and the compiled kernel hash are saved
for every arm. Native ranking, fingerprint/recovery/tranche/partial planners,
fee calculations, execution floors and next-candle execution rules are retained.

Warmup is 24 hours of causal input and never contributes trades or returns.
The actual freeze was 15:36:31 UTC; the first eligible evaluation bar starts
15:40 UTC. The 15:35 bar spans the freeze and is excluded from both completed
pre-freeze warmup and eligible evaluation. Missing exact anchors deny the
frozen quality predicate; gaps are never filled. All 619 fixed identities remain
in the input and audit. Assembly reports unknown/missing status and bars rather
than silently dropping assets or backdating a later online observation.
Fixed membership was already known before the freeze. For conservative
execution admission, evaluation candles additionally require a captured online
AssetPairs observation no later than their start. This can censor otherwise
known members' early bars; it does not reconstruct their true listing dates or
unobserved offline/status intervals. The earliest captured online observation
does not prove continued eligibility throughout the window. These limitations
and censor counts must accompany eventual all-arm economic interpretation.

Each arm runs in a fresh interpreter with a fresh native book. Every completed
evaluation frame checks cash/basis/realized conservation, physical cash versus
PARK/ordinary ownership, exact native parent lots and outstanding PARK caps.
Reports separate realized/open P&L, fees and modeled slippage, retain terminal
asset quantities and actual mark ages, and include failures/incomplete runs.
The matrix receipt records every arm before execution and preserves completed,
failed and unrun arms when an integrity failure or timeout stops the matrix.
Activation, inactivity and observed/missing evaluation rows are reported
separately from mechanical `PASS`; no economic winner is selected by this code.
Decimal conservation uses the inherited `1e-18` tolerance; reported balances
are not clamped to hide rounding residuals. Whole/partial fills must preserve
the frozen 1% net profit floor. These modeled five-minute fills do not establish
venue execution finality, dust tradability, live persistence or predictive edge.

Use source readiness without reading a market tape:

```bash
python3 research/holdout_replay/replay.py readiness \
  --repo-root . \
  --native-receipt research/holdout_replay/vendor/source-recovery.receipt.json
```

Run the constructed-fixture tests without downloads:

```bash
python3 -m unittest discover -s research/holdout_replay/tests -p 'test_*.py' -v
```

The public assembly/evaluation entrypoints check the actual UTC clock against
November 9, 2026 at 00:00 UTC before any prospective capture/prepared payload
is read. There is no public clock override. The collecting holdout remains
unopened and unscored. Constructed candles exercise entry, recovery, partial
and whole disposal, genuine modeled PARK-funded buys, ownership, causal
prefix invariance and repeatability; their prices and returns are fictional.

After the embargo has ended, assembly and evaluation require independently
reviewed original capture provenance and the exact preserved source archives:

```bash
python3 research/holdout_replay/assemble_captures.py assemble \
  --repo-root . --capture-dir /path/to/capture --output /path/to/prepared
python3 research/holdout_replay/replay.py evaluate-matrix \
  --repo-root . \
  --native-receipt research/holdout_replay/vendor/source-recovery.receipt.json \
  --source-archives /path/to/original/source/archives \
  --prepared /path/to/prepared --output /path/to/results --seconds 900
```

Code and fictional-fixture validation do not prove that the collecting real
captures form a complete usable evaluation tape. Genuine capture integrity,
coverage/censoring and post-end all-arm scoring remain a November closeout task.
Prepared receipt hashes detect subsequent byte changes. They do not independently
prove market authenticity, acquisition time, completeness or historical online
eligibility; original capture/source provenance still needs independent review.
No research result grants account, order, production merge or deployment authority.
