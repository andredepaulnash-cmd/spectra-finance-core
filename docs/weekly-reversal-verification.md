# Weekly reversal verification and recovery scope

## Recovery provenance

The recovered health-review source overlay contains 173 files. Independently
computed Git blob identities show 98 exact matches with published PR42 head
`b736ee1272728f4045cc6746e21d904a5ed65715` and 75 new paths. There are no collisions
and no replacement of existing published production files. All original source
archives remain preserved. The recovered source includes both independently
reviewed successor-health corrections and their regression tests.

The publication overlay is separate from operational source restoration. The
saved campaign's original 95-file source identity must stay unchanged unless an
explicit reviewed migration changes that authority. The publication portability
fix only moves the exact frozen `RepairEvidence` value type into the research
package and changes one import; it does not change the accessor's behavior.

## What the checks mean

Synthetic tests exercise the actual local SQLite/native accounting, allocator,
source ownership, cash funding, replay and fail-closed gates. Process-exit tests
exercise their named interruption points. They do not establish hardware power-loss
safety, universally atomic multi-store commits, production readiness, historical
performance or derivative execution.

The standalone CI workflow targets the current stacked PR base. The pre-existing
CI workflows only target `main` and `spectra-core-phase2`, so they produced no
runs/statuses on the original PR42 head. Absence of a check was not a pass.
`tests/weekly-reversal-suite.txt` explicitly lists the synthetic research and
capital-safety suites run by the new workflow; it is not the full application test
suite. No historical price input or live account is used by those tests.

## Reproducible local test setup

Use Python 3.12 and `uv sync --dev --frozen` in a complete checkout. Run the listed
suites serially with one BLAS thread. The cloud verification used the selected
relevant dependency closure constrained to this same `uv.lock`, with an isolated
HOME and real repository `tests/conftest.py`; no safety fixture was removed.

```
export PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
mapfile -t suites < tests/weekly-reversal-suite.txt
uv run python -m pytest -q --tb=short "${suites[@]}"
```

The complete application environment, frontend build and unrelated application
suites are separate from this focused integration check. Recovered research files
have source-hash dependencies and pre-existing formatting debt; a black check on
the recovered research/active funding subset reports 23 files needing formatting.
They are intentionally not silently reformatted, because formatting changes
pinned source identities. A green synthetic workflow does not imply a full
repository formatting pass.

## Explicit unresolved limitations

- The valid partial-fill precision mismatch described in the integration document
  remains blocked. Tests and guards are not weakened to make it pass.
- Historical readiness still requires exact input/source/protocol/authority,
  predecessor evidence, single-writer/exclusivity and disk-capacity validation.
- An archive quota enforces a finite stop. It cannot prove that unknown future
  SQLite/journal growth will permit the entire campaign to finish.
- Deduplicated byte reconstruction does not authorize a historical replay.
- The owned-spot weekly comparison does not verify leveraged derivatives or a
  claimed percentage of preserved capital.

## Current run evidence

At the 19 October 2026 draft publication:

- All 378 final-tree focused cases passed in 306.07 seconds, covering the
  local repair-evidence type, coordinator crash/recovery harness, retention,
  owned-spot/compounding adapters, entry and successor-health guards, checkpoint
  restart/process tests, and bounded runner/process tests.
- All 88 suites in the explicit manifest collect successfully: 1,723 cases.
- A separate 156-case batch passed in 74.52 seconds (52 retention, 102 existing
  Phase 1/capital-safety, and two preserved health-audit cases). These overlap
  the focused suite and are not summed as unique tests.
- All 27 independent archival audit cases passed against the publication module
  in 3.47 seconds. The only difference from its originally reviewed freeze is
  import ordering; the non-import AST is identical.
- The unshortened recovered-source regression completed in 5,809.29 seconds:
  1,556 cases and 45 subtests passed. Its only two failures were the reproduced
  subprocess-helper import problem. Both fixed cases and the full 179-case
  coordinator suite passed in the final-tree focused run. All long end-to-end
  caller scenarios passed; no test or economic guard was weakened.
- Exact-commit GitHub CI is pending until its check reports completion. The
  existing absence of CI on the original head is not reported as success.

This is a dated publication checkpoint, not a declaration that the complete
integration has passed. The PR body and checks report subsequent completion or
failures. No earlier recovered test count is represented as a current rerun.
