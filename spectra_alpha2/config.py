"""Validated runtime configuration for the paper-only Alpha 2.0 service."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .policy import MAX_OPEN_POSITIONS
from .safety import PaperSafetySettings


@dataclass(frozen=True, slots=True)
class Alpha2Config:
    database_path: Path
    hummingbot_api_url: str
    hummingbot_api_username: str
    hummingbot_api_password: str
    scan_interval_seconds: float
    target_positions: int
    max_new_positions_per_cycle: int
    min_quote_volume_24h: Decimal | None
    entry_quote_assets: frozenset[str]
    max_spread_fraction: Decimal
    max_slippage_fraction: Decimal
    order_timeout_seconds: float

    @classmethod
    def from_environment(cls, env=None) -> "Alpha2Config":
        env=os.environ if env is None else env
        PaperSafetySettings.from_environment(env).assert_paper_mode()
        if env.get("SPECTRA_PAPER_CONNECTORS")!="kraken_paper_trade":
            raise RuntimeError("Alpha 2.0 requires SPECTRA_PAPER_CONNECTORS=kraken_paper_trade")
        def integer(name,default):
            try: value=int(env.get(name,str(default)))
            except (TypeError,ValueError) as exc: raise ValueError(f"{name} must be an integer") from exc
            return value
        def decimal(name,default):
            try: value=Decimal(str(env.get(name,str(default))))
            except Exception as exc: raise ValueError(f"{name} must be a decimal") from exc
            if not value.is_finite(): raise ValueError(f"{name} must be finite")
            return value
        target=integer("SPECTRA_MAX_POSITIONS",MAX_OPEN_POSITIONS)
        if not 1<=target<=MAX_OPEN_POSITIONS:
            raise ValueError(f"SPECTRA_MAX_POSITIONS must be between 1 and {MAX_OPEN_POSITIONS}")
        max_new=integer("SPECTRA_MAX_NEW_POSITIONS_PER_CYCLE",50)
        if max_new<1: raise ValueError("SPECTRA_MAX_NEW_POSITIONS_PER_CYCLE must be positive")
        interval=float(env.get("SPECTRA_SCAN_INTERVAL_SECONDS","10"))
        timeout=float(env.get("SPECTRA_ORDER_TIMEOUT_SECONDS","60"))
        if interval<=0 or timeout<=0: raise ValueError("scan and order timeout values must be positive")
        raw_min_volume=env.get("SPECTRA_MIN_QUOTE_VOLUME_24H","100000").strip()
        min_volume=decimal("SPECTRA_MIN_QUOTE_VOLUME_24H",raw_min_volume) if raw_min_volume else None
        quote_assets=frozenset(
            item.strip().upper()
            for item in env.get("SPECTRA_ENTRY_QUOTE_ASSETS","USD").split(",")
            if item.strip()
        )
        if min_volume is not None and min_volume <= 0:
            raise ValueError("SPECTRA_MIN_QUOTE_VOLUME_24H must be positive when configured")
        max_spread=decimal("SPECTRA_MAX_SPREAD_FRACTION","0.01")
        max_slippage=decimal("SPECTRA_MAX_SLIPPAGE_FRACTION","0.005")
        if not Decimal("0")<=max_spread<=Decimal("0.02") or not Decimal("0")<=max_slippage<=Decimal("0.02"):
            raise ValueError("market volume, spread, or slippage configuration is outside safe bounds")
        username=env.get("SPECTRA_HB_API_USERNAME","")
        password=env.get("SPECTRA_HB_API_PASSWORD","")
        if not username or not password:
            raise RuntimeError("internal Hummingbot API service credentials are required")
        return cls(
            database_path=Path(env.get("SPECTRA_DB_PATH","/data/spectra_alpha2.sqlite3")),
            hummingbot_api_url=env.get("SPECTRA_HB_API_URL","http://hummingbot-api:8000"),
            hummingbot_api_username=username,
            hummingbot_api_password=password,
            scan_interval_seconds=interval,
            target_positions=target,
            max_new_positions_per_cycle=max_new,
            min_quote_volume_24h=min_volume,
            entry_quote_assets=quote_assets,
            max_spread_fraction=max_spread,
            max_slippage_fraction=max_slippage,
            order_timeout_seconds=timeout,
        )
