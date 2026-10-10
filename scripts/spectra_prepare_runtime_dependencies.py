#!/usr/bin/env python3
"""Prepare source-bound dependency inputs; never install, import, or start the API."""
from __future__ import annotations

import argparse
import ast
import email
import hashlib
import json
import platform
import re
import sys
import zipfile
from pathlib import Path

CORE_COMMIT = "9af100d6822da7d2d0291a906c730ef172284ee2"
API_COMMIT = "84426ffbf9ba09522b4444664f9770236205ae29"
WHEEL_SHA256 = "b92048954145b055ac7bbf85cfdfa4e042c4153eb66aabc29740f4ec34a76dbc"
SOURCE_HASHES = {
    "core-setup.py": "ba4c5760e8ece5993a2e9bfc5d4381318b142674208bc65fc5a0649d209f6c6a",
    "core-extra-requirements.txt": "6bfa6b389b6f2c00dec4443d7bfe50a177cd7ddf8b417221c7d5e3964ec55b17",
    "core-environment.yml": "e7c7f7cc3ad29a5dc95a52ed4f230d9bd2df47f8d8d675cf224b134bfe00928c",
    "api-environment.yml": "82131a516ad12161e74322082ddee9d9aecd3ab6db8c098ca2d9a5addfb66321",
}
# These are constraints for this preparation run, not edits to upstream sources.
# The compiled wheel was built against NumPy 2.3.5; that matched-build trial
# was rejected by the resolver because pandas-ta pins numba 0.61.2, which
# requires NumPy <2.3. This separate runtime trial uses NumPy 2.2.6 and must
# pass compiled ABI checks. Pandas 2.3.2 meets both pinned upstream minimums.
COMPATIBILITY_CONSTRAINTS = (
    "numpy==2.2.6", "pandas==2.3.2", "setuptools==80.8.0",
    "Cython==3.0.12", "wheel==0.48.0", "aiohttp<3.14",
    "injective-py==1.14.1", "xrpl-py==4.4.0",
    "web3==7.16.0", "eip712==0.3.3", "eth-typing>=5,<6",
)
NATIVE_ONLY = {"libcxx", "libta-lib"}
CONDA_PYPI_NAMES = {"docker-py": "docker"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_sources(root: Path) -> dict[str, str]:
    source_root = root / "deploy/runtime_readiness/dependencies/sources"
    result = {}
    for name, expected in SOURCE_HASHES.items():
        path = source_root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing regular pinned source: {name}")
        data = path.read_bytes()
        if digest(data) != expected:
            raise ValueError(f"Pinned source mismatch: {name}")
        result[name] = data.decode("utf-8")
    return result


def core_requirements(source: str) -> list[str]:
    tree = ast.parse(source)
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "install_requires"
                           for t in n.targets)]
    if len(assignments) != 1:
        raise ValueError("Expected exactly one literal core install_requires")
    value = ast.literal_eval(assignments[0].value)
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ValueError("Core requirements must be literal strings")
    return value


def api_requirements(source: str) -> tuple[list[str], list[str], str]:
    """Read the exact pinned simple YAML without executing a YAML constructor."""
    requirements, native, cleaned = [], [], []
    in_dependencies = False
    in_pip = False
    removed_core = 0
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "dependencies:":
            in_dependencies = True
        if in_dependencies and stripped == "- pip:":
            in_pip = True
            cleaned.append(line)
            continue
        if in_dependencies and stripped.startswith("- ") and not stripped.startswith("- #"):
            item = stripped[2:].split("#", 1)[0].strip()
            if in_pip:
                if item == "hummingbot":
                    removed_core += 1
                    continue
                requirements.append(item)
            else:
                name = re.split(r"[<>=!~]", item, 1)[0]
                if name == "python":
                    if item != "python=3.12":
                        raise ValueError("Pinned API Python 3.12 required")
                elif name == "pip":
                    pass
                elif name in NATIVE_ONLY:
                    native.append(item)
                else:
                    replacement = CONDA_PYPI_NAMES.get(name, name)
                    requirements.append(replacement + item[len(name):])
        cleaned.append(line)
    if removed_core != 1:
        raise ValueError("Exactly one unpinned API hummingbot entry must be removed")
    return requirements, native, "".join(cleaned)


