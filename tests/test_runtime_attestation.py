"""Read-only runtime evidence must never disclose secrets or imply live approval."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "spectra_runtime_attest.py"
spec = importlib.util.spec_from_file_location("spectra_runtime_attest", SCRIPT)
attest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attest)

IMAGE = "sha256:" + "a" * 64
DIGEST = "hummingbot/hummingbot-api@sha256:" + "b" * 64
SECRET = "DO_NOT_SHARE-password-token-PRIVATE"


class FakeHost:
    def __init__(self, tmp_path):
        self.spectra = tmp_path / "spectra"
        self.api = tmp_path / "api"
        self.calls = []
        self.roots = {
            str(self.spectra): str(self.spectra),
            str(self.api): str(self.api),
        }
        self.heads = {
            str(self.spectra): attest.EXPECTED_SPECTRA_HEAD,
            str(self.api): attest.EXPECTED_API_HEAD,
        }
        self.status = {str(self.spectra): "", str(self.api): ""}
        self.config_names = {str(self.spectra): "", str(self.api): ""}
        self.index = {str(self.spectra): "", str(self.api): ""}
        self.release = (
            'ID=bazzite\nVERSION_ID="43"\nVARIANT_ID=bazzite\nPRETTY_NAME="'
            + SECRET
            + '"\n'
        )
        self.ostree = {
            "deployments": [
                {
                    "booted": True,
                    "checksum": "c" * 64,
                    "base-checksum": "d" * 64,
                    "version": "43.20261010",
                    "staged": False,
                    "origin": SECRET,
                }
            ],
            "secret": SECRET,
        }
        self.versions = {
            "client_version": "28.4.0",
            "server_version": "28.4.0",
            "server_api_version": "1.51",
            "context": SECRET,
        }
        self.container = {
            "id": "e" * 64,
            "image_id": IMAGE,
            "configured_image": DIGEST,
            "running": True,
            "status": "running",
            "Env": [SECRET],
            "Mounts": [{"Source": SECRET}],
        }
        self.image = {
            "id": IMAGE,
            "repo_digests": [DIGEST],
            "declared_revision": attest.EXPECTED_API_HEAD,
            "labels": {"password": SECRET},
        }
        self.hashes = {name: "f" * 64 for name in attest.CORE_FILES}
        self.core = {
            "package_version": "2.11.0",
            "declared_vcs_revision": attest.REVIEWED_CORE_SOURCE_HEAD,
            "editable": False,
            "files_sha256": dict(self.hashes),
            "direct_url": {"url": SECRET},
        }
        self.fail = None

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail and self.fail in argv:
            raise RuntimeError(SECRET)
        if argv[0] == "git":
            path = argv[3]
            if argv[-1] == "--show-toplevel":
                return self.roots[path] + "\n"
            if argv[-1] == "HEAD":
                return self.heads[path] + "\n"
            if "status" in argv:
                return self.status[path]
            if "config" in argv:
                return self.config_names[path]
            if "ls-files" in argv:
                return self.index[path]
            raise AssertionError("unexpected git command")
        if argv[:3] == ["rpm-ostree", "status", "--json"]:
            return self.render(self.ostree)
        assert argv[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
        action = argv[3]
        if action == "version":
            return self.render(self.versions)
        if action == "container":
            return self.render(self.container)
        if action == "image":
            return self.render(self.image)
        if action == "exec":
            assert argv[4:10] == ["e" * 64, "python", "-I", "-S", "-B", "-c"]
            assert argv[10] == attest.CORE_PROBE
            return self.render(self.core)
        raise AssertionError("unexpected Docker command")

    @staticmethod
    def render(value):
        return value if isinstance(value, str) else json.dumps(value)

    def collect(self, **kwargs):
        return attest.collect_evidence(
            self.spectra,
            self.api,
            runner=self,
            os_release_reader=lambda: self.release,
            core_hash_reference=self.hashes,
            **kwargs
        )


def test_complete_static_facts_still_do_not_grant_activation(tmp_path):
    host = FakeHost(tmp_path)
    report = host.collect()
    assert report["approved_for_live"] is False
    assert report["runtime_attestation_complete"] is False
    assert report["hummingbot_core"]["matches_reviewed_source_hashes"] is True
    assert report["missing_evidence"] == [
        "api_image_source_binding_not_verified",
        "runtime_execution_semantics_not_proven",
    ]
    assert "actual_git_revision" not in report["hummingbot_core"]
    assert SECRET not in json.dumps(report)


def test_read_only_commands_are_narrow_and_use_a_local_socket(tmp_path):
    host = FakeHost(tmp_path)
    host.collect()
    docker_calls = [call for call in host.calls if call[0] == "docker"]
    assert all(
        call[1:3] == ["--host", "unix:///var/run/docker.sock"] for call in docker_calls
    )
    assert {call[3] for call in docker_calls} == {
        "version",
        "container",
        "image",
        "exec",
    }
    assert attest.CONTAINER_FORMAT in docker_calls[1]
    assert ".Env" not in attest.CONTAINER_FORMAT
    assert ".Mounts" not in attest.CONTAINER_FORMAT
    assert ".Config.Labels" in attest.IMAGE_FORMAT
    assert "org.opencontainers.image.revision" in attest.IMAGE_FORMAT
    assert "import hummingbot" not in attest.CORE_PROBE
    assert "from hummingbot" not in attest.CORE_PROBE
    assert "-S" in docker_calls[-1]
    assert "-B" in docker_calls[-1]
    git_calls = [call for call in host.calls if call[0] == "git"]
    assert all("--no-optional-locks" in call for call in git_calls)
    assert not any("diff" in call or "remote" in call for call in git_calls)


@pytest.mark.parametrize(
    "reference",
    [
        "hummingbot/hummingbot-api:latest",
        "hummingbot/hummingbot-api",
        "hummingbot/hummingbot-api:2.11.0",
    ],
)
def test_mutable_image_reference_is_not_a_runtime_pin(tmp_path, reference):
    host = FakeHost(tmp_path)
    host.container["configured_image"] = reference
    report = host.collect()
    assert (
        "api_container_uses_mutable_or_unknown_reference" in report["missing_evidence"]
    )
    assert report["api_image"]["repo_digests"] == [DIGEST]
    assert report["approved_for_live"] is False


def test_missing_repo_digest_even_with_image_id_is_blocked(tmp_path):
    host = FakeHost(tmp_path)
    host.image["repo_digests"] = None
    report = host.collect()
    assert "api_image_repository_digest_missing" in report["missing_evidence"]
    assert "api_image_configured_digest_mismatch" in report["missing_evidence"]


def test_non_bazzite_and_missing_booted_checksum_do_not_attest_host(tmp_path):
    host = FakeHost(tmp_path)
    host.release = "ID=ubuntu\nVERSION_ID=24.04\n"
    host.ostree = {"deployments": [{"booted": True, "checksum": SECRET}]}
    report = host.collect()
    assert "host_is_not_verified_bazzite" in report["missing_evidence"]
    assert "booted_ostree_deployment_unavailable" in report["missing_evidence"]
    assert SECRET not in json.dumps(report)


def test_dirty_checkout_reports_boolean_without_filenames_or_diff(tmp_path):
    host = FakeHost(tmp_path)
    host.status[str(host.api)] = " M .env-" + SECRET + "\n"
    report = host.collect()
    assert report["repositories"]["api"]["clean"] is False
    assert "api_repository_dirty" in report["missing_evidence"]
    assert SECRET not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)


def test_wrong_repository_root_is_rejected_before_head_check(tmp_path):
    host = FakeHost(tmp_path)
    host.roots[str(host.spectra)] = str(tmp_path)
    report = host.collect()
    assert report["repositories"]["spectra"]["head"] is None
    assert "spectra_repository_root_mismatch" in report["missing_evidence"]
    assert not any(
        call[0] == "git" and call[3] == str(host.spectra) and call[-1] == "HEAD"
        for call in host.calls
    )


def test_wrong_repository_revision_is_not_replaced_with_expected_pin(tmp_path):
    host = FakeHost(tmp_path)
    host.heads[str(host.api)] = "1" * 40
    report = host.collect()
    assert report["repositories"]["api"]["head"] == "1" * 40
    assert "api_repository_revision_mismatch" in report["missing_evidence"]


def test_git_filters_prevent_status_without_disclosing_names_or_values(tmp_path):
    host = FakeHost(tmp_path)
    host.config_names[str(host.api)] = "filter." + SECRET + ".process\x00"
    report = host.collect()
    assert report["repositories"]["api"]["clean"] is None
    assert "api_repository_filters_prevent_safe_status" in report["missing_evidence"]
    assert not any(
        call[0] == "git" and call[3] == str(host.api) and "status" in call
        for call in host.calls
    )
    assert SECRET not in json.dumps(report)


def test_submodule_prevents_nested_status_or_an_unqualified_clean_claim(tmp_path):
    host = FakeHost(tmp_path)
    host.index[str(host.spectra)] = "100644\x00160000\x00"
    report = host.collect()
    assert report["repositories"]["spectra"]["clean"] is None
    assert (
        "spectra_repository_submodules_prevent_safe_status"
        in report["missing_evidence"]
    )
    assert SECRET not in json.dumps(report)


def test_missing_core_never_inherits_reviewed_source_revision(tmp_path):
    host = FakeHost(tmp_path)
    host.core = {
        "package_version": None,
        "declared_vcs_revision": None,
        "files_sha256": {},
    }
    report = host.collect()
    assert report["hummingbot_core"]["declared_vcs_revision"] is None
    assert "installed_core_distribution_missing" in report["missing_evidence"]
    assert (
        "installed_core_declared_revision_missing_or_mismatched"
        in report["missing_evidence"]
    )
    assert "installed_core_source_hashes_incomplete" in report["missing_evidence"]


def test_matching_hashes_do_not_establish_installed_commit(tmp_path):
    host = FakeHost(tmp_path)
    host.core["declared_vcs_revision"] = None
    report = host.collect()
    assert report["hummingbot_core"]["matches_reviewed_source_hashes"] is True
    assert report["hummingbot_core"]["declared_vcs_revision"] is None
    assert (
        "installed_core_declared_revision_missing_or_mismatched"
        in report["missing_evidence"]
    )


def test_core_source_difference_and_editable_install_are_explicit(tmp_path):
    host = FakeHost(tmp_path)
    host.core["files_sha256"][attest.CORE_FILES[0]] = "0" * 64
    host.core["editable"] = True
    report = host.collect()
    assert report["hummingbot_core"]["matches_reviewed_source_hashes"] is False
    assert "installed_core_source_hashes_mismatch" in report["missing_evidence"]
    assert "installed_core_editable_distribution" in report["missing_evidence"]


@pytest.mark.parametrize(
    "bad",
    [
        "user:secret@registry/repo",
        "https://" + SECRET + "@registry/repo",
        SECRET + "\n",
        "repo@sha256:not-a-digest",
    ],
)
def test_invalid_or_credential_image_reference_is_never_echoed(tmp_path, bad):
    host = FakeHost(tmp_path)
    host.container["configured_image"] = bad
    host.image["repo_digests"] = [bad]
    report = host.collect()
    assert report["api_container"]["configured_image"] is None
    assert report["api_image"]["repo_digests"] == []
    assert bad not in json.dumps(report)


@pytest.mark.parametrize(
    "bad",
    [
        "[]",
        "null",
        '{"deployments":[],"deployments":[]}',
        '{"deployments":NaN}',
        "{",
        "[" * 2000,
    ],
)
def test_malformed_evidence_fails_closed_without_raw_output(tmp_path, bad):
    host = FakeHost(tmp_path)
    host.ostree = bad
    report = host.collect()
    assert report["host"]["booted_deployment"] is None
    assert "ostree_status_unavailable" in report["missing_evidence"]


def test_command_error_message_is_not_exported(tmp_path):
    host = FakeHost(tmp_path)
    host.fail = "inspect"
    report = host.collect()
    assert "api_container_unavailable" in report["missing_evidence"]
    assert SECRET not in json.dumps(report)
    assert not any("exec" in call for call in host.calls)


def test_stopped_container_does_not_execute_probe(tmp_path):
    host = FakeHost(tmp_path)
    host.container["running"] = False
    host.container["status"] = "exited"
    report = host.collect()
    assert "api_container_not_running" in report["missing_evidence"]
    assert "installed_core_probe_not_run" in report["missing_evidence"]
    assert not any("exec" in call for call in host.calls)


@pytest.mark.parametrize(
    "container", ["--privileged", "name;echo password", "name\nother", ""]
)
def test_invalid_container_selector_performs_no_commands(tmp_path, container):
    host = FakeHost(tmp_path)
    with pytest.raises(ValueError, match="invalid container"):
        host.collect(container=container)
    assert host.calls == []


@pytest.mark.parametrize(
    "socket", ["tcp://example:2375", "/tmp/docker.sock\x00", "relative.sock"]
)
def test_remote_or_invalid_docker_target_performs_no_commands(tmp_path, socket):
    host = FakeHost(tmp_path)
    with pytest.raises(ValueError, match="invalid local Docker"):
        host.collect(docker_socket=socket)
    assert host.calls == []


def test_runner_discards_stderr_and_failed_stdout():
    runner = attest.BoundedRunner(timeout=1, max_bytes=64)
    with pytest.raises(attest.EvidenceUnavailable) as error:
        runner(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import sys; print('"
                + SECRET
                + "'); print('"
                + SECRET
                + "', file=sys.stderr); sys.exit(1)",
            ]
        )
    assert SECRET not in str(error.value)


def test_runner_kills_oversized_output():
    runner = attest.BoundedRunner(timeout=1, max_bytes=16)
    with pytest.raises(attest.EvidenceUnavailable, match="output unavailable"):
        runner([sys.executable, "-I", "-S", "-c", "print('x' * 1000)"])


def test_runner_timeout_is_bounded():
    runner = attest.BoundedRunner(timeout=0.05, max_bytes=16)
    with pytest.raises(attest.EvidenceUnavailable, match="timed out"):
        runner([sys.executable, "-I", "-S", "-c", "import time; time.sleep(5)"])


def test_reference_contract_is_source_only_and_bounded(tmp_path):
    contract = Path(__file__).parents[1] / "spectra-runtime.contract.json"
    hashes = attest.load_core_reference(contract)
    assert set(hashes) == set(attest.CORE_FILES)
    altered = json.loads(contract.read_text())
    altered["approved_for_live"] = True
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(altered))
    with pytest.raises(attest.EvidenceUnavailable):
        attest.load_core_reference(path)


def test_contract_fifo_or_device_is_rejected_without_waiting(tmp_path):
    path = tmp_path / "contract-fifo.json"
    os.mkfifo(path)
    with pytest.raises(attest.EvidenceUnavailable):
        attest.load_core_reference(path)
    with pytest.raises(attest.EvidenceUnavailable):
        attest.load_core_reference(Path("/dev/zero"))
    path.unlink()
    path.write_bytes(b" " * (attest.MAX_COMMAND_BYTES + 1))
    with pytest.raises(attest.EvidenceUnavailable):
        attest.load_core_reference(path)


def test_probe_does_not_import_package_or_sitecustomize_and_avoids_fifo():
    assert "import hummingbot" not in attest.CORE_PROBE
    assert "from hummingbot" not in attest.CORE_PROBE
    assert "sysconfig.get_path" in attest.CORE_PROBE
    assert "os.O_NOFOLLOW" in attest.CORE_PROBE
    assert "os.O_NONBLOCK" in attest.CORE_PROBE
    assert "stat.S_ISREG" in attest.CORE_PROBE
    assert "signal.alarm(10)" in attest.CORE_PROBE
    assert 'direct.get("vcs_info"' in attest.CORE_PROBE
    assert 'out["direct_url"]' not in attest.CORE_PROBE


def test_inert_probe_hashes_installed_files_without_executing_package(tmp_path):
    package = tmp_path / "hummingbot"
    package.mkdir()
    marker = tmp_path / "package-was-imported"
    (package / "__init__.py").write_text(
        "raise RuntimeError('must not import trading package')\n"
    )
    metadata = tmp_path / "hummingbot-2.11.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: hummingbot\nVersion: 2.11.0\n"
    )
    (metadata / "direct_url.json").write_text(
        json.dumps(
            {
                "url": "https://user:" + SECRET + "@private.example/repo",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": attest.REVIEWED_CORE_SOURCE_HEAD,
                },
            }
        )
    )
    for name in attest.CORE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# " + SECRET + "\n")
    fifo = tmp_path / attest.CORE_FILES[-1]
    fifo.unlink()
    os.mkfifo(fifo)
    code = (
        "import sysconfig\nsysconfig.get_path = lambda key: "
        + repr(str(tmp_path))
        + "\n"
        + attest.CORE_PROBE
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["package_version"] == "2.11.0"
    assert report["declared_vcs_revision"] == attest.REVIEWED_CORE_SOURCE_HEAD
    assert set(report["files_sha256"]) == set(attest.CORE_FILES[:-1])
    assert SECRET not in result.stdout
    assert not marker.exists()
    assert result.stderr == ""


def test_cli_runs_without_project_dependencies_and_returns_json_on_missing_host():
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["approved_for_live"] is False
    assert report["runtime_attestation_complete"] is False
    assert report["missing_evidence"]
    assert result.stderr == ""
