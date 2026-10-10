"""Passing vulnerability reproductions never mean installed/live readiness."""

import asyncio
import importlib.util
import socket
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_limit_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("spectra_limit_source_probe", SCRIPT)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def test_all_vendored_primary_source_bytes_match_exact_pin():
    manifest, sources = PROBE.verify_source_integrity()
    assert manifest["core_revision"] == PROBE.CORE_PIN
    assert manifest["source_logic_only"] is True
    assert manifest["installed_backend_verified"] is False
    assert len(sources) == 8


def test_probe_external_io_trap_rejects_socket_creation():
    with PROBE.io_trap(), pytest.raises(AssertionError, match="External I/O"):
        socket.socket()


@pytest.mark.parametrize("case", PROBE.CASES)
def test_exact_pinned_limit_lifecycle_observation(case):
    finding = asyncio.run(PROBE.probe_case(case))
    assert finding["case"] == case
    assert finding["source_scope"] == "source_logic_only"
    assert finding["installed_backend_verified"] is False
    assert finding["live_approval_eligible"] is False
    if case in {
        "fixed_limit_partial_cancel_retry",
        "stop_known_partial_preserves_hold",
        "partial_failure_fixed_limit_retry",
        "retry_exhaustion_preserves_known_partial",
        "kraken_limit_request_drops_ioc",
    }:
        assert finding["outcome"] == "observed_source_behavior"
    else:
        assert finding["outcome"] == "deployment_blocker"
