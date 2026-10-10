# Reviewed source build for one-attempt LIMIT execution

This recipe binds the five corrected Python sources in `overlay.manifest.json`
to official Hummingbot core commit
`9af100d6822da7d2d0291a906c730ef172284ee2` and Spectra's private API commit
`84426ffbf9ba09522b4444664f9770236205ae29`. It preserves the upstream core
package version; a version string alone cannot identify this repair.

The reviewed files are a build recipe and source contract. **No compiled wheel,
image digest, installed Bazzite backend, or live activation is attested by this
PR.** `build.contract.json` deliberately records those facts as unverified.
The operational stack lock remains unchanged.

Protected fixed LIMIT executors consume one durable attempt and preserve every
order identity. Cancellation, failure, ambiguous placement, and restart cannot
authorize another submission. Restart snapshots restore accounting evidence
without restoring trading authority. Retained tracker references keep late
fills available beyond the ordinary cache TTL; known numbers remain provisional
when fee valuation or venue status is uncertain and are never represented as a
proved final settlement.
The tracker also retains the greatest validated cumulative-filled quantity from
order-status updates. If that watermark exceeds known trade fills, accounting
remains provisional until the exact trade evidence catches up; a closed status
does not turn lagging fill history into a confirmed total.
Even an apparent full fill remains in accounting-only shutdown while finality
is unproven, so the API does not archive a stale one-time total. Valid late fills
can update its active accounting and durable snapshot with dispatch disabled.

The Kraken AddOrder barrier also durably claims each client order ID for direct
MARKET and LIMIT requests. A market claim records no limit price. This transport
protection does not authorize a MARKET escape or change Spectra's LIMIT policy.
Ambiguous transport replies, malformed success responses, and redirects fail
closed without another AddOrder request. Read-only order/trade reconciliation
and fresh Spectra authorization remain necessary before a new attempt.

## Build prerequisites and inputs

Use BuildKit and an authenticated token that can read the private API repository.
Provide that token through a BuildKit secret file, never a build argument or
environment variable. The source stage uses Git askpass to read the mounted
secret and removes the Git directories in the same build operation. It never
copies credentials, source Git configuration, or the secret into the image.

Resolve and review an appropriate `continuumio/miniconda3` base image first, then
use its exact `continuumio/miniconda3@sha256:<64 lowercase hex>` reference. There
is no mutable default and no verified digest invented in this repository.
Pin the intended platform as well. The initial API checkout needs a current,
independently reviewed builder; credentials remain confined to its secret mount.

From the Spectra Finance checkout, after the overlay manifest is frozen:

```sh
sha256sum deploy/hummingbot_core/overlay.manifest.json
docker build --platform linux/amd64 \
  --file deploy/hummingbot_core/Dockerfile \
  --build-arg BASE_IMAGE=continuumio/miniconda3@sha256:<reviewed-digest> \
  --build-arg OVERLAY_MANIFEST_SHA256=<reviewed-manifest-sha256> \
  --secret id=github_token,src=/absolute/path/to/private-read-token \
  --tag spectra-hummingbot-api:review-candidate .
```

The placeholder values must be replaced with actual reviewed digests. This
command builds an image; it does not start the API or place an order. Do not
replace a running deployment merely because the source tests pass.

The source stage fetches exact Git commits and rejects dirty checkouts,
configured Git filters, unexpected source paths, symlinks, duplicate JSON keys,
baseline checksum mismatches, patched checksum mismatches, and invalid Python
before source writes. It accepts no tar archive and extracts no wheel archive.
It also verifies the exact API MIT and core Apache 2.0 license bytes against both
source checkouts and the packaged copies. The image retains those texts and the
derivative-source attribution notice under `/opt/spectra-core/licenses`.
The five approved paths are the order executor, Kraken connector, REST transport,
client order tracker, and the new durable attempt guard. Validation failures do not alter those source
files. Files are replaced individually; this is not a crash-atomic transaction
across a running checkout. Use a disposable build checkout.

