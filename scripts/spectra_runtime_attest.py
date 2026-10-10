#!/usr/bin/env python3
"""Collect static, read-only runtime facts without granting live authority."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import stat
import subprocess
import threading
from pathlib import Path
from typing import Callable, Sequence

EXPECTED_SPECTRA_HEAD = "24b152457d9a8b3e007e69e7879bab38d1beb254"
EXPECTED_API_HEAD = "84426ffbf9ba09522b4444664f9770236205ae29"
REVIEWED_CORE_SOURCE_HEAD = "9af100d6822da7d2d0291a906c730ef172284ee2"
CORE_FILES = (
    "hummingbot/strategy_v2/executors/order_executor/data_types.py",
    "hummingbot/strategy_v2/executors/order_executor/order_executor.py",
    "hummingbot/connector/exchange/kraken/kraken_exchange.py",
    "hummingbot/strategy_v2/models/executors.py",
    "hummingbot/core/data_type/common.py",
)
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
SAFE_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,95}\Z")
SAFE_VERSION = re.compile(r"[0-9][a-zA-Z0-9.+_-]{0,95}\Z")
REPOSITORY_PATTERN = (
    r"(?:(?:[a-z0-9.-]+)(?::[0-9]{1,5})?/)?"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
)
REPO_DIGEST = re.compile(REPOSITORY_PATTERN + r"@sha256:[0-9a-f]{64}\Z")
SAFE_IMAGE = re.compile(
    REPOSITORY_PATTERN
    + r"(?::[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})?\Z"
)
MAX_COMMAND_BYTES = 64 * 1024
COMMAND_TIMEOUT = 15.0

CONTAINER_FORMAT = (
    '{"id":{{json .Id}},"image_id":{{json .Image}},'
    '"configured_image":{{json .Config.Image}},'
    '"running":{{json .State.Running}},"status":{{json .State.Status}}}'
)
IMAGE_FORMAT = (
    '{"id":{{json .Id}},"repo_digests":{{json .RepoDigests}},'
    '"declared_revision":{{json (index .Config.Labels '
    '"org.opencontainers.image.revision")}}}'
)
VERSION_FORMAT = (
    '{"client_version":{{json .Client.Version}},'
    '"server_version":{{json .Server.Version}},'
    '"server_api_version":{{json .Server.APIVersion}}}'
)

# No Hummingbot/config/executor import occurs here. find_spec of a top-level
# package discovers its path without executing that package's __init__.py.
CORE_PROBE = r"""
import email.parser, hashlib, importlib.metadata, importlib.util, json, os, pathlib, signal, stat, sys, sysconfig
# A timed-out Docker CLI can leave exec running; bound the probe independently.
signal.alarm(10)
for key in ("purelib", "platlib"):
    directory = sysconfig.get_path(key)
    if directory and directory not in sys.path:
        sys.path.append(directory)
names = (
    "hummingbot/strategy_v2/executors/order_executor/data_types.py",
    "hummingbot/strategy_v2/executors/order_executor/order_executor.py",
    "hummingbot/connector/exchange/kraken/kraken_exchange.py",
    "hummingbot/strategy_v2/models/executors.py",
    "hummingbot/core/data_type/common.py",
)
out = {"package_version": None, "declared_vcs_revision": None,
       "editable": None, "files_sha256": {}}
