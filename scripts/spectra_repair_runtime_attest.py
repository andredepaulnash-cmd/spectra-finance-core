#!/usr/bin/env python3
"""Collect bounded repair fingerprints from explicit local paths, without activation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
from typing import Callable, Sequence

CONTRACT_SHA256 = "6fcebcf62fcd85599c9a6b1afba171b104e0ced7b22566fa5bfef3e96290027d"
PR33_COMMIT = "fad5f320b2f87cc7c089bea2750dd8344aa1b661"
PR36_COMMIT = "dd80d15b0f556ceb3088e9a5daf276ba1ba3a37a"
WHEEL_SHA256 = "b92048954145b055ac7bbf85cfdfa4e042c4153eb66aabc29740f4ec34a76dbc"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_WHEEL_BYTES = 32 * 1024 * 1024
MAX_COMMAND_BYTES = 16 * 1024
SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
VERSION_FORMAT = (
    '{"client_version":{{json .Client.Version}},'
    '"server_version":{{json .Server.Version}},'
    '"server_api_version":{{json .Server.APIVersion}}}'
)
IMAGE_FORMAT = (
    '{"id":{{json .Id}},"architecture":{{json .Architecture}},'
    '"os":{{json .Os}},"declared_revision":'
    '{{json (index .Config.Labels "org.opencontainers.image.revision")}}}'
)


class Unavailable(Exception):
    """Only a generic reason may be reported, never a private path or stderr."""


def _absolute_parts(path: Path) -> tuple[str, ...]:
    path = Path(path)
    if ".." in path.parts or len(str(path)) > 4096:
        raise Unavailable("unsafe local selector")
    path = path.absolute()
    if not path.is_absolute() or path.anchor != "/":
        raise Unavailable("unsupported local selector")
    return path.parts[1:]


def _open_directory(path: Path) -> int:
    """Traverse directory descriptors: no parent or final-component symlinks."""
    parts = _absolute_parts(path)
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current
    except (OSError, ValueError):
        os.close(current)
        raise Unavailable("local directory unavailable") from None


def read_regular_file(path: Path, limit: int) -> bytes:
    """Bound a regular-file read and refuse symlinks, devices and FIFOs."""
    parts = _absolute_parts(path)
    if not parts:
        raise Unavailable("local file unavailable")
    parent = _open_directory(Path("/").joinpath(*parts[:-1]))
    try:
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise Unavailable("local file unavailable")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise Unavailable("local file unavailable")
            return data
    except (OSError, ValueError):
        raise Unavailable("local file unavailable") from None
    finally:
        os.close(parent)


def read_selected_file(root: Path, relative: str, limit: int) -> bytes:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise Unavailable("invalid reviewed relative path")
    return read_regular_file(Path(root) / path, limit)


def _json_object(raw: bytes | str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError("invalid JSON constant")

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=bad_constant)
    except (ValueError, TypeError, RecursionError, UnicodeDecodeError):
        raise Unavailable("invalid JSON evidence") from None
    if not isinstance(value, dict):
        raise Unavailable("invalid JSON evidence")
    return value


def load_contract(path: Path) -> dict:
    """Contract tampering is rejected before any selected candidate file is read."""
    raw = read_regular_file(path, 64 * 1024)
    if hashlib.sha256(raw).hexdigest() != CONTRACT_SHA256:
        raise Unavailable("reviewed repair contract unavailable or mismatched")
    return _json_object(raw)


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and SAFE_TOKEN.fullmatch(value) else None


def _read_os_release() -> str:
    # /etc/os-release is frequently a symlink to /usr/lib/os-release. Read the
    # two named regular files without following links to other/private paths.
    for path in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            return read_regular_file(path, 16 * 1024).decode("utf-8", errors="strict")
        except (Unavailable, UnicodeDecodeError):
            pass
    raise Unavailable("OS release unavailable")


def host_facts(
    os_release_reader: Callable[[], str] = _read_os_release,
) -> dict:
    release = {}
    try:
        raw = os_release_reader()
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16 * 1024:
            raise Unavailable("OS release unavailable")
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VARIANT_ID", "VERSION_ID"}:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if _token(value):
                    release[key] = value
    except (Unavailable, OSError, UnicodeError, ValueError):
        pass
    libc = {"family": None, "version": None}
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION")
        family, version = value.split(" ", 1) if value else (None, None)
        if family == "glibc" and _token(version):
            libc = {"family": "glibc", "version": version}
    except (OSError, ValueError, AttributeError):
        pass
    return {
        "scope": "machine_where_this_command_executes",
        "os_release": release,
        "os_family": _token(platform.system()),
        "architecture": _token(platform.machine()),
        "kernel_release": _token(platform.release()),
        "collector_python_version": _token(platform.python_version()),
        "collector_python_implementation": _token(platform.python_implementation()),
        "collector_python_version_pair": list(sys.version_info[:2]),
        "libc": libc,
        "container_tool_presence": {
            name: shutil.which(name) is not None for name in ("docker", "podman", "buildah")
        },
        "remote_runner_identity_verified": False,
        "native_wheel_abi_verified": False,
        "libc_version_is_compatibility_proof": False,
    }


class BoundedRunner:
    """Run fixed metadata commands with bounded output and discarded stderr."""

    def __init__(self, config_directory: Path, timeout: float = 10.0) -> None:
        # No inherited DOCKER_HOST/context, credential config or environment is
        # used. The caller supplies an already existing empty config directory.
        self.environment = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "DOCKER_CONFIG": str(config_directory.absolute()),
        }
        self.timeout = timeout

    def __call__(self, argv: Sequence[str]) -> str:
        try:
            process = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, env=self.environment,
            )
        except (OSError, ValueError):
            raise Unavailable("metadata command unavailable") from None
        chunks = []

        def stop():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

        def capture():
            try:
                assert process.stdout is not None
                chunks.append(process.stdout.read(MAX_COMMAND_BYTES + 1))
                if len(chunks[0]) > MAX_COMMAND_BYTES:
                    stop()
            except (OSError, ValueError):
                chunks.append(b"")

        reader = threading.Thread(target=capture, daemon=True)
        reader.start()
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            stop()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            reader.join(timeout=1)
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()
            raise Unavailable("metadata command unavailable") from None
        reader.join(timeout=1)
        if reader.is_alive():
            stop()
            reader.join(timeout=1)
        if not reader.is_alive() and process.stdout is not None:
            process.stdout.close()
        if process.returncode != 0 or reader.is_alive() or not chunks or len(chunks[0]) > MAX_COMMAND_BYTES:
            raise Unavailable("metadata command unavailable")
        try:
            return chunks[0].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise Unavailable("metadata command unavailable") from None


def _require_local_socket(path: Path) -> None:
    parts = _absolute_parts(path)
    if not parts:
        raise Unavailable("local daemon socket unavailable")
    parent = _open_directory(Path("/").joinpath(*parts[:-1]))
    try:
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode):
            raise Unavailable("local daemon socket unavailable")
    finally:
        os.close(parent)


def image_facts(
    docker_socket: Path | None,
    docker_config_empty_dir: Path | None,
    image_id: str | None,
    runner: Callable[[Sequence[str]], str] | None = None,
) -> dict:
    result = {
        "status": "NOT_SUPPLIED", "observed_id": None, "declared_revision": None,
        "architecture": None, "os": None, "docker_versions": {},
        "matches_reviewed_image": None, "source_binding_proven": False,
        "expected_image_status": "PENDING_REVIEWED_IMAGE_NOT_BUILT",
    }
    if docker_socket is None and docker_config_empty_dir is None and image_id is None:
        return result
    if docker_socket is None or docker_config_empty_dir is None or not IMAGE_ID.fullmatch(image_id or ""):
        raise ValueError("image observations need an immutable image ID, local socket and empty config directory")
    socket_parts = _absolute_parts(docker_socket)
    if not socket_parts or not str(docker_socket).startswith("/") or "://" in str(docker_socket):
        raise ValueError("use an explicit absolute local Unix socket")
    result["status"] = "UNAVAILABLE"
    try:
        _require_local_socket(docker_socket)
        config = _open_directory(docker_config_empty_dir)
        try:
            if os.listdir(config):
                raise Unavailable("Docker config directory is not empty")
        finally:
            os.close(config)
        executable = shutil.which("docker", path=os.defpath)
        if executable is None and runner is None:
            raise Unavailable("Docker executable unavailable")
        run = runner or BoundedRunner(docker_config_empty_dir)
        docker = [executable or "docker", "--config", str(docker_config_empty_dir.absolute()),
                  "--host", "unix://" + str(docker_socket)]
        version_raw = run(docker + ["version", "--format", VERSION_FORMAT])
        image_raw = run(docker + ["image", "inspect", "--format", IMAGE_FORMAT, image_id])
        if len(version_raw.encode("utf-8")) > MAX_COMMAND_BYTES or len(image_raw.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise Unavailable("metadata command unavailable")
        versions = _json_object(version_raw)
        image = _json_object(image_raw)
        result["docker_versions"] = {key: _token(versions.get(key)) for key in (
            "client_version", "server_version", "server_api_version")}
        observed = image.get("id")
        result["observed_id"] = observed if isinstance(observed, str) and IMAGE_ID.fullmatch(observed) else None
        revision = image.get("declared_revision")
        result["declared_revision"] = revision if isinstance(revision, str) and SHA1.fullmatch(revision) else None
        result["architecture"] = _token(image.get("architecture"))
        result["os"] = _token(image.get("os"))
        result["status"] = "OBSERVED_UNBOUND" if result["observed_id"] == image_id else "IDENTITY_MISMATCH"
        # Labels and IDs are observations only; there is no reviewed image
        # digest/source-binding record with which to approve this image.
        return result
    except (Unavailable, OSError, ValueError, TypeError, AttributeError):
        return result


def _source_facts(root: Path | None, contract: dict) -> dict:
    result = {"status": "NOT_SUPPLIED", "scope": "32_selected_PR33_repair_and_build_files",
              "reference_commit": PR33_COMMIT, "whole_checkout_proven": False,
              "selected_file_bytes_match": False, "files": []}
    if root is None:
        return result
    for row in contract["repair_reference"]["selected_files"]:
        fact = {"path": row["path"], "status": "UNAVAILABLE", "sha256": None, "git_blob_sha": None}
        try:
            raw = read_selected_file(root, row["path"], MAX_SOURCE_BYTES)
            fact["sha256"] = hashlib.sha256(raw).hexdigest()
            fact["git_blob_sha"] = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            fact["status"] = "MATCH" if (
                fact["sha256"] == row["sha256"] and fact["git_blob_sha"] == row["git_blob_sha"]
                and len(raw) == row["bytes"]) else "MISMATCH"
        except (Unavailable, OSError, ValueError):
            pass
        result["files"].append(fact)
    result["selected_file_bytes_match"] = all(row["status"] == "MATCH" for row in result["files"])
    result["status"] = "MATCH" if result["selected_file_bytes_match"] else "MISSING_OR_MISMATCHED"
    return result


def _wheel_facts(path: Path | None, contract: dict) -> dict:
    result = {"status": "NOT_SUPPLIED", "sha256": None, "bytes": None,
              "matches_preferred_artifact": False, "native_abi_verified": False,
              "manylinux_certified": False}
    if path is None:
        return result
    try:
        raw = read_regular_file(path, MAX_WHEEL_BYTES)
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        result["bytes"] = len(raw)
        expected = contract["preferred_wheel"]
        result["matches_preferred_artifact"] = result["sha256"] == expected["sha256"] and len(raw) == expected["bytes"]
        result["status"] = "MATCH" if result["matches_preferred_artifact"] else "MISMATCH"
    except (Unavailable, OSError, ValueError):
        result["status"] = "UNAVAILABLE"
    return result


def _installed_facts(root: Path | None, contract: dict) -> dict:
    result = {"status": "NOT_SUPPLIED", "scope": "five_static_overlay_file_bytes_only",
              "five_overlay_hashes_match": False, "files": [],
              "compiled_package_identity": "UNKNOWN", "installed_api_identity": "UNKNOWN",
              "native_abi_verified": False, "runtime_semantics_verified": False}
    if root is None:
        return result
    for row in contract["installed_overlay_scope"]["files"]:
        fact = {"path": row["path"], "status": "UNAVAILABLE", "sha256": None}
        try:
            raw = read_selected_file(root, row["path"], MAX_SOURCE_BYTES)
            fact["sha256"] = hashlib.sha256(raw).hexdigest()
            fact["status"] = "MATCH" if fact["sha256"] == row["sha256"] and len(raw) == row["bytes"] else "MISMATCH"
        except (Unavailable, OSError, ValueError):
            pass
        result["files"].append(fact)
    result["five_overlay_hashes_match"] = all(row["status"] == "MATCH" for row in result["files"])
    result["status"] = "MATCH" if result["five_overlay_hashes_match"] else "MISSING_OR_MISMATCHED"
    return result


def collect(
    contract_path: Path,
    *,
    spectra_path: Path | None = None,
    wheel_path: Path | None = None,
    installed_root: Path | None = None,
    docker_socket: Path | None = None,
    docker_config_empty_dir: Path | None = None,
    image_id: str | None = None,
    os_release_reader: Callable[[], str] = _read_os_release,
    runner: Callable[[Sequence[str]], str] | None = None,
) -> dict:
    report = {
        "schema_version": 1, "scope": "repair_static_read_only_attestation",
        "collected_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "candidate_base_commit": PR36_COMMIT, "repair_reference_commit": PR33_COMMIT,
        "approved_for_live": False, "runtime_attestation_complete": False,
        "installed_bazzite_verified": False, "production_modified": False,
        "venue_requests_performed": False, "credentials_read": False,
        "private_journals_read": False, "candidate_code_imported": False,
        "services_started": False, "holdout_payloads_read": False,
        "collector_writes_files": False,
        "contract": {"status": "UNAVAILABLE_OR_MISMATCHED", "expected_sha256": CONTRACT_SHA256},
        "host": host_facts(os_release_reader),
        "source": {"status": "NOT_RUN"}, "wheel": {"status": "NOT_RUN"},
        "installed_overlays": {"status": "NOT_RUN"}, "image": {"status": "NOT_RUN"},
        "overall_status": "BLOCKED", "pending_proof": [],
    }
    try:
        contract = load_contract(contract_path)
    except (Unavailable, OSError, ValueError):
        report["pending_proof"] = ["reviewed_repair_contract_missing_or_mismatched"]
        return report
    report["contract"]["status"] = "MATCH"
    required = contract["host_requirements"]
    host = report["host"]
    host["observed_os_is_bazzite"] = host["os_release"].get("ID") == required["target_os_id"]
    host["static_preconditions_match"] = (
        host["observed_os_is_bazzite"] and host["os_family"] == required["os_family"]
        and host["architecture"] == required["architecture"]
        and tuple(host["collector_python_version_pair"]) >= tuple(required["collector_python_minimum"])
    )
    host["precondition_status"] = "MATCH" if host["static_preconditions_match"] else "MISSING_OR_UNSUPPORTED"
    report["source"] = _source_facts(spectra_path, contract)
    report["wheel"] = _wheel_facts(wheel_path, contract)
    report["installed_overlays"] = _installed_facts(installed_root, contract)
    report["image"] = image_facts(docker_socket, docker_config_empty_dir, image_id, runner)
    pending = {
        "reviewed_API_image_and_source_binding_not_available",
        "actual_native_ABI_and_full_backend_dependencies_not_verified_by_static_collector",
        "runtime_cancellation_late_fill_finality_and_restart_semantics_not_verified",
        "MCP_runner_and_actual_target_machine_identity_not_verified_by_this_collector",
        "whole_Spectra_checkout_and_full_installed_package_identity_not_proven",
    }
    if not host["static_preconditions_match"]:
        pending.add("target_Bazzite_host_preconditions_missing_or_unsupported")
    for name in ("source", "wheel", "installed_overlays"):
        if report[name]["status"] != "MATCH":
            pending.add(name + "_missing_or_mismatched")
    if report["image"]["status"] != "OBSERVED_UNBOUND":
        pending.add("local_image_metadata_missing_or_mismatched")
    report["pending_proof"] = sorted(pending)
    report["overall_status"] = "INCOMPLETE"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--spectra-path", type=Path)
    parser.add_argument("--wheel-path", type=Path)
    parser.add_argument("--installed-root", type=Path, help="explicit site-packages root; no Python package discovery/import")
    parser.add_argument("--docker-socket", type=Path)
    parser.add_argument("--docker-config-empty-dir", type=Path)
    parser.add_argument("--image-id")
    args = parser.parse_args(argv)
    if sys.version_info[:2] < (3, 12) or not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
        parser.error("run Python 3.12 or later with -I -S -B")
    if sys.flags.optimize:
        parser.error("run without -O or -OO")
    arguments = vars(args).copy()
    arguments.pop("contract")
    try:
        report = collect(args.contract, **arguments)
    except ValueError:
        parser.error("use explicit local paths and all three image metadata selectors")
    print(json.dumps(report, indent=2, sort_keys=True))
    # Static matches cannot become a complete operational/live attestation.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
