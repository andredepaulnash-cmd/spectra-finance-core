# MCP machine verification before October 15

Prepare and run the available tests now. When Architect exposes the computer's
MCP runner, use that runner for the actual Bazzite checks. October 15 is
Architect's birthday; this document schedules no work, reminder, or automated
machine action for that day.

This is a preparation and isolated testing runbook. Production
`spectra-core-phase2` stays at
`6e894fa9c2b55384d0f6120f41ca91892e1fe207`. No production installation,
compose change, private venue request, live order, or money movement is included.
The existing Nov. 9 research holdout is independent of this operational work;
never open its sealed market payloads or score its outcomes during these tests.

## What the current evidence proves

| Evidence | Current result | What still needs the computer or builder |
| --- | --- | --- |
| PR33 execution and accounting repair | 1,359 source tests passed | Real installed backend and complete API environment |
| PR36 strict core compilation | 61 x86-64 extensions; final compile took 299.32 seconds | Target machine libraries and complete API image |
| Disposable installed data-module check | Five overlay hashes and one compiled data-class ABI passed | Full installed connector/executor/configuration guard |
| Runtime packages in the old build venv | `pip check` had 39 missing packages and one pandas version mismatch | A clean source-bound dependency resolution and successful `pip check` |
| Immutable image and base identity | Not established | Reviewed platform-specific base digest, builder, resolved dependencies, image provenance and digest |
| Architect's MCP runner and Bazzite machine | No runner is exposed to this session yet | Actual runner inventory and machine receipts |

The old `scripts/spectra_runtime_attest.py` binds the PR31 baseline. Its host
inventory can be useful, but a baseline pass cannot prove that the PR33 repair
or PR36 wheel is installed. Use the new repair-specific collector below.

## Session setup and isolation

Record the runner's advertised tool capabilities and the machine reached by
those tools. Do not infer that a local endpoint in another workspace reaches
Architect's computer. Use only the exposed runner operations. First record
the read-only host inventory; a runner connection alone is not a compatibility
pass.

Supply an explicit reviewed checkout and explicit wheel path. Keep receipts,
temporary environments, fixture data and test images in a newly created private
test directory. Do not use any existing `bots`, credentials, database, SQLite
attempt journal, claim anchor, or production mount as a test fixture. Do not
export a full environment listing; it can contain secrets. Keep credentials
absent from the isolated test process, container and fixture workspace.

The collector rejects symlinks in every selected path component. Use observed
canonical physical absolute paths for the checkout, wheel, installed root,
empty Docker configuration directory and local socket. On Bazzite, `/home`
may point to `/var/home`; `/var/run` commonly points to `/run`. Resolve only
the metadata of an explicitly selected path, then record its physical path;
do not scan private directories to find an alternative installation.

The following setup creates only a temporary test directory. Set
`SPECTRA_REVIEW_CHECKOUT` and `SPECTRA_REVIEW_WHEEL` to the actual reviewed
local paths; these variables are deliberately not inferred.

```sh
: "${SPECTRA_REVIEW_CHECKOUT:?Supply the reviewed checkout path}"
: "${SPECTRA_REVIEW_WHEEL:?Supply the final reviewed wheel path}"
umask 077
SPECTRA_MACHINE_TEST_DIR=$(mktemp -d -t spectra-machine-check.XXXXXXXX)
mkdir "$SPECTRA_MACHINE_TEST_DIR/docker-config"
cd "$SPECTRA_REVIEW_CHECKOUT"
```

A partial preparation checkout contains only selected inherited files. It is
sufficient for the bounded preflight and attestor; the lifecycle test stage
requires the full verified Spectra checkout and its frozen development
environment. Record the actual commit/tree and selected file hashes separately.
The repair source is PR33 `fad5f320b2f87cc7c089bea2750dd8344aa1b661`, tree
`6d15736a95b20bbd0e5aee8e34a07217f8143b91`. This additive preparation branch
may have a later commit; its inherited repaired files must still match the
contract.

## Stage 1 — checks available before the runner connects

These source and artifact checks run against explicit local inputs. They prove
those inputs and the local process only; they do not describe Bazzite.

```sh
python3.12 -I -S -B scripts/spectra_isolated_build_preflight.py \
  --source-root . > "$SPECTRA_MACHINE_TEST_DIR/source-preflight.json"

sha256sum "$SPECTRA_REVIEW_WHEEL"

python3.12 -I -S -B deploy/hummingbot_core/apply_overlay.py \
  --manifest deploy/hummingbot_core/overlay.manifest.json \
  --expected-manifest-sha256 c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77 \
  --overlay-root deploy/hummingbot_core \
  --verify-overlay

python3.12 -I -S -B deploy/hummingbot_core/apply_overlay.py \
  --manifest deploy/hummingbot_core/overlay.manifest.json \
  --expected-manifest-sha256 c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77 \
  --verify-wheel "$SPECTRA_REVIEW_WHEEL"
```

