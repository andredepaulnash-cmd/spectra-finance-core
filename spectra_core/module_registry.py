"""Source-backed Spectra Finance module readiness registry.

Names and statuses are customer-facing. Runtime state is supplied separately by
ModuleLifecycleStore; this registry never claims that unverified code is active.
"""
from __future__ import annotations

from copy import deepcopy

_MODULES = (
    {"id": "spectra-intelligence", "name": "Spectra Intelligence", "status": "implemented", "readiness": "analysis-and-orchestration", "description": "Coordinates Condor intelligence, strategy evaluation, asset selection, lifecycle supervision and proposals; no independent capital authority."},
    {"id": "asset-intelligence", "name": "Asset Intelligence", "status": "partial", "readiness": "discovery-and-fingerprints-present", "description": "Kraken market discovery, multi-horizon asset fingerprints and historical fleet research are present; autonomous fleet admission and rotation integration remain in progress."},
    {"id": "portfolio-scheduler", "name": "Portfolio Scheduler", "status": "integration-in-progress", "readiness": "autonomous-admission-controller-added-ci-pending", "description": "The new Core background supervisor connects market discovery, Governor reservations, protected Hummingbot executor creation, VCAP servicing and automatic vacancy replenishment; full CI and restart certification are pending."},
    {"id": "capital-governor", "name": "Capital Governor", "status": "implemented", "readiness": "protected-core", "description": "Sole capital and policy authority; validates proposals, reservations, ownership, position limits and dispatch eligibility."},
    {"id": "vcap", "name": "VCAP", "status": "partial", "readiness": "lifecycle-and-recovery-present", "description": "Tranche planning, recovery lifecycle, profit-floor exits, protected continuation and multi-position supervision exist; autonomous portfolio admission and complete end-to-end execution remain in progress."},
    {"id": "dttp", "name": "DTTP", "status": "partial", "readiness": "source-recovered", "description": "Protected-profit continuation behavior is linked to VCAP; full independent runtime and paper-execution integration remain unverified. Activation trigger defaults off."},
    {"id": "dtsl", "name": "DTSL", "status": "unverified", "readiness": "definition-recovery", "description": "Independent definition and complete source implementation have not yet been verified. Activation is blocked."},
    {"id": "pssm", "name": "PSSM", "status": "not-implemented", "readiness": "doctrine-only", "description": "Named in operating doctrine; no complete production implementation verified."},
    {"id": "mir", "name": "MIR", "status": "research-only", "readiness": "candidate", "description": "Research candidate; no complete production runtime verified."},
    {"id": "selected-recovery", "name": "Selected Recovery", "status": "default-off", "readiness": "integration-candidate", "description": "Native recovery shadow accounting and sealed-basis integration; activation remains disabled."},
    {"id": "dust-lifecycle", "name": "Dust Lifecycle", "status": "default-off", "readiness": "integration-candidate", "description": "Evidence-bound residual classification, ownership checkpoints and guarded successor admission; activation remains disabled."},
    {"id": "paper-execution", "name": "Paper Execution", "status": "integration-in-progress", "readiness": "signed-api-order-verified-core-executor-pending", "description": "The API signed paper-order route now has CI coverage for fills, balances and order/trade history; the Core-managed executor and full portfolio lifecycle still require end-to-end verification."},
    {"id": "live-execution", "name": "Live Execution", "status": "available-behind-authorization", "readiness": "runtime-gate-present-capital-policy-halted-by-default", "description": "Live connectors remain part of the Hummingbot execution architecture; live dispatch requires SPECTRA_PAPER_ONLY=false, SPECTRA_ALLOW_LIVE_EXECUTION=true, a separately configured live capital policy and a deliberately released live breaker. Autonomous live portfolio management additionally requires SPECTRA_LIVE_AUTORUN=true."},
    {"id": "safety-controls", "name": "Safety and Ownership Controls", "status": "implemented", "readiness": "protected-core", "description": "Capital Governor, position mutation ownership, bounded protected buys, emergency halt and fail-closed execution boundaries."},
)


def get_spectra_module_registry() -> dict:
    """Return a detached source-backed readiness snapshot."""
    return {
        "product": "Spectra Finance",
        "release_stage": "IN_DEVELOPMENT",
        "live_trading_available": True,
        "live_trading_enabled": False,
        "live_execution_requires_explicit_authorization": True,
        "paper_only_default": True,
        "paper_only_target": False,
        "status_note": (
            "Paper mode is the default. Live capability is preserved but "
            "remains separately authorized and capital-halted until configured. "
            "Runtime lifecycle state is persisted separately; unverified modules "
            "cannot be activated."
        ),
        "system_flow": (
            {"id": "spectra-intelligence", "name": "Spectra Intelligence / Condor", "role": "Discover, evaluate, coordinate and propose", "authority": "No independent capital authority"},
            {"id": "portfolio-scheduler", "name": "Portfolio Scheduler", "role": "Maintain the target fleet and submit eligible entry proposals", "authority": "No capital authority; every entry requires Capital Governor approval"},
            {"id": "capital-governor", "name": "Capital Governor", "role": "Validate, authorize and account", "authority": "Sole capital and policy authority"},
            {"id": "paper-execution", "name": "Hummingbot Paper Execution", "role": "Execute authorized simulated orders", "authority": "Governor authorization and isolated paper connector"},
            {"id": "live-execution", "name": "Hummingbot Live Execution", "role": "Execute only after separate authorization", "authority": "Explicit live flag, live capital policy and live breaker"},
        ),
        "modules": deepcopy(_MODULES),
    }
