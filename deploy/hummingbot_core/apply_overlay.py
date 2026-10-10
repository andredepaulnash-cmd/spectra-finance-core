"""Verify and apply the Spectra core repair; never place orders.

This build helper accepts Git checkouts, not source tarballs. All manifest,
path, baseline, and replacement validation finishes before any source write.
The runtime command verifies the installed wheel before handing off to uvicorn.
"""

import argparse
import ast
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

CORE_REPOSITORY = "hummingbot/hummingbot"
CORE_COMMIT = "9af100d6822da7d2d0291a906c730ef172284ee2"
API_REPOSITORY = "andredepaulnash-cmd/spectra-hummingbot-api"
API_COMMIT = "84426ffbf9ba09522b4444664f9770236205ae29"
CONTRACT = "spectra-single-attempt-v1"
API_ENV_SHA256 = "82131a516ad12161e74322082ddee9d9aecd3ab6db8c098ca2d9a5addfb66321"
CORE_SETUP_SHA256 = "ba4c5760e8ece5993a2e9bfc5d4381318b142674208bc65fc5a0649d209f6c6a"
CORE_PIP_SHA256 = "6bfa6b389b6f2c00dec4443d7bfe50a177cd7ddf8b417221c7d5e3964ec55b17"
LICENSE_SHA256 = {
    "hummingbot-core-LICENSE": "f0dd03c14db3dfbce03c166645f88fda96599ad1a5d217fddb49134de8c72a29",
    "hummingbot-api-LICENSE": "a809254b8a99d3b1ea974cc6bce5eaf216a6bc575778a6ef35428a09e1ec0ebf",
}
MAX_SOURCE_BYTES = 8 * 1024 * 1024
ALLOWED_FILES = {
    "hummingbot/strategy_v2/executors/order_executor/order_executor.py": "patches/order_executor.py",
    "hummingbot/connector/exchange/kraken/kraken_exchange.py": "patches/kraken_exchange.py",
    "hummingbot/strategy_v2/executors/order_executor/spectra_attempt_guard.py": "patches/spectra_attempt_guard.py",
    "hummingbot/core/web_assistant/connections/rest_connection.py": "patches/rest_connection.py",
    "hummingbot/connector/client_order_tracker.py": "patches/client_order_tracker.py",
}
NEW_FILE = "hummingbot/strategy_v2/executors/order_executor/spectra_attempt_guard.py"
REQUIRED_CONFIG_FIELDS = {
    "id",
    "connector_name",
    "trading_pair",
    "side",
    "amount",
    "price",
    "execution_strategy",
    "position_action",
}


