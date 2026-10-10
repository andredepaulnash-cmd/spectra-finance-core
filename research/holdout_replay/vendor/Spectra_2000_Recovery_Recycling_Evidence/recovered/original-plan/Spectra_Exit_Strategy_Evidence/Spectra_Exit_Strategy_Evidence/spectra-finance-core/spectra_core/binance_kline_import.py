"""Strict importer for official Binance Public Data spot kline archives.

The archive contains recorded OHLCV/trade aggregates, not quotes or an order
book.  Consumers must therefore use an explicitly estimated bar-execution model
and must not relabel the result as a top-of-book replay.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable


D = Decimal
ARCHIVE_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+USDT)-1m-(?P<day>\d{4}-\d{2}-\d{2})\.zip$"
)
EXCLUDED_BASES = frozenset(
    {
        "BTC",
        "XBT",
        "ETH",
        "USDT",
        "USDC",
        "FDUSD",
        "TUSD",
        "DAI",
        "BUSD",
    }
)


def _decimal(value: object, field: str, *, positive: bool = False) -> D:
    result = D(str(value))
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{field} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _second(raw: str) -> int:
    value = int(raw)
    # Binance spot archives use microseconds for data from 2025 onward, while
    # older archives use milliseconds.  Reject rather than guess other units.
    if value >= 10**15:
        if value % 1_000_000:
            raise ValueError("kline open time is not aligned to a whole second")
        return value // 1_000_000
    if value >= 10**12:
        if value % 1_000:
            raise ValueError("kline open time is not aligned to a whole second")
        return value // 1_000
    raise ValueError("unsupported kline timestamp unit")


@dataclass(frozen=True)
class MinuteBar:
    timestamp: int
    open: D
    high: D
    low: D
    close: D
    base_volume: D
    quote_volume: D
    trades: int

    def __post_init__(self) -> None:
        if type(self.timestamp) is not int or self.timestamp <= 0:
            raise ValueError("timestamp must be a positive integer")
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field, positive=True))
        for field in ("base_volume", "quote_volume"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field))
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC geometry")
        if self.high < self.low:
            raise ValueError("high must be >= low")
        if type(self.trades) is not int or self.trades < 0:
            raise ValueError("trades must be a nonnegative integer")

    @property
    def vwap(self) -> D | None:
        return self.quote_volume / self.base_volume if self.base_volume > 0 else None


@dataclass(frozen=True)
class KlineDataset:
    series: dict[str, tuple[MinuteBar, ...]]
    manifest: dict

    @property
    def pairs(self) -> tuple[str, ...]:
        return tuple(sorted(self.series))

    @property
    def frames(self) -> int:
        return len(next(iter(self.series.values())))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_archive(path: Path) -> tuple[str, date, tuple[MinuteBar, ...]]:
    match = ARCHIVE_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(f"unexpected archive name: {path.name}")
    symbol = match.group("symbol")
    base = symbol[: -len("USDT")]
    if not base or base in EXCLUDED_BASES:
        raise ValueError(f"excluded or invalid base asset: {base}")
    day = date.fromisoformat(match.group("day"))
    expected_member = path.name[:-4] + ".csv"
    bars: list[MinuteBar] = []
    with zipfile.ZipFile(path) as archive:
        if archive.namelist() != [expected_member]:
            raise ValueError(f"archive must contain only {expected_member}")
        with archive.open(expected_member) as stream:
            for raw in stream:
                columns = raw.decode("utf-8").rstrip("\r\n").split(",")
                if len(columns) != 12:
                    raise ValueError("Binance kline row must have 12 columns")
                bars.append(
                    MinuteBar(
                        _second(columns[0]),
                        D(columns[1]),
                        D(columns[2]),
                        D(columns[3]),
                        D(columns[4]),
                        D(columns[5]),
                        D(columns[7]),
                        int(columns[8]),
                    )
                )
    if len(bars) != 1440:
        raise ValueError(f"{path.name} has {len(bars)} rows; expected 1440")
    expected_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    if bars[0].timestamp != expected_start:
        raise ValueError(f"{path.name} does not begin at UTC midnight")
    if any(right.timestamp - left.timestamp != 60 for left, right in zip(bars, bars[1:])):
        raise ValueError(f"{path.name} contains a missing or duplicate minute")
    return f"{base}-USDT", day, tuple(bars)


def load_archives(
    paths: Iterable[str | Path],
    *,
    required_days: int = 3,
    minimum_assets: int = 51,
    protocol_id: str = "spectra-50-unique-altcoin-fleet-v1",
) -> KlineDataset:
    """Load a complete synchronized universe without forward filling.

    Every retained asset must have every minute of the same consecutive UTC
    dates.  An incomplete asset or date fails the entire import so a fleet
    shortfall cannot be hidden by silently shrinking the requested universe.
    """

    grouped: dict[str, dict[date, tuple[MinuteBar, ...]]] = {}
    sources = []
    for raw_path in sorted((Path(path) for path in paths), key=lambda item: item.name):
        pair, day, bars = _read_archive(raw_path)
        if day in grouped.setdefault(pair, {}):
            raise ValueError(f"duplicate archive for {pair} {day}")
        grouped[pair][day] = bars
        sources.append(
            {"file": raw_path.name, "sha256": _sha256(raw_path), "bytes": raw_path.stat().st_size}
        )
    if len(grouped) < minimum_assets:
        raise ValueError(f"need at least {minimum_assets} distinct altcoin assets")
    day_sets = {tuple(sorted(values)) for values in grouped.values()}
    if len(day_sets) != 1:
        raise ValueError("every asset must have the exact same dates")
    days = next(iter(day_sets))
    if len(days) != required_days:
        raise ValueError(f"need exactly {required_days} complete UTC days")
    if any((right - left).days != 1 for left, right in zip(days, days[1:])):
        raise ValueError("dataset dates must be consecutive")

    series = {
        pair: tuple(bar for day in days for bar in daily[day])
        for pair, daily in sorted(grouped.items())
    }
    reference = tuple(bar.timestamp for bar in next(iter(series.values())))
    for pair, bars in series.items():
        if tuple(bar.timestamp for bar in bars) != reference:
            raise ValueError(f"{pair} is not synchronized to the common minute grid")
    source_fingerprint = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema": 1,
        "kind": "recorded_ohlcv_with_estimated_execution",
        "venue": "binance_spot_public_data",
        "protocol_id": protocol_id,
        "source": "Official Binance Public Data daily spot 1m kline ZIP archives",
        "source_url": "https://data.binance.vision/",
        "pairs": tuple(sorted(series)),
        "asset_count": len(series),
        "days": tuple(day.isoformat() for day in days),
        "frames": len(reference),
        "rows": len(reference) * len(series),
        "start_timestamp": reference[0],
        "end_timestamp": reference[-1] + 60,
        "interval_seconds": 60,
        "complete_synchronized_grid": True,
        "forward_fill": False,
        "source_fingerprint": source_fingerprint,
        "raw_sources": tuple(sources),
        "recorded_fields": (
            "open",
            "high",
            "low",
            "close",
            "base_volume",
            "quote_volume",
            "trade_count",
        ),
        "not_recorded": ("bid", "ask", "spread", "order_book_depth", "queue_position"),
        "limitations": (
            "OHLCV bars do not prove executable bid/ask prices or book depth",
            "execution prices and participation fills are explicit estimates",
            "historical venue filters and account fee tier are not present in these archives",
        ),
    }
    return KlineDataset(series, manifest)


def write_manifest(dataset: KlineDataset, path: str | Path) -> None:
    Path(path).write_text(json.dumps(dataset.manifest, indent=2) + "\n")
