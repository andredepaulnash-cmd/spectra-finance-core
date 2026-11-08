import type { AgentSummary } from "@/lib/api";

/** Public product names. Slugs remain unchanged so existing sessions and routing keep working. */
const BRAND_NAMES: Record<string, string> = {
  condor: "Spectra Intelligence",
  adaptive_grid_trader: "Spectra Grid",
  directional_trader: "Spectra Momentum",
  fundamental_analyst: "Spectra Research",
  market_making_expert: "Spectra Liquidity",
  meteora_launch_lp: "Spectra Launch",
  smart_money_flow: "Spectra Flow",
  solana_dex_lp_expert: "Spectra DEX",
  xrpl_market_maker: "Spectra Markets",
  spectra_finance: "Spectra Finance",
};

export function brandedAgentName(agent: Pick<AgentSummary, "slug" | "name"> | null | undefined): string {
  if (!agent) return "Spectra Intelligence";
  return BRAND_NAMES[agent.slug] ?? (agent.name.startsWith("Spectra ") ? agent.name : `Spectra ${agent.name.replace(/\s+(Trader|Expert|Analyst|LP|Maker)$/i, "")}`);
}