class OverlayError(ValueError):
    """A source, build, or runtime binding could not be established."""


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise OverlayError("Expected a lowercase SHA-256 digest")
    return value


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise OverlayError("Invalid relative file path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise OverlayError("Path traversal or noncanonical path rejected")
    if PurePosixPath(value).is_absolute():
        raise OverlayError("Absolute archive or overlay path rejected")
    return parts


def _private_path(path):
    """Reject symlinks in every existing absolute component."""
    value = Path(path).absolute()
    for component in (value, *value.parents):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise OverlayError("Symlink path component rejected")
    return value


def _directory(path):
    path = _private_path(path)
    if not path.is_dir():
        raise OverlayError("Expected an existing directory")
    return path


def _regular(path, maximum=MAX_SOURCE_BYTES):
    path = _private_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise OverlayError("Expected a readable regular file") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise OverlayError("Nonregular or oversized file rejected")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            result = stream.read(maximum + 1)
        if len(result) > maximum:
            raise OverlayError("Oversized file rejected")
        return result
    finally:
        os.close(descriptor)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise OverlayError("Duplicate JSON key rejected")
        result[key] = value
    return result


def load_manifest(path, expected_sha256):
    data = _regular(path)
    if _sha256(data) != _hash(expected_sha256):
        raise OverlayError("Overlay manifest checksum mismatch")
    try:
        manifest = json.loads(data, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise OverlayError("Invalid overlay manifest") from error
    if not isinstance(manifest, dict):
        raise OverlayError("Overlay manifest must be an object")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise OverlayError("Unsupported overlay manifest schema")
    if manifest.get("upstream") != {
        "repository": CORE_REPOSITORY,
        "commit": CORE_COMMIT,
    }:
        raise OverlayError("Unexpected upstream core identity")
    if manifest.get("contract") != CONTRACT:
        raise OverlayError("Unexpected execution contract")
    rows = manifest.get("files")
    if not isinstance(rows, list) or len(rows) != len(ALLOWED_FILES):
        raise OverlayError("The complete five-file overlay is required")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise OverlayError("Invalid overlay file record")
        path = row.get("path")
        overlay = row.get("overlay")
        _relative(path)
        _relative(overlay)
        if path in seen or ALLOWED_FILES.get(path) != overlay:
            raise OverlayError("Duplicate or unapproved overlay file")
        seen.add(path)
        _hash(row.get("sha256"))
        if path == NEW_FILE:
            if row.get("upstream_sha256") is not None:
                raise OverlayError("New guard must not have an upstream checksum")
        else:
            _hash(row.get("upstream_sha256"))
    return manifest


def _git(root, arguments, env=None):
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "credential.helper=",
        "-C",
        str(root),
        *arguments,
    ]
    clean_env = dict(os.environ if env is None else env)
    clean_env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    try:
        result = subprocess.run(
            command,
            env=clean_env,
            capture_output=True,
            check=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OverlayError("Pinned Git source operation failed") from error
    return result.stdout.decode("utf-8").strip()


def verify_checkout(root, commit):
    root = _directory(root)
    if _git(root, ["rev-parse", "HEAD"]) != commit:
        raise OverlayError("Git checkout does not match the required source commit")
    configuration = _git(root, ["config", "--local", "--list"])
    if any(line.lower().startswith("filter.") for line in configuration.splitlines()):
        raise OverlayError(
            "Configured Git filters are not allowed during source verification"
        )
    if _git(root, ["status", "--porcelain=v1", "--untracked-files=normal"]):
        raise OverlayError(
            "Pinned Git checkout must be clean before applying the overlay"
        )


def _replacement_source(overlay_root, row):
    replacement = _regular(overlay_root.joinpath(*_relative(row["overlay"])))
    if _sha256(replacement) != row["sha256"]:
        raise OverlayError("Patched source checksum mismatch")
    try:
        compile(replacement, row["path"], "exec")
    except (SyntaxError, UnicodeError) as error:
        raise OverlayError("Patched Python source does not compile") from error
    return replacement


def verify_overlay(overlay_root, manifest):
    """Check source bytes without a checkout, backend import, or source write."""
    overlay_root = _directory(overlay_root)
    for row in manifest["files"]:
        _replacement_source(overlay_root, row)


def _source_plan(root, overlay_root, manifest):
    root, overlay_root = _directory(root), _directory(overlay_root)
    plan = []
    for row in manifest["files"]:
        target = root.joinpath(*_relative(row["path"]))
        _directory(target.parent)
        _private_path(target)
        baseline = row["upstream_sha256"]
        if baseline is None:
            if target.exists():
                raise OverlayError("New guard path already exists")
        elif _sha256(_regular(target)) != baseline:
            raise OverlayError("Upstream source checksum mismatch")
        replacement = _replacement_source(overlay_root, row)
        plan.append((target, replacement, baseline))
    return plan


def apply_overlay(root, overlay_root, manifest):
    verify_checkout(root, CORE_COMMIT)
    plan = _source_plan(root, overlay_root, manifest)
    staged = []
    try:
        for target, replacement, baseline in plan:
            descriptor, name = tempfile.mkstemp(
                prefix=".spectra-overlay-", dir=target.parent
            )
            staged.append((Path(name), target))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o644)
        # Check all baselines again before the first replacement.
        for target, _, baseline in plan:
            _private_path(target)
            if baseline is None:
                if target.exists():
                    raise OverlayError("New guard path appeared during validation")
            elif _sha256(_regular(target)) != baseline:
                raise OverlayError("Upstream source changed during validation")
        for temporary, target in staged:
            _directory(target.parent)
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    verify_files(root, manifest)


def verify_files(root, manifest):
    root = _directory(root)
    for row in manifest["files"]:
        if _sha256(_regular(root.joinpath(*_relative(row["path"])))) != row["sha256"]:
            raise OverlayError("Installed or overlaid core source checksum mismatch")


def verify_base_image(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"continuumio/miniconda3@sha256:[0-9a-f]{64}", value
    ):
        raise OverlayError(
            "BASE_IMAGE must be an explicit Miniconda SHA-256 image reference"
        )


def prepare_api_dependencies(api_root, core_root, output_root):
    """Remove bare PyPI core and write upstream runtime requirements separately."""
    api_root, core_root = _directory(api_root), _directory(core_root)
    output_root = _directory(output_root)
    environment = _regular(api_root / "environment.yml")
    setup = _regular(core_root / "setup.py")
    extras = _regular(core_root / "setup/pip_packages.txt")
    if _sha256(environment) != API_ENV_SHA256 or _sha256(setup) != CORE_SETUP_SHA256:
        raise OverlayError("Pinned dependency source checksum mismatch")
    if _sha256(extras) != CORE_PIP_SHA256:
        raise OverlayError("Pinned core pip source checksum mismatch")
    line = b"      - hummingbot\n"
    if environment.count(line) != 1:
        raise OverlayError("Expected exactly one bare upstream hummingbot requirement")
    tree = ast.parse(setup)
    requirements = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "install_requires"
            for target in node.targets
        ):
            if requirements is not None:
                raise OverlayError("Duplicate core runtime dependency declaration")
            requirements = ast.literal_eval(node.value)
    if (
        not isinstance(requirements, list)
        or not requirements
        or any(
            not isinstance(value, str) or "\n" in value or "\r" in value
            for value in requirements
        )
    ):
        raise OverlayError("Invalid core runtime dependency declaration")
    if any(
        re.match(r"(?i)^hummingbot(?:$|[=<>\[ @])", value) for value in requirements
    ):
        raise OverlayError("Core dependency declaration must not reinstall hummingbot")
    # All dependency-source validation is complete before these generated writes.
    (output_root / "environment.no-pypi-core.yml").write_bytes(
        environment.replace(line, b"")
    )
    (output_root / "core-runtime-requirements.txt").write_text(
        "\n".join(requirements) + "\n", encoding="utf-8"
    )


