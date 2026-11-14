# Spectra Release Manager

This directory separates upstream candidate discovery from the measured local
runtime inventory and the compatibility gates required before a release can
be approved.

## Upstream candidate discovery

`components.json` records upstream repositories and how candidates are found.
Condor and Hummingbot API candidates are immutable commit SHAs; Hummingbot
Client and Gateway candidates are release tags. A candidate is not an approved
version, and no mutable `latest` tag is a release pin.

Run:

```bash
spectra update check
```

The discovery command is read-only. It does not edit the manifest, update a
checkout, pull an image, restart a service, change credentials, or approve a
candidate.

## Installed runtime inventory

`runtime-components.json` lists the host and runtime layers that must be
measured, including Spectra Core, Condor, Hummingbot API/Client, Kraken paper
connector, PostgreSQL, EMQX, optional Gateway, dashboard, Python, and the
container runtime.

Run on the actual installed machine:

```bash
spectra update inventory
```

The command records source commits/branches/dirty flags, Compose file hash,
container IDs and immutable local image IDs, host OS facts, Core Python and
frontend versions, paper/live safety flags, and aggregate state-directory
metadata. It deliberately does **not** record .env values, credentials,
journal contents, or filenames inside state directories. The report is written
with owner-only permissions under the installation's backups directory.

Do not guess the local image IDs, versions, volume identities, or host
deployment from CI. The actual Bazzite installation must run this command
before any component can be approved.

## Compatibility matrix

`compatibility-matrix.json` maps component dependencies to required gates:
agent/model inheritance, provider contracts, CapitalGovernor, Kraken paper
tracker readiness, order acknowledgement/reconciliation, fill lifecycle,
executor restart recovery, backup/restore, and dashboard connectivity.

The baseline remains `awaiting-host-inventory`. A green unit test or an
upstream candidate does not approve a runtime combination.

## Recoverable release transaction proof

The staged-release pointer primitive in
`scripts/spectra_release_transaction.py` atomically switches a `current`
symlink to a staged release and can restore the previous pointer. CI exercises
an isolated apply/rollback cycle:

```bash
spectra update verify-rollback
```

This proof runs entirely in a temporary directory and confirms that the prior
release remains intact. It does **not** touch the live Alpha installation.
The current Alpha installer still uses fixed source directories, so
`spectra update apply` and live release rollback remain intentionally blocked
until the install layout is migrated to the pointer model and the local
inventory/image IDs are measured and approved.

## Approval gates

A candidate must not become an installed version until Spectra has:

1. Recorded the current repo commit and container image ID/digest independently.
2. Identified compatibility constraints and upstream breaking changes.
3. Passed unit, integration, clean-install, paper-execution, accounting,
   restart-recovery, and backup/restore tests.
4. Passed paper-only safety checks with live execution unavailable.
5. Been explicitly approved by the operator.

The eventual apply workflow must create a backup, stage the update, verify
health, and retain a known-good rollback target. It must preserve local
credentials, agent memory/journals, strategies, database volumes, and paper
history. It must never use a mutable `latest` image as an approved version.