def small_regular(path, limit):
    fd = os.open(pathlib.Path(path).resolve(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file exceeds limit")
    return data.decode("utf-8", errors="strict")
try:
    dist = importlib.metadata.distribution("hummingbot")
    metadata_root = pathlib.Path(dist._path)
    metadata_text = small_regular(metadata_root / "METADATA", 1024 * 1024)
    out["package_version"] = email.parser.Parser().parsestr(metadata_text).get("Version")
    text = small_regular(metadata_root / "direct_url.json", 16384)
    if text:
        direct = json.loads(text)
        vcs = direct.get("vcs_info", {})
        if vcs.get("vcs") == "git":
            out["declared_vcs_revision"] = vcs.get("commit_id")
        editable = direct.get("dir_info", {}).get("editable")
        if isinstance(editable, bool):
            out["editable"] = editable
except Exception:
    pass
try:
    spec = importlib.util.find_spec("hummingbot")
    roots = list(spec.submodule_search_locations or []) if spec else []
    if len(roots) == 1:
        root = pathlib.Path(roots[0]).resolve()
        for name in names:
            path = (root / name.removeprefix("hummingbot/")).resolve()
            if not path.is_relative_to(root):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        continue
                    data = stream.read(2 * 1024 * 1024 + 1)
                if len(data) <= 2 * 1024 * 1024:
                    out["files_sha256"][name] = hashlib.sha256(data).hexdigest()
            except Exception:
                pass
except Exception:
    pass
print(json.dumps(out, sort_keys=True))
signal.alarm(0)
""".strip()


class EvidenceUnavailable(Exception):
    """A command failed; command diagnostics must never enter the report."""


class BoundedRunner:
    """Bound time and captured output; stderr is discarded, never reported."""

    def __init__(
        self,
        timeout: float = COMMAND_TIMEOUT,
        max_bytes: int = MAX_COMMAND_BYTES,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes

    def __call__(self, argv: Sequence[str]) -> str:
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise EvidenceUnavailable("command unavailable") from None
        chunks: list[bytes] = []

        def terminate_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

        def capture() -> None:
            try:
                assert process.stdout is not None
                chunks.append(process.stdout.read(self.max_bytes + 1))
                if len(chunks[0]) > self.max_bytes:
                    terminate_group()
            except (OSError, ValueError):
                chunks.append(b"")

        reader = threading.Thread(target=capture, daemon=True)
        reader.start()
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            terminate_group()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            reader.join(timeout=1)
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()
            raise EvidenceUnavailable("command timed out") from None
        reader.join(timeout=1)
        if reader.is_alive():
            terminate_group()
            reader.join(timeout=1)
        if not reader.is_alive() and process.stdout is not None:
            process.stdout.close()
        if reader.is_alive() or not chunks or len(chunks[0]) > self.max_bytes:
            raise EvidenceUnavailable("command output unavailable")
        if process.returncode != 0:
            raise EvidenceUnavailable("command failed")
        try:
            return chunks[0].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise EvidenceUnavailable("command output unavailable") from None


def read_regular_file(path: Path, limit: int) -> bytes:
    """Never wait on a FIFO/device, follow a last-moment symlink, or read unbounded."""
    try:
        fd = os.open(path.resolve(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise EvidenceUnavailable("evidence file unavailable")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise EvidenceUnavailable("evidence file unavailable")
        return data
    except (OSError, ValueError, RuntimeError):
        raise EvidenceUnavailable("evidence file unavailable") from None


def read_os_release() -> str:
    try:
        return read_regular_file(Path("/etc/os-release"), 16 * 1024).decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError:
        raise EvidenceUnavailable("host release unavailable") from None


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and SAFE_TOKEN.fullmatch(value) else None


def _version(value: object) -> str | None:
    return value if isinstance(value, str) and SAFE_VERSION.fullmatch(value) else None


def _sha(value: object, pattern: re.Pattern[str] = SHA1) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _object(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise EvidenceUnavailable("invalid evidence")
    try:

        def unique_object(pairs: list[tuple[str, object]]) -> dict:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate evidence key")
                result[key] = value
            return result

        def invalid_constant(_: str) -> None:
            raise ValueError("invalid evidence constant")

        result = json.loads(
            text, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
    except (ValueError, TypeError, RecursionError):
        raise EvidenceUnavailable("invalid evidence") from None
    if not isinstance(result, dict):
        raise EvidenceUnavailable("invalid evidence")
    return result


def load_core_reference(path: Path) -> dict[str, str]:
    """Load only this reviewed contract's five hashes, never deployment pins."""
    try:
        data = read_regular_file(path, MAX_COMMAND_BYTES)
        contract = _object(data.decode("utf-8", errors="strict"))
        references = contract["source_references"]
        core = references["hummingbot_core"]
        hashes = core["file_sha256"]
        valid = (
            type(contract.get("schema_version")) is int
            and contract.get("schema_version") == 1
            and contract.get("verification_only") is True
            and contract.get("approved_for_live") is False
            and contract.get("installed_stack_attested") is False
            and core.get("reference_only") is True
            and core.get("approved_deployment_target") is False
            and core.get("revision") == REVIEWED_CORE_SOURCE_HEAD
            and references["spectra_execution_candidate"].get("revision")
            == EXPECTED_SPECTRA_HEAD
            and references["spectra_hummingbot_api"].get("revision")
            == EXPECTED_API_HEAD
            and isinstance(hashes, dict)
            and set(hashes) == set(CORE_FILES)
            and all(_sha(value, SHA256) for value in hashes.values())
        )
        if not valid:
            raise EvidenceUnavailable("invalid source reference")
        return hashes
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise EvidenceUnavailable("source reference unavailable") from None


def collect_evidence(
    spectra_path: Path,
    api_path: Path,
    *,
    container: str = "hummingbot-api",
    docker_socket: str = "/var/run/docker.sock",
    runner: Callable[[Sequence[str]], str] | None = None,
    os_release_reader: Callable[[], str] = read_os_release,
    core_hash_reference: dict[str, str] | None = None,
) -> dict:
    """Return sanitized observations; incomplete facts always fail closed."""
    if not isinstance(container, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container
    ):
        raise ValueError("invalid container selector")
    if (
        not isinstance(docker_socket, str)
        or not docker_socket.startswith("/")
        or not docker_socket.endswith(".sock")
        or len(docker_socket) > 1024
        or "\x00" in docker_socket
        or "://" in docker_socket
    ):
        raise ValueError("invalid local Docker socket")
    run = runner or BoundedRunner()
    missing: set[str] = {
        "api_image_source_binding_not_verified",
        "runtime_execution_semantics_not_proven",
    }
    report = {
        "schema_version": 1,
        "collected_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "approved_for_live": False,
        "runtime_attestation_complete": False,
        "host": {"os_release": {}, "booted_deployment": None},
        "docker": {},
        "repositories": {},
        "api_container": {},
        "api_image": {},
        "hummingbot_core": {},
    }
    docker = ["docker", "--host", f"unix://{docker_socket}"]

    def observed(argv: Sequence[str], reason: str) -> dict:
        try:
            return _object(run(argv))
        except Exception:
            missing.add(reason)
            return {}

    try:
        release = os_release_reader()
        if not isinstance(release, str) or len(release) > 16 * 1024:
            raise EvidenceUnavailable("host release unavailable")
        for line in release.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VARIANT_ID", "VERSION_ID"}:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if _token(value):
                    report["host"]["os_release"][key] = value
    except Exception:
        missing.add("os_release_unavailable")
    if report["host"]["os_release"].get("ID") != "bazzite":
        missing.add("host_is_not_verified_bazzite")
    ostree = observed(["rpm-ostree", "status", "--json"], "ostree_status_unavailable")
    deployments = ostree.get("deployments")
    if isinstance(deployments, list):
        booted = [
            item
            for item in deployments
            if isinstance(item, dict) and item.get("booted") is True
        ]
        if len(booted) == 1 and _sha(booted[0].get("checksum"), SHA256):
            item = booted[0]
            report["host"]["booted_deployment"] = {
                "checksum": item["checksum"],
                "base_checksum": _sha(item.get("base-checksum"), SHA256),
                "version": _version(item.get("version")),
                "staged": (
                    item.get("staged") if isinstance(item.get("staged"), bool) else None
                ),
            }
    if report["host"]["booted_deployment"] is None:
        missing.add("booted_ostree_deployment_unavailable")

    versions = observed(
        docker + ["version", "--format", VERSION_FORMAT], "docker_version_unavailable"
    )
    report["docker"] = {
        key: _version(versions.get(key))
        for key in ("client_version", "server_version", "server_api_version")
    }
    if any(value is None for value in report["docker"].values()):
        missing.add("docker_version_incomplete")

    for label, supplied, expected in (
        ("spectra", spectra_path, EXPECTED_SPECTRA_HEAD),
        ("api", api_path, EXPECTED_API_HEAD),
    ):
        facts = {
            "head": None,
            "clean": None,
            "root_matches_requested": False,
            "matches_reviewed_head": False,
        }
        report["repositories"][label] = facts
        try:
            path = Path(supplied).expanduser().resolve()
            command = [
                "git",
                "--no-optional-locks",
                "-C",
                str(path),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
            ]
            root = run(command + ["rev-parse", "--show-toplevel"]).strip()
            if not root or Path(root).resolve() != path:
                missing.add(f"{label}_repository_root_mismatch")
                continue
            facts["root_matches_requested"] = True
            head = _sha(run(command + ["rev-parse", "--verify", "HEAD"]).strip())
            if not head:
                raise EvidenceUnavailable("repository revision unavailable")
            facts["head"] = head
            facts["matches_reviewed_head"] = head == expected
            if not facts["matches_reviewed_head"]:
                missing.add(f"{label}_repository_revision_mismatch")
            # Clean/process filter commands may execute application code during
            # status. Inspect names only, never their values, and fail closed.
            names = run(command + ["config", "--null", "--list", "--name-only"])
            if (
                not isinstance(names, str)
                or len(names.encode("utf-8")) > MAX_COMMAND_BYTES
            ):
                raise EvidenceUnavailable("repository configuration unavailable")
            if any(name.lower().startswith("filter.") for name in names.split("\x00")):
                missing.add(f"{label}_repository_filters_prevent_safe_status")
                continue
            # A submodule's status could run its own filters. No submodule
            # checkout is traversed; require a separate review if present.
            index = run(command + ["ls-files", "--format=%(objectmode)", "--null"])
            if (
                not isinstance(index, str)
                or len(index.encode("utf-8")) > MAX_COMMAND_BYTES
            ):
                raise EvidenceUnavailable("repository index unavailable")
            if any(item == "160000" for item in index.split("\x00")):
                missing.add(f"{label}_repository_submodules_prevent_safe_status")
                continue
            status = run(
                command + ["status", "--porcelain=v1", "--untracked-files=normal"]
            )
            if (
                not isinstance(status, str)
                or len(status.encode("utf-8")) > MAX_COMMAND_BYTES
            ):
                raise EvidenceUnavailable("repository status unavailable")
            facts["clean"] = status == ""
            if not facts["clean"]:
                missing.add(f"{label}_repository_dirty")
        except Exception:
            missing.add(f"{label}_repository_unavailable")

    inspected = observed(
        docker + ["container", "inspect", "--format", CONTAINER_FORMAT, container],
        "api_container_unavailable",
    )
    image_id = _sha(inspected.get("image_id"), IMAGE_ID)
    configured = inspected.get("configured_image")
    if (
        not isinstance(configured, str)
        or not SAFE_IMAGE.fullmatch(configured)
        or "://" in configured
    ):
        configured = None
    immutable_reference = bool(configured and REPO_DIGEST.fullmatch(configured))
    report["api_container"] = {
        "id": _sha(inspected.get("id"), SHA256),
        "image_id": image_id,
        "configured_image": configured,
        "configured_reference_is_digest": immutable_reference,
        "running": (
            inspected.get("running")
            if isinstance(inspected.get("running"), bool)
            else None
        ),
        "status": (
            inspected.get("status")
            if isinstance(inspected.get("status"), str)
            and inspected.get("status")
            in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }
            else None
        ),
    }
    if report["api_container"]["id"] is None or image_id is None:
        missing.add("api_container_identity_incomplete")
    if report["api_container"]["running"] is not True:
        missing.add("api_container_not_running")
    if not immutable_reference:
        missing.add("api_container_uses_mutable_or_unknown_reference")
    if image_id:
        inspected_image = observed(
            docker + ["image", "inspect", "--format", IMAGE_FORMAT, image_id],
            "api_image_unavailable",
        )
        digests = inspected_image.get("repo_digests")
        safe_digests = (
            sorted(
                {
                    value
                    for value in digests
                    if isinstance(value, str) and REPO_DIGEST.fullmatch(value)
                }
            )
            if isinstance(digests, list)
            else []
        )
        report["api_image"] = {
            "id": _sha(inspected_image.get("id"), IMAGE_ID),
            "repo_digests": safe_digests,
            "declared_revision": _sha(inspected_image.get("declared_revision")),
        }
        if report["api_image"]["id"] != image_id:
            missing.add("api_image_identity_mismatch")
        if not safe_digests:
            missing.add("api_image_repository_digest_missing")
        if immutable_reference and configured not in safe_digests:
            missing.add("api_image_configured_digest_mismatch")
        if report["api_image"]["declared_revision"] != EXPECTED_API_HEAD:
            missing.add("api_image_declared_revision_missing_or_mismatched")

    container_id = report["api_container"]["id"]
    if report["api_container"]["running"] is True and image_id and container_id:
        # The exec is a fixed, inert metadata/hash probe; never an arbitrary command.
        core = observed(
            docker
            + ["exec", container_id, "python", "-I", "-S", "-B", "-c", CORE_PROBE],
            "installed_core_probe_unavailable",
        )
        hashes = core.get("files_sha256")
        safe_hashes = (
            {name: _sha(hashes.get(name), SHA256) for name in CORE_FILES}
            if isinstance(hashes, dict)
            else {}
        )
        report["hummingbot_core"] = {
            "package_version": _version(core.get("package_version")),
            "declared_vcs_revision": _sha(core.get("declared_vcs_revision")),
            "editable": (
                core.get("editable") if isinstance(core.get("editable"), bool) else None
            ),
            "files_sha256": safe_hashes,
        }
        if not report["hummingbot_core"]["package_version"]:
            missing.add("installed_core_distribution_missing")
        if (
            report["hummingbot_core"]["declared_vcs_revision"]
            != REVIEWED_CORE_SOURCE_HEAD
        ):
            missing.add("installed_core_declared_revision_missing_or_mismatched")
        if report["hummingbot_core"]["editable"] is True:
            missing.add("installed_core_editable_distribution")
        if any(not safe_hashes.get(name) for name in CORE_FILES):
            missing.add("installed_core_source_hashes_incomplete")
        if core_hash_reference is not None:
            valid_reference = (
                isinstance(core_hash_reference, dict)
                and set(core_hash_reference) == set(CORE_FILES)
                and all(_sha(value, SHA256) for value in core_hash_reference.values())
            )
            if not valid_reference:
                missing.add("core_source_hash_reference_invalid")
            else:
                matches = safe_hashes == core_hash_reference
                report["hummingbot_core"]["matches_reviewed_source_hashes"] = matches
                if not matches:
                    missing.add("installed_core_source_hashes_mismatch")
        else:
            missing.add("core_source_hash_reference_not_supplied")
    else:
        missing.add("installed_core_probe_not_run")

    report["missing_evidence"] = sorted(missing)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spectra-path", type=Path, default=Path("~/SpectraStack/condor")
    )
    parser.add_argument(
        "--api-path", type=Path, default=Path("~/SpectraStack/hummingbot-api")
    )
    parser.add_argument("--container", default="hummingbot-api")
    parser.add_argument("--docker-socket", default="/var/run/docker.sock")
    parser.add_argument(
        "--contract", type=Path, help="optional reviewed source-reference contract"
    )
    args = parser.parse_args(argv)
    reference = None
    reference_unavailable = False
    if args.contract is not None:
        try:
            reference = load_core_reference(args.contract)
        except EvidenceUnavailable:
            reference_unavailable = True
    try:
        report = collect_evidence(
            args.spectra_path,
            args.api_path,
            container=args.container,
            docker_socket=args.docker_socket,
            core_hash_reference=reference,
        )
    except ValueError:
        parser.error(
            "invalid collector selector; use a container name and local Unix socket"
        )
    if reference_unavailable:
        report["missing_evidence"].append(
            "source_reference_contract_unavailable_or_invalid"
        )
        report["missing_evidence"].sort()
    print(json.dumps(report, indent=2, sort_keys=True))
    # Missing evidence never becomes a successful attestation exit code.
    return 2 if report["missing_evidence"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
