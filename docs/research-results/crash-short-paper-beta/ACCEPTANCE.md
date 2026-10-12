# Paper short implementation acceptance — October 12, 2026

## Scope and disposition

The exact frozen BTC/ETH fresh24h candidate is implemented as an opt-in local
paper/synthetic book. There is no PR40 dependency, live adapter, deployment,
current-market evaluation or holdout use. The base is default commit
`6e894fa9c2b55384d0f6120f41ca91892e1fe207`; existing production blobs are unchanged.
The only existing file changed is the focused CI workflow, which retains all 29
original test files and adds four paper test files.

## Executed acceptance

- **397 passed** across that exact 33-file focused list, with the pinned historical
  signal fixture enabled (CPython 3.12.14). This includes **149 new tests** and the
  existing focused spot/VCAP regression suite. All-repository tests were not run.
- Normal repository conftest/package collection was used. Existing preference
  initialization requires a writable HOME; the first local collection hit the
  cloud sandbox's read-only home, then passed with an isolated temporary HOME.
  No baseline source was changed to bypass that environment issue.
- New/changed Python: Black, isort, compileall and `git diff --check` pass.
- The actual `python -m spectra_core.crash_short_runtime --enable-paper` command
  processed **52,128** frozen intervals to a durable SQLite book. The pinned
  verifier compared all account fields, **181** signals, **12** modeled fills and
  **1,154** entry opportunities against the existing frozen BTC/ETH reference.
  All passed; maximum absolute account discrepancy was **9.1e-13 USD**, from
  Decimal-versus-binary64 rounding (comparison tolerance 1e-8).
- Final standalone synthetic CLI fixture opened two positions, then a separate
  process resumed close-only, verified the committed prefix and closed both at
  eligible observed opens. Final synthetic equity is arbitrary test evidence,
  not an expected return.
- Full-file restart/duplicate replay of the frozen book produces the same final
  receipt and leaves the database byte-for-byte unchanged. No duplicate fills,
  costs, funding or ownership events are created.

The machine-readable [equivalence receipt](equivalence-receipt.json) contains the
per-field errors. [Reference hashes](reference-hashes.json) identify the exact
historical evidence. Large raw data and SQLite databases are not committed.

## Independent review before commit

A separate reviewer inspected frozen policy/effect ordering, state ownership,
accounting, input provenance, default-off isolation, the normal entrypoint,
controls, the verifier and documentation. They added ten independent adversarial
regressions and reported **no remaining blocking findings** after these fixes:

- Parse the same immutable input bytes that were hashed; no file-reopen race.
- Bind persisted state, receipt content and retained BTC history to checksums;
  fail closed on edited cash, missing cycles/history, or corrupted reports.
- Read-only status reconciles through one SQLite snapshot.
- Verify exact duplicate/conflicting/concurrent-cycle behavior.
- Use an explicit 34-digit monetary Decimal context, independent of caller traps
  or rounding settings.
- Provide bounded immutable-input stop/resume controls without rewriting prior
  observations or historical controls.

Their focused independent run passed all 149 new tests with the pinned historical
signal check; all ten adversarial tests passed again after the final history
checksum call-site correction. Full-window account equivalence and the wider
397-test regression run were separately executed by the implementation worker.

## Economic and deployment limits

The frozen terminal gross / cost / funding equities of approximately $2,446.86 /
$2,408.37 / $2,382.52 are reproduced only to prove implementation equivalence.
They are a postselected fractional spot-price proxy, not executable perpetual or
futures results or an independent validation. Actual whole-contract sizing,
listing availability, collateral/margin/liquidation, fees and funding are not
modeled. Unknown actual metadata is blocked; even populated metadata is
unsupported by this release. Negative modeled equity is preserved.

Hosted CI is a separate post-publication check; this document records local
acceptance, not a CI or deployment attestation. Actual Bazzite install/startup and
installed-artifact verification remain pending host access. Draft publication
does not authorize merge, activation, deployment or any live order.
