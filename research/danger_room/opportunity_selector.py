"""Causal research ranking of recurring price opportunities, not realized profit.

Only completed observations may be admitted. A counted opportunity is a
non-overlapping close-to-close recovery from an observed trough whose movement
exceeds declared modeled costs and a net target. It is a screening proxy: OHLC
does not establish a resting order's fill, its capacity, or an obtainable profit.
No production policy, recovery ladder, or owned position is changed here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math
from typing import Iterable, Mapping, Sequence

import numpy as np


DAY_SECONDS = 86400
BAR_SECONDS = 300
BARS_PER_DAY = DAY_SECONDS // BAR_SECONDS
EXCLUDED_SPOT_ASSETS = frozenset({"BTC", "XBT", "ETH"})


@dataclass(frozen=True)
class SelectorConfig:
    """Fixed research assumptions; declare them before inspecting outcomes."""

    history_days: int = 28
    minimum_history_days: int = 7
    minimum_daily_coverage: float = 0.95
    minimum_recent_coverage: float = 0.95
    minimum_opportunity_days_fraction: float = 0.5
    minimum_daily_dollar_volume: float = 1000.0
    minimum_daily_trades: float = 25.0
    net_target_bps: float = 174.0
    fee_bps_per_side: float = 40.0
    slippage_bps_per_side: float = 5.0
    maximum_recovery_days: int = 7

    def __post_init__(self) -> None:
        for name in ("history_days", "minimum_history_days", "maximum_recovery_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.minimum_history_days > self.history_days:
            raise ValueError("minimum_history_days exceeds history_days")
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        for name in ("minimum_daily_coverage", "minimum_recent_coverage"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if not 0 <= self.minimum_opportunity_days_fraction <= 1:
            raise ValueError("minimum_opportunity_days_fraction must be in [0, 1]")
        for name in ("minimum_daily_dollar_volume", "minimum_daily_trades",
                     "net_target_bps", "fee_bps_per_side", "slippage_bps_per_side"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if max(self.fee_bps_per_side, self.slippage_bps_per_side) >= 10000:
            raise ValueError("sell-side modeled costs must be below proceeds")

    @property
    def gross_recovery_ratio(self) -> float:
        """Exact ratio under buy/sell ad valorem research cost assumptions."""
        fee = self.fee_bps_per_side / 10000
        slip = self.slippage_bps_per_side / 10000
        return ((1 + self.net_target_bps / 10000) * (1 + slip) * (1 + fee)
                / ((1 - slip) * (1 - fee)))


def _asset(pair: str) -> str:
    if "-" in pair:
        return pair.split("-", 1)[0].upper()
    if "/" in pair:
        return pair.split("/", 1)[0].upper()
    return pair[:-3].upper() if pair.upper().endswith("USD") else pair.upper()


class OpportunitySelector:
    """Streaming 5-minute feature accumulator with completed-day ranking.

    ``bars`` has columns open, high, low, close, base volume, trade count.
    ``completed_at`` is the bar close time, never its open time. Missing asset
    rows stay missing; a time gap clears all cross-gap recovery episodes. Rank
    is a recommendation for new admission; held assets are excluded from the
    returned list and no liquidation, repatching, or list mutation occurs.
    """

    def __init__(self, pairs: Sequence[str], config: SelectorConfig | None = None,
                 dataset_categories: Mapping[str, str] | None = None,
                 ladder_categories: Mapping[str, str] | None = None) -> None:
        self.pairs = tuple(pairs)
        if not self.pairs or len(set(self.pairs)) != len(self.pairs):
            raise ValueError("pairs must be nonempty and unique")
        if any(not isinstance(pair, str) or not pair for pair in self.pairs):
            raise ValueError("pairs must be nonempty strings")
        self.config = config or SelectorConfig()
        self.dataset_categories = dict(dataset_categories or {})
        self.ladder_categories = dict(ladder_categories or {})
        unknown = (set(self.dataset_categories) | set(self.ladder_categories)) - set(self.pairs)
        if unknown:
            raise ValueError("category maps must be restricted to the declared universe")
        self._allowed = np.array([
            _asset(pair) not in EXCLUDED_SPOT_ASSETS
            and self.dataset_categories.get(pair, "crypto_candidate") == "crypto_candidate"
            for pair in self.pairs
        ], dtype=bool)
        self._n = len(self.pairs)
        self._history: deque[dict[str, np.ndarray | int]] = deque(maxlen=self.config.history_days)
        self._completed_at: int | None = None
        self._day: int | None = None
        self._latest_valid = np.zeros(self._n, dtype=bool)
        self._trough = np.full(self._n, np.nan)
        self._trough_at = np.zeros(self._n, dtype=np.int64)
        self._rank_cache_key = None
        self._rank_cache_diagnostics: dict[str, dict] = {}
        self._rank_cache_ordered: list[str] = []
        self._reset_day()

    def _reset_day(self) -> None:
        self._count = np.zeros(self._n, dtype=np.int64)
        self._cycles = np.zeros(self._n, dtype=np.int64)
        self._cycle_gain = np.zeros(self._n)
        self._cycle_net_gain = np.zeros(self._n)
        self._recovery_seconds = np.zeros(self._n)
        self._dollar_volume = np.zeros(self._n)
        self._trades = np.zeros(self._n)
        self._first = np.full(self._n, np.nan)
        self._last = np.full(self._n, np.nan)
        self._peak = np.full(self._n, np.nan)
        self._lowest_close = np.full(self._n, np.nan)
        self._drawdown = np.zeros(self._n)

    def _finish_day(self, day: int) -> None:
        self._history.append({
            "day": day, "count": self._count.copy(), "cycles": self._cycles.copy(),
            "cycle_gain": self._cycle_gain.copy(),
            "cycle_net_gain": self._cycle_net_gain.copy(),
            "recovery_seconds": self._recovery_seconds.copy(),
            "dollar_volume": self._dollar_volume.copy(), "trades": self._trades.copy(),
            "first": self._first.copy(), "last": self._last.copy(),
            "peak": self._peak.copy(), "lowest_close": self._lowest_close.copy(),
            "drawdown": self._drawdown.copy(),
        })
        self._reset_day()

    def update(self, completed_at: int, bars: np.ndarray) -> None:
        if (isinstance(completed_at, bool) or not isinstance(completed_at, (int, np.integer))
                or completed_at % BAR_SECONDS):
            raise ValueError("completed_at must be an aligned integer 5-minute close time")
        completed_at = int(completed_at)
        if self._completed_at is not None and completed_at <= self._completed_at:
            raise ValueError("completed observations must be strictly chronological")
        values = np.asarray(bars, dtype=float)
        if values.shape != (self._n, 6):
            raise ValueError("bars must have shape (number of pairs, 6)")
        day = (completed_at - 1) // DAY_SECONDS
        if self._day is None:
            self._day = day
        if day < self._day:
            raise ValueError("completed day moved backwards")
        # Calendar gaps are explicit zero-coverage days, never forward filled.
        while self._day < day:
            self._finish_day(self._day)
            self._day += 1
        if self._completed_at is not None and completed_at != self._completed_at + BAR_SECONDS:
            self._trough.fill(np.nan)
        self._completed_at = completed_at
        o, h, low, close, volume, count = values.T
        valid = (np.isfinite(values).all(axis=1) & (o > 0) & (low > 0)
                 & (h >= np.maximum(o, close)) & (low <= np.minimum(o, close))
                 & (close > 0) & (volume > 0) & (count > 0)
                 & (count == np.floor(count)))
        self._latest_valid = valid
        self._trough[~valid] = np.nan
        first_seen = valid & np.isnan(self._first)
        self._first[first_seen] = close[first_seen]
        self._last[valid] = close[valid]
        self._count += valid
        self._dollar_volume[valid] += volume[valid] * close[valid]
        self._trades[valid] += count[valid]
        self._peak[valid] = np.fmax(self._peak[valid], close[valid])
        self._lowest_close[valid] = np.fmin(self._lowest_close[valid], close[valid])
        self._drawdown[valid] = np.maximum(
            self._drawdown[valid], 1 - close[valid] / self._peak[valid])
        new_trough = valid & (np.isnan(self._trough) | (close < self._trough)
                             | (completed_at - self._trough_at
                                > self.config.maximum_recovery_days * DAY_SECONDS))
        self._trough[new_trough] = close[new_trough]
        self._trough_at[new_trough] = completed_at
        recovered = valid & ~new_trough & (close >= self._trough * self.config.gross_recovery_ratio)
        self._cycles += recovered
        self._cycle_gain[recovered] += close[recovered] / self._trough[recovered] - 1
        fee = self.config.fee_bps_per_side / 10000
        slip = self.config.slippage_bps_per_side / 10000
        self._cycle_net_gain[recovered] += (close[recovered] / self._trough[recovered]
                                         * (1 - slip) * (1 - fee)
                                         / ((1 + slip) * (1 + fee)) - 1)
        self._recovery_seconds[recovered] += completed_at - self._trough_at[recovered]
        # Reset at the recovery close so no observation contributes to two
        # overlapping cycles. High/low cannot create or complete an episode.
        self._trough[recovered] = close[recovered]
        self._trough_at[recovered] = completed_at
        if completed_at % DAY_SECONDS == 0:
            self._finish_day(self._day)
            self._day += 1

    def rank(self, timestamp: int, held_pairs: Iterable[str] = (),
             limit: int | None = None) -> tuple[list[str], dict[str, dict]]:
        """Rank new admissions without touching held assets or their owners."""
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, np.integer))
                or timestamp % BAR_SECONDS):
            raise ValueError("decision timestamp must be an aligned integer")
        if self._completed_at is not None and timestamp < self._completed_at:
            raise ValueError("ranking before consumed evidence would leak future information")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a nonnegative integer")
        held = set(held_pairs)
        if held - set(self.pairs):
            raise ValueError("held pairs must belong to the declared universe")
        diagnostics: dict[str, dict] = {}
        if not self._history:
            for i, pair in enumerate(self.pairs):
                diagnostics[pair] = self._diagnostic_base(i, timestamp)
                diagnostics[pair].update(eligible=False, reason="insufficient_completed_history", score=0.0)
            return [], diagnostics
        history = list(self._history)
        cache_key = (len(history), int(history[-1]["day"]), self._latest_valid.tobytes(),
                     self._completed_at is not None and int(timestamp) == self._completed_at)
        if cache_key == self._rank_cache_key:
            return self._rank_result(self._rank_cache_ordered, self._rank_cache_diagnostics,
                                     int(timestamp), held, limit)
        count = np.stack([day["count"] for day in history])
        cycles = np.stack([day["cycles"] for day in history])
        sufficient = count >= math.ceil(BARS_PER_DAY * self.config.minimum_daily_coverage)
        recent = min(7, len(history))
        observed_days = sufficient.sum(axis=0)
        coverage = count[-recent:].sum(axis=0) / (BARS_PER_DAY * recent)
        dollar_volume = np.stack([day["dollar_volume"] for day in history])[-recent:].mean(axis=0)
        trades = np.stack([day["trades"] for day in history])[-recent:].mean(axis=0)
        recent_at = self._completed_at is not None and int(timestamp) == self._completed_at
        eligible = (self._allowed & self._latest_valid & recent_at
                    & (observed_days >= self.config.minimum_history_days)
                    & (coverage >= self.config.minimum_recent_coverage)
                    & (dollar_volume >= self.config.minimum_daily_dollar_volume)
                    & (trades >= self.config.minimum_daily_trades))
        first = np.stack([day["first"] for day in history])
        last = np.stack([day["last"] for day in history])
        signed_return = np.zeros(self._n)
        for i in range(self._n):
            usable = np.flatnonzero(sufficient[:, i] & np.isfinite(first[:, i]) & np.isfinite(last[:, i]))
            if len(usable):
                signed_return[i] = last[usable[-1], i] / first[usable[0], i] - 1
        median_return = float(np.median(signed_return[eligible])) if eligible.any() else 0.0
        relative_strength = signed_return - median_return
        for i, pair in enumerate(self.pairs):
            diagnostic = self._diagnostic_base(i, timestamp)
            n_days = int(observed_days[i])
            active = (cycles[:, i] > 0) & sufficient[:, i]
            recurrence = float(active.sum() / max(n_days, 1))
            # Recent quiet days count against the score, including missing days.
            recent_recurrence = float(active[-recent:].sum() / recent)
            has_recurring_opportunity = (recurrence >= self.config.minimum_opportunity_days_fraction
                                         and recent_recurrence > 0)
            eligible[i] &= has_recurring_opportunity
            eligible_cycles = int(cycles[sufficient[:, i], i].sum())
            recovery_seconds = sum(float(day["recovery_seconds"][i]) for day, ok in zip(history, sufficient[:, i]) if ok)
            recovery_days = recovery_seconds / max(eligible_cycles, 1) / DAY_SECONDS
            drawdown = max(float(day["drawdown"][i]) for day in history)
            prior_peak = float("nan")
            for day in history:
                lowest = float(day["lowest_close"][i])
                if math.isfinite(prior_peak) and math.isfinite(lowest):
                    drawdown = max(drawdown, 1 - lowest / prior_peak)
                daily_peak = float(day["peak"][i])
                if math.isfinite(daily_peak):
                    prior_peak = max(prior_peak, daily_peak) if math.isfinite(prior_peak) else daily_peak
            active_idx = np.flatnonzero(active)
            idle_days = len(history) - 1 - int(active_idx[-1]) if len(active_idx) else len(history)
            strength_factor = 1 + float(np.clip(relative_strength[i], -0.5, 0.5))
            score = (recurrence * recent_recurrence
                     * (1 + min(eligible_cycles / max(n_days, 1), 4) / 4)
                     * strength_factor
                     / (1 + drawdown * 4 + recovery_days + idle_days / 7))
            if not self._allowed[i]:
                reason = "excluded_spot_asset_or_dataset_category"
            elif not recent_at or not self._latest_valid[i]:
                reason = "missing_or_stale_latest_bar"
            elif n_days < self.config.minimum_history_days:
                reason = "insufficient_completed_history"
            elif coverage[i] < self.config.minimum_recent_coverage:
                reason = "insufficient_recent_coverage"
            elif dollar_volume[i] < self.config.minimum_daily_dollar_volume or trades[i] < self.config.minimum_daily_trades:
                reason = "insufficient_observed_liquidity"
            elif not has_recurring_opportunity:
                reason = "insufficient_recurring_opportunity"
            else:
                reason = "eligible" if pair not in held else "held_asset_new_admission_excluded"
            diagnostic.update(
                eligible=bool(eligible[i]), reason=reason, score=float(score),
                observed_completed_days=n_days, completed_days_in_window=len(history),
                opportunity_days_fraction=recurrence,
                recent_opportunity_days_fraction=recent_recurrence,
                nonoverlapping_recovery_count=eligible_cycles,
                mean_recovery_days=float(recovery_days), days_since_opportunity=idle_days,
                mean_modeled_net_recovery_fraction=sum(float(day["cycle_net_gain"][i]) for day, ok in zip(history, sufficient[:, i]) if ok) / max(eligible_cycles, 1),
                observed_close_drawdown_fraction=float(drawdown),
                signed_return_fraction=float(signed_return[i]),
                relative_strength_fraction=float(relative_strength[i]),
                recent_coverage=float(coverage[i]),
                mean_daily_dollar_volume=float(dollar_volume[i]),
                mean_daily_trade_count=float(trades[i]),
                held_asset=pair in held,
            )
            diagnostics[pair] = diagnostic
        ordered = sorted((pair for i, pair in enumerate(self.pairs) if eligible[i]),
                         key=lambda pair: (-diagnostics[pair]["score"], pair))
        # Cache completed-day calculations, not the caller's holdings or time.
        # A changed latest validity mask invalidates the cache too.
        for diagnostic in diagnostics.values():
            diagnostic["held_asset"] = False
            if diagnostic["eligible"]:
                diagnostic["reason"] = "eligible"
        self._rank_cache_key = cache_key
        self._rank_cache_diagnostics = diagnostics
        self._rank_cache_ordered = ordered
        return self._rank_result(ordered, diagnostics, int(timestamp), held, limit)

    def _rank_result(self, ordered: list[str], diagnostics: dict[str, dict], timestamp: int,
                     held: set[str], limit: int | None) -> tuple[list[str], dict[str, dict]]:
        copied = {}
        for pair, original in diagnostics.items():
            diagnostic = dict(original)
            diagnostic["evidence_cutoff"] = self._completed_at
            diagnostic["decision_timestamp"] = timestamp
            diagnostic["held_asset"] = pair in held
            if pair in held and diagnostic["eligible"]:
                diagnostic["reason"] = "held_asset_new_admission_excluded"
            copied[pair] = diagnostic
        candidates = [pair for pair in ordered if pair not in held]
        return candidates if limit is None else candidates[:limit], copied

    def _diagnostic_base(self, i: int, timestamp: int) -> dict:
        return {
            "evidence_cutoff": self._completed_at,
            "decision_timestamp": int(timestamp),
            "completed_day_feature_cutoff": (int(self._history[-1]["day"]) + 1) * DAY_SECONDS if self._history else None,
            "dataset_category": self.dataset_categories.get(self.pairs[i], "crypto_candidate"),
            "ladder_category": self.ladder_categories.get(self.pairs[i], "UNKNOWN"),
            "gross_recovery_ratio": self.config.gross_recovery_ratio,
            "screening_proxy_only": True,
            "actual_order_fill_proven": False,
        }


class MatrixOpportunitySelector:
    """Incremental adapter: decision at open index i consumes rows strictly < i."""

    def __init__(self, rows: np.ndarray, pairs: Sequence[str], start_timestamp: int,
                 config: SelectorConfig | None = None,
                 dataset_categories: Mapping[str, str] | None = None,
                 ladder_categories: Mapping[str, str] | None = None) -> None:
        if (isinstance(start_timestamp, bool) or not isinstance(start_timestamp, (int, np.integer))
                or start_timestamp % BAR_SECONDS):
            raise ValueError("start_timestamp must be an aligned integer")
        if rows.ndim != 3 or rows.shape[1:] != (len(pairs), 6):
            raise ValueError("rows must have shape (frames, pairs, 6)")
        self.rows = rows
        self.start_timestamp = int(start_timestamp)
        self.selector = OpportunitySelector(pairs, config, dataset_categories, ladder_categories)
        self._index = 0

    def rank_at_index(self, index: int, held_pairs: Iterable[str] = (),
                      limit: int | None = None) -> tuple[list[str], dict[str, dict]]:
        if (isinstance(index, bool) or not isinstance(index, (int, np.integer))
                or not self._index <= index <= len(self.rows)):
            raise ValueError("index must be an in-range monotonically increasing integer")
        while self._index < index:
            self.selector.update(self.start_timestamp + (self._index + 1) * BAR_SECONDS,
                                 self.rows[self._index])
            self._index += 1
        return self.selector.rank(self.start_timestamp + int(index) * BAR_SECONDS, held_pairs, limit)