The wheel stage removes the API's bare `hummingbot` requirement from its exact
environment file. It installs the core's separately enumerated runtime
dependencies, compiles a local wheel in the API's Python 3.12 environment,
verifies the wheel's patched bytes, then installs that wheel with `--no-deps`.
The build guard checks installed file hashes, actual imported module locations,
module and class markers, and required executor configuration fields. It starts
no API lifespan and constructs no exchange connector.

The core requires Cython, C++ compilation, NumPy, TA-Lib libraries, and broad
blockchain dependencies. The upstream API/core requirements are not a complete
dependency lock. Base-image pinning and exact source binding alone do not make
these dependency resolutions reproducible. The build records conda explicit
packages and pip versions under `/opt/spectra-core/dependency-evidence`; review
those results, the wheel hash, build provenance, and the resulting image digest
before deciding on an installed candidate.

## Persistent journal and startup

The existing API compose mount `./bots:/hummingbot-api/bots` covers the suggested
journal path:

```text
/hummingbot-api/bots/data/spectra-execution/attempts.sqlite3
```

Startup requires both `SPECTRA_ATTEMPT_JOURNAL` and
`SPECTRA_ATTEMPT_JOURNAL_ID`. The latter is the exact 32-character lowercase
identity returned by explicit journal initialization. The parent must already
exist, be private (normally `0700`), and belong to the runtime user. The regular
journal and its adjacent `attempts.sqlite3.claims` claim anchor must already exist,
be private (normally `0600`), and have the matching identity. The guard verifies
the anchor hash chain against the SQLite claim scopes. Backups and recovery must
preserve both files together. This Dockerfile inherits the base image's root user: a host directory
owned by a different user does not satisfy that ownership requirement.

For a **fresh, independently authorized deployment only**, prepare the durable
directory and initialize a journal offline using
`patches/spectra_attempt_guard.py`'s `initialize_journal` function or its CLI.
Run that operation as the same UID that will run the API and preserve the
returned identity outside the disposable container. This recipe neither
initializes nor creates a trading journal. Never reinitialize after losing a
mount, restoring a deployment, or encountering uncertainty: recover the
existing journal, claim anchor, and identity instead. Its non-releasable claims
must survive rebuilding the image and restarting the API. A coherent rollback
of both files across a process restart cannot prove continuity without an
external trusted checkpoint. Do not blindly restore old claims: manual recovery
and complete venue order/trade reconciliation are required before execution.

The image entrypoint checks the installed source and the existing journal before
executing uvicorn. Missing storage, wrong identity, permissions mismatch,
unpatched core, import shadowing, or a wheel overwritten by a later package
install prevents startup. The guard does not claim exchange finality and does
not release claims or accounting holds.

The old API startup surface check is empty. This standalone image enforces the
new check through its entrypoint; direct `uvicorn`, an overridden entrypoint,
ordinary API source installs, and other bot images are outside this recipe's
guard. They require equivalent binding before they can be considered repaired.
The existing compose file still names its old mutable image; switching it is a
separate reviewed deployment operation. No such switch is performed here.

## Verification boundary

`apply_overlay.py --verify-overlay` validates manifest structure/checksum and
all replacement hashes and Python syntax without a source checkout, backend
import, generated file, or network call. CI supplies the checksum of its own
checked-out manifest, which establishes internal source coherence. The separate
reviewed publication receipt binds those bytes to the expected candidate; this
CI operation alone does not authenticate a deployment or approve source changes.

The local overlay tests exercise checksum, path, symlink, dirty Git checkout,
wheel member, and startup guard refusals with temporary files and inert module
doubles. They do not build the real compiled core. A successful actual Docker
build would additionally run the installed compiled-core guard, but would still
not attest an installed Bazzite image, real fee profile, or live authorization.
