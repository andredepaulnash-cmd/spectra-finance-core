#!/usr/bin/env python3
"""Read-only, secret-safe inventory of an installed Spectra Alpha stack.

The report records observed source refs, immutable local container image IDs,
host/runtime facts, and paper/live state metadata. It never reads credential
values into the report, changes a checkout, pulls images, or restarts services.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(runner: Runner, command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(command, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None


def _git_state(path: Path, runner: Runner) -> dict[str, Any]:
    if not path.is_dir() or not (path / ".git").exists():
        return {"status": "not-installed", "path": str(path)}
    head = _run(runner, ["git", "-C", str(path), "rev-parse", "HEAD"])
    branch = _run(runner, ["git", "-C", str(path), "branch", "--show-current"])
    dirty = _run(runner, ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=normal"])
    tag = _run(runner, ["git", "-C", str(path), "describe", "--tags", "--exact-match", "HEAD"])
    if head is None or head.returncode != 0:
        return {"status": "unavailable", "path": str(path)}
    changes = [line for line in (dirty.stdout if dirty and dirty.returncode == 0 else "").splitlines() if line.strip()]
    return {
        "status": "measured",
        "path": str(path),
        "commit": head.stdout.strip(),
        "branch": branch.stdout.strip() if branch and branch.returncode == 0 else None,
        "tag": tag.stdout.strip() if tag and tag.returncode == 0 else None,
        "dirty": bool(changes),
        "changed_path_count": len(changes),
    }


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file() or path.is_symlink():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _state_summary(raw_path: str | None, default: Path, base: Path) -> dict[str, Any]:
    path = Path(raw_path).expanduser() if raw_path else default
    if not path.is_absolute():
        path = base / path
    if path.is_symlink():
        return {"status": "symlink-refused", "path": str(path)}
    if not path.is_dir():
        return {"status": "not-present", "path": str(path), "file_count": 0, "bytes": 0}
    count = total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_file():
            count += 1
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return {"status": "measured", "path": str(path.resolve()), "file_count": count, "bytes": total}


def _sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_facts() -> dict[str, Any]:
    os_release: dict[str, str] = {}
    path = Path("/etc/os-release")
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"')
    return {
        "os_id": os_release.get("ID"),
        "os_name": os_release.get("NAME"),
        "os_version": os_release.get("VERSION_ID"),
        "kernel": platform.release(),
        "immutable_ostree_boot": Path("/run/ostree-booted").exists(),
    }


def _container_inventory(install_root: Path, api_dir: Path, runner: Runner) -> dict[str, Any]:
    kind_file = install_root / ".compose-kind"
    kind = kind_file.read_text(encoding="utf-8").strip().lower() if kind_file.is_file() else ""
    candidates = [kind] if kind in {"podman", "docker"} else ["podman", "docker"]
    runtime = None
    for candidate in candidates:
        check = _run(runner, [candidate, "version"])
        if check is not None and check.returncode == 0:
            runtime = candidate
            break
    if runtime is None:
        return {"status": "runtime-unavailable", "engine": None, "containers": []}

    ps = _run(runner, [runtime, "ps", "-a", "--format", "{{.Names}}|{{.Image}}"])
    if ps is None or ps.returncode != 0:
        return {"status": "inspect-unavailable", "engine": runtime, "containers": []}

    known_names = {"hummingbot-api", "hummingbot-broker", "hummingbot-postgres"}
    containers: list[dict[str, Any]] = []
    for line in ps.stdout.splitlines():
        if "|" not in line:
            continue
        name, image_tag = line.split("|", 1)
        name = name.strip()
        if name not in known_names and not name.startswith("spectra-alpha-") and "gateway" not in name.lower():
            continue
        inspected = _run(runner, [runtime, "inspect", "--format", "{{.Id}}|{{.Image}}", name])
        container_id = image_id = None
        if inspected is not None and inspected.returncode == 0:
            values = inspected.stdout.strip().split("|", 1)
            if values:
                container_id = values[0]
            if len(values) > 1:
                image_id = values[1]
        containers.append({
            "name": name,
            "image_tag": image_tag.strip() or None,
            "container_id": container_id,
            "image_id": image_id,
            "status": "measured" if image_id else "tag-only",
        })
    return {"status": "measured", "engine": runtime, "containers": containers}


def _python_version(core_dir: Path, runner: Runner) -> str | None:
    executable = core_dir / ".venv" / "bin" / "python"
    if not executable.is_file():
        return None
    result = _run(runner, [str(executable), "--version"])
    return result.stdout.strip() if result and result.returncode == 0 else None


def collect_inventory(install_root: Path, runner: Runner = subprocess.run) -> dict[str, Any]:
    install_root = install_root.expanduser().resolve()
    core_dir = install_root / "spectra-core"
    api_dir = install_root / "spectra-hummingbot-api"
    env = _read_env(core_dir / ".env")
    compose_candidates = [api_dir / "docker-compose.alpha.yml", api_dir / "docker-compose.yml"]
    compose_file = next((path for path in compose_candidates if path.is_file()), None)
    compose_kind = (install_root / ".compose-kind").read_text(encoding="utf-8").strip() if (install_root / ".compose-kind").is_file() else None
    containers = _container_inventory(install_root, api_dir, runner)
    by_name = {item["name"]: item for item in containers.get("containers", [])}

    package_path = core_dir / "frontend" / "package.json"
    frontend: dict[str, Any] = {"status": "not-installed"}
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
            frontend = {
                "status": "measured",
                "name": package.get("name"),
                "version": package.get("version"),
                "package_json_sha256": _sha256(package_path),
                "lockfile_sha256": next(
                    (_sha256(core_dir / "frontend" / name) for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock") if (core_dir / "frontend" / name).is_file()),
                    None,
                ),
            }
        except (OSError, json.JSONDecodeError):
            frontend = {"status": "unavailable"}

    paper_state = _state_summary(
        env.get("SPECTRA_PAPER_STATE_DIR"),
        core_dir / "data" / "spectra-paper",
        core_dir,
    )
    live_state = _state_summary(
        env.get("SPECTRA_STATE_DIR"),
        core_dir / "data" / "spectra",
        core_dir,
    )
    gateway = next((item for name, item in by_name.items() if "gateway" in name.lower()), None)
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "install_root": str(install_root),
        "host": _host_facts(),
        "source_repositories": {
            "spectra_core": _git_state(core_dir, runner),
            "hummingbot_api": _git_state(api_dir, runner),
        },
        "runtimes": {
            "compose_kind": compose_kind,
            "compose_file": str(compose_file) if compose_file else None,
            "compose_file_sha256": _sha256(compose_file) if compose_file else None,
            "core_python": _python_version(core_dir, runner),
            "frontend": frontend,
            "containers": containers,
        },
        "components": {
            "spectra_core": _git_state(core_dir, runner),
            "condor": {
                "status": "version-not-measured",
                "note": "Capture the installed package version from the Core venv when available; do not infer it from upstream.",
            },
            "hummingbot_api": {
                "source": _git_state(api_dir, runner),
                "container": by_name.get("hummingbot-api"),
            },
            "hummingbot_client": {
                "status": "not_independently_measured",
                "note": "Client version may be bundled in the API image; inspect the installed image before approval.",
            },
            "gateway": gateway or {"status": "not_observed"},
            "postgres": by_name.get("hummingbot-postgres") or {"status": "not_observed"},
            "emqx": by_name.get("hummingbot-broker") or {"status": "not_observed"},
            "frontend": frontend,
        },
        "paper_safety": {
            "paper_only": env.get("SPECTRA_PAPER_ONLY", "").lower() == "true",
            "live_execution_disabled": env.get("SPECTRA_ALLOW_LIVE_EXECUTION", "").lower() == "false",
            "credentials_included": False,
        },
        "state": {"paper": paper_state, "live": live_state},
        "policy": {
            "read_only": True,
            "no_git_checkout": True,
            "no_image_pull": True,
            "no_service_restart": True,
            "no_credentials_or_env_values_recorded": True,
        },
    }
    return report


def write_inventory(report: dict[str, Any], output: Path) -> Path:
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".spectra-inventory-", dir=output.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, default=Path.home() / "SpectraFinance-Alpha")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = collect_inventory(args.install_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or (args.install_root / "backups" / f"release-inventory-{timestamp}.json")
    path = write_inventory(report, output)
    print(json.dumps(report, indent=2))
    print(f"\n[SPECTRA RELEASE] Read-only inventory written to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
