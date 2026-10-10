# Source-bound runtime dependency preparation

This packet prepares CPython 3.12 on Linux x86_64 for the repaired compiled
Hummingbot wheel. It preserves the exact PR33 core/API source inputs and the
PR36 wheel identity. It is additive preparation; the inherited Docker recipe,
stack lock, production branch, and financial research are unchanged.

Run the generator with a Python 3.12 interpreter and an explicit verified wheel:

```bash
python3.12 -I -S -B scripts/spectra_prepare_runtime_dependencies.py \
  --spectra-root . \
  --wheel /absolute/path/hummingbot-20260922-cp312-cp312-linux_x86_64.whl \
  --output-dir /absolute/path/new-private-work/dependency-inputs
```

The generator only reads pinned source and wheel bytes, checks wheel package
metadata against the literal `install_requires`, and writes resolver inputs and
a preparation receipt. It never installs packages, imports Hummingbot, starts
an API, reads an account, or touches a journal. It requires no third party Python
packages. Its preparation receipt intentionally records every runtime/release
verification flag as false.

`requirements.source-derived.txt` contains all 47 declared core wheel
requirements, Python packages declared by the pinned API environment, the
legacy `eip712-structs` extra from `core/setup/pip_packages.txt`, and pinned build
tools. Duplicate upstream requirements remain visible. The unpinned PyPI
`hummingbot` entry is removed; only the independently identified local wheel
can supply the core. `environment.no-pypi-core.yml` preserves the native conda
requirements. Pip's `docker` distribution is the explicit counterpart to the
conda `docker-py` package.

`constraints.compatibility.txt` describes the runtime trial rather than a
complete environment lock. It uses NumPy 2.2.6 and pandas 2.3.2, keeps the
reviewed build tooling versions, and carries the core environment's additional
aiohttp and XRPL bounds, together with explicit compatible Web3, Injective,
eip712 and eth-typing pins derived from the completed trial. The preferred wheel was compiled with
NumPy 2.3.5. That header/runtime difference requires installed compiled checks;
a solver result alone does not establish ABI compatibility.

The first clean resolution attempt retaining NumPy 2.3.5 failed: the downloaded
pandas-ta 0.4.71b0 metadata pins numba 0.61.2, whose metadata requires NumPy
below 2.3. The separate 2.2.6 trial also exposed a legacy eip712-structs packaging
problem: isolated metadata generation imports undeclared `eth_utils`; a
transparent packaging bootstrap then backtracked into an older sdist lacking
`requirements.txt`. A diagnostic latest-version constraint exposed further
SDK/Web3/websockets resolution rejection in that attempt. Explicit compatible
SDK pins subsequently resolved the separate trial. These failures are retained in
`execution.receipt.json` with exact input and log hashes.

A separately labeled trial, `requirements.core-api-without-legacy-extra.txt`,
excludes only `eip712-structs`. That package is not a declared dependency of the
compiled wheel, but the original Docker recipe includes it. Thus even a clean
installation and passing installed guard in that trial cannot certify the
complete inherited Docker recipe. Its explicit omission must be resolved
before claiming a complete API image.

The dependency execution receipt distinguishes source preparation, successful
installation, `pip check`, the bounded data-module ABI check, the full PR33
installed-source guard, and a complete image. Consult the receipt for measured
results; preparation flags are never silently promoted to live authority.

The API retrieval receipt records 224 physically verified source blobs from
commit `84426ffbf9ba09522b4444664f9770236205ae29` and tree
`25f37e7712de3a5bf9b0ae443b360c796c8b7b87`. This covers every application file
copied by the inherited Docker recipe. Six repository credential-template
paths were excluded, so the retrieval is not a full repository checkout.
No repository credentials, account state, API startup, or exchange operation
is part of this preparation.

The completed separate trial and offline reconstruction both installed 197
Python distributions, passed `pip check`, passed the compiled data-module ABI
check with NumPy 2.2.6, and passed the exact PR33 installed-source guard. The
partial lock hashes all 197 wheel artifacts (267,835,299 bytes). It is a Python
artifact lock for this trial, not a conda/native/full-image lock.

For an already prepared **disposable, non-inheriting** dependency venv, the
portable bounded full installed-source guard is:

```bash
/absolute/path/disposable-venv/bin/python -I -S -B \
  deploy/runtime_readiness/dependencies/check_installed_guard.py \
  --spectra-root .
```

The wrapper verifies the exact inherited guard/manifest before import, adds only
the explicit venv package directory, checks all five repaired source/class
markers and required executor fields, and refuses network/process audit events.
It instantiates no connector or executor, starts no API, and checks no journal.
A Python audit hook is bounded protection; it is **not an operating-system
network sandbox**. The container and runner tests must provide their own
isolation and account-state protections.

The installed artifact ownership check found three conflicting msgpack module
paths owned by both msgpack-python 0.5.6 and msgpack 1.2.3. The modern version
won in both installations and a constructed round trip passed, but the
namespace overlap remains an API environment release blocker. The six other
reported overlaps are documentation files owned by eth-rlp and eth_abi.
`cleanup-proposal.md` and its proposed diffs describe a minimal derivative
packaging fix without changing the current wheel or inherited build files.

A complete container still needs an independently reviewed immutable
`continuumio/miniconda3@sha256:...` base reference, native conda resolution,
a builder, final image identity, and installed image/machine tests. There is no
base digest or image digest in this packet. Bazzite verification awaits the
user-provided MCP runner. The Nov. 9 research holdout is separate from these
operational checks and is not opened by them.
