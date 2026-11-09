"""Rank Kraken USD spot markets for mean-reversion candidates.

Market data comes from Spectra's configured Kraken connector through the
Hummingbot API. This routine is research-only; it does not authorize or place
orders.
"""

CATEGORY = "Market Data"

from datetime import datetime, timezone

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from spectra_core.market_discovery import scan_kraken_universe


class Config(BaseModel):
    """Scan Kraken USD spot markets using the Governor-compatible MR ranker."""

    connector: str = Field(default="kraken", description="Market-data connector; Kraken only")
    top_n: int = Field(default=100, description="Top N Kraken markets by 24h USD volume")
    show_n: int = Field(default=25, description="Number of ranked candidates to show")
    min_volume_usd: float = Field(default=10_000_000, description="Minimum 24h USD quote volume")
    min_history_days: int = Field(default=90, description="Completed daily candles required")
    min_median_daily_quote_volume_usd: float = Field(default=1_000_000, description="Minimum median daily USD turnover over 30 completed sessions")
    min_active_day_ratio: float = Field(default=0.8, description="Minimum fraction of the last 30 sessions with at least 0.8% high-low range")
    min_median_daily_range_pct: float = Field(default=0.8, description="Minimum median daily high-low range over 30 sessions")
    max_current_move_pct: float = Field(default=15.0, description="Reject extreme current moves from the last completed daily close")


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    if config.connector.strip().lower() != "kraken":
        return "Spectra mean-reversion discovery is pinned to Kraken market data."

    client = await get_client(context._chat_id, context=context)
    if not client:
        return "No configured Spectra Execution Engine server is available."

    try:
        result = await scan_kraken_universe(
            client,
            top_n=config.top_n,
            show_n=config.show_n,
            min_volume_usd=config.min_volume_usd,
            min_history_days=config.min_history_days,
            min_median_daily_quote_volume_usd=config.min_median_daily_quote_volume_usd,
            min_active_day_ratio=config.min_active_day_ratio,
            min_median_daily_range_pct=config.min_median_daily_range_pct,
            max_current_move_pct=config.max_current_move_pct,
            quote_assets=("USD",),
        )
    except Exception as exc:
        return f"Kraken market discovery failed: {type(exc).__name__}: {exc}"

    lines = [
        "Spectra Mean-Reversion Scanner — Kraken USD spot",
        f"Refreshed: {datetime.fromtimestamp(result['as_of'], timezone.utc).isoformat()}",
        f"Analyzed: {result['counts']['analyzed']} | "
        f"Candidates passing research guardrails: {result['counts']['eligible']}",
        f"Governor approved list loaded: {result['governor_approved_list_loaded']}",
        "Research only — no capital authorization or orders were created.",
        "",
    ]
    for index, item in enumerate(result["candidates"], 1):
        lines.append(
            f"{index}. {item['trading_pair']} — " + f"{item['current_price']:,.8g} | "
            f"24h volume rank #{item['volume_rank_24h']} | "
            f"24h volume " + f"{item['volume_24h_usd']:,.0f} USD | "
            f"Score {item['selection_score']:.1f}/100 | "
            f"{item['discount_to_vwap_pct']:+.1f}% vs 90d VWAP | "
            f"RSI {item['rsi_14d']:.1f}"
        )
        lines.append(
            f"   7d {item['return_7d_pct']:+.1f}% | "
            f"30d {item['return_30d_pct']:+.1f}% | "
            f"active days {item['active_range_days_30d']}/30 | "
            f"median range {item['median_daily_range_30d_pct']:.2f}% | "
            f"median daily USD volume " + f"{item['median_daily_quote_volume_30d_usd']:,.0f} USD"
        )

    if not result["candidates"]:
        lines.append("No Kraken markets passed the configured data and recovery checks.")

    return "\n".join(lines)
