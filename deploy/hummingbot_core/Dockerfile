# BuildKit is required for the private API checkout secret.
# Supply an immutable continuumio/miniconda3@sha256:<64 hex> reference.
# There is deliberately no default base image or overlay checksum.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS sources
ARG BASE_IMAGE
ARG OVERLAY_MANIFEST_SHA256

RUN python -c 'import re,sys; assert re.fullmatch(r"continuumio/miniconda3@sha256:[0-9a-f]{64}",sys.argv[1]), "An immutable Miniconda digest is required"' "$BASE_IMAGE"
RUN apt-get update && apt-get install -y --no-install-recommends git gcc g++ python3-dev build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY deploy/hummingbot_core /build/repair
RUN --mount=type=secret,id=github_token,required=true python /build/repair/apply_overlay.py --manifest /build/repair/overlay.manifest.json --expected-manifest-sha256 "$OVERLAY_MANIFEST_SHA256" --overlay-root /build/repair --base-image "$BASE_IMAGE" --prepare-sources /build
RUN python -c 'import pathlib,sys; pathlib.Path("/build/manifest.sha256").write_text(sys.argv[1]+"\n")' "$OVERLAY_MANIFEST_SHA256"

FROM sources AS wheel
# API environment provides the same Python ABI and TA-Lib libraries as runtime.
# All broad upstream dependencies are resolved here; they are not a frozen lock.
RUN conda env create -f /build/environment.no-pypi-core.yml && conda clean -afy
ENV PATH="/opt/conda/envs/hummingbot-api/bin:$PATH"
RUN python -m pip install "setuptools==80.8.0" wheel "cython>=3.0.12"
RUN python -m pip install -r /build/core-runtime-requirements.txt -r /build/core/setup/pip_packages.txt
RUN python -m pip wheel --no-deps --no-build-isolation /build/core --wheel-dir /build/wheels
RUN mkdir -p /build/evidence
RUN python -c 'import pathlib,subprocess,sys; wheels=list(pathlib.Path("/build/wheels").glob("hummingbot-*.whl")); assert len(wheels)==1,"Exactly one locally built core wheel required"; verified=subprocess.run([sys.executable,"/build/repair/apply_overlay.py","--manifest","/build/repair/overlay.manifest.json","--expected-manifest-sha256-file","/build/manifest.sha256","--verify-wheel",str(wheels[0])],check=True,capture_output=True,text=True); pathlib.Path("/build/evidence/wheel-identity.json").write_text(verified.stdout); subprocess.run([sys.executable,"-m","pip","install","--no-deps","--force-reinstall",str(wheels[0])],check=True)'
# This imports the installed compiled core but starts no API or connector.
RUN python /build/repair/apply_overlay.py --manifest /build/repair/overlay.manifest.json --expected-manifest-sha256-file /build/manifest.sha256 --verify-installed
RUN python -m pip check && conda list -n hummingbot-api --explicit > /build/evidence/conda-explicit.txt && python -m pip freeze --all > /build/evidence/pip-freeze.txt

FROM ${BASE_IMAGE} AS runtime
ARG BASE_IMAGE
ARG OVERLAY_MANIFEST_SHA256
LABEL org.spectra.api.repository="andredepaulnash-cmd/spectra-hummingbot-api" \
      org.spectra.api.source="84426ffbf9ba09522b4444664f9770236205ae29" \
      org.spectra.core.repository="hummingbot/hummingbot" \
      org.spectra.core.source="9af100d6822da7d2d0291a906c730ef172284ee2" \
      org.spectra.execution.contract="spectra-single-attempt-v1" \
      org.spectra.core.overlay.sha256="${OVERLAY_MANIFEST_SHA256}" \
      org.spectra.base.image="${BASE_IMAGE}"
RUN apt-get update && apt-get install -y --no-install-recommends libusb-1.0-0 && rm -rf /var/lib/apt/lists/*
COPY --from=wheel /opt/conda/envs/hummingbot-api /opt/conda/envs/hummingbot-api
ENV PATH="/opt/conda/envs/hummingbot-api/bin:$PATH"
ENV CONDA_DEFAULT_ENV=hummingbot-api
WORKDIR /hummingbot-api
COPY --from=sources /build/api/main.py /build/api/config.py /build/api/deps.py ./
COPY --from=sources /build/api/models ./models
COPY --from=sources /build/api/routers ./routers
COPY --from=sources /build/api/services ./services
COPY --from=sources /build/api/utils ./utils
COPY --from=sources /build/api/database ./database
COPY --from=sources /build/api/bots/controllers ./bots/controllers
COPY --from=sources /build/api/bots/scripts ./bots/scripts
COPY --from=sources /build/repair/apply_overlay.py /build/repair/overlay.manifest.json /build/repair/build.contract.json /opt/spectra-core/
COPY --from=sources /build/repair/licenses /opt/spectra-core/licenses
# Preserve exact upstream license bytes independently of wheel packaging.
RUN python -c 'import hashlib,pathlib; root=pathlib.Path("/opt/spectra-core/licenses"); expected={"hummingbot-core-LICENSE":"f0dd03c14db3dfbce03c166645f88fda96599ad1a5d217fddb49134de8c72a29","hummingbot-api-LICENSE":"a809254b8a99d3b1ea974cc6bce5eaf216a6bc575778a6ef35428a09e1ec0ebf"}; assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in expected.items()), "Pinned upstream license bytes required"; assert (root/"NOTICE").is_file(), "Spectra modification notice required"'
COPY --from=sources /build/manifest.sha256 /opt/spectra-core/manifest.sha256
COPY --from=wheel /build/evidence /opt/spectra-core/dependency-evidence
RUN mkdir -p bots/instances bots/conf bots/credentials bots/data bots/archived
# The existing private journal and its ID MUST be provided at runtime.
# Do not initialize a journal in the image: the persistent bots mount owns it.
EXPOSE 8000
ENTRYPOINT ["python", "/opt/spectra-core/apply_overlay.py", "--manifest", "/opt/spectra-core/overlay.manifest.json", "--expected-manifest-sha256-file", "/opt/spectra-core/manifest.sha256", "--runtime"]
