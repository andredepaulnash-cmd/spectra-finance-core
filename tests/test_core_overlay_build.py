"""Adversarial source-build checks; no compiled backend or venue is exercised."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parents[1] / "deploy" / "hummingbot_core"
SPEC = importlib.util.spec_from_file_location(
    "spectra_overlay_build", BUILD / "apply_overlay.py"
)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    checkout, overlay = tmp_path / "core", tmp_path / "overlay"
    checkout.mkdir()
    overlay.mkdir()
    rows = []
    for index, (path, patch) in enumerate(GUARD.ALLOWED_FILES.items()):
        original = f"BASELINE = {index}\n".encode()
        replacement = f"PATCHED = {index}\n".encode()
        target, patched = checkout / path, overlay / patch
        target.parent.mkdir(parents=True, exist_ok=True)
        patched.parent.mkdir(parents=True, exist_ok=True)
        if path != GUARD.NEW_FILE:
            target.write_bytes(original)
        patched.write_bytes(replacement)
        rows.append(
            {
                "path": path,
                "overlay": patch,
                "upstream_sha256": None if path == GUARD.NEW_FILE else digest(original),
                "sha256": digest(replacement),
            }
        )
    manifest = {
        "schema_version": 1,
        "upstream": {"repository": GUARD.CORE_REPOSITORY, "commit": GUARD.CORE_COMMIT},
        "contract": GUARD.CONTRACT,
        "files": rows,
    }
    monkeypatch.setattr(GUARD, "verify_checkout", lambda root, commit: None)
    return checkout, overlay, manifest


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def manifest_file(tmp_path, manifest):
    path = tmp_path / "manifest.json"
    data = json.dumps(manifest).encode()
    path.write_bytes(data)
    return path, digest(data)


def test_valid_overlay_applies_complete_patch_set(source_tree):
    root, overlay, manifest = source_tree
    GUARD.apply_overlay(root, overlay, manifest)
    GUARD.verify_files(root, manifest)
    assert not list(root.rglob(".spectra-overlay-*"))


@pytest.mark.parametrize(
    "corruption", ["baseline", "replacement", "syntax", "new_guard_exists"]
)
def test_last_file_validation_failure_leaves_all_original_sources_untouched(
    source_tree, corruption
):
    root, overlay, manifest = source_tree
    row = manifest["files"][-1]
    if corruption == "baseline":
        (root / row["path"]).write_bytes(b"changed baseline\n")
    elif corruption == "replacement":
        (overlay / row["overlay"]).write_bytes(b"changed replacement\n")
    elif corruption == "syntax":
        changed = b"this is not valid: Python !!!\n"
        (overlay / row["overlay"]).write_bytes(changed)
        row["sha256"] = digest(changed)
    else:
        (root / GUARD.NEW_FILE).write_bytes(b"existing_guard = True\n")
    before = snapshot(root)
    with pytest.raises(GUARD.OverlayError):
        GUARD.apply_overlay(root, overlay, manifest)
    assert snapshot(root) == before
    assert not list(root.rglob(".spectra-overlay-*"))


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/outside.py",
        "hummingbot/../outside.py",
        "hummingbot//x.py",
        "hummingbot\\x.py",
    ],
)
def test_manifest_path_traversal_rejected_before_source_writes(
    source_tree, tmp_path, path
):
    root, _, manifest = source_tree
    manifest["files"][-1]["path"] = path
    before = snapshot(root)
    file, checksum = manifest_file(tmp_path, manifest)
    with pytest.raises(GUARD.OverlayError):
        GUARD.load_manifest(file, checksum)
    assert snapshot(root) == before


def test_manifest_checksum_prevents_substituting_approved_path_bytes(
    source_tree, tmp_path
):
    _, _, manifest = source_tree
    file, checksum = manifest_file(tmp_path, manifest)
    manifest["files"][0]["sha256"] = "0" * 64
    file.write_text(json.dumps(manifest))
    with pytest.raises(GUARD.OverlayError, match="manifest checksum"):
        GUARD.load_manifest(file, checksum)


@pytest.mark.parametrize(
    "kind",
    ["duplicate_path", "wrong_pin", "wrong_contract", "boolean_schema", "missing_file"],
)
def test_incomplete_or_misidentified_overlay_rejected(source_tree, tmp_path, kind):
    _, _, manifest = source_tree
    if kind == "duplicate_path":
        manifest["files"][-1] = copy.deepcopy(manifest["files"][0])
    elif kind == "wrong_pin":
        manifest["upstream"]["commit"] = "0" * 40
    elif kind == "wrong_contract":
        manifest["contract"] = "ordinary-upstream"
    elif kind == "boolean_schema":
        manifest["schema_version"] = True
    else:
        manifest["files"].pop()
    file, checksum = manifest_file(tmp_path, manifest)
    with pytest.raises(GUARD.OverlayError):
        GUARD.load_manifest(file, checksum)


def test_duplicate_json_keys_rejected(tmp_path):
    file = tmp_path / "manifest.json"
    data = b'{"schema_version":1,"schema_version":1}'
    file.write_bytes(data)
    with pytest.raises(GUARD.OverlayError, match="Duplicate JSON"):
        GUARD.load_manifest(file, digest(data))


@pytest.mark.parametrize("kind", ["replacement", "target", "parent"])
def test_overlay_symlink_cannot_write_outside_source_tree(source_tree, tmp_path, kind):
    root, overlay, manifest = source_tree
    row = next(
        row
        for row in manifest["files"]
        if row["overlay"] == "patches/rest_connection.py"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.py"
    victim.write_bytes(b"outside remains untouched\n")
    if kind == "replacement":
        patched = overlay / row["overlay"]
        patched.unlink()
        patched.symlink_to(victim)
    elif kind == "target":
        target = root / row["path"]
        target.unlink()
        target.symlink_to(victim)
    else:
        parent = (root / row["path"]).parent
        (root / row["path"]).unlink()
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
    before = snapshot(root)
    with pytest.raises(GUARD.OverlayError, match="Symlink"):
        GUARD.apply_overlay(root, overlay, manifest)
    assert snapshot(root) == before
    assert victim.read_bytes() == b"outside remains untouched\n"


def test_fifo_overlay_rejected_without_blocking_or_source_writes(source_tree):
    root, overlay, manifest = source_tree
    patched = overlay / manifest["files"][-1]["overlay"]
    patched.unlink()
    os.mkfifo(patched)
    before = snapshot(root)
    with pytest.raises(GUARD.OverlayError, match="Nonregular"):
        GUARD.apply_overlay(root, overlay, manifest)
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "value",
    [
        None,
        "continuumio/miniconda3",
        "continuumio/miniconda3:latest",
        "continuumio/miniconda3:latest@sha256:" + "a" * 64,
        "other/image@sha256:" + "a" * 64,
        "continuumio/miniconda3@sha256:" + "A" * 64,
    ],
)
def test_mutable_or_unapproved_base_image_refused(value):
    with pytest.raises(GUARD.OverlayError):
        GUARD.verify_base_image(value)


def test_explicit_immutable_base_image_accepted():
    GUARD.verify_base_image("continuumio/miniconda3@sha256:" + "a" * 64)


@pytest.mark.parametrize("problem", ["wrong_head", "dirty", "filter"])
def test_checkout_identity_and_cleanliness_guard_precedes_writes(
    tmp_path, monkeypatch, problem
):
    calls = []

    def git(root, args):
        calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            return "wrong" if problem == "wrong_head" else GUARD.CORE_COMMIT
        if args == ["config", "--local", "--list"]:
            return "filter.custom.process=some-command" if problem == "filter" else ""
        return " M modified.py" if problem == "dirty" else ""

    monkeypatch.setattr(GUARD, "_git", git)
    with pytest.raises(GUARD.OverlayError):
        GUARD.verify_checkout(tmp_path, GUARD.CORE_COMMIT)
    if problem == "filter":
        assert not any(args[0] == "status" for args in calls)


def wheel_file(tmp_path, overlay, manifest, extra=None):
    file = tmp_path / "candidate.whl"
    with zipfile.ZipFile(file, "w") as archive:
        for row in manifest["files"]:
            archive.writestr(row["path"], (overlay / row["overlay"]).read_bytes())
        if extra:
            archive.writestr(*extra)
    return file


def test_built_wheel_retains_every_manifested_source_without_extraction(
    source_tree, tmp_path
):
    _, overlay, manifest = source_tree
    file = wheel_file(tmp_path, overlay, manifest)
    assert GUARD.verify_wheel(file, manifest) == digest(file.read_bytes())
    assert not (tmp_path / "hummingbot").exists()


@pytest.mark.parametrize("path", ["../../outside", "/absolute/member", "normal\\wrong"])
def test_wheel_archive_traversal_rejected_without_extraction(
    source_tree, tmp_path, path
):
    _, overlay, manifest = source_tree
    file = wheel_file(tmp_path, overlay, manifest, (path, b"bad"))
    with pytest.raises(GUARD.OverlayError):
        GUARD.verify_wheel(file, manifest)
    assert not (tmp_path / "hummingbot").exists()


def test_wheel_symlink_member_rejected(source_tree, tmp_path):
    _, overlay, manifest = source_tree
    info = zipfile.ZipInfo("hummingbot/link.py")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    file = wheel_file(tmp_path, overlay, manifest, (info, b"../../outside"))
    with pytest.raises(GUARD.OverlayError, match="symlink"):
        GUARD.verify_wheel(file, manifest)


def test_wheel_changed_source_cannot_use_same_manifest(source_tree, tmp_path):
    _, overlay, manifest = source_tree
    (overlay / manifest["files"][0]["overlay"]).write_bytes(b"unauthorized = True\n")
    file = wheel_file(tmp_path, overlay, manifest)
    with pytest.raises(GUARD.OverlayError, match="wheel source checksum"):
        GUARD.verify_wheel(file, manifest)


def test_duplicate_wheel_members_are_not_accepted(source_tree, tmp_path):
    _, overlay, manifest = source_tree
    row = manifest["files"][0]
    with pytest.warns(UserWarning, match="Duplicate name"):
        file = wheel_file(tmp_path, overlay, manifest, (row["path"], b"duplicate\n"))
    with pytest.raises(GUARD.OverlayError, match="Duplicate"):
        GUARD.verify_wheel(file, manifest)


def test_source_tarball_is_never_extracted(source_tree, tmp_path):
    root, overlay, manifest = source_tree
    archive = tmp_path / "unsafe-source.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("../escaped.py")
        data = b"escaped = True\n"
        member.size = len(data)
        stream.addfile(member, io.BytesIO(data))
    before = snapshot(root)
    with pytest.raises(GUARD.OverlayError, match="directory"):
        GUARD.apply_overlay(archive, overlay, manifest)
    assert snapshot(root) == before
    assert not (tmp_path / "escaped.py").exists()


@pytest.fixture
def installed_doubles(source_tree, monkeypatch):
    root, overlay, manifest = source_tree
    GUARD.apply_overlay(root, overlay, manifest)
    monkeypatch.setattr(
        GUARD.importlib.metadata,
        "distribution",
        lambda name: types.SimpleNamespace(locate_file=lambda path: root),
    )
    modules = {}
    for path in GUARD.ALLOWED_FILES:
        name = path[:-3].replace("/", ".")
        module = types.SimpleNamespace(
            __file__=str(root / path), SPECTRA_EXECUTION_CONTRACT=GUARD.CONTRACT
        )
        for owner in (
            "OrderExecutor",
            "KrakenExchange",
            "RESTConnection",
            "ClientOrderTracker",
        ):
            setattr(
                module,
                owner,
                types.SimpleNamespace(SPECTRA_EXECUTION_CONTRACT=GUARD.CONTRACT),
            )
        for method in (
            "retain_spectra_order",
            "mark_spectra_accounting_ambiguous",
            "spectra_order_ambiguity",
            "spectra_order_fill_lag",
            "spectra_order_fill_watermark",
        ):
            setattr(module.ClientOrderTracker, method, lambda *args: None)
        modules[name] = module
    config_name = "hummingbot.strategy_v2.executors.order_executor.data_types"
    modules[config_name] = types.SimpleNamespace(
        __file__=str(
            root / "hummingbot/strategy_v2/executors/order_executor/data_types.py"
        ),
        OrderExecutorConfig=types.SimpleNamespace(
            model_fields=dict.fromkeys(GUARD.REQUIRED_CONFIG_FIELDS)
        ),
    )
    monkeypatch.setattr(GUARD.importlib, "import_module", lambda name: modules[name])
    return root, manifest, modules


def test_inert_installed_guard_checks_actual_module_paths_and_config(installed_doubles):
    _, manifest, _ = installed_doubles
    GUARD.verify_installed(manifest)


@pytest.mark.parametrize(
    "problem",
    [
        "shadow",
        "module_marker",
        "class_marker",
        "config_field",
        "tracker_surface",
        "tracker_fill_lag_surface",
        "tracker_fill_watermark_surface",
        "source_overwritten",
    ],
)
def test_installed_guard_rejects_shadowing_or_replaced_core(
    installed_doubles, problem, tmp_path
):
    root, manifest, modules = installed_doubles
    executor = modules["hummingbot.strategy_v2.executors.order_executor.order_executor"]
    if problem == "shadow":
        executor.__file__ = str(tmp_path / "shadow.py")
    elif problem == "module_marker":
        executor.SPECTRA_EXECUTION_CONTRACT = "other"
    elif problem == "class_marker":
        executor.OrderExecutor.SPECTRA_EXECUTION_CONTRACT = "other"
    elif problem == "config_field":
        del modules[
            "hummingbot.strategy_v2.executors/order_executor/data_types".replace(
                "/", "."
            )
        ].OrderExecutorConfig.model_fields["price"]
    elif problem == "tracker_surface":
        del modules[
            "hummingbot.connector.client_order_tracker"
        ].ClientOrderTracker.mark_spectra_accounting_ambiguous
    elif problem == "tracker_fill_lag_surface":
        del modules[
            "hummingbot.connector.client_order_tracker"
        ].ClientOrderTracker.spectra_order_fill_lag
    elif problem == "tracker_fill_watermark_surface":
        del modules[
            "hummingbot.connector.client_order_tracker"
        ].ClientOrderTracker.spectra_order_fill_watermark
    else:
        (root / manifest["files"][0]["path"]).write_bytes(b"original_upstream = True\n")
    with pytest.raises(GUARD.OverlayError):
        GUARD.verify_installed(manifest)


def test_missing_journal_never_reaches_uvicorn(monkeypatch):
    launched = []
    fake = types.SimpleNamespace(
        verify_journal=lambda: (_ for _ in ()).throw(RuntimeError("missing"))
    )
    monkeypatch.setattr(GUARD.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(GUARD.os, "execvp", lambda *args: launched.append(args))
    with pytest.raises(GUARD.OverlayError, match="persistent attempt journal"):
        GUARD.verify_runtime_journal()
    assert launched == []


def test_build_contract_does_not_attest_unbuilt_or_live_backend():
    contract = json.loads((BUILD / "build.contract.json").read_text())
    assert contract["compiled_build_verified"] is False
    assert contract["installed_backend_verified"] is False
    assert contract["approved_for_live"] is False
    assert contract["base_image"]["verified_reference"] is None
    assert contract["wheel_sha256"] is None
    assert contract["api_image_digest"] is None


def test_private_api_token_is_a_required_secret_and_entrypoint_verifies_runtime():
    dockerfile = (BUILD / "Dockerfile").read_text()
    assert "--mount=type=secret,id=github_token,required=true" in dockerfile
    assert "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}" in dockerfile
    assert "ARG BASE_IMAGE=" not in dockerfile
    assert "ARG GITHUB_TOKEN" not in dockerfile
    assert '--runtime"]' in dockerfile
    assert "pip install hummingbot" not in dockerfile


@pytest.fixture
def real_patch_copy(tmp_path):
    overlay = tmp_path / "actual-overlay"
    overlay.mkdir()
    rows = []
    for path, patch in GUARD.ALLOWED_FILES.items():
        data = (BUILD / patch).read_bytes()
        target = overlay / patch
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rows.append(
            {
                "path": path,
                "overlay": patch,
                # verify-overlay establishes internal coherence, not baseline identity.
                "upstream_sha256": None if path == GUARD.NEW_FILE else "a" * 64,
                "sha256": digest(data),
            }
        )
    manifest = {
        "schema_version": 1,
        "upstream": {"repository": GUARD.CORE_REPOSITORY, "commit": GUARD.CORE_COMMIT},
        "contract": GUARD.CONTRACT,
        "files": rows,
    }
    return overlay, manifest


def test_cli_verifies_actual_overlay_without_git_backend_import_or_writes(
    real_patch_copy, tmp_path, monkeypatch
):
    overlay, manifest = real_patch_copy
    file, checksum = manifest_file(tmp_path, manifest)
    before = snapshot(overlay)

    def forbidden(*args, **kwargs):
        raise AssertionError("Source coherence must not use Git or import a backend")

    monkeypatch.setattr(GUARD, "_git", forbidden)
    monkeypatch.setattr(GUARD.importlib, "import_module", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_overlay.py",
            "--manifest",
            str(file),
            "--expected-manifest-sha256",
            checksum,
            "--overlay-root",
            str(overlay),
            "--verify-overlay",
        ],
    )
    assert GUARD.main() == 0
    assert snapshot(overlay) == before


def test_cli_refuses_corrupted_actual_overlay_bytes(
    real_patch_copy, tmp_path, monkeypatch, capsys
):
    overlay, manifest = real_patch_copy
    file, checksum = manifest_file(tmp_path, manifest)
    target = overlay / "patches/kraken_exchange.py"
    target.write_bytes(target.read_bytes() + b"\nUNAUTHORIZED = True\n")
    before = snapshot(overlay)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_overlay.py",
            "--manifest",
            str(file),
            "--expected-manifest-sha256",
            checksum,
            "--overlay-root",
            str(overlay),
            "--verify-overlay",
        ],
    )
    assert GUARD.main() == 2
    assert "Patched source checksum mismatch" in capsys.readouterr().err
    assert snapshot(overlay) == before


def test_verify_overlay_refuses_checksum_matching_invalid_python(source_tree):
    _, overlay, manifest = source_tree
    row = manifest["files"][-1]
    data = b"def broken(:\n"
    (overlay / row["overlay"]).write_bytes(data)
    row["sha256"] = digest(data)
    before = snapshot(overlay)
    with pytest.raises(GUARD.OverlayError, match="does not compile"):
        GUARD.verify_overlay(overlay, manifest)
    assert snapshot(overlay) == before


def test_packaged_license_texts_match_exact_pins_and_are_copied_to_image():
    for name, expected in GUARD.LICENSE_SHA256.items():
        assert digest((BUILD / "licenses" / name).read_bytes()) == expected
    dockerfile = (BUILD / "Dockerfile").read_text()
    assert (
        "COPY --from=sources /build/repair/licenses /opt/spectra-core/licenses"
        in dockerfile
    )


@pytest.mark.parametrize("tamper", [False, True])
def test_license_verification_checks_source_and_packaged_text_without_writes(
    tmp_path, tamper
):
    api, core, overlay = tmp_path / "api", tmp_path / "core", tmp_path / "overlay"
    api.mkdir()
    core.mkdir()
    (overlay / "licenses").mkdir(parents=True)
    for name, source_root in (
        ("hummingbot-api-LICENSE", api),
        ("hummingbot-core-LICENSE", core),
    ):
        data = (BUILD / "licenses" / name).read_bytes()
        (source_root / "LICENSE").write_bytes(data)
        (overlay / "licenses" / name).write_bytes(data)
    if tamper:
        (api / "LICENSE").write_bytes(b"substituted license\n")
    before = snapshot(tmp_path)
    if tamper:
        with pytest.raises(GUARD.OverlayError, match="license checksum mismatch"):
            GUARD.verify_licenses(api, core, overlay)
    else:
        GUARD.verify_licenses(api, core, overlay)
    assert snapshot(tmp_path) == before
