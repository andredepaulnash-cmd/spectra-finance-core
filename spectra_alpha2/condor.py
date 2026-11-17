"""Deterministic Condor market ranking; recommendation-only, never execution."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .market_data import MarketCandidate


@dataclass(frozen=True, slots=True)
class MarketRecommendation:
    trading_pair: str
    score: float
    quote_volume_24h: float
    spread_fraction: float
    rationale: tuple[str, ...]


class CondorAnalyzer:
    """Ranks already-screened scanner candidates using explainable liquidity rules."""

    def recommend(self, candidates: list[MarketCandidate], *, limit: int = 74) -> list[MarketRecommendation]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        ranked=[]
        for candidate in candidates:
            volume=float(candidate.quote_volume_24h)
            spread=float(candidate.spread_fraction)
            if volume <= 0 or spread < 0:
                continue
            liquidity_score=min(math.log10(volume)/8.0,1.0)
            spread_score=max(0.0,1.0-spread/0.003)
            score=0.65*liquidity_score+0.35*spread_score
            rationale=(
                f"24h quote volume {volume:,.0f} USDT",
                f"top-of-book spread {spread*100:.3f}%",
                "USDT spot market with limit-order support",
            )
            ranked.append(MarketRecommendation(
                trading_pair=candidate.trading_pair,
                score=score,
                quote_volume_24h=volume,
                spread_fraction=spread,
                rationale=rationale,
            ))
        ranked.sort(key=lambda item:(item.score,item.quote_volume_24h,item.trading_pair),reverse=True)
        return ranked[:limit]