The preferred existing wheel is
`hummingbot-20260922-cp312-cp312-linux_x86_64.whl`, 20,910,055 bytes, SHA256
`b92048954145b055ac7bbf85cfdfa4e042c4153eb66aabc29740f4ec34a76dbc`.
Compare the displayed whole-wheel checksum with that exact value. A new build
needs its own independently recorded checksum; do not silently substitute the
previous digest. This wheel is `cp312/linux_x86_64`, not a manylinux certificate.

Use the additive runtime dependency preparation packet to check exact core/API
requirements and resolve them in a clean disposable environment. Preserve the
original PR33 recipe and all inherited source pins. The old build venv's
dependency findings describe that incomplete environment; they are not 40
independent software defects. A dependency declaration, dry-run plan or solver
success is not an installed runtime pass. Keep any unresolved dependencies,
missing platform packages and unavailable artifact sources explicit.

The input-preparation command is safe to run before the machine is connected:

```sh
python3.12 -I -S -B scripts/spectra_prepare_runtime_dependencies.py \
  --spectra-root . \
  --wheel "$SPECTRA_REVIEW_WHEEL" \
  --output-dir "$SPECTRA_MACHINE_TEST_DIR/dependency-inputs"
```

That output directory must be new. The command prepares verified inputs only;
it never installs dependencies or starts the API. Follow
[the dependency preparation instructions](../deploy/runtime_readiness/dependencies/README.md)
for the separate clean resolution and installation steps. Keep any initial
failed resolution and packaging trial in the evidence rather than hiding it
behind a later successful preparation command.

## Stage 2 — actual MCP machine inventory and repair attestation

Run this stage only through the runner that is actually exposed on Architect's
computer. Do not substitute the workspace's Ubuntu inventory.

```sh
python3 -I -S -B scripts/spectra_repair_runtime_attest.py \
  --contract deploy/runtime_readiness/repair-attestation.contract.json \
  --spectra-path "$SPECTRA_REVIEW_CHECKOUT" \
  --wheel-path "$SPECTRA_REVIEW_WHEEL" \
  > "$SPECTRA_MACHINE_TEST_DIR/repair-host-attestation.json"
```

Record OS/version, architecture, Python version and ABI, glibc/library
availability, the declared container tooling, and explicit source/wheel
fingerprints. Inventory must establish whether this machine is Bazzite and
whether it can host the intended `linux/amd64` candidate. A host Python mismatch
can be addressed inside a disposable container or Python environment; it does
not authorize replacing Bazzite's system Python or changing its immutable base.

If an existing explicitly selected installation is being examined, add
`--installed-root /absolute/path/to/site-packages`. If an existing local Docker
socket and exact candidate image ID are supplied, add
`--docker-socket /absolute/path/to/docker.sock`,
`--docker-config-empty-dir /absolute/path/to/new-empty-directory` and
`--image-id sha256:...`. The empty Docker configuration directory must exist
and contain no files; it prevents reading the user's Docker credentials or
configuration. Create it inside the private test directory.
Replace every placeholder with an observed explicit value. These collector
options read selected facts; they do not start a container, import a trading
module, discover private directories, or prove venue behavior. An observed
image ID remains unapproved until compared with reviewed build provenance and
an actual expected image digest. No expected API image digest exists yet.

Check each receipt section independently. Host suitability, selected repaired
file hashes, wheel identity, installed overlay hashes and observed image
identity are different claims. Missing evidence remains missing even if the
process produces well-formed JSON. The repair collector exits 2 for incomplete
proof, which is expected while runtime compatibility and image/source binding
remain pending. Preserve `approved_for_live=false`; retain both the JSON and
actual exit status.

`source.status == "MATCH"` and `selected_file_bytes_match == true` describe
the 32 selected PR33 file fingerprints. `wheel.status == "MATCH"` describes
the exact selected wheel. `installed_overlays.status == "MATCH"` describes
five installed Python file hashes only; compiled installed identity remains
UNKNOWN in that collector. None of these fields grants a runtime semantics,
API compatibility or live-approval pass.

## Stage 3 — clean dependencies, builder and image

Keep host inventory read-only. Build and install dependencies only in the
private test environment or isolated image. Review the source-bound runtime
dependency packet first. For every actual installation record the Python and
platform identity, exact resolved package artifacts and hashes, conda explicit
packages where applicable, pip snapshot, and the result of `pip check`.

Required inputs for a complete API image are:

