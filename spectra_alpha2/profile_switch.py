"""Reconciliation gate for switching between isolated Spectra execution profiles.

This gate is intentionally pure. The runtime must obtain these facts from
Hummingbot order/fill reconciliation and the active portfolio supervisor before
calling it; client-provided booleans are never authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass

from .execution_profile import ExecutionProfile


class ProfileSwitchRejected(RuntimeError):
    """The active execution environment is not safe to leave."""


@dataclass(frozen=True, slots=True)
class ProfileRuntimeState:
    supervisor_stopped: bool
    pending_orders: int
    unreconciled_fills: int
    balances_reconciled: bool
    target_profile_ready: bool

    def __post_init__(self) -> None:
        if self.pending_orders < 0 or self.unreconciled_fills < 0:
            raise ValueError("reconciliation counts cannot be negative")


def assert_profile_switch_allowed(
    current: ExecutionProfile,
    target: ExecutionProfile,
    state: ProfileRuntimeState,
) -> None:
    """Permit only a quiescent, fully reconciled transition to another profile."""
    if current.profile_id == target.profile_id:
        return
    if current.portfolio_namespace == target.portfolio_namespace:
        raise ProfileSwitchRejected("source and target profiles must have separate state namespaces")
    if not state.supervisor_stopped:
        raise ProfileSwitchRejected("active portfolio supervisor must be stopped before switching")
    if state.pending_orders:
        raise ProfileSwitchRejected("cannot switch while exchange orders are pending")
    if state.unreconciled_fills:
        raise ProfileSwitchRejected("cannot switch while fills remain unreconciled")
    if not state.balances_reconciled:
        raise ProfileSwitchRejected("cannot switch before exchange balances are reconciled")
    if not state.target_profile_ready:
        raise ProfileSwitchRejected("target execution profile is not ready")
