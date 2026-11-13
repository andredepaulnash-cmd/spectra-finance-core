# Spectra Finance release management

## Purpose

The release manager is the lifecycle authority for the software stack. It records
what is installed, what upstream has published, what has passed compatibility
testing, and what Spectra has explicitly approved. An upstream release is a
candidate, never an instruction to replace a working installation.

The architecture invariant is:

> Condor may think. Hummingbot may execute. Only Spectra Core may authorize capital.

## First slice: inventory and fail-closed validation

`release/spectra-release-manifest.json` is the current release-candidate
inventory. It separates five concerns:

- Spectra Core's own tagged release lifecycle.
- Condor's upstream source and the Spectra overlay embedded in this repository.
- The Spectra Hummingbot API fork pinned to a full commit.
- The Hummingbot Client/core package version consumed by the API.
- Gateway as a separately tracked, optional component until its runtime is
  actually installed and accepted.

The manifest is intentionally marked `candidate`. The current API package
metadata pins Hummingbot `20260922`; upstream Hummingbot Client and Gateway
latest releases observed for this snapshot are `v2.17.0`. Condor and the
Spectra API use commit tracking rather than pretending they have matching
versioned releases.

These are source snapshots, not proof of what is running on a user's machine.
The manifest explicitly records Gateway as unverified and lists the remaining
release blockers. Do not infer runtime activation from a repository entry.

## Commands

From the repository root:

```bash
python scripts/spectra_release.py validate
python scripts/spectra_release.py status
python scripts/spectra_release.py plan
```

All commands in this first slice are read-only. `--require-approved` is a
promotion gate for CI and must fail while the manifest remains a candidate:

```bash
python scripts/spectra_release.py validate --require-approved
```

There is deliberately no `apply` command yet. Update application must not be
added until the manifest is consumed by a transactional updater with preflight,
backup, immutable artifact verification, health checks, and rollback.

## Upstream discovery

The scheduled `.github/workflows/upstream-release-watch.yml` workflow checks
the official Hummingbot Client and Gateway latest releases and the current
Condor and Spectra Hummingbot API main commits. When the observed upstream
snapshot differs from the last reviewed snapshot, it opens or updates a PR
containing `release/upstream-candidates.json`.

That PR is discovery only. It never changes the approved component pins or
updates the running installation. A human-reviewed compatibility PR must
select the candidate revisions and run the complete test matrix before a new
Spectra release can be approved.

## Planned lifecycle

1. **Discover:** scheduled GitHub Actions compare upstream Hummingbot Client
   and Gateway releases, Condor commits, and Spectra API commits.
2. **Propose:** create a candidate snapshot and a reviewable dependency-update
   pull request. Never merge automatically.
3. **Validate:** run unit, integration, paper lifecycle, clean-install,
   migration, backup/restore, and Bazzite acceptance tests.
4. **Approve:** promote exact source commits and immutable image digests into
   an approved Spectra release. Publish checksums, SBOM, provenance, and
   changelog.
5. **Apply:** the local Spectra CLI downloads and verifies the approved
   release, shows a plan, takes a consistent backup, applies reversible
   migrations, updates only selected components, and verifies health.
6. **Rollback:** restore the prior component set and application state without
   deleting user credentials, memory, skills, strategies, databases, or paper
   history.

The updater must treat mutable tags such as `latest` as unapproved. It must
never use `compose down -v`, prune volumes, or replace stateful services as a
side effect of a software update.

## Release gates

An installable release remains blocked until all of the following are proven:

- Every runtime component has an exact source revision or immutable image
  digest; transitive Hummingbot dependencies are pinned.
- Spectra's Condor overlay is compatible with the reviewed upstream Condor
  commit.
- Hummingbot API, Client, and optional Gateway pass their compatibility matrix.
- Paper-only mode is verified at the API, executor, and CapitalGovernor
  boundaries; live execution remains disabled.
- Kraken public market data, governed paper entry, fills, accounting, and
  restart recovery pass end-to-end tests.
- A clean Bazzite install, in-place update, backup/restore, and rollback pass
  without losing local state.
- Artifacts have checksums, an SBOM, and provenance; secrets never enter logs
  or Git.

No release is approved by this inventory alone.