| Input | Required check |
| --- | --- |
| Core source | Commit `9af100d6822da7d2d0291a906c730ef172284ee2`, tree `06c31cf98f8573c2f2a1c819bcfbcebe56d8ee2a`; every tracked input physically verified before compilation |
| API source | Commit `84426ffbf9ba09522b4444664f9770236205ae29`, tree `25f37e7712de3a5bf9b0ae443b360c796c8b7b87`; full source checkout verified |
| Five-file overlay | Exact manifest checksum `c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77` and all replacement hashes |
| Platform/base | Explicit `linux/amd64` and independently reviewed immutable `continuumio/miniconda3@sha256:...` reference |
| Builder | Actual supported isolated builder; BuildKit is required by the inherited recipe's secret mount |
| Private API access | Existing authorized read access; secret confined to the builder secret mount, never a build argument, output, or image layer |
| Dependencies | Actual clean installation, acceptable versions, retained resolution evidence and `pip check` exit 0 |

When those inputs are actually available, follow
[the inherited build recipe](../deploy/hummingbot_core/README.md) in a disposable
context. The pinned recipe is an initial resolution recipe, not a complete
dependency lock. If the additive dependency work proposes a new resolved build
packet, review that packet and its source identities separately before using
it. Do not call a mutable base tag or unreviewed resolution reproducible.

The inherited build runs the installed compiled-core guard without starting the
API or constructing an exchange connector. Retain the complete build log,
source identities, base/platform digest, wheel checksum, package snapshots,
installed-guard result and resulting image ID/digest. A rebuild may produce a
different wheel checksum; binary reproducibility has not been established.

If the wheel is installed in a disposable Python 3.12 venv with its reviewed
runtime packages, the bounded data-module check is:

```sh
: "${SPECTRA_TEST_PYTHON:?Supply the disposable Python 3.12 venv interpreter}"
"$SPECTRA_TEST_PYTHON" -I -S -B scripts/spectra_check_compiled_data_module.py \
  --manifest deploy/hummingbot_core/overlay.manifest.json \
  --wheel "$SPECTRA_REVIEW_WHEEL" \
  --expected-wheel-sha256 b92048954145b055ac7bbf85cfdfa4e042c4153eb66aabc29740f4ec34a76dbc \
  > "$SPECTRA_MACHINE_TEST_DIR/compiled-data-module.json"
```

Use the recorded original digest above only for the original preferred wheel.
Run without `-O`/`-OO`; the checker refuses optimized Python. Its audit hook
blocks socket and subprocess events in that check but is not an OS sandbox.
The check exercises one data-class ABI and installed patch bytes; it is not
the full backend guard or a proof that all 61 extensions are usable on Bazzite.

After a real image exists, an isolated repeat of its full installed guard can
use an explicitly reviewed local image ID. This command runs the guard only,
with no network, host mount, port, credential or journal:

```sh
: "${SPECTRA_TEST_IMAGE_ID:?Supply the reviewed immutable local image ID}"
: "${SPECTRA_TEST_DOCKER_SOCKET:?Supply the observed local Docker socket path}"
python3 -I -S -B -c 'import re,sys; sys.exit(0 if re.fullmatch(r"sha256:[0-9a-f]{64}", sys.argv[1]) else "Invalid immutable image ID")' "$SPECTRA_TEST_IMAGE_ID" || exit 2
docker --config "$SPECTRA_MACHINE_TEST_DIR/docker-config" \
  --host "unix://$SPECTRA_TEST_DOCKER_SOCKET" \
  run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --env PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python "$SPECTRA_TEST_IMAGE_ID" \
  /opt/spectra-core/apply_overlay.py \
  --manifest /opt/spectra-core/overlay.manifest.json \
  --expected-manifest-sha256-file /opt/spectra-core/manifest.sha256 \
  --verify-installed
```

Do not translate this Docker command to another runtime without checking that
runtime's equivalent isolation and image identity. A guard failure is evidence
to retain and resolve; it does not justify bypassing the installed checks.
The socket is an explicitly observed local path, so inherited `DOCKER_HOST`
cannot select another daemon. `--pull never` prevents a missing local image
from triggering a remote image pull. A malformed image ID stops the stage
before container execution.

## Stage 4 — disposable lifecycle, restart and accounting fixtures

Use the full exact repaired Spectra checkout and frozen development environment.
The named modules use temporary journals and inert transports/framework doubles.
They do not request market data, import the sealed holdout, or submit venue
orders. Run the focused suite in the test environment, with its credential
variables absent and no production directories mounted:

