#!/usr/bin/env python3
"""Run the exact PR33 installed-source guard in a disposable dependency venv."""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

GUARD_SHA256 = "abf8790cb143256049ba3d9ecb3801515015fd71b58099536460310f7b3a4965"
MANIFEST_SHA256 = "c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spectra-root", type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode or sys.flags.optimize:
        parser.error("Use disposable Python 3.12 with -I -S -B, without optimization")
    venv = Path(sys.executable).absolute().parent.parent
    packages = venv / "lib/python3.12/site-packages"
    config = venv / "pyvenv.cfg"
    if not config.is_file() or not packages.is_dir():
        parser.error("An explicit disposable dependency venv is required")
    settings = [line.strip().split("=", 1) for line in config.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#") and "=" in line]
    inheritance = [value.strip().casefold() for key, value in settings
                   if key.strip().casefold() == "include-system-site-packages"]
    if inheritance != ["false"]:
        parser.error("System package inheritance is prohibited")
    guard_path = args.spectra_root / "deploy/hummingbot_core/apply_overlay.py"
    manifest_path = guard_path.with_name("overlay.manifest.json")
    for path, expected in [(guard_path, GUARD_SHA256), (manifest_path, MANIFEST_SHA256)]:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            parser.error("Exact reviewed PR33 guard and manifest are required")
    manifest = json.loads(manifest_path.read_bytes())
    sys.path.append(str(packages))  # No site import, .pth, or base site-packages.
    def deny_transport_and_launch(event, arguments):
        if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp", "os.fork", "os.forkpty", "os.exec"):
            raise RuntimeError("Network and process launch are prohibited in this installed guard")
    sys.addaudithook(deny_transport_and_launch)
    spec = importlib.util.spec_from_file_location("reviewed_pr33_guard", guard_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify_installed(manifest)
    # Verify every Hummingbot module imported by the guard belongs to this venv.
    imported = []
    for name, loaded in sys.modules.items():
        if name == "hummingbot" or name.startswith("hummingbot."):
            source = getattr(loaded, "__file__", None)
            if source and not Path(source).resolve().is_relative_to(venv.resolve()):
                raise RuntimeError("Hummingbot module escaped the disposable venv")
            imported.append(name)
    import numpy
    if not Path(numpy.__file__).resolve().is_relative_to(venv.resolve()):
        raise RuntimeError("NumPy escaped the disposable venv")
    print(json.dumps({"scope": "exact_pr33_installed_source_guard_in_disposable_runtime_trial", "status": "PASS", "guard_sha256": GUARD_SHA256, "manifest_sha256": MANIFEST_SHA256, "numpy_runtime_version": numpy.__version__, "system_packages_inherited": False, "site_and_pth_processing_disabled": True, "network_and_process_audit_guard_enabled": True, "hummingbot_modules_imported": sorted(imported), "five_installed_overlay_hashes_match": True, "required_executor_config_fields_verified": True, "connector_instantiated": False, "executor_instantiated": False, "api_started": False, "journal_checked": False, "account_connected": False, "full_api_image_verified": False, "approved_for_live": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
