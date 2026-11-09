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

/** Replace legacy implementation names in user-facing notifications and copy. */
export function publicBrandText(value: string): string {
  return value
    .replace(/\bCondor\b/gi, "Spectra Intelligence")
    .replace(/\bHummingbot(?:s)?\b/gi, "Execution Engine");
}

export function brandedAgentName(agent: { slug: string; name: string } | null | undefined): string {
  if (!agent) return "Spectra Intelligence";
  return BRAND_NAMES[agent.slug] ?? (agent.name.startsWith("Spectra ") ? agent.name : `Spectra ${agent.name.replace(/\s+(Trader|Expert|Analyst|LP|Maker)$/i, "")}`);
}