```sh
UV_PROJECT_ENVIRONMENT="$SPECTRA_MACHINE_TEST_DIR/lifecycle-venv" \
  UV_CACHE_DIR="$SPECTRA_MACHINE_TEST_DIR/uv-cache" \
  uv sync --dev --frozen --python 3.12
UV_PROJECT_ENVIRONMENT="$SPECTRA_MACHINE_TEST_DIR/lifecycle-venv" \
  UV_CACHE_DIR="$SPECTRA_MACHINE_TEST_DIR/uv-cache" \
  uv run --frozen --python 3.12 pytest -q \
  --basetemp "$SPECTRA_MACHINE_TEST_DIR/pytest-fixtures" \
  tests/test_attempt_journal.py \
  tests/test_core_overlay_build.py \
  tests/test_corrected_limit_executor.py \
  tests/test_corrected_kraken_placement.py \
  tests/test_protected_attempt_identity.py \
  tests/test_protected_buy_reconciliation.py \
  tests/test_spectra_executor_accounting_flow.py \
  tests/test_live_fill_reconciliation.py \
  tests/test_integrated_runtime_restart.py \
  tests/test_cash_runtime_resume.py
```

Both commands explicitly select the same new private environment and cache;
they do not synchronize the checkout's existing `.venv`. The pytest base
directory is also inside the new test directory. It must be a fresh path,
because pytest clears its own base directory on a subsequent run.

Record actual test counts, duration and exit status; do not reuse the earlier
1,359-test result as this machine's result. Source tests on Bazzite still are
not a full compiled-backend integration test. The installed full backend guard
and these lifecycle fixtures supply separate evidence. A complete API startup
or installed lifecycle harness needs its own reviewed, credentials-free,
network-disabled test configuration and receipt. If that harness is unavailable,
keep the integration item pending rather than calling fixture tests a full
runtime smoke test.

| Scenario | Expected outcome |
| --- | --- |
| Fixed LIMIT canceled, failed, timed out or ambiguously acknowledged | Attempt remains spent; no automatic replacement or market fallback |
| HTTP redirect or ambiguous AddOrder result | No second AddOrder POST |
| Restart after claim commit | Original claim survives; no replayed submission |
| Wrong/missing journal identity, permissions, mount or anchor | Fail closed; no initialization, repair or claim release |
| Partial fill, cancel race, duplicate trade, late fill | Retain exact order identities, deduplicate valid trades, preserve known cumulative fills |
| Status cumulative volume exceeds detailed trades | Accounting unavailable/provisional until matching trade evidence catches up |
| Apparent full fill without venue/business-ledger finality | Accounting remains visible; no automatic final archive or claim release |
| Temporary test account fixture restart | Cash/ownership and unresolved evidence remain coherent; no production journal touched |

## Acceptance and recovery

Report each stage as PASS, FAIL or NOT_RUN, with its precise evidence scope.
Compatibility is supported only for the machine, platform, Python, libraries,
dependencies and exact artifacts actually tested. A source/hash pass alone
cannot grant an installed-backend pass. A data-module ABI pass cannot grant a
full API compatibility pass. No test in this runbook proves a hedging profit.

Before considering an installed candidate ready for later deployment review,
require the exact repaired sources, complete dependency installation with
`pip check` exit 0, real immutable image provenance, target-machine artifact
comparison, bounded full installed guard, and completed isolated lifecycle
integration evidence. Keep any missing full API startup/restart test explicit.
Account-specific fees, venue reconciliation and live authorization remain
separate prerequisites; this session uses no private account credentials.

On a failed source, wheel, dependency or installed-guard check, stop that test
stage, retain its logs and receipts, and keep the subsequent dependent stages
NOT_RUN. Correct the disposable environment or artifact and repeat only the
affected checks. Never downgrade a required expected digest or bypass a guard
to turn a failure into a pass.

On a temporary test process/container failure, stop only the exact process or
container created by this test session. Preserve the fixture journal and claim
anchor together long enough to inspect the failure; neither is a production
journal. Remove only paths verified to be inside the newly created test
directory after useful evidence is retained. Do not use broad compose shutdown,
system-wide package removal, image pruning, or directory deletion.

If an unexpected production process, mount, journal, credential or venue route
is discovered in the test environment, stop new test dispatch and record the
boundary violation. Do not delete or reset production journals, alter claims,
auto-cancel orders, or restore an older coherent SQLite/anchor pair. Recovery of
real uncertainty requires the existing journal and anchor, an external trusted
checkpoint where needed, and venue/business-ledger reconciliation under the
separately authorized recovery process.

Keep the final receipts and the last verified test inputs. Return the machine
to its pre-test state by removing only the disposable environment and stopping
only test-owned processes. Production remains unchanged. If no MCP runner,
builder, full API checkout, immutable base digest or complete runtime exists,
report that exact blocker and the completed preparation; do not mark the
machine verified.