def _clone_pin(repository, commit, destination, env=None):
    destination.mkdir()
    _git(destination, ["init", "--quiet"], env)
    _git(
        destination,
        ["remote", "add", "origin", f"https://github.com/{repository}.git"],
        env,
    )
    _git(destination, ["fetch", "--quiet", "--depth=1", "origin", commit], env)
    _git(destination, ["checkout", "--quiet", "--detach", commit], env)
    verify_checkout(destination, commit)


def verify_licenses(api_root, core_root, overlay_root):
    """Require exact license text from both pinned source checkouts and image context."""
    sources = {
        "hummingbot-api-LICENSE": Path(api_root) / "LICENSE",
        "hummingbot-core-LICENSE": Path(core_root) / "LICENSE",
    }
    for name, source in sources.items():
        license_copy = Path(overlay_root) / "licenses" / name
        if (
            _sha256(_regular(source)) != LICENSE_SHA256[name]
            or _sha256(_regular(license_copy)) != LICENSE_SHA256[name]
        ):
            raise OverlayError("Pinned source or packaged license checksum mismatch")


def prepare_sources(work_root, overlay_root, manifest, base_image):
    verify_base_image(base_image)
    work_root = _directory(work_root)
    if (work_root / "api").exists() or (work_root / "core").exists():
        raise OverlayError("Pinned source destination must be empty")
    token = Path("/run/secrets/github_token")
    if not _regular(token, maximum=4096).strip():
        raise OverlayError(
            "Private API checkout requires the BuildKit GitHub token secret"
        )
    with tempfile.TemporaryDirectory(prefix="spectra-git-askpass-") as temporary:
        askpass = Path(temporary) / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            " *Username*) printf '%s\\n' x-access-token ;;\n"
            " *Password*) cat /run/secrets/github_token ;;\n"
            " *) exit 1 ;;\nesac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env = dict(os.environ, GIT_ASKPASS=str(askpass))
        _clone_pin(API_REPOSITORY, API_COMMIT, work_root / "api", env)
    _clone_pin(CORE_REPOSITORY, CORE_COMMIT, work_root / "core")
    verify_licenses(work_root / "api", work_root / "core", overlay_root)
    prepare_api_dependencies(work_root / "api", work_root / "core", work_root)
    apply_overlay(work_root / "core", overlay_root, manifest)
    # No credential, askpass helper, or Git configuration is retained in this layer.
    shutil.rmtree(work_root / "api/.git")
    shutil.rmtree(work_root / "core/.git")


def verify_wheel(path, manifest):
    _regular(path, maximum=512 * 1024 * 1024)
    try:
        with zipfile.ZipFile(path) as wheel:
            entries = wheel.infolist()
            if (
                len(entries) > 20000
                or sum(row.file_size for row in entries) > 512 * 1024 * 1024
            ):
                raise OverlayError("Oversized wheel archive rejected")
            seen = set()
            for row in entries:
                name = row.filename.rstrip("/")
                _relative(name)
                if name in seen or stat.S_ISLNK(row.external_attr >> 16):
                    raise OverlayError("Duplicate or symlink wheel member rejected")
                seen.add(name)
            for row in manifest["files"]:
                member = wheel.getinfo(row["path"])
                if member.file_size > MAX_SOURCE_BYTES:
                    raise OverlayError("Oversized patched wheel source rejected")
                with wheel.open(member) as stream:
                    source = stream.read(MAX_SOURCE_BYTES + 1)
                if _sha256(source) != row["sha256"]:
                    raise OverlayError("Built wheel source checksum mismatch")
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise OverlayError("Invalid core wheel artifact") from error
    return _sha256(_regular(path, maximum=512 * 1024 * 1024))


