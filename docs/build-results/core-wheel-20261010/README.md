# PR33 compiled core wheel; API image and Bazzite still pending

The corrected Hummingbot core compiled successfully in an isolated Python 3.12
build environment. All 61 upstream Cython modules were produced as x86-64 ELF
extensions. The PR33 wheel guard accepted the five exact overlay files, and a
disposable installation passed a compiled data-class ABI check with no
connector, executor, or API import. This is concrete wheel evidence. It does
not establish a complete API image or an installed deployment.

The inherited PR33 source recipe, contracts, runtime collector and stack lock
remain byte-identical. No production branch, installation, account, journal,
order, or money movement was changed. The separate market-history acquisition
requested as item 3 remains deferred.

## Exact source identity

The preferred final artifact is
`hummingbot-20260922-cp312-cp312-linux_x86_64.whl`, **20,910,055 bytes**,
SHA256 **`b92048954145b055ac7bbf85cfdfa4e042c4153eb66aabc29740f4ec34a76dbc`**.
The final strict compiler completed in **299.32 seconds**, with exit 0.
Its 1,007 archive members include all 61 compiled extensions and the exact
upstream Apache-2.0 license. `wheel-identity.json` records every ELF checksum.

| Input | Verified identity |
|---|---|
| Spectra PR33 | `fad5f320b2f87cc7c089bea2750dd8344aa1b661` |
| PR33 complete source tree | `6d15736a95b20bbd0e5aee8e34a07217f8143b91` |
| Official core commit | `9af100d6822da7d2d0291a906c730ef172284ee2` |
| Official core tree | `06c31cf98f8573c2f2a1c819bcfbcebe56d8ee2a` |
| Private API source reference | `84426ffbf9ba09522b4444664f9770236205ae29` |
| API tree reference | `25f37e7712de3a5bf9b0ae443b360c796c8b7b87` |
| Overlay manifest SHA256 | `c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77` |

The full 1,892-entry core Git tree was independently compared with GitHub's
exact pinned tree before overlay application. Sixteen PR33 build/reference
files and eleven pinned core/API source fragments were checked by Git blob
identity and SHA256. No full private API checkout or API compilation was
performed. The API environment and recipe were verified as reference inputs.

The portable compiler checks every physical tracked input against the pinned
tree and executable mode, admitting only the four exact tracked overlays and
one exact new guard. It rejects extra or ignored files, symlinks, prior build
outputs, configured filters and included Git configuration. It does not trust
index flags or `git diff` to prove clean physical source.

## What passed and what remains open

| Evidence | Result and limit |
|---|---|
| Core wheel compilation | Passed; all 61 pinned Cython extensions present |
| Five overlaid wheel member hashes | Passed using unchanged PR33 wheel guard |
| Isolated installation patch hashes | Passed; all five match the frozen manifest |
| Compiled data-module ABI | Passed for `OrderBookQueryResult`, including installed binary-to-wheel checksum |
| Portable source and refusal tests | 18 passed locally; GitHub CI checks these same bounded source tests |
| Full runtime dependencies | Incomplete; `pip check` exits 1, with 40 findings recorded |
| Conda/API runtime environment | Not created; this build used an isolated venv inheriting base package directories |
| Full installed backend guard | Not run; no connector or executor module was imported |
| Container image | Not built; Docker, Podman, Buildah and usable Docker sockets absent |
| Immutable Miniconda base image | No independently reviewed digest supplied |
| Actual Bazzite host | Unavailable; observed build host is Ubuntu 24.04 on x86-64 |
| Production/live approval | Remains false |

The build used CPython 3.12.14, GCC/G++, Cython 3.0.12, setuptools 80.8.0,
wheel 0.48.0 and NumPy 2.3.5. Its inherited pandas 2.2.3 is below the API/core
runtime requirement. The dependency snapshot records this and other missing
or incompatible runtime packages. The wheel has the `cp312-cp312-linux_x86_64`
tag; it is not a manylinux portability certification. A matching Python ABI
does not prove the Bazzite library set is compatible.

Builds used two Cython generation workers by overriding `os.cpu_count` in the
disposable build process, leaving `setup.py` byte-identical. The initial full
compile took about five minutes. This is a measured wheel-build duration,
not an ETA for the remaining API image or Bazzite validation. Dependency
resolution is not fully locked and binary reproducibility is not established.

