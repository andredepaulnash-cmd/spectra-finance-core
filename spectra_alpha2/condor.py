"""Deterministic Condor market ranking; recommendation-only, never execution."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .market_data import MarketCandidate
from .policy import MAX_OPEN_POSITIONS


@dataclass(frozen=True, slots=True)
class MarketRecommendation:
    trading_pair: str
    score: float
    quote_volume_24h: float
    spread_fraction: float
    change_24h_fraction: float
    rationale: tuple[str, ...]


class CondorAnalyzer:
    """Ranks quality-screened 24h gainers by percentage change, then execution quality."""

    def recommend(self, candidates: list[MarketCandidate], *, limit: int = MAX_OPEN_POSITIONS) -> list[MarketRecommendation]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        ranked = []
        for candidate in candidates:
            volume = float(candidate.quote_volume_24h)
            spread = float(candidate.spread_fraction)
            change = float(candidate.change_24h_fraction)
            if volume <= 0 or spread < 0:
                continue
            liquidity_score = min(math.log10(volume) / 8.0, 1.0)
            spread_score = max(0.0, 1.0 - spread / 0.003)
            # Reward positive momentum up to +5%; extreme gains receive no extra
            # advantage, leaving liquidity and execution quality to decide ties.
            momentum_score = max(0.0, min(change / 0.05, 1.0))
            score = 0.40 * liquidity_score + 0.35 * spread_score + 0.25 * momentum_score
            rationale = (
                f"24h price change {change * 100:+.2f}%",
                f"24h quote volume {volume:,.0f} USD",
                f"top-of-book spread {spread * 100:.3f}%",
                "USD spot market with Kraken public candles and paper execution",
            )
            ranked.append(MarketRecommendation(
                trading_pair=candidate.trading_pair,
                score=score,
                quote_volume_24h=volume,
                spread_fraction=spread,
                change_24h_fraction=change,
                rationale=rationale,
            ))
        # Architect's portfolio-building policy prioritizes the strongest positive
        # 24-hour gainers. Liquidity/spread remain hard scanner gates and score
        # tie-breakers; they do not displace a stronger gainer from the shortlist.
        ranked.sort(
            key=lambda item: (
                item.change_24h_fraction,
                item.score,
                item.quote_volume_24h,
                item.trading_pair,
            ),
            reverse=True,
        )
        return ranked[:limit]
