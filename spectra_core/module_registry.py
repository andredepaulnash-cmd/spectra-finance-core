"""Source-backed Spectra Finance module readiness registry.

This is a product visibility contract, not an activation switch. Statuses are
intentionally conservative and must be updated only after source and test review.
"""

from __future__ import annotations

from copy import deepcopy


_MODULES = (
    {
        "id": "condor",
        "name": "Condor",
        "status": "implemented",
        "readiness": "analysis-only",
        "description": "Research, analysis and proposals. Condor does not directly authorize or control capital.",
    },
    {
        "id": "capital-governor",
        "name": "Spectra Core / Capital Governor",
        "status": "implemented",
        "readiness": "protected-core",
        "description": "The sole capital and policy authority; validates proposals, reservations, ownership and dispatch eligibility.",
    },
    {
        "id": "vcap",
        "name": "VCAP",
        "status": "partial",
        "readiness": "historical-runtime-present",
        "description": "Profit-floor exits, protected continuation, high-water marks and multi-position supervision exist; complete current paper-execution integration remains in progress.",
    },
    {
        "id": "dttp",
        "name": "DTTP",
        "status": "partial",
        "readiness": "source-recovered",
        "description": "Protected-profit continuation behavior is linked to VCAP; its complete current runtime and paper-execution integration still need verification.",
    },
    {
        "id": "dtsl",
        "name": "DTSL",
        "status": "unverified",
        "readiness": "definition-recovery",
        "description": "Referenced alongside DTTP in historical work; its independent definition and complete source implementation are still being reconstructed.",
    },
    {
        "id": "pssm",
        "name": "PSSM",
        "status": "not-implemented",
        "readiness": "doctrine-only",
        "description": "Named in Condor's operating doctrine, but no complete production implementation has been verified.",
    },
    {
        "id": "mir",
        "name": "MIR",
        "status": "research-only",
        "readiness": "candidate",
        "description": "Historical material mentions MIR rotation as later strategy/accounting work. It is a candidate for the additional remembered module, not a verified production implementation.",
    },
    {
        "id": "selected-recovery",
        "name": "Selected Recovery",
        "status": "default-off",
        "readiness": "integration-candidate",
        "description": "Native recovery shadow accounting and sealed-basis integration are present in a draft candidate; activation remains disabled.",
    },
    {
        "id": "dust-lifecycle",
        "name": "Dust Lifecycle",
        "status": "default-off",
        "readiness": "integration-candidate",
        "description": "Evidence-bound residual classification, ownership checkpoints and guarded successor admission are being integrated; activation remains disabled.",
    },
    {
        "id": "paper-execution",
        "name": "Hummingbot Paper Execution",
        "status": "integration-in-progress",
        "readiness": "paper-only",
        "description": "Governed paper execution and Core-signed API dispatch are under integration; local connector, fill reconciliation and Bazzite verification remain outstanding.",
    },
    {
        "id": "safety-controls",
        "name": "Safety and Ownership Controls",
        "status": "implemented",
        "readiness": "protected-core",
        "description": "Capital Governor, position mutation ownership, bounded protected buys, emergency halt and fail-closed execution boundaries.",
    },
)

_SYSTEM_FLOW = (
    {
        "id": "condor",
        "name": "Condor",
        "role": "Analyze and propose",
        "authority": "No independent capital authority",
    },
    {
        "id": "spectra-core",
        "name": "Spectra Core",
        "role": "Validate, authorize and account",
        "authority": "Sole capital and policy authority",
    },
    {
        "id": "hummingbot",
        "name": "Hummingbot",
        "role": "Execute approved paper orders",
        "authority": "Execution adapter only",
    },
)


def get_spectra_module_registry() -> dict:
    """Return a detached, read-only-by-convention product readiness snapshot."""
    return {
        "product": "Spectra Finance",
        "release_stage": "IN_DEVELOPMENT",
        "live_trading_enabled": False,
        "paper_only_target": True,
        "status_note": (
            "Source-backed readiness, not a claim that every module is active. "
            "Draft branches and local runtime validation remain separate checks."
        ),
        "system_flow": deepcopy(_SYSTEM_FLOW),
        "modules": deepcopy(_MODULES),
    }
