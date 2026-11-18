# Spectra Finance Alpha 2.0 — Bazzite desktop installation

This release branch packages the existing Spectra Core + Condor + Hummingbot API
stack as a local desktop-dashboard installation for Bazzite Linux.

## Safety boundary

- Paper trading only.
- `SPECTRA_PAPER_ONLY=true` is enforced for Core and Hummingbot API.
- `SPECTRA_ALLOW_LIVE_EXECUTION=false` is enforced for Core and Hummingbot API.
- The only configured execution connector is `kraken_paper_trade`.
- The API and dashboard bind to loopback; they are not exposed to the LAN.
- The installer does not install RPM packages into the immutable Bazzite host.
- The Hummingbot API container is not given the host Podman/Docker socket.
- Do not add live exchange credentials to this Alpha installation.

The dashboard is a local web application launched from a native desktop-menu
entry named **Spectra Finance Alpha 2.0**. It opens the local command center at
`http://127.0.0.1:8088/overview`.

## Requirements

- Bazzite Linux on x86_64 or aarch64.
- A user session with rootless Podman working.
- `git` and `curl` available to the user.
- Network access for downloading source, Python/Node tooling, and container images.
- Enough free disk space for the Python environment, dashboard build, and
  Hummingbot API/Postgres/EMQX container images.

The installer installs `uv` and Node.js in the user's home directory. It uses
rootless Podman and `podman-compose` on Bazzite. It deliberately stops rather
than modifying the immutable host if required host tools are missing.

## Install

Run this from a terminal in the Bazzite user session:

```bash
curl -fsSL https://raw.githubusercontent.com/andredepaulnash-cmd/spectra-finance-core/release/alpha-2.0-bazzite-desktop-20261117/install-alpha.sh | bash
```

The installer uses the release branch by default. To choose a different install
directory, set `SPECTRA_ALPHA_HOME` before running it.

## Launch and operate

After a successful install, launch **Spectra Finance Alpha 2.0** from the
desktop application menu. The installer also opens the dashboard once at the
end of installation.

Operator commands:

```bash
~/.local/bin/spectra status
~/.local/bin/spectra start
~/.local/bin/spectra stop
~/.local/bin/spectra restart
~/.local/bin/spectra logs
~/.local/bin/spectra doctor
~/.local/bin/spectra backup
```

Dashboard URL: `http://127.0.0.1:8088/overview`

## Installation and acceptance checks

A successful installer exit requires the Hummingbot API to answer its
authenticated health endpoint, the Kraken paper connector to initialize, and
the Spectra dashboard to respond. The installer also verifies that the API
reports paper-only mode and live dispatch unavailable.

After installation, run:

```bash
~/.local/bin/spectra doctor
```

The doctor is read-only. It checks paper-mode flags, the paper connector,
runtime endpoint, local dashboard, containers, market-data verification, and
the latest backup archive.

A passing installation check does **not** certify the unresolved execution and
position-lifecycle CI suites as release-ready. This build is an isolated
paper-trading installation candidate; it must not be used with live capital.
