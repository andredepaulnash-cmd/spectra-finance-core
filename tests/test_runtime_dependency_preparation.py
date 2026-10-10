import email
import sys
import types
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dependency_preparation", ROOT / "scripts/spectra_prepare_runtime_dependencies.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class DependencyPreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "spectra"
        target = self.root / "deploy/runtime_readiness/dependencies/sources"
        shutil.copytree(ROOT / "deploy/runtime_readiness/dependencies/sources", target)
        self.sources = m.verify_sources(self.root)
        self.requirements = m.core_requirements(self.sources["core-setup.py"])
        self.wheel = Path(self.temp.name) / "fictional-unit-fixture.whl"

    def tearDown(self):
        self.temp.cleanup()

    def fictional_wheel(self, requirements=None, package="hummingbot", version="20260922", extra_metadata=False):
        requirements = self.requirements if requirements is None else requirements
        metadata = "Metadata-Version: 2.4\nName: " + package + "\nVersion: " + version + "\n"
        metadata += "".join("Requires-Dist: " + r + "\n" for r in requirements)
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("hummingbot-20260922.dist-info/METADATA", metadata)
            if extra_metadata:
                archive.writestr("other.dist-info/METADATA", metadata)
        return hashlib.sha256(self.wheel.read_bytes()).hexdigest()

    def test_pinned_source_bundle(self):
        self.assertEqual(len(self.sources), 4)
        self.assertEqual(len(self.requirements), 47)

    def test_all_source_mutations_refused(self):
        for name in m.SOURCE_HASHES:
            with self.subTest(name=name):
                p = self.root / "deploy/runtime_readiness/dependencies/sources" / name
                original = p.read_bytes()
                p.write_bytes(original + b"# mutation\n")
                with self.assertRaisesRegex(ValueError, "Pinned source mismatch"):
                    m.verify_sources(self.root)
                p.write_bytes(original)

    def test_missing_source_refused(self):
        (self.root / "deploy/runtime_readiness/dependencies/sources/core-setup.py").unlink()
        with self.assertRaisesRegex(ValueError, "Missing regular"):
            m.verify_sources(self.root)

    def test_symlink_source_refused(self):
        p = self.root / "deploy/runtime_readiness/dependencies/sources/core-setup.py"
        actual = p.with_name("alternate.py")
        p.rename(actual)
        p.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, "Missing regular"):
            m.verify_sources(self.root)

    def test_setup_is_never_executed(self):
        source = "raise RuntimeError('must not execute')\ndef main():\n install_requires=['example>=1']\n"
        self.assertEqual(m.core_requirements(source), ["example>=1"])

    def test_executable_requirement_expression_refused(self):
        with self.assertRaises((ValueError, TypeError)):
            m.core_requirements("install_requires=__import__('os').system('false')")

    def test_duplicate_literal_assignment_refused(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            m.core_requirements("install_requires=['a']\ninstall_requires=['b']")

    def test_api_core_removed_and_native_explicit(self):
        requirements, native, cleaned = m.api_requirements(self.sources["api-environment.yml"])
        self.assertNotIn("hummingbot", requirements)
        self.assertNotIn("      - hummingbot\n", cleaned)
        self.assertIn("docker", requirements)
        self.assertNotIn("docker-py", requirements)
        self.assertEqual(native, ["libcxx", "libta-lib>=0.6.4"])
        self.assertIn("  - libcxx", cleaned)
        self.assertIn("  - python=3.12\n", cleaned)

    def test_api_wrong_python_refused(self):
        with self.assertRaisesRegex(ValueError, "Python 3.12"):
            m.api_requirements(self.sources["api-environment.yml"].replace("python=3.12", "python=3.13"))

    def test_api_absent_core_refused(self):
        with self.assertRaisesRegex(ValueError, "Exactly one"):
            m.api_requirements(self.sources["api-environment.yml"].replace("      - hummingbot\n", ""))

    def test_api_duplicate_core_refused(self):
        with self.assertRaisesRegex(ValueError, "Exactly one"):
            m.api_requirements(self.sources["api-environment.yml"] + "      - hummingbot\n")

    def test_wheel_hash_refused(self):
        self.fictional_wheel()
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            m.verify_wheel(self.wheel, self.requirements)

    def test_metadata_normalization(self):
        reqs = ["async-timeout<5,>=4.0.2" if x == "async-timeout>=4.0.2,<5" else x for x in self.requirements]
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel(reqs)):
            identity = m.verify_wheel(self.wheel, self.requirements)
        self.assertEqual(identity["requires_dist_count"], 47)

    def test_metadata_requirement_omission_refused(self):
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel(self.requirements[:-1])):
            with self.assertRaisesRegex(ValueError, "differs"):
                m.verify_wheel(self.wheel, self.requirements)

    def test_metadata_direct_reference_refused(self):
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel(["example @ https://example.invalid/pkg"])):
            with self.assertRaisesRegex(ValueError, "Only simple"):
                m.verify_wheel(self.wheel, self.requirements)

    def test_wrong_package_identity_refused(self):
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel(package="unrelated")):
            with self.assertRaisesRegex(ValueError, "Unexpected core"):
                m.verify_wheel(self.wheel, self.requirements)

    def test_multiple_metadata_refused(self):
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel(extra_metadata=True)):
            with self.assertRaisesRegex(ValueError, "Exactly one wheel metadata"):
                m.verify_wheel(self.wheel, self.requirements)

    def test_prepare_deterministic_and_not_install_authority(self):
        output = Path(self.temp.name) / "output"
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel()):
            receipt = m.prepare(self.root, self.wheel, output)
            before = {x.name: x.read_bytes() for x in output.iterdir()}
            self.assertEqual(receipt, m.prepare(self.root, self.wheel, output))
            self.assertEqual(before, {x.name: x.read_bytes() for x in output.iterdir()})
        self.assertEqual(receipt["source_requirement_count"], 79)
        for key in ["dependency_resolution_run", "dependencies_fully_locked", "full_api_image_built", "installed_backend_verified", "approved_for_live", "compiled_abi_compatibility_verified"]:
            self.assertFalse(receipt[key])
        self.assertFalse(receipt["native_requirements_satisfied_by_pip"])
        self.assertIn("numpy==2.2.6", (output / "constraints.compatibility.txt").read_text())
        self.assertEqual(receipt["compiled_wheel_numpy_headers"], "2.3.5")

    def test_wrong_host_refused_before_outputs(self):
        output = Path(self.temp.name) / "output"
        with patch.object(m.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(ValueError, "CPython 3.12"):
                m.prepare(self.root, self.wheel, output)
        self.assertFalse(output.exists())

    def test_output_symlink_refused(self):
        output = Path(self.temp.name) / "output"
        output.mkdir()
        alternate = Path(self.temp.name) / "alternate.txt"
        alternate.write_text("do not change")
        (output / "requirements.source-derived.txt").symlink_to(alternate)
        with patch.object(m, "WHEEL_SHA256", self.fictional_wheel()):
            with self.assertRaisesRegex(ValueError, "symlink output"):
                m.prepare(self.root, self.wheel, output)
        self.assertEqual(alternate.read_text(), "do not change")


class InstalledGuardRefusalTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("installed_guard_wrapper", ROOT / "deploy/runtime_readiness/dependencies/check_installed_guard.py")
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.venv = self.path / "venv"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "lib/python3.12/site-packages").mkdir(parents=True)
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        self.root = self.path / "spectra"
        target = self.root / "deploy/hummingbot_core"
        target.mkdir(parents=True)
        for name in ["apply_overlay.py", "overlay.manifest.json"]:
            shutil.copyfile(ROOT / "deploy/hummingbot_core" / name, target / name)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, flags=None):
        selected_flags = flags or types.SimpleNamespace(isolated=True, no_site=True, optimize=0)
        with patch.object(sys, "argv", ["guard", "--spectra-root", str(self.root)]), patch.object(sys, "executable", str(self.venv / "bin/python")), patch.object(sys, "flags", selected_flags), patch.object(sys, "dont_write_bytecode", True):
            with self.assertRaises(SystemExit) as ctx:
                self.guard.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_nonisolated_invocation_refused(self):
        self.invoke(types.SimpleNamespace(isolated=False, no_site=True, optimize=0))

    def test_missing_disposable_venv_refused(self):
        (self.venv / "pyvenv.cfg").unlink()
        self.invoke()

    def test_system_inheritance_refused(self):
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = true\n# include-system-site-packages = false\n")
        self.invoke()

    def test_duplicate_inheritance_declarations_refused(self):
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = true\ninclude-system-site-packages = false\n")
        self.invoke()

    def test_mutated_reviewed_guard_refused_before_import(self):
        path = self.root / "deploy/hummingbot_core/apply_overlay.py"
        path.write_bytes(path.read_bytes() + b"raise RuntimeError('must not execute')\n")
        self.invoke()

    def test_mutated_manifest_refused_before_import(self):
        path = self.root / "deploy/hummingbot_core/overlay.manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.invoke()


if __name__ == "__main__":
    unittest.main()
