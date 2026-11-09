"""Source-backed Spectra Finance module readiness registry.

Names and statuses are customer-facing. Runtime state is supplied separately by
ModuleLifecycleStore; this registry never claims that unverified code is active.
"""
from __future__ import annotations

from copy import deepcopy

_MODULES = (
    {"id":"spectra-intelligence","name":"Spectra Intelligence","status":"implemented","readiness":"analysis-only","description":"Research, analysis, task coordination and proposals; no independent capital authority."},
    {"id":"capital-governor","name":"Capital Governor","status":"implemented","readiness":"protected-core","description":"Sole capital and policy authority; validates proposals, reservations, ownership and dispatch eligibility."},
    {"id":"vcap","name":"VCAP","status":"partial","readiness":"historical-runtime-present","description":"Profit-floor exits, protected continuation, high-water marks and multi-position supervision exist; complete current paper-execution integration remains in progress."},
    {"id":"dttp","name":"DTTP","status":"partial","readiness":"source-recovered","description":"Protected-profit continuation behavior is linked to VCAP; full independent runtime and paper-execution integration remain unverified. Activation trigger defaults off."},
    {"id":"dtsl","name":"DTSL","status":"unverified","readiness":"definition-recovery","description":"Independent definition and complete source implementation have not yet been verified. Activation is blocked."},
    {"id":"pssm","name":"PSSM","status":"not-implemented","readiness":"doctrine-only","description":"Named in operating doctrine; no complete production implementation verified."},
    {"id":"mir","name":"MIR","status":"research-only","readiness":"candidate","description":"Research candidate; no complete production runtime verified."},
    {"id":"selected-recovery","name":"Selected Recovery","status":"default-off","readiness":"integration-candidate","description":"Native recovery shadow accounting and sealed-basis integration; activation remains disabled."},
    {"id":"dust-lifecycle","name":"Dust Lifecycle","status":"default-off","readiness":"integration-candidate","description":"Evidence-bound residual classification, ownership checkpoints and guarded successor admission; activation remains disabled."},
    {"id":"paper-execution","name":"Paper Execution","status":"integration-in-progress","readiness":"paper-only","description":"Governed paper execution; local connector, fill reconciliation and workstation verification remain outstanding."},
    {"id":"safety-controls","name":"Safety and Ownership Controls","status":"implemented","readiness":"protected-core","description":"Capital Governor, position mutation ownership, bounded protected buys, emergency halt and fail-closed execution boundaries."},
)

def get_spectra_module_registry() -> dict:
    """Return a detached source-backed readiness snapshot."""
    return {
        "product": "Spectra Finance",
        "release_stage": "IN_DEVELOPMENT",
        "live_trading_enabled": False,
        "paper_only_target": True,
        "status_note": (
            "Readiness is source-backed; runtime lifecycle state is persisted "
            "separately. Unverified modules cannot be activated."
        ),
        "system_flow": (
            {"id":"spectra-intelligence","name":"Spectra Intelligence","role":"Research, coordinate and propose","authority":"No independent capital authority"},
            {"id":"capital-governor","name":"Capital Governor","role":"Validate, authorize and account","authority":"Sole capital and policy authority"},
            {"id":"paper-execution","name":"Paper Execution","role":"Simulate authorized orders","authority":"Paper-only execution adapter"},
        ),
        "modules": deepcopy(_MODULES),
    }