def verify_wheel(path: Path, expected_requirements: list[str]) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("A regular locally compiled wheel is required")
    data = path.read_bytes()
    if digest(data) != WHEEL_SHA256:
        raise ValueError("Compiled wheel SHA256 mismatch")
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) != len(set(members)):
            raise ValueError("Duplicate wheel members")
        metadata_names = [x for x in members if x.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Exactly one wheel metadata entry required")
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
        if metadata.get("Name") != "hummingbot" or metadata.get("Version") != "20260922":
            raise ValueError("Unexpected core package identity")
        def normalized(requirement: str) -> tuple[str, tuple[str, ...]]:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)(.*)", requirement)
            if not match or not re.fullmatch(r"[<>=!~0-9A-Za-z.,*+\s-]*", match[2]):
                raise ValueError("Only simple pinned core requirements are supported")
            name = re.sub(r"[-_.]+", "-", match[1]).casefold()
            specs = tuple(sorted(x.strip() for x in match[2].split(",") if x.strip()))
            return name, specs
        actual = sorted(normalized(x) for x in metadata.get_all("Requires-Dist", []))
        expected = sorted(normalized(x) for x in expected_requirements)
        if actual != expected:
            raise ValueError("Wheel metadata differs from pinned setup requirements")
    return {"sha256": WHEEL_SHA256, "bytes": len(data), "package": "hummingbot",
            "version": "20260922", "requires_dist_count": len(expected_requirements)}


def prepare(root: Path, wheel: Path, output: Path) -> dict:
    if sys.version_info[:2] != (3, 12) or platform.machine() != "x86_64" or sys.platform != "linux":
        raise ValueError("This preparation packet targets CPython 3.12 Linux x86_64")
    sources = verify_sources(root)
    core = core_requirements(sources["core-setup.py"])
    api, native, cleaned = api_requirements(sources["api-environment.yml"])
    wheel_identity = verify_wheel(wheel, core)
    extra = [x.strip() for x in sources["core-extra-requirements.txt"].splitlines() if x.strip()]
    requirements = core + api + extra + ["setuptools==80.8.0", "Cython==3.0.12", "wheel==0.48.0"]
    if any(x.startswith("-") or "@" in x or "hummingbot" in x.casefold() for x in requirements):
        raise ValueError("Unsafe directive, direct reference, or PyPI core requirement")
    output.mkdir(parents=True, exist_ok=True)
    products = {
        "requirements.source-derived.txt": "\n".join(requirements) + "\n",
        "requirements.core-api-without-legacy-extra.txt": "\n".join(core + api + ["setuptools==80.8.0", "Cython==3.0.12", "wheel==0.48.0"]) + "\n",
        "requirements.legacy-extra.txt": "\n".join(extra) + "\n",
        "constraints.compatibility.txt": "\n".join(COMPATIBILITY_CONSTRAINTS) + "\n",
        "environment.no-pypi-core.yml": cleaned,
    }
    for name, content in products.items():
        target = output / name
        if target.is_symlink():
            raise ValueError("Refusing a symlink output")
        target.write_bytes(content.encode("utf-8"))
    receipt = {
        "schema_version": 1, "scope": "source_bound_dependency_preparation",
        "core_source_commit": CORE_COMMIT, "api_source_commit": API_COMMIT,
        "source_sha256": SOURCE_HASHES, "core_wheel": wheel_identity,
        "target": {"implementation": "CPython", "python": "3.12", "os": "linux", "arch": "x86_64"},
        "source_requirement_count": len(requirements), "native_conda_requirements": native,
        "separate_trial_excludes_legacy_extra": extra,
        "excluded_legacy_extra_is_not_a_declared_wheel_dependency": all(x not in core for x in extra),
        "native_requirements_satisfied_by_pip": False,
        "conda_to_pypi_name_mapping": CONDA_PYPI_NAMES,
        "compatibility_constraints": list(COMPATIBILITY_CONSTRAINTS),
        "compiled_wheel_numpy_headers": "2.3.5", "runtime_trial_numpy": "2.2.6",
        "compiled_abi_compatibility_verified": False,
        "products_sha256": {k: digest(v.encode("utf-8")) for k, v in products.items()},
        "dependency_resolution_run": False, "dependencies_fully_locked": False,
        "full_api_image_built": False, "installed_backend_verified": False, "approved_for_live": False,
    }
    target = output / "preparation.receipt.json"
    if target.is_symlink():
        raise ValueError("Refusing a symlink receipt output")
    target.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spectra-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = prepare(args.spectra_root, args.wheel, args.output_dir)
    except (ValueError, OSError, SyntaxError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
