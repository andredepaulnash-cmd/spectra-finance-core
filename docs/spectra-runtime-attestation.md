# Read-only Bazzite runtime evidence

This collector records facts from the machine where it runs. The reference
contract describes reviewed source; it does not identify an installed build or
approve that build for trading. The collector always preserves
`approved_for_live: false` and `runtime_attestation_complete: false`.

Run this on the actual Bazzite machine, with the collector and contract files
available locally. It requires Python 3.12 or later and access to the existing
Docker Unix socket. It installs no dependencies.

```bash
python3 -I -S -B scripts/spectra_runtime_attest.py --contract spectra-runtime.contract.json > spectra-runtime-evidence.json
```

Exit code **2** means the JSON report was produced with missing evidence. That
is expected even when the static facts match: this read-only collector does
not prove image build provenance or live execution semantics. A collector
argument error also returns 2, but emits no JSON. Check that the output file
contains a JSON object before sharing it.

The script also works independently of a Spectra checkout. Supply its actual
location in the command, and give explicit checkout paths when the installed
layout differs from the defaults:

```bash
python3 -I -S -B /path/to/spectra_runtime_attest.py --contract /path/to/spectra-runtime.contract.json --spectra-path /path/to/condor --api-path /path/to/hummingbot-api --container hummingbot-api > spectra-runtime-evidence.json
```

Defaults are `~/SpectraStack/condor`, `~/SpectraStack/hummingbot-api`, the
`hummingbot-api` container, and `/var/run/docker.sock`. A rootless installation
can select its existing local socket with `--docker-socket
/run/user/1000/docker.sock`. Remote Docker targets are rejected. The collector
never uses the current Docker context or `DOCKER_HOST` to select a remote
machine. Missing permissions or missing commands become generic missing
evidence, without returning command stderr.

## What is recorded

- Only `ID`, `VARIANT_ID`, and `VERSION_ID` from `/etc/os-release`; the booted
  OSTree checksum, base checksum, version, and staged flag.
- Selected Docker client/server version facts, the container's immutable ID,
  immutable image ID, configured image reference, running state, and status.
- The selected image's ID, valid repository digests, and the OCI revision
  declaration. No other image labels are collected.
- Exact Git heads and boolean cleanliness for the two explicitly selected
  repository roots. Paths, filenames, diffs, remote URLs, and configuration
  values do not enter the JSON. A wrong root, different revision, dirty tree,
  configured Git filter, or indexed submodule is reported as missing evidence.
- Installed Hummingbot distribution version, its declared Git revision from
  `direct_url.json` when available, editable-install flag, and SHA256 hashes of
  the five fixed executor/config/connector/enum files listed in the contract.

The inspected container's **immutable ID** is used for the metadata/hash probe,
so a later replacement of the friendly container name cannot silently change
the observed container. The probe uses `python -I -S -B`, standard-library
metadata/spec discovery, and file hashing. It does not import Hummingbot,
executor classes, application configuration, or site customization scripts.
The `-B` flag prevents bytecode cache writes on both the host and the container.
Site-package directories are discovered through `sysconfig`; editable layouts
that require executing `.pth` files may remain unverified.

Each command has a 15-second deadline and a 64 KiB output limit. Timeout or
oversized output kills the command group; stderr is discarded.
The in-container probe also has its own 10-second termination alarm, because
stopping a Docker CLI does not necessarily stop its `exec` process. Contract,
metadata, and source reads accept regular files only, reject FIFO/device
reads, and enforce size limits. A missing component fails closed. Normal runs
take seconds; a machine where every command stalls can take roughly four
minutes. No environment values, mounts, credentials, account data, orders,
trade history, source contents, or private repository URLs are exported.

The collector performs no HTTP request, order placement, container creation,
restart, rebuild, package installation, Git checkout, or stack-lock change.
Only the explicitly redirected JSON file is written.

## How to interpret the evidence

The reviewed Spectra candidate is
`24b152457d9a8b3e007e69e7879bab38d1beb254` (PR #31); the reviewed API source is
`84426ffbf9ba09522b4444664f9770236205ae29`; the examined Hummingbot core source
is `9af100d6822da7d2d0291a906c730ef172284ee2`. Actual heads and metadata are
reported only when observed. The expected values never replace missing or
different installed evidence.

An API checkout at the expected head does not prove the running image was
built from it. An OCI revision label is a declaration, not build provenance.
Matching five core-file hashes does not establish the installed Git revision
or prove the rest of the package matches. A `latest` or other tagged image
reference remains mutable even if the current image has a repository digest.

The examined core is a **source reference, not an approved deployment target**.
Its cancellation/replacement issues are tracked in
[LIMIT runtime readiness](spectra-limit-runtime-readiness.md). No report from
this collector enables trading or overrides those blockers. Actual image
build binding, installed configuration behavior, cancellation/late-fill
reconciliation, account fee bounds, and restart behavior still require the
separate runtime review before activation.
