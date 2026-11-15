from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.spectra_release_inventory import collect_inventory, write_inventory


def completed(command, stdout="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_inventory_records_observed_refs_images_and_safety_without_secrets(tmp_path):
    install = tmp_path / "SpectraFinance-Alpha"
    core = install / "spectra-core"
    api = install / "spectra-hummingbot-api"
    for repo in (core, api):
        (repo / ".git").mkdir(parents=True)
    (core / ".venv" / "bin").mkdir(parents=True)
    (core / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (core / ".env").write_text(
        "SPECTRA_PAPER_ONLY=true\n"
        "SPECTRA_ALLOW_LIVE_EXECUTION=false\n"
        "OPENROUTER_API_KEY=secret-value-must-not-appear\n",
        encoding="utf-8",
    )
    (install / ".compose-kind").write_text("podman\n", encoding="utf-8")
    (api / "docker-compose.alpha.yml").write_text("services: {}\n", encoding="utf-8")
    (core / "data" / "spectra-paper").mkdir(parents=True)
    (core / "data" / "spectra-paper" / "journal.json").write_text("{}", encoding="utf-8")
    (core / "data" / "spectra").mkdir(parents=True)
    (core / "frontend").mkdir(parents=True)
    (core / "frontend" / "package.json").write_text('{"name":"spectra-ui","version":"1.2.3"}', encoding="utf-8")
    (core / "frontend" / "package-lock.json").write_text("{}", encoding="utf-8")

    def runner(command, **kwargs):
        if command[0] == "git":
            if "rev-parse" in command:
                return completed(command, "deadbeef\n")
            if "branch" in command:
                return completed(command, "alpha\n")
            if "status" in command:
                return completed(command, " M strategy.py\n?? local-note.txt\n")
            if "describe" in command:
                return completed(command, "", 1)
        if command[:2] == ["podman", "version"]:
            return completed(command, "podman 5.0\n")
        if command[:2] == ["podman", "ps"]:
            return completed(command, "hummingbot-api|spectra-api:test\nhummingbot-postgres|postgres:16\n")
        if command[:2] == ["podman", "inspect"]:
            return completed(command, "container-id|sha256:image-id\n")
        if command[0].endswith("/python"):
            return completed(command, "Python 3.12.4\n")
        return completed(command, "", 1)

    report = collect_inventory(install, runner=runner)
    encoded = json.dumps(report)

    assert report["source_repositories"]["spectra_core"]["commit"] == "deadbeef"
    assert report["source_repositories"]["spectra_core"]["dirty"] is True
    assert report["source_repositories"]["spectra_core"]["changed_path_count"] == 2
    assert report["runtimes"]["containers"]["engine"] == "podman"
    assert report["components"]["hummingbot_api"]["container"]["image_id"] == "sha256:image-id"
    assert report["components"]["postgres"]["image_tag"] == "postgres:16"
    assert report["components"]["gateway"]["status"] == "not_observed"
    assert report["paper_safety"]["paper_only"] is True
    assert report["paper_safety"]["live_execution_disabled"] is True
    assert report["state"]["paper"]["file_count"] == 1
    assert report["runtimes"]["frontend"]["version"] == "1.2.3"
    assert "secret-value-must-not-appear" not in encoded
    assert "OPENROUTER_API_KEY" not in encoded


def test_inventory_handles_missing_runtime_and_repositories_without_guessing(tmp_path):
    install = tmp_path / "empty-install"
    install.mkdir()

    def runner(command, **kwargs):
        return completed(command, "", 127)

    report = collect_inventory(install, runner=runner)
    assert report["source_repositories"]["spectra_core"]["status"] == "not-installed"
    assert report["runtimes"]["containers"]["status"] == "runtime-unavailable"
    assert report["paper_safety"]["paper_only"] is False
    assert report["components"]["hummingbot_client"]["status"] == "not_independently_measured"


def test_inventory_output_is_private(tmp_path):
    output = tmp_path / "backups" / "inventory.json"
    path = write_inventory({"safe": True}, output)
    assert json.loads(path.read_text(encoding="utf-8")) == {"safe": True}
    assert os.stat(path).st_mode & 0o777 == 0o600
