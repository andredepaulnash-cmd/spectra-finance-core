"""Rank Kraken USD spot markets for mean-reversion candidates.

Market data comes from Spectra's configured Kraken connector through the
Hummingbot API. This routine is research-only; it does not authorize or place
orders.
"""

CATEGORY = "Market Data"

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from spectra_core.market_discovery import scan_kraken_universe


class Config(BaseModel):
    """Scan Kraken USD spot markets using the Governor-compatible MR ranker."""

    connector: str = Field(default="kraken", description="Market-data connector; Kraken only")
    top_n: int = Field(default=80, description="Top N Kraken markets by 24h USD volume")
    show_n: int = Field(default=25, description="Number of ranked candidates to show")
    min_volume_usd: float = Field(default=10_000_000, description="Minimum 24h USD quote volume")
    min_history_days: int = Field(default=90, description="Completed daily candles required")


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
            quote_assets=("USD",),
        )
    except Exception as exc:
        return f"Kraken market discovery failed: {type(exc).__name__}: {exc}"

    lines = [
        "Spectra Mean-Reversion Scanner — Kraken USD spot",
        f"Analyzed: {result['counts']['analyzed']} | "
        f"Candidates passing research guardrails: {result['counts']['eligible']}",
        f"Governor approved list loaded: {result['governor_approved_list_loaded']}",
        "Research only — no capital authorization or orders were created.",
        "",
    ]
    for index, item in enumerate(result["candidates"], 1):
        lines.append(
            f"{index}. {item['trading_pair']} — $\{item['current_price']:,.8g} | "
            f"Score {item['selection_score']:.1f}/100 | "
            f"{item['discount_to_vwap_pct']:+.1f}% vs 90d VWAP | "
            f"RSI {item['rsi_14d']:.1f}"
        )
        lines.append(
            f"   7d {item['return_7d_pct']:+.1f}% | "
            f"30d {item['return_30d_pct']:+.1f}% | "
            f"Governor approved: {item['governor_approved']}"
        )

    if not result["candidates"]:
        lines.append("No Kraken markets passed the configured data and recovery checks.")

    return "\n".join(lines)
