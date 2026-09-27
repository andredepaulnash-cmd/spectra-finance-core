from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import exp, isfinite, log, sqrt
from typing import Any, Mapping

from spectra_core.kraken_market_fabric import KrakenUniverseSnapshot


LN2 = log(2.0)
BPS = 10_000.0
FINGERPRINT_SCHEMA_VERSION = 1


class FingerprintHorizon(StrEnum):
    """Behavioral memory horizons.

    These are *decay horizons*, not candle intervals.  Spectra can feed the same
    observation stream into every horizon and let each memory decay at its own
    speed.
    """

    FAST = "FAST"
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


HORIZON_HALF_LIFE_SECONDS: dict[FingerprintHorizon, float] = {
    FingerprintHorizon.FAST: 5 * 60.0,
    FingerprintHorizon.SHORT: 60 * 60.0,
    FingerprintHorizon.MEDIUM: 24 * 60 * 60.0,
    FingerprintHorizon.LONG: 7 * 24 * 60 * 60.0,
}


def _finite_float(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc

    if not isfinite(result):
        raise ValueError(f"{field} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    if non_negative and result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _optional_finite(
    value: Any,
    field: str,
    *,
    non_negative: bool = False,
    lower: float | None = None,
    upper: float | None = None,
) -> float | None:
    if value is None:
        return None
    result = _finite_float(value, field, non_negative=non_negative)
    if lower is not None and result < lower:
        raise ValueError(f"{field} must be >= {lower}")
    if upper is not None and result > upper:
        raise ValueError(f"{field} must be <= {upper}")
    return result


def _ema(previous: float | None, value: float | None, alpha: float) -> float | None:
    if value is None:
        return previous
    if previous is None:
        return value
    return previous + alpha * (value - previous)


def _sign(value: float | None) -> float:
    if value is None or value == 0:
        return 0.0
    return 1.0 if value > 0 else -1.0


@dataclass(frozen=True)
class AssetObservation:
    """One normalized point in an asset's behavioral history.

    Statistical features intentionally use floats.  They are descriptive inputs
    for ranking/learning, not capital-accounting values.  Exact money authority
    remains in Spectra Core's Decimal-based Capital Governor.
    """

    trading_pair: str
    timestamp: float
    price: float
    quote_volume_24h: float
    spread_bps: float | None = None
    depth_quote: float | None = None
    depth_imbalance: float | None = None
    microprice_edge_bps: float | None = None

    def __post_init__(self) -> None:
        pair = self.trading_pair.strip().upper()
        if "-" not in pair:
            raise ValueError("trading_pair must be BASE-QUOTE")
        object.__setattr__(self, "trading_pair", pair)
        object.__setattr__(
            self,
            "timestamp",
            _finite_float(self.timestamp, "timestamp", positive=True),
        )
        object.__setattr__(
            self,
            "price",
            _finite_float(self.price, "price", positive=True),
        )
        object.__setattr__(
            self,
            "quote_volume_24h",
            _finite_float(
                self.quote_volume_24h,
                "quote_volume_24h",
                non_negative=True,
            ),
        )
        object.__setattr__(
            self,
            "spread_bps",
            _optional_finite(
                self.spread_bps,
                "spread_bps",
                non_negative=True,
            ),
        )
        object.__setattr__(
            self,
            "depth_quote",
            _optional_finite(
                self.depth_quote,
                "depth_quote",
                non_negative=True,
            ),
        )
        object.__setattr__(
            self,
            "depth_imbalance",
            _optional_finite(
                self.depth_imbalance,
                "depth_imbalance",
                lower=-1.0,
                upper=1.0,
            ),
        )
        object.__setattr__(
            self,
            "microprice_edge_bps",
            _optional_finite(
                self.microprice_edge_bps,
                "microprice_edge_bps",
            ),
        )


@dataclass(frozen=True)
class BehaviorWindow:
    horizon: FingerprintHorizon
    half_life_seconds: float
    observations: int = 0
    effective_samples: float = 0.0
    first_timestamp: float | None = None
    last_timestamp: float | None = None

    ema_return_bps: float | None = None
    ema_abs_return_bps: float | None = None
    ema_squared_return_bps2: float | None = None
    direction_persistence: float | None = None

    ema_quote_volume_24h: float | None = None
    ema_spread_bps: float | None = None
    ema_depth_quote: float | None = None
    ema_depth_imbalance: float | None = None
    ema_microprice_edge_bps: float | None = None

    @property
    def realized_volatility_bps(self) -> float | None:
        if self.ema_squared_return_bps2 is None:
            return None
        mean = self.ema_return_bps or 0.0
        variance = max(0.0, self.ema_squared_return_bps2 - mean * mean)
        return sqrt(variance)

    @property
    def momentum_persistence(self) -> float | None:
        """Positive directional persistence proxy in [0, 1].

        This is deliberately a descriptive proxy, not a trade score.
        """
        if self.direction_persistence is None:
            return None
        return max(0.0, min(1.0, self.direction_persistence))

    @property
    def reversal_tendency(self) -> float | None:
        """Positive serial-reversal proxy in [0, 1].

        Values near one mean adjacent observed returns have tended to flip sign.
        It is *not* proof of profitable mean reversion after costs.
        """
        if self.direction_persistence is None:
            return None
        return max(0.0, min(1.0, -self.direction_persistence))

    def confidence(self, *, now: float) -> float:
        """Coverage × sample density × freshness, bounded to [0, 1]."""
        now = _finite_float(now, "now", positive=True)
        if (
            self.observations < 2
            or self.first_timestamp is None
            or self.last_timestamp is None
        ):
            return 0.0

        age = max(0.0, now - self.last_timestamp)
        freshness = exp(-LN2 * age / self.half_life_seconds)

        coverage = max(0.0, self.last_timestamp - self.first_timestamp)
        coverage_score = min(1.0, coverage / (2.0 * self.half_life_seconds))

        # Twelve effective observations is enough to stop treating a horizon as
        # pure cold-start, but temporal coverage is still required above.
        sample_score = min(1.0, self.effective_samples / 12.0)

        return max(
            0.0,
            min(1.0, freshness * sqrt(coverage_score * sample_score)),
        )

    def is_stale(self, *, now: float, max_half_lives: float = 3.0) -> bool:
        now = _finite_float(now, "now", positive=True)
        max_half_lives = _finite_float(
            max_half_lives,
            "max_half_lives",
            positive=True,
        )
        if self.last_timestamp is None:
            return True
        return now - self.last_timestamp > self.half_life_seconds * max_half_lives

    def update(
        self,
        observation: AssetObservation,
        *,
        return_bps: float | None,
        previous_return_bps: float | None,
    ) -> BehaviorWindow:
        if self.last_timestamp is not None and observation.timestamp <= self.last_timestamp:
            raise ValueError(
                f"{self.horizon} observation timestamp must increase: "
                f"{observation.timestamp} <= {self.last_timestamp}"
            )

        if self.last_timestamp is None:
            decay = 0.0
            alpha = 1.0
        else:
            elapsed = observation.timestamp - self.last_timestamp
            decay = exp(-LN2 * elapsed / self.half_life_seconds)
            alpha = 1.0 - decay

        directional_sample: float | None = None
        if return_bps is not None and previous_return_bps is not None:
            directional_sample = _sign(return_bps) * _sign(previous_return_bps)

        squared_return = return_bps * return_bps if return_bps is not None else None
        abs_return = abs(return_bps) if return_bps is not None else None

        return replace(
            self,
            observations=self.observations + 1,
            effective_samples=self.effective_samples * decay + 1.0,
            first_timestamp=(
                observation.timestamp
                if self.first_timestamp is None
                else self.first_timestamp
            ),
            last_timestamp=observation.timestamp,
            ema_return_bps=_ema(self.ema_return_bps, return_bps, alpha),
            ema_abs_return_bps=_ema(
                self.ema_abs_return_bps,
                abs_return,
                alpha,
            ),
            ema_squared_return_bps2=_ema(
                self.ema_squared_return_bps2,
                squared_return,
                alpha,
            ),
            direction_persistence=_ema(
                self.direction_persistence,
                directional_sample,
                alpha,
            ),
            ema_quote_volume_24h=_ema(
                self.ema_quote_volume_24h,
                observation.quote_volume_24h,
                alpha,
            ),
            ema_spread_bps=_ema(
                self.ema_spread_bps,
                observation.spread_bps,
                alpha,
            ),
            ema_depth_quote=_ema(
                self.ema_depth_quote,
                observation.depth_quote,
                alpha,
            ),
            ema_depth_imbalance=_ema(
                self.ema_depth_imbalance,
                observation.depth_imbalance,
                alpha,
            ),
            ema_microprice_edge_bps=_ema(
                self.ema_microprice_edge_bps,
                observation.microprice_edge_bps,
                alpha,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "half_life_seconds": self.half_life_seconds,
            "observations": self.observations,
            "effective_samples": self.effective_samples,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "ema_return_bps": self.ema_return_bps,
            "ema_abs_return_bps": self.ema_abs_return_bps,
            "ema_squared_return_bps2": self.ema_squared_return_bps2,
            "direction_persistence": self.direction_persistence,
            "ema_quote_volume_24h": self.ema_quote_volume_24h,
            "ema_spread_bps": self.ema_spread_bps,
            "ema_depth_quote": self.ema_depth_quote,
            "ema_depth_imbalance": self.ema_depth_imbalance,
            "ema_microprice_edge_bps": self.ema_microprice_edge_bps,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BehaviorWindow:
        horizon = FingerprintHorizon(str(raw["horizon"]))
        configured_half_life = HORIZON_HALF_LIFE_SECONDS[horizon]
        half_life = _finite_float(
            raw["half_life_seconds"],
            "half_life_seconds",
            positive=True,
        )
        if half_life != configured_half_life:
            raise ValueError(
                f"{horizon} half-life {half_life} does not match "
                f"configured {configured_half_life}"
            )

        return cls(
            horizon=horizon,
            half_life_seconds=half_life,
            observations=int(raw.get("observations", 0)),
            effective_samples=_finite_float(
                raw.get("effective_samples", 0.0),
                "effective_samples",
                non_negative=True,
            ),
            first_timestamp=_optional_finite(
                raw.get("first_timestamp"),
                "first_timestamp",
                lower=0.0,
            ),
            last_timestamp=_optional_finite(
                raw.get("last_timestamp"),
                "last_timestamp",
                lower=0.0,
            ),
            ema_return_bps=_optional_finite(
                raw.get("ema_return_bps"),
                "ema_return_bps",
            ),
            ema_abs_return_bps=_optional_finite(
                raw.get("ema_abs_return_bps"),
                "ema_abs_return_bps",
                non_negative=True,
            ),
            ema_squared_return_bps2=_optional_finite(
                raw.get("ema_squared_return_bps2"),
                "ema_squared_return_bps2",
                non_negative=True,
            ),
            direction_persistence=_optional_finite(
                raw.get("direction_persistence"),
                "direction_persistence",
                lower=-1.0,
                upper=1.0,
            ),
            ema_quote_volume_24h=_optional_finite(
                raw.get("ema_quote_volume_24h"),
                "ema_quote_volume_24h",
                non_negative=True,
            ),
            ema_spread_bps=_optional_finite(
                raw.get("ema_spread_bps"),
                "ema_spread_bps",
                non_negative=True,
            ),
            ema_depth_quote=_optional_finite(
                raw.get("ema_depth_quote"),
                "ema_depth_quote",
                non_negative=True,
            ),
            ema_depth_imbalance=_optional_finite(
                raw.get("ema_depth_imbalance"),
                "ema_depth_imbalance",
                lower=-1.0,
                upper=1.0,
            ),
            ema_microprice_edge_bps=_optional_finite(
                raw.get("ema_microprice_edge_bps"),
                "ema_microprice_edge_bps",
            ),
        )


@dataclass(frozen=True)
class AssetFingerprint:
    trading_pair: str
    first_seen_at: float
    last_seen_at: float
    last_price: float
    observation_count: int
    last_return_bps: float | None
    windows: dict[FingerprintHorizon, BehaviorWindow]

    def __post_init__(self) -> None:
        pair = self.trading_pair.strip().upper()
        if "-" not in pair:
            raise ValueError("trading_pair must be BASE-QUOTE")
        object.__setattr__(self, "trading_pair", pair)

        if set(self.windows) != set(FingerprintHorizon):
            raise ValueError("fingerprint must contain every configured horizon")

    @classmethod
    def cold_start(cls, observation: AssetObservation) -> AssetFingerprint:
        windows = {
            horizon: BehaviorWindow(
                horizon=horizon,
                half_life_seconds=HORIZON_HALF_LIFE_SECONDS[horizon],
            ).update(
                observation,
                return_bps=None,
                previous_return_bps=None,
            )
            for horizon in FingerprintHorizon
        }
        return cls(
            trading_pair=observation.trading_pair,
            first_seen_at=observation.timestamp,
            last_seen_at=observation.timestamp,
            last_price=observation.price,
            observation_count=1,
            last_return_bps=None,
            windows=windows,
        )

    def update(self, observation: AssetObservation) -> AssetFingerprint:
        if observation.trading_pair != self.trading_pair:
            raise ValueError(
                f"observation pair {observation.trading_pair} does not match "
                f"fingerprint {self.trading_pair}"
            )
        if observation.timestamp <= self.last_seen_at:
            raise ValueError(
                f"observation timestamp must increase: "
                f"{observation.timestamp} <= {self.last_seen_at}"
            )

        return_bps = (observation.price / self.last_price - 1.0) * BPS

        windows = {
            horizon: window.update(
                observation,
                return_bps=return_bps,
                previous_return_bps=self.last_return_bps,
            )
            for horizon, window in self.windows.items()
        }

        return AssetFingerprint(
            trading_pair=self.trading_pair,
            first_seen_at=self.first_seen_at,
            last_seen_at=observation.timestamp,
            last_price=observation.price,
            observation_count=self.observation_count + 1,
            last_return_bps=return_bps,
            windows=windows,
        )

    def confidence(self, horizon: FingerprintHorizon, *, now: float) -> float:
        return self.windows[horizon].confidence(now=now)

    def cold_start_horizons(
        self,
        *,
        now: float,
        threshold: float = 0.35,
    ) -> tuple[FingerprintHorizon, ...]:
        threshold = _finite_float(threshold, "threshold", non_negative=True)
        if threshold > 1.0:
            raise ValueError("threshold must be <= 1")
        return tuple(
            horizon
            for horizon in FingerprintHorizon
            if self.confidence(horizon, now=now) < threshold
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FINGERPRINT_SCHEMA_VERSION,
            "trading_pair": self.trading_pair,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "last_price": self.last_price,
            "observation_count": self.observation_count,
            "last_return_bps": self.last_return_bps,
            "windows": {
                horizon.value: self.windows[horizon].to_dict()
                for horizon in FingerprintHorizon
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AssetFingerprint:
        if int(raw.get("schema_version", -1)) != FINGERPRINT_SCHEMA_VERSION:
            raise ValueError("unsupported asset fingerprint schema version")

        windows_raw = raw.get("windows")
        if not isinstance(windows_raw, Mapping):
            raise ValueError("fingerprint windows must be a mapping")

        windows: dict[FingerprintHorizon, BehaviorWindow] = {}
        for horizon in FingerprintHorizon:
            row = windows_raw.get(horizon.value)
            if not isinstance(row, Mapping):
                raise ValueError(f"missing fingerprint horizon {horizon.value}")
            window = BehaviorWindow.from_dict(row)
            if window.horizon != horizon:
                raise ValueError(
                    f"window key {horizon.value} contains {window.horizon.value}"
                )
            windows[horizon] = window

        first_seen = _finite_float(
            raw["first_seen_at"],
            "first_seen_at",
            positive=True,
        )
        last_seen = _finite_float(
            raw["last_seen_at"],
            "last_seen_at",
            positive=True,
        )
        if last_seen < first_seen:
            raise ValueError("last_seen_at cannot precede first_seen_at")

        count = int(raw["observation_count"])
        if count < 1:
            raise ValueError("observation_count must be positive")

        return cls(
            trading_pair=str(raw["trading_pair"]),
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            last_price=_finite_float(
                raw["last_price"],
                "last_price",
                positive=True,
            ),
            observation_count=count,
            last_return_bps=_optional_finite(
                raw.get("last_return_bps"),
                "last_return_bps",
            ),
            windows=windows,
        )


def observation_from_kraken_universe(
    snapshot: KrakenUniverseSnapshot,
    trading_pair: str,
) -> AssetObservation:
    """Project the canonical Kraken universe into the fingerprint input shape."""
    pair = trading_pair.strip().upper()
    if pair not in snapshot.eligible_pairs:
        raise ValueError(f"{pair} is not eligible in this Kraken universe snapshot")

    ticker = snapshot.tickers[pair]
    book = snapshot.books.get(pair)

    timestamp_candidates = [
        value
        for value in (
            ticker.timestamp,
            book.timestamp if book is not None else None,
            snapshot.updated_at,
        )
        if value is not None and float(value) > 0
    ]
    if not timestamp_candidates:
        raise ValueError(f"{pair} has no usable market-data timestamp")

    return AssetObservation(
        trading_pair=pair,
        timestamp=max(float(value) for value in timestamp_candidates),
        price=float(ticker.price),
        quote_volume_24h=float(ticker.quote_volume_24h),
        spread_bps=float(book.spread_bps) if book is not None else None,
        depth_quote=(
            float(book.bid_depth_quote + book.ask_depth_quote)
            if book is not None
            else None
        ),
        depth_imbalance=float(book.depth_imbalance) if book is not None else None,
        microprice_edge_bps=(
            float(book.microprice_edge_bps)
            if book is not None
            else None
        ),
    )
