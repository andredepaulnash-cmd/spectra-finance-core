"""Fail-closed paper-only runtime settings. No exchange credential schema exists here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class PaperSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaperSafetySettings:
    paper_only: bool
    allow_live_execution: bool
    connector: str = "kraken_paper_trade"

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "PaperSafetySettings":
        if env.get("SPECTRA_PAPER_ONLY") != "true":
            raise PaperSafetyError("SPECTRA_PAPER_ONLY must be exactly 'true'")
        if env.get("SPECTRA_ALLOW_LIVE_EXECUTION") != "false":
            raise PaperSafetyError("SPECTRA_ALLOW_LIVE_EXECUTION must be exactly 'false'")
        forbidden = []
        for key in env:
            upper = key.upper()
            if any(
                token in upper
                for token in (
                    "KRAKEN_API_KEY",
                    "KRAKEN_API_SECRET",
                    "EXCHANGE_API_KEY",
                    "EXCHANGE_API_SECRET",
                    "EXCHANGE_PRIVATE_KEY",
                    "EXCHANGE_PASSPHRASE",
                )
            ):
                forbidden.append(key)
        if forbidden:
            raise PaperSafetyError("exchange credential environment variables are forbidden")
        configured_connector = env.get("SPECTRA_PAPER_CONNECTOR", "kraken_paper_trade")
        if configured_connector != "kraken_paper_trade":
            raise PaperSafetyError("SPECTRA_PAPER_CONNECTOR must be kraken_paper_trade")
        return cls(paper_only=True, allow_live_execution=False)

    def assert_paper_mode(self) -> None:
        if not self.paper_only or self.allow_live_execution:
            raise PaperSafetyError("paper-only invariant violated")

    def validate_order_boundary(self, connector: str, authorization_id: str | None) -> None:
        self.assert_paper_mode()
        if connector != self.connector:
            raise PaperSafetyError("only the configured paper connector is permitted")
        if not authorization_id or not authorization_id.strip():
            raise PaperSafetyError("CapitalGovernor authorization is required")
