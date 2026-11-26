"""Explicit, isolated PAPER/LIVE profile identity for Spectra Core.

Profile resolution is configuration, not trade authorization. Every dispatch still
requires CapitalGovernor authorization and an API-verified signed receipt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


_ACCOUNT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONNECTOR = re.compile(r"^[a-z0-9_]{1,64}$")


class ExecutionProfileError(ValueError):
    """Invalid or inconsistent execution profile."""


class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    mode: ExecutionMode
    account_name: str
    connector_name: str
    portfolio_namespace: str

    @property
    def profile_id(self) -> str:
        return f"{self.mode.value}:{self.account_name}:{self.connector_name}"

    def assert_order_scope(self, account_name: str, connector_name: str) -> None:
        if (account_name, connector_name) != (self.account_name, self.connector_name):
            raise ExecutionProfileError(
                "order account or connector does not match the resolved execution profile"
            )


def resolve_execution_profile(env: Mapping[str, str]) -> ExecutionProfile:
    """Resolve the same profile contract used by the Hummingbot API service."""
    raw = env.get("SPECTRA_EXECUTION_MODE", "PAPER").strip().upper()
    try:
        mode = ExecutionMode(raw)
    except ValueError as exc:
        raise ExecutionProfileError("SPECTRA_EXECUTION_MODE must be PAPER or LIVE") from exc

    paper_account = env.get("SPECTRA_PAPER_ACCOUNT", "master_account").strip()
    if mode is ExecutionMode.PAPER:
        account = paper_account
        connector = env.get("SPECTRA_PAPER_CONNECTOR", "kraken_paper_trade").strip().lower()
        namespace = "paper"
        if connector != "kraken_paper_trade":
            raise ExecutionProfileError("PAPER profile requires kraken_paper_trade")
    else:
        if env.get("SPECTRA_LIVE_PROFILE_ENABLED", "").strip().lower() != "true":
            raise ExecutionProfileError("LIVE profile is not explicitly enabled")
        account = env.get("SPECTRA_LIVE_ACCOUNT", "").strip()
        connector = env.get("SPECTRA_LIVE_CONNECTOR", "").strip().lower()
        namespace = "live"
        if not connector or connector.endswith("_paper_trade"):
            raise ExecutionProfileError("LIVE profile requires a native non-paper connector")
        if account == paper_account:
            raise ExecutionProfileError("PAPER and LIVE require separate account identities")

    if not _ACCOUNT.fullmatch(account):
        raise ExecutionProfileError("execution profile account name is missing or invalid")
    if not _CONNECTOR.fullmatch(connector):
        raise ExecutionProfileError("execution profile connector name is missing or invalid")
    return ExecutionProfile(mode, account, connector, namespace)
