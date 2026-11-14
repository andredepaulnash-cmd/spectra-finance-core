import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_inventory_covers_all_deployed_and_optional_layers():
    runtime = json.loads((ROOT / "release" / "runtime-components.json").read_text())
    ids = {item["id"] for item in runtime["components"]}
    assert {
        "host-os", "spectra-core", "condor", "hummingbot-api",
        "hummingbot-client", "kraken-paper-connector", "postgres",
        "emqx", "gateway", "spectra-dashboard", "python-runtime",
        "container-runtime",
    } <= ids


def test_compatibility_matrix_does_not_approve_unmeasured_baseline():
    matrix = json.loads((ROOT / "release" / "compatibility-matrix.json").read_text())
    assert matrix["baseline_status"] == "awaiting-host-inventory"
    assert matrix["policy"]["candidate_refs_are_not_approved"] is True
    assert matrix["policy"]["mutable_latest_tags_are_not_release_pins"] is True
    assert matrix["policy"]["apply_requires_operator_approval"] is True
    assert matrix["policy"]["paper_only_default"] is True
    assert matrix["policy"]["live_execution_default"] is False


def test_compatibility_matrix_includes_paper_execution_and_recovery_gates():
    matrix = json.loads((ROOT / "release" / "compatibility-matrix.json").read_text())
    api = next(item for item in matrix["components"] if item["id"] == "hummingbot-api")
    assert "market-data-tracker-readiness" in api["gates"]
    assert "order-submit-ack-reconciliation" in api["gates"]
    assert "fill-lifecycle" in api["gates"]
    assert "executor-restart-recovery" in api["gates"]
    core = next(item for item in matrix["components"] if item["id"] == "spectra-core")
    assert "capital-governor" in core["gates"]
    assert "paper-state-restore" in core["gates"]