## Reproduce the isolated source and wheel checks

Use a disposable core checkout and separate empty wheel-output directory.
The commands below compile source; they neither start the API nor instantiate
an exchange connector. Use the exact recorded build package versions for this
candidate; a complete API runtime needs its separately resolved dependencies.

```sh
git clone --no-checkout https://github.com/hummingbot/hummingbot.git /tmp/spectra-core-wheel-checkout
git -C /tmp/spectra-core-wheel-checkout checkout --detach 9af100d6822da7d2d0291a906c730ef172284ee2
python3.12 -m venv --system-site-packages /tmp/spectra-core-wheel-venv
/tmp/spectra-core-wheel-venv/bin/python -m pip install setuptools==80.8.0 wheel==0.48.0 Cython==3.0.12 numpy==2.3.5 pandas==2.2.3
python3.12 -I -S -B scripts/spectra_isolated_build_preflight.py --source-root .
python3.12 -I -S -B deploy/hummingbot_core/apply_overlay.py --manifest deploy/hummingbot_core/overlay.manifest.json --expected-manifest-sha256 c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77 --overlay-root deploy/hummingbot_core --apply /tmp/spectra-core-wheel-checkout
python3.12 -I -S -B scripts/spectra_compile_isolated_core_wheel.py --core-checkout /tmp/spectra-core-wheel-checkout --manifest deploy/hummingbot_core/overlay.manifest.json --wheel-dir /tmp/spectra-core-wheels --verify-source-only
/tmp/spectra-core-wheel-venv/bin/python -B scripts/spectra_compile_isolated_core_wheel.py --core-checkout /tmp/spectra-core-wheel-checkout --manifest deploy/hummingbot_core/overlay.manifest.json --wheel-dir /tmp/spectra-core-wheels
python3.12 -I -S -B deploy/hummingbot_core/apply_overlay.py --manifest deploy/hummingbot_core/overlay.manifest.json --expected-manifest-sha256 c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77 --verify-wheel /tmp/spectra-core-wheels/hummingbot-20260922-cp312-cp312-linux_x86_64.whl
```

After independently recording the resulting wheel SHA256, install only in the
disposable venv and supply that digest to the ABI checker:

```sh
/tmp/spectra-core-wheel-venv/bin/python -m pip install --no-index --no-deps /tmp/spectra-core-wheels/hummingbot-20260922-cp312-cp312-linux_x86_64.whl
/tmp/spectra-core-wheel-venv/bin/python -I -S -B scripts/spectra_check_compiled_data_module.py --manifest deploy/hummingbot_core/overlay.manifest.json --wheel /tmp/spectra-core-wheels/hummingbot-20260922-cp312-cp312-linux_x86_64.whl --expected-wheel-sha256 <recorded-wheel-sha256>
```

The ABI checker refuses `-O`/`-OO`, skips site customization and `.pth` execution,
and uses a Python audit hook to refuse socket operations and process launch
after its input checks. That hook is a bounded test precaution, not an operating
system network sandbox. The check loads one compiled data module and its logger
dependencies, creates only an inert data object, and verifies that connector
and executor modules were not imported. It does not test the full backend.

The build preflight's exit 0 means only that its sixteen input comparisons
passed. It never grants builder readiness, installed-image approval or trading
authority. CI does not install unpinned dependencies or rebuild the wheel.

## Next operational prerequisite

Provide an isolated Docker BuildKit builder, an independently reviewed immutable
Miniconda digest and the intended platform. The exact inherited Docker recipe
then needs separately supplied private API read access through its BuildKit
secret and a complete resolved dependency environment. Record and review the
compiled installed-module guard, dependency manifests, wheel checksum, image
digest and build provenance before any deployment decision.

The inherited read-only Bazzite collector remains available in
`scripts/spectra_runtime_attest.py`, with usage in
`docs/spectra-runtime-attestation.md`. Its existing source contract still
describes the PR31 baseline. It can collect generic host/container facts but
cannot establish that the PR33 repair is installed. A future installed-image
comparison must bind this new wheel/image evidence and the corrected overlay
hashes without interpreting the old baseline match as deployment approval.

The operational stack lock remains `PARTIAL`, and no report or wheel changes
`approved_for_live: false`.
