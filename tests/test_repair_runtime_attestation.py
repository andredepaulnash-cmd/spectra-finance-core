"""Fail-closed tests for explicit repair fingerprints and static proof boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/spectra_repair_runtime_attest.py"
CONTRACT = ROOT / "deploy/runtime_readiness/repair-attestation.contract.json"
SPEC = importlib.util.spec_from_file_location("repair_attest", SCRIPT)
assert SPEC and SPEC.loader
ATTEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ATTEST)
REFERENCE = json.loads(CONTRACT.read_bytes())
SOURCE_PATHS = [row["path"] for row in REFERENCE["repair_reference"]["selected_files"]]
OVERLAY_PATHS = [row["path"] for row in REFERENCE["installed_overlay_scope"]["files"]]


@pytest.fixture
def source_copy(tmp_path):
    target = tmp_path / "source"
    for path in SOURCE_PATHS:
        output = target / path
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, output)
    return target


@pytest.fixture
def installed_copy(tmp_path):
    target = tmp_path / "installed"
    manifest = json.loads((ROOT / "deploy/hummingbot_core/overlay.manifest.json").read_bytes())
    for row in manifest["files"]:
        output = target / row["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "deploy/hummingbot_core" / row["overlay"], output)
    return target


def assert_incomplete(report):
    assert report["overall_status"] in {"INCOMPLETE", "BLOCKED"}
    for field in (
        "approved_for_live", "runtime_attestation_complete", "installed_bazzite_verified",
        "production_modified", "venue_requests_performed", "credentials_read",
        "private_journals_read", "candidate_code_imported", "services_started",
        "holdout_payloads_read", "collector_writes_files",
    ):
        assert report[field] is False


def test_exact_32_physical_source_files_match():
    report = ATTEST.collect(CONTRACT, spectra_path=ROOT)
    assert report["contract"]["status"] == "MATCH"
    assert report["source"]["status"] == "MATCH"
    assert report["source"]["selected_file_bytes_match"] is True
    assert len(report["source"]["files"]) == 32
    assert report["source"]["whole_checkout_proven"] is False
    assert report["source"]["reference_commit"] == ATTEST.PR33_COMMIT
    assert_incomplete(report)


@pytest.mark.parametrize("path", SOURCE_PATHS)
def test_each_pinned_source_mutation_refuses_match(source_copy, path):
    target = source_copy / path
    target.write_bytes(target.read_bytes() + b"\n# mutation\n")
    report = ATTEST.collect(CONTRACT, spectra_path=source_copy)
    assert report["source"]["status"] == "MISSING_OR_MISMATCHED"
    actual = next(row for row in report["source"]["files"] if row["path"] == path)
    assert actual["status"] == "MISMATCH"
    assert report["source"]["selected_file_bytes_match"] is False
    assert_incomplete(report)


def test_missing_source_and_unsupplied_selectors_fail_closed(tmp_path):
    report = ATTEST.collect(CONTRACT, spectra_path=tmp_path)
    assert report["source"]["selected_file_bytes_match"] is False
    assert all(row["status"] == "UNAVAILABLE" for row in report["source"]["files"])
    omitted = ATTEST.collect(CONTRACT)
    assert omitted["source"]["status"] == "NOT_SUPPLIED"
    assert omitted["wheel"]["status"] == "NOT_SUPPLIED"
    assert omitted["installed_overlays"]["status"] == "NOT_SUPPLIED"
    assert omitted["image"]["status"] == "NOT_SUPPLIED"
    assert_incomplete(omitted)


def test_tampered_contract_prevents_selected_file_reads(tmp_path, monkeypatch):
    contract = tmp_path / "contract.json"
    payload = json.loads(CONTRACT.read_bytes())
    payload["proof_boundaries"]["approved_for_live"] = True
    payload["repair_reference"]["selected_files"][0]["path"] = "private/credentials.json"
    contract.write_text(json.dumps(payload))
    calls = []

    def forbidden(*args):
        calls.append(args)
        raise AssertionError("unreviewed contract must not choose paths")

    monkeypatch.setattr(ATTEST, "read_selected_file", forbidden)
    report = ATTEST.collect(contract, spectra_path=tmp_path, installed_root=tmp_path)
    assert report["contract"]["status"] == "UNAVAILABLE_OR_MISMATCHED"
    assert report["source"]["status"] == "NOT_RUN"
    assert calls == []
    assert_incomplete(report)


def test_contract_missing_and_old_pr31_contract_are_refused(tmp_path):
    for path in (tmp_path / "missing", ROOT / "spectra-runtime.contract.json"):
        report = ATTEST.collect(path, spectra_path=ROOT)
        assert report["contract"]["status"] == "UNAVAILABLE_OR_MISMATCHED"
        assert report["source"]["status"] == "NOT_RUN"
        assert_incomplete(report)


def test_installed_overlay_matches_are_bounded_static_proof(installed_copy):
    (installed_copy / "hummingbot/__init__.py").write_text("raise AssertionError('never import candidate')\n")
    report = ATTEST.collect(CONTRACT, installed_root=installed_copy)
    assert report["installed_overlays"]["status"] == "MATCH"
    assert report["installed_overlays"]["five_overlay_hashes_match"] is True
    assert len(report["installed_overlays"]["files"]) == 5
    assert report["installed_overlays"]["compiled_package_identity"] == "UNKNOWN"
    assert report["installed_overlays"]["installed_api_identity"] == "UNKNOWN"
    assert report["installed_overlays"]["native_abi_verified"] is False
    assert_incomplete(report)


@pytest.mark.parametrize("path", OVERLAY_PATHS)
def test_each_installed_overlay_mutation_refuses_match(installed_copy, path):
    target = installed_copy / path
    target.write_bytes(target.read_bytes() + b"\n# mutation\n")
    report = ATTEST.collect(CONTRACT, installed_root=installed_copy)
    assert report["installed_overlays"]["five_overlay_hashes_match"] is False
    assert next(row for row in report["installed_overlays"]["files"] if row["path"] == path)["status"] == "MISMATCH"
    assert_incomplete(report)


def test_missing_installed_overlay_refuses_match(installed_copy):
    (installed_copy / OVERLAY_PATHS[0]).unlink()
    report = ATTEST.collect(CONTRACT, installed_root=installed_copy)
    assert report["installed_overlays"]["five_overlay_hashes_match"] is False
    assert report["installed_overlays"]["files"][0]["status"] == "UNAVAILABLE"


def test_wrong_or_missing_wheel_is_not_approved(tmp_path):
    wrong = tmp_path / "candidate.whl"
    wrong.write_bytes(b"a renamed file is not the compiled wheel")
    report = ATTEST.collect(CONTRACT, wheel_path=wrong)
    assert report["wheel"]["status"] == "MISMATCH"
    assert report["wheel"]["sha256"] == hashlib.sha256(wrong.read_bytes()).hexdigest()
    assert report["wheel"]["matches_preferred_artifact"] is False
    assert ATTEST.collect(CONTRACT, wheel_path=tmp_path / "missing")["wheel"]["status"] == "UNAVAILABLE"
    assert_incomplete(report)


def test_regular_reader_refuses_file_and_parent_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "input").write_bytes(b"secret content must not be exported")
    (tmp_path / "file-link").symlink_to(real / "input")
    (tmp_path / "parent-link").symlink_to(real, target_is_directory=True)
    for path in (tmp_path / "file-link", tmp_path / "parent-link/input"):
        with pytest.raises(ATTEST.Unavailable):
            ATTEST.read_regular_file(path, 1024)


def test_regular_reader_refuses_fifo_device_oversize_and_traversal(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    oversized = tmp_path / "large"
    oversized.write_bytes(b"x" * 20)
    for path, limit in ((fifo, 1024), (Path("/dev/null"), 1024), (oversized, 10)):
        with pytest.raises(ATTEST.Unavailable):
            ATTEST.read_regular_file(path, limit)
    with pytest.raises(ATTEST.Unavailable):
        ATTEST.read_selected_file(tmp_path, "../private", 10)


@pytest.mark.parametrize("system,architecture,release", [
    ("Linux", "aarch64", "ID=bazzite\n"),
    ("Windows", "x86_64", "ID=bazzite\n"),
    ("Linux", "x86_64", "ID=ubuntu\n"),
    ("Linux", "x86_64", "PRETTY_NAME=credential-content\n"),
])
def test_unsupported_or_unknown_host_is_never_target_attestation(monkeypatch, system, architecture, release):
    monkeypatch.setattr(ATTEST.platform, "system", lambda: system)
    monkeypatch.setattr(ATTEST.platform, "machine", lambda: architecture)
    report = ATTEST.collect(CONTRACT, os_release_reader=lambda: release)
    assert report["host"]["static_preconditions_match"] is False
    assert report["host"]["precondition_status"] == "MISSING_OR_UNSUPPORTED"
    assert report["installed_bazzite_verified"] is False
    assert_incomplete(report)


def test_bazzite_metadata_is_preconditions_only_and_redacts_other_os_fields(monkeypatch):
    monkeypatch.setattr(ATTEST.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ATTEST.platform, "machine", lambda: "x86_64")
    report = ATTEST.collect(CONTRACT, os_release_reader=lambda: (
        'ID="bazzite"\nVERSION_ID="42"\nHOME_URL="secret-value"\n'
        'PRETTY_NAME="private machine name"\nID_LIKE="secret-value"\n'))
    assert report["host"]["static_preconditions_match"] is True
    assert report["host"]["native_wheel_abi_verified"] is False
    assert report["host"]["libc_version_is_compatibility_proof"] is False
    assert "secret-value" not in json.dumps(report)
    assert "private machine name" not in json.dumps(report)
    assert_incomplete(report)


def test_os_release_unknown_or_malformed_is_not_substituted():
    def unavailable():
        raise OSError("private-path-or-secret must be redacted")

    report = ATTEST.collect(CONTRACT, os_release_reader=unavailable)
    assert report["host"]["os_release"] == {}
    assert "private-path-or-secret" not in json.dumps(report)


def test_default_collection_never_launches_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("metadata-only default must not launch processes")

    monkeypatch.setattr(ATTEST.subprocess, "Popen", forbidden)
    report = ATTEST.collect(CONTRACT, spectra_path=ROOT)
    assert report["source"]["status"] == "MATCH"


@pytest.fixture
def docker_selectors(tmp_path, monkeypatch):
    endpoint = tmp_path / "daemon.sock"
    # The test fixture supplies a fictional socket-type observation. It starts
    # no daemon and creates no sockets; the real helper is tested separately.
    def fictional_socket(path):
        assert path == endpoint
    monkeypatch.setattr(ATTEST, "_require_local_socket", fictional_socket)
    config = tmp_path / "empty-config"
    config.mkdir()
    return endpoint, config


def test_real_socket_selector_refuses_regular_file_and_symlink(tmp_path):
    regular = tmp_path / "regular.sock"
    regular.write_bytes(b"not a socket")
    linked = tmp_path / "linked.sock"
    linked.symlink_to(regular)
    for path in (regular, linked):
        with pytest.raises(ATTEST.Unavailable):
            ATTEST._require_local_socket(path)


def metadata_runner(commands, observed_id="sha256:" + "a" * 64):
    def run(argv):
        commands.append(list(argv))
        if "version" in argv:
            return json.dumps({"client_version": "28.0.1", "server_version": "28.0.1", "server_api_version": "1.48", "private_env": "secret"})
        assert argv[-5:-3] == ["image", "inspect"]
        return json.dumps({"id": observed_id, "architecture": "amd64", "os": "linux", "declared_revision": "84426ffbf9ba09522b4444664f9770236205ae29", "credentials": "secret"})
    return run


def test_image_metadata_is_observed_unbound_with_fixed_read_only_commands(docker_selectors):
    endpoint, config = docker_selectors
    commands = []
    report = ATTEST.collect(CONTRACT, docker_socket=endpoint,
        docker_config_empty_dir=config, image_id="sha256:" + "a" * 64,
        runner=metadata_runner(commands))
    assert report["image"]["status"] == "OBSERVED_UNBOUND"
    assert report["image"]["matches_reviewed_image"] is None
    assert report["image"]["source_binding_proven"] is False
    assert len(commands) == 2
    assert all("exec" not in command and "run" not in command and "start" not in command for command in commands)
    assert all(command[1:5] == ["--config", str(config), "--host", "unix://" + str(endpoint)] for command in commands)
    assert "secret" not in json.dumps(report)
    assert_incomplete(report)


def test_wrong_image_identity_cannot_match_even_with_revision_label(docker_selectors):
    endpoint, config = docker_selectors
    report = ATTEST.collect(CONTRACT, docker_socket=endpoint,
        docker_config_empty_dir=config, image_id="sha256:" + "a" * 64,
        runner=metadata_runner([], "sha256:" + "b" * 64))
    assert report["image"]["status"] == "IDENTITY_MISMATCH"
    assert report["image"]["source_binding_proven"] is False


def test_nonempty_config_directory_prevents_credentials_read(docker_selectors):
    endpoint, config = docker_selectors
    (config / "config.json").write_text('{"auths":{"secret":"private-credential"}}')
    calls = []
    report = ATTEST.collect(CONTRACT, docker_socket=endpoint,
        docker_config_empty_dir=config, image_id="sha256:" + "a" * 64,
        runner=metadata_runner(calls))
    assert report["image"]["status"] == "UNAVAILABLE"
    assert calls == []
    assert "private-credential" not in json.dumps(report)


def test_remote_partial_and_mutable_image_selectors_are_refused(tmp_path):
    for options in (
        {"image_id": "candidate:latest"},
        {"image_id": "sha256:" + "a" * 64},
        {"docker_socket": Path("tcp://remote:2375"), "docker_config_empty_dir": tmp_path, "image_id": "sha256:" + "a" * 64},
    ):
        with pytest.raises(ValueError):
            ATTEST.collect(CONTRACT, **options)


def test_metadata_errors_do_not_disclose_command_stderr(docker_selectors):
    endpoint, config = docker_selectors

    def bad(argv):
        raise ATTEST.Unavailable("private credential directory / secret stderr")

    report = ATTEST.collect(CONTRACT, docker_socket=endpoint,
        docker_config_empty_dir=config, image_id="sha256:" + "a" * 64, runner=bad)
    assert report["image"]["status"] == "UNAVAILABLE"
    assert "secret stderr" not in json.dumps(report)


def test_json_metadata_refuses_duplicate_keys_and_invalid_constants():
    for value in ('{"id": "a", "id": "b"}', '{"id": NaN}', '[]'):
        with pytest.raises(ATTEST.Unavailable):
            ATTEST._json_object(value)


def test_bounded_runner_drops_inherited_environment_and_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    runner = ATTEST.BoundedRunner(tmp_path)
    assert "DOCKER_HOST" not in runner.environment
    assert "AWS_SECRET_ACCESS_KEY" not in runner.environment
    text = runner([sys.executable, "-I", "-S", "-B", "-c", "import sys; print('safe'); print('secret stderr', file=sys.stderr)"])
    assert text == "safe\n"


def test_bounded_runner_refuses_timeout_and_excess_output(tmp_path):
    runner = ATTEST.BoundedRunner(tmp_path, timeout=0.15)
    for program in ("import time; time.sleep(2)", "print('x'*20000)"):
        with pytest.raises(ATTEST.Unavailable):
            runner([sys.executable, "-I", "-S", "-B", "-c", program])


def test_cli_produces_incomplete_json_and_requires_isolation_flags():
    command = [sys.executable, "-I", "-S", "-B", str(SCRIPT), "--contract", str(CONTRACT), "--spectra-path", str(ROOT)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["source"]["status"] == "MATCH"
    assert_incomplete(report)
    unsafe = subprocess.run([sys.executable, "-B", str(SCRIPT), "--contract", str(CONTRACT)], capture_output=True, text=True, timeout=20)
    assert unsafe.returncode == 2
    assert unsafe.stdout == ""
    assert "-I -S -B" in unsafe.stderr
    optimized = subprocess.run([sys.executable, "-I", "-S", "-B", "-O", str(SCRIPT), "--contract", str(CONTRACT)], capture_output=True, text=True, timeout=20)
    assert optimized.returncode == 2
    assert optimized.stdout == ""