def verify_installed(manifest):
    try:
        distribution = importlib.metadata.distribution("hummingbot")
    except importlib.metadata.PackageNotFoundError as error:
        raise OverlayError("Installed hummingbot wheel is missing") from error
    root = Path(distribution.locate_file("."))
    verify_files(root, manifest)
    modules = {
        "hummingbot.strategy_v2.executors.order_executor.order_executor": "OrderExecutor",
        "hummingbot.connector.exchange.kraken.kraken_exchange": "KrakenExchange",
        "hummingbot.strategy_v2.executors.order_executor.spectra_attempt_guard": None,
        "hummingbot.core.web_assistant.connections.rest_connection": "RESTConnection",
        "hummingbot.connector.client_order_tracker": "ClientOrderTracker",
    }
    for name, class_name in modules.items():
        module = importlib.import_module(name)
        expected = root / (name.replace(".", "/") + ".py")
        if Path(module.__file__).resolve() != expected.resolve():
            raise OverlayError(
                "Loaded core module does not belong to the verified wheel"
            )
        if getattr(module, "SPECTRA_EXECUTION_CONTRACT", None) != CONTRACT:
            raise OverlayError("Installed execution module marker mismatch")
        if (
            class_name
            and getattr(getattr(module, class_name), "SPECTRA_EXECUTION_CONTRACT", None)
            != CONTRACT
        ):
            raise OverlayError("Installed execution class marker mismatch")
        if class_name == "ClientOrderTracker" and any(
            not callable(getattr(module.ClientOrderTracker, method, None))
            for method in (
                "retain_spectra_order",
                "mark_spectra_accounting_ambiguous",
                "spectra_order_ambiguity",
                "spectra_order_fill_lag",
                "spectra_order_fill_watermark",
            )
        ):
            raise OverlayError(
                "Installed tracker is missing required accounting safety methods"
            )
    config_module = importlib.import_module(
        "hummingbot.strategy_v2.executors.order_executor.data_types"
    )
    expected_config = (
        root / "hummingbot/strategy_v2/executors/order_executor/data_types.py"
    )
    if Path(config_module.__file__).resolve() != expected_config.resolve():
        raise OverlayError(
            "Loaded executor config does not belong to the verified wheel"
        )
    if not REQUIRED_CONFIG_FIELDS.issubset(
        config_module.OrderExecutorConfig.model_fields
    ):
        raise OverlayError("Installed order executor config is missing required fields")


def verify_runtime_journal():
    guard = importlib.import_module(
        "hummingbot.strategy_v2.executors.order_executor.spectra_attempt_guard"
    )
    try:
        guard.verify_journal()
    except RuntimeError as error:
        raise OverlayError(
            "The existing persistent attempt journal cannot be verified"
        ) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    checksum = parser.add_mutually_exclusive_group(required=True)
    checksum.add_argument("--expected-manifest-sha256")
    checksum.add_argument("--expected-manifest-sha256-file", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", type=Path, metavar="CORE_CHECKOUT")
    action.add_argument("--prepare-sources", type=Path, metavar="BUILD_ROOT")
    action.add_argument("--verify-wheel", type=Path)
    action.add_argument("--verify-installed", action="store_true")
    action.add_argument("--verify-overlay", action="store_true")
    action.add_argument("--runtime", action="store_true")
    parser.add_argument("--overlay-root", type=Path)
    parser.add_argument("--base-image")
    args = parser.parse_args()
    try:
        expected = args.expected_manifest_sha256
        if args.expected_manifest_sha256_file:
            expected = (
                _regular(args.expected_manifest_sha256_file, maximum=128)
                .decode("ascii")
                .strip()
            )
        manifest = load_manifest(args.manifest, expected)
        if args.verify_overlay:
            if args.overlay_root is None:
                raise OverlayError("An explicit overlay directory is required")
            verify_overlay(args.overlay_root, manifest)
        elif args.apply or args.prepare_sources:
            if args.overlay_root is None:
                raise OverlayError("An explicit overlay directory is required")
            if args.prepare_sources:
                prepare_sources(
                    args.prepare_sources, args.overlay_root, manifest, args.base_image
                )
            else:
                apply_overlay(args.apply, args.overlay_root, manifest)
        elif args.verify_wheel:
            print(
                json.dumps({"wheel_sha256": verify_wheel(args.verify_wheel, manifest)})
            )
        else:
            verify_installed(manifest)
            if args.runtime:
                verify_runtime_journal()
                os.execvp(
                    "uvicorn",
                    ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
                )
    except (OverlayError, ImportError, OSError) as error:
        print(f"Spectra core binding refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
