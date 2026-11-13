# Spectra Release Manager

This directory is the source of truth for the platform's intended component
inventory and release policy.

## First milestone: observe before changing

`components.json` records the upstream repositories and their update models:

- Spectra Core: Spectra-owned, SemVer release tags.
- Condor and Hummingbot API: continuously deployed upstream branches; capture
  commit SHAs as candidates, never install directly from `main`.
- Hummingbot Client and Gateway: versioned upstream releases; capture tags as
  candidates.

The approved refs are intentionally `null` until the currently installed
versions have been measured and compatibility-tested. Do not invent a baseline
or label an unverified version as approved.

## Check upstream candidates

From the repository root:

```bash
python scripts/check_upstream_releases.py
```

The command writes `release-candidates.json` (ignored from version control by
design if locally generated) and prints the same report. It is read-only: it
does not edit the manifest, update a checkout, pull an image, restart a
service, change credentials, or approve a candidate.

GitHub Actions runs the same discovery weekly and on manual dispatch, then
publishes the report as an artifact.

## Approval gates before an updater exists

A candidate must not become an installed version until Spectra has:

1. Recorded the current repo commit and image digest independently.
2. Identified compatibility constraints and upstream breaking changes.
3. Passed unit, integration, clean-install, paper-execution, accounting,
   restart-recovery, and backup/restore tests.
4. Passed paper-only safety checks with live execution unavailable.
5. Been explicitly approved by the operator.

The eventual apply workflow must create a backup, stage the update, verify
health, and retain a known-good rollback target. It must preserve local
credentials, agent memory/journals, strategies, database volumes, and paper
history. It must never use a mutable `latest` image as an approved version.

## Deliberate limitations of this first milestone

This is upstream discovery, not yet an installer/updater. It does not guess
installed image tags or digests, because the current repository audit has not
established those runtime facts. The next milestones add image/source inventory,
compatibility PR generation, staged apply/rollback, and Spectra CLI commands.
