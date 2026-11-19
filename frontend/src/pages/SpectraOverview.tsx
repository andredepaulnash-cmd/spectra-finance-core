import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  Activity,
  CircleAlert,
  LockKeyhole,
  Server,
  ShieldCheck,
  Wallet,
} from "lucide-react";

import { useServer } from "@/hooks/useServer";
import { api, type MarketDiscoveryCandidate, type MarketDiscoveryListKey, type SpectraModuleControl, type SpectraModuleStatus } from "@/lib/api";

type DailyCandidateSnapshot = Pick<MarketDiscoveryCandidate, "symbol" | "base_asset" | "selection_score" | "current_price"> & { rank: number };
type DailyMarketSnapshot = { date: string; as_of: number; candidates: DailyCandidateSnapshot[] };
type DailyMarketHistory = { current: DailyMarketSnapshot | null; previous: DailyMarketSnapshot | null };
const DAILY_MARKET_HISTORY_KEY = "spectra.daily-market-shortlist.v1";

const marketListLabels: Record<MarketDiscoveryListKey, string> = {
  mean_reversion_watchlist: "Mean-reversion research",
  mean_reversion_candidates: "Eligible mean-reversion",
  recovery_watchlist: "Recovery watchlist",
  top_24h_gainers: "24h gainers",
  top_24h_losers: "24h losers",
  top_3d_losers: "3d losers",
  top_24h_volume: "24h volume",
  volatility_leaders: "Volatility leaders",
  rejected_candidates: "Rejected candidates",
};

function formatUsd(value: number) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value);
}

function formatMarketPrice(value: number) {
  return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 }).format(value);
}

function formatCompactUsd(value: number) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function statusLabel(status: string) {
  switch (status) {
    case "implemented":
      return "Implemented";
    case "partial":
      return "Partial";
    case "unverified":
      return "Unverified";
    case "not-implemented":
      return "Not implemented";
    case "research-only":
      return "Research only";
    case "default-off":
      return "Default off";
    case "integration-in-progress":
      return "Integration in progress";
    default:
      return status.replaceAll("-", " ");
  }
}

function statusTone(status: string) {
  if (status === "implemented") {
    return "border-emerald-500/30 bg-emerald-500/10 text-emerald-500";
  }
  if (status === "partial" || status === "integration-in-progress") {
    return "border-amber-500/30 bg-amber-500/10 text-amber-500";
  }
  if (status === "not-implemented" || status === "unverified") {
    return "border-[var(--color-border)] bg-[var(--color-panel)] text-[var(--color-text-muted)]";
  }
  return "border-sky-500/30 bg-sky-500/10 text-sky-500";
}

function ModuleCard({ module, control, canOperate, busy, onToggle }: { module: SpectraModuleStatus; control?: SpectraModuleControl; canOperate: boolean; busy: boolean; onToggle: (enabled: boolean) => void }) {
  return (
    <article className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h3 className="font-semibold">{module.name}</h3>
        <span className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${statusTone(module.status)}`}>
          {statusLabel(module.status)}
        </span>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-[var(--color-text-muted)]">
        {module.description}
      </p>
      <p className="mt-3 text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
        {module.readiness.replaceAll("-", " ")}
      </p>
      {control ? (
        <div className="mt-4 flex items-center justify-between gap-3 border-t border-[var(--color-border)] pt-3">
          <div>
            <p className="text-xs font-semibold">Runtime: {control.state}</p>
            <p className="mt-1 text-[11px] text-[var(--color-text-muted)]">{control.reason}</p>
          </div>
          <button
            type="button"
            disabled={!canOperate || busy}
            aria-pressed={control.trigger_enabled}
            onClick={() => onToggle(!control.trigger_enabled)}
            className={`rounded-lg border px-3 py-2 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${control.trigger_enabled ? "border-amber-500/40 bg-amber-500/10 text-amber-500" : "border-[var(--color-border)] hover:bg-[var(--color-surface-hover)]"}`}
          >
            {busy ? "Saving…" : control.trigger_enabled ? "Trigger enabled" : "Trigger off"}
          </button>
        </div>
      ) : null}
    </article>
  );
}

export function SpectraOverview() {
  const { server } = useServer();
  const queryClient = useQueryClient();
  const triggerMutation = useMutation({
    mutationFn: ({ moduleId, enabled }: { moduleId: string; enabled: boolean }) =>
      api.setSpectraModuleTrigger(server!, moduleId, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["spectra-modules", server] }),
  });
  const [activeMarketList, setActiveMarketList] = useState<MarketDiscoveryListKey>("mean_reversion_watchlist");
  const [dailyMarketHistory, setDailyMarketHistory] = useState<DailyMarketHistory>(() => {
    try {
      const saved = window.localStorage.getItem(DAILY_MARKET_HISTORY_KEY);
      return saved ? JSON.parse(saved) as DailyMarketHistory : { current: null, previous: null };
    } catch {
      return { current: null, previous: null };
    }
  });
  const runtime = useQuery({
    queryKey: ["spectra-runtime", server],
    queryFn: () => api.getSpectraRuntime(server!),
    enabled: Boolean(server),
    staleTime: 10_000,
    refetchInterval: 15_000,
    retry: 1,
  });
  const registry = useQuery({
    queryKey: ["spectra-modules", server],
    queryFn: () => api.getSpectraModules(server!),
    enabled: Boolean(server),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: 1,
  });

  // Kraken USD spot discovery is scoped to the selected Hummingbot API server.
  // Rankings are research-only and never authorize order placement.
  const marketDiscovery = useQuery({
    queryKey: ["spectra-market-discovery", server],
    queryFn: () => api.getMarketDiscovery(server!),
    enabled: Boolean(server),
    staleTime: 240_000,
    refetchInterval: 300_000,
    retry: 1,
  });

  // Keep a browser-local daily baseline so the shortlist can explain what
  // entered, exited, or changed score since the previous daily snapshot.
  // This is reporting state only: it cannot authorize or submit orders.
  useEffect(() => {
    const data = marketDiscovery.data;
    if (!data) return;
    const snapshot: DailyMarketSnapshot = {
      date: new Date(data.as_of * 1000).toISOString().slice(0, 10),
      as_of: data.as_of,
      candidates: data.candidates.map((candidate, index) => ({
        symbol: candidate.symbol,
        base_asset: candidate.base_asset,
        selection_score: candidate.selection_score,
        current_price: candidate.current_price,
        rank: index + 1,
      })),
    };
    setDailyMarketHistory((existing) => {
      const next = existing.current?.date === snapshot.date
        ? { ...existing, current: snapshot }
        : { current: snapshot, previous: existing.current };
      try {
        window.localStorage.setItem(DAILY_MARKET_HISTORY_KEY, JSON.stringify(next));
      } catch {
        // The live shortlist remains available even if browser storage is disabled.
      }
      return next;
    });
  }, [marketDiscovery.data]);

  const previousDailyCandidates = new Map(
    (dailyMarketHistory.previous?.candidates ?? []).map((candidate) => [candidate.symbol, candidate]),
  );
  const currentSymbols = new Set((marketDiscovery.data?.candidates ?? []).map((candidate) => candidate.symbol));
  const newDailyCandidates = (marketDiscovery.data?.candidates ?? []).filter(
    (candidate) => !previousDailyCandidates.has(candidate.symbol),
  );
  const exitedDailyCandidates = (dailyMarketHistory.previous?.candidates ?? []).filter(
    (candidate) => !currentSymbols.has(candidate.symbol),
  );
  const activeMarketRows =
    marketDiscovery.data?.lists?.[activeMarketList] ??
    (activeMarketList === "mean_reversion_candidates" ? marketDiscovery.data?.candidates ?? [] : []);

  // Only query account/executor data after the selected server proves it is
  // paper-only and has initialized the exact Kraken paper connector.
  const krakenPaperReady =
    runtime.data?.mode === "PAPER_ONLY" &&
    runtime.data.paper_only === true &&
    runtime.data.paper_connectors?.some((name) => name === "kraken_paper_trade" || name.endsWith("/kraken_paper_trade")) === true;
  const paperPortfolio = useQuery({
    queryKey: ["spectra-paper-portfolio", server],
    queryFn: () => api.getPortfolio(server!, true),
    enabled: Boolean(server) && krakenPaperReady,
    staleTime: 10_000,
    refetchInterval: 15_000,
    retry: 1,
  });
  const paperExecutors = useQuery({
    queryKey: ["spectra-paper-executors", server],
    queryFn: () => api.getExecutors(server!, { limit: 25 }),
    enabled: Boolean(server) && krakenPaperReady,
    staleTime: 10_000,
    refetchInterval: 15_000,
    retry: 1,
  });
  const krakenPaperBalance = paperPortfolio.data?.connectors.find(
    (connector) => connector.connector === "kraken_paper_trade",
  );
  const krakenPaperActivity = (paperExecutors.data ?? []).filter(
    (executor) => executor.connector === "kraken_paper_trade",
  );
  const portfolioSupervisor = runtime.data?.portfolio_supervisor;
  const accountingHealth = runtime.data?.accounting_health;
  const accountingTotals = accountingHealth?.totals;
  const accountingHealthy =
    accountingHealth?.allows_new_exposure === true &&
    (accountingHealth.state === "HEALTHY" ||
      accountingHealth.state === "NO_ACTIVE_POSITIONS");

  if (!server) {
    return (
      <div className="mx-auto max-w-3xl p-8">
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-6">
          <Server className="mb-3 h-6 w-6 text-[var(--color-text-muted)]" />
          <h1 className="text-xl font-semibold">Choose a server</h1>
          <p className="mt-2 text-sm text-[var(--color-text-muted)]">
            Select your local Spectra paper server from the server selector to view system status.
          </p>
        </div>
      </div>
    );
  }

  const paperConfirmed =
    runtime.data?.mode === "PAPER_ONLY" &&
    runtime.data.paper_only === true &&
    runtime.data.live_dispatch_available === false;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-5 md:p-8">
      <header className="spectra-hero flex flex-wrap items-center justify-between gap-5">
        <div className="relative z-10">
          <div className="mb-3 flex items-center gap-3">
            <span className="spectra-mark"><Activity className="h-5 w-5" /></span>
            <div className="spectra-eyebrow text-[10px] font-bold uppercase">Spectra Finance / Command</div>
          </div>
          <h1 className="text-3xl font-semibold tracking-tight md:text-4xl">Command Center</h1>
          <p className="mt-2 max-w-2xl text-sm text-[var(--color-text-muted)]">
            Intelligence, capital governance and execution visibility — one controlled system.
          </p>
        </div>
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-semibold">
            {paperConfirmed ? (
              <ShieldCheck className="h-4 w-4 text-emerald-500" />
            ) : (
              <LockKeyhole className="h-4 w-4 text-amber-500" />
            )}
            {paperConfirmed ? "Paper-only boundary confirmed" : "Execution locked"}
          </div>
          <p className="mt-1 text-xs text-[var(--color-text-muted)]">
            {runtime.isLoading
              ? "Checking authenticated runtime status…"
              : runtime.data?.error ?? (paperConfirmed ? "Live dispatch unavailable" : "Paper-only status could not be verified")}
          </p>
        </div>
      </header>

      <section className="spectra-hero rounded-xl p-6 md:p-8">
        <div className="flex flex-wrap items-center justify-between gap-5">
          <div>
            <div className="spectra-eyebrow text-[10px] font-bold uppercase tracking-widest">One unified platform</div>
            <h2 className="mt-2 text-2xl font-semibold">The Spectra Intelligence System</h2>
            <p className="mt-2 max-w-2xl text-sm text-[var(--color-text-muted)]">
              Research, strategy, capital governance and execution visibility — coordinated through one platform.
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm">
            <ShieldCheck className="h-4 w-4 text-emerald-500" />
            <span>Capital controls active</span>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4 md:p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold">Execution boundary</h2>
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">Read-only status from the selected server</p>
          </div>
          <span className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${paperConfirmed ? statusTone("implemented") : statusTone("unverified")}`}>
            {runtime.data?.mode ?? "Checking"}
          </span>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <div>
            <p className="text-xs text-[var(--color-text-muted)]">Paper venues</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{runtime.data?.paper_connector_count ?? "—"}</p>
          </div>
          <div>
            <p className="text-xs text-[var(--color-text-muted)]">Live dispatch</p>
            <p className="mt-1 text-xl font-semibold">Unavailable</p>
          </div>
          <div>
            <p className="text-xs text-[var(--color-text-muted)]">Product stage</p>
            <p className="mt-1 text-xl font-semibold">{registry.data?.release_stage ?? "—"}</p>
          </div>
        </div>
        {runtime.data?.paper_connectors?.length ? (
          <p className="mt-3 text-xs text-[var(--color-text-muted)]">
            Connected paper venues: {runtime.data.paper_connectors.map(() => "Kraken paper").join(", ")}
          </p>
        ) : null}
      </section>

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4 md:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold">Autonomous portfolio supervisor</h2>
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              Live status from the governed 83-position portfolio lifecycle
            </p>
          </div>
          <span className={"rounded-full border px-2.5 py-1 text-[11px] font-medium " + statusTone(
            portfolioSupervisor?.supervisor_state === "RUNNING"
              ? "implemented"
              : portfolioSupervisor?.supervisor_state === "BLOCKED"
                ? "integration-in-progress"
                : "unverified",
          )}>
            {portfolioSupervisor?.supervisor_state?.replaceAll("_", " ") ?? "Not verified"}
          </span>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <p className="text-xs text-[var(--color-text-muted)]">Fleet target</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{portfolioSupervisor?.target_active_positions ?? "—"}</p>
          </div>
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <p className="text-xs text-[var(--color-text-muted)]">Active positions</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{portfolioSupervisor?.active_positions ?? "—"}</p>
          </div>
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <p className="text-xs text-[var(--color-text-muted)]">Open vacancies</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{portfolioSupervisor?.vacancies ?? "—"}</p>
          </div>
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <p className="text-xs text-[var(--color-text-muted)]">New entries this cycle</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{portfolioSupervisor?.new_entries_this_cycle ?? "—"}</p>
          </div>
        </div>
        <p className="mt-3 text-xs text-[var(--color-text-muted)]">
          {portfolioSupervisor?.blocked_reason ??
            portfolioSupervisor?.last_error ??
            portfolioSupervisor?.error ??
            (portfolioSupervisor?.supervisor_state === "RUNNING"
              ? "Supervisor is publishing portfolio state. Capital admission remains subject to the Governor and accounting gates."
              : "No verified portfolio-supervisor cycle has been reported yet.")}
        </p>
      </section>

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4 md:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold">Capital reconciliation</h2>
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              Durable capital-accounting health status
            </p>
          </div>
          <span className={"rounded-full border px-2.5 py-1 text-[11px] font-medium " + (accountingHealthy ? statusTone("implemented") : statusTone("integration-in-progress"))}>
            {accountingHealth?.state?.replaceAll("_", " ") ?? "Not verified"}
          </span>
        </div>
        {!accountingHealth ? (
          <p className="text-sm text-[var(--color-text-muted)]">
            Capital accounting health is unavailable. The dashboard will not assume reconciliation is healthy.
          </p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-lg border border-[var(--color-border)] p-3">
                <p className="text-xs text-[var(--color-text-muted)]">Active positions</p>
                <p className="mt-1 text-xl font-semibold tabular-nums">{accountingHealth.active_positions}</p>
              </div>
              <div className="rounded-lg border border-[var(--color-border)] p-3">
                <p className="text-xs text-[var(--color-text-muted)]">Confirmed fills</p>
                <p className="mt-1 text-xl font-semibold tabular-nums">{accountingTotals?.confirmed_fills ?? 0}</p>
              </div>
              <div className="rounded-lg border border-[var(--color-border)] p-3">
                <p className="text-xs text-[var(--color-text-muted)]">Ledgered fills</p>
                <p className="mt-1 text-xl font-semibold tabular-nums">{accountingTotals?.ledgered_fills ?? 0}</p>
              </div>
              <div className="rounded-lg border border-[var(--color-border)] p-3">
                <p className="text-xs text-[var(--color-text-muted)]">Unresolved orders / trades</p>
                <p className="mt-1 text-xl font-semibold tabular-nums">
                  {(accountingTotals?.unresolved_orders ?? 0) + (accountingTotals?.unresolved_trades ?? 0)}
                </p>
              </div>
            </div>
            <p className="mt-3 text-xs text-[var(--color-text-muted)]">
              {accountingHealthy
                ? "The accounting freshness gate is clear. Other capital-policy checks still apply."
                : accountingHealth.error ?? "Accounting is on hold, stale, conflicted, or not yet verified. New exposure must remain blocked."}
            </p>
          </>
        )}
      </section>

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4 md:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Wallet className="h-4 w-4 text-[var(--color-text-muted)]" />
              <h2 className="font-semibold">Kraken paper account</h2>
            </div>
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              Read-only account and executor data from the selected market connectivity service
            </p>
          </div>
          <span className={"rounded-full border px-2.5 py-1 text-[11px] font-medium " + (krakenPaperReady ? statusTone("implemented") : statusTone("unverified"))}>
            {krakenPaperReady ? "Connector connected" : "Waiting for paper connector"}
          </span>
        </div>
        {!krakenPaperReady ? (
          <p className="text-sm text-[var(--color-text-muted)]">
            Portfolio and executor data stay hidden until the server confirms the Kraken paper connector is initialized.
          </p>
        ) : (
          <>
            <div className="mb-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-[var(--color-border)] p-3">
                <p className="text-xs text-[var(--color-text-muted)]">Simulated account value</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">
                  {paperPortfolio.isLoading ? "Loading…" : krakenPaperBalance ? formatUsd(krakenPaperBalance.total_usd) : "—"}
                </p>
                {paperPortfolio.isError ? (
                  <p className="mt-1 text-xs text-amber-500">Portfolio endpoint unavailable</p>
                ) : null}
              </div>
              <div className="rounded-lg border border-[var(--color-border)] p-3">
                <p className="text-xs text-[var(--color-text-muted)]">Paper executors</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">
                  {paperExecutors.isLoading ? "Loading…" : krakenPaperActivity.length}
                </p>
                {paperExecutors.isError ? (
                  <p className="mt-1 text-xs text-amber-500">Executor endpoint unavailable</p>
                ) : null}
              </div>
            </div>
            {krakenPaperBalance?.balances.length ? (
              <div className="mb-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {krakenPaperBalance.balances.map((balance) => (
                  <div key={balance.token} className="flex items-center justify-between rounded-lg bg-[var(--color-panel)] px-3 py-2 text-sm">
                    <span className="font-medium">{balance.token}</span>
                    <span className="tabular-nums text-[var(--color-text-muted)]">
                      {balance.total.toLocaleString(undefined, { maximumFractionDigits: 8 })}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
            <div>
              <h3 className="mb-2 text-sm font-semibold">Recent paper executors</h3>
              {krakenPaperActivity.length ? (
                <div className="mb-3 flex flex-wrap gap-2" role="tablist" aria-label="Dynamic market research lists">
              {(Object.keys(marketListLabels) as MarketDiscoveryListKey[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={activeMarketList === key}
                  onClick={() => setActiveMarketList(key)}
                  className={"rounded-lg border px-3 py-2 text-xs font-medium transition-colors " + (activeMarketList === key ? "border-[var(--color-primary)] bg-[var(--color-primary)]/10 text-[var(--color-primary)]" : "border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]")}
                >
                  {marketListLabels[key]}
                  <span className="ml-2 tabular-nums opacity-70">
                    {marketDiscovery.data?.lists?.[key]?.length ?? 0}
                  </span>
                </button>
              ))}
            </div>
            <p className="mb-3 text-xs text-[var(--color-text-muted)]">
              {activeMarketList === "mean_reversion_watchlist"
                ? "Mean-reversion research includes analyzed markets whether they pass entry guardrails or not. Rejection reasons remain visible."
                : activeMarketList === "mean_reversion_candidates"
                  ? "Only eligible mean-reversion candidates appear here. This is the only discovery list consumed by the existing approved-asset writer."
                  : activeMarketList === "recovery_watchlist"
                    ? "Losers with at least two independent reversal observations. Research only; this does not activate VCAP, DTTP, DTSL, or authorize an entry."
                    : activeMarketList === "top_24h_gainers" || activeMarketList === "top_24h_losers"
                      ? "Measured from hourly candles. Missing or stale hourly references are omitted rather than estimated."
                      : activeMarketList === "top_3d_losers"
                        ? "Three-day return is measured against the close three completed daily candles earlier."
                        : "Research rankings only. No market list grants capital authority."}
            </p>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1120px] text-left text-sm">
                <thead className="text-xs text-[var(--color-text-muted)]">
                  <tr>
                    <th className="pb-2 pr-3 font-medium">Rank / Asset</th>
                    <th className="pb-2 pr-3 text-right font-medium">Price</th>
                    <th className="pb-2 pr-3 text-right font-medium">24h Volume</th>
                    <th className="pb-2 pr-3 text-right font-medium">24h</th>
                    <th className="pb-2 pr-3 text-right font-medium">3d</th>
                    <th className="pb-2 pr-3 text-right font-medium">vs 90d VWAP</th>
                    <th className="pb-2 pr-3 text-right font-medium">RSI</th>
                    <th className="pb-2 pr-3 font-medium">Status / reversal evidence</th>
                    <th className="pb-2 text-right font-medium">Score</th>
                    <th className="pb-2 pl-3 text-right font-medium">1d Δ</th>
                  </tr>
                </thead>
                <tbody>
                  {activeMarketRows.map((candidate, index) => {
                    const rejection = candidate.rejection_reasons ?? [];
                    const rationale = candidate.selection_reasons ?? [];
                    const reversal = candidate.reversal_signal_reasons ?? [];
                    const status = candidate.selection_score == null
                      ? "History unavailable"
                      : candidate.eligible
                        ? "Eligible"
                        : candidate.entry_liquidity_eligible === false
                          ? "Below entry liquidity floor"
                          : "Research only · guardrail rejected";
                    return (
                      <tr key={candidate.trading_pair} className="border-t border-[var(--color-border)] align-top">
                        <td className="py-3 pr-3">
                          <div className="font-semibold">{index + 1}. {candidate.base_asset}</div>
                          <div className="mt-1 text-xs text-[var(--color-text-muted)]">{candidate.trading_pair}</div>
                          <div className="mt-1 max-w-[280px] text-xs text-[var(--color-text-muted)]">
                            {[...rationale, ...rejection].join(" · ") || "Live Kraken research market"}
                          </div>
                        </td>
                        <td className="py-3 pr-3 text-right tabular-nums">{formatMarketPrice(candidate.current_price)}</td>
                        <td className="py-3 pr-3 text-right tabular-nums">{formatCompactUsd(candidate.volume_24h_usd)}</td>
                        <td className={"py-3 pr-3 text-right tabular-nums " + (candidate.change_24h_pct == null ? "text-[var(--color-text-muted)]" : candidate.change_24h_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
                          {candidate.change_24h_pct == null ? "—" : (candidate.change_24h_pct >= 0 ? "+" : "") + candidate.change_24h_pct.toFixed(2) + "%"}
                        </td>
                        <td className={"py-3 pr-3 text-right tabular-nums " + (candidate.return_3d_pct == null ? "text-[var(--color-text-muted)]" : candidate.return_3d_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
                          {candidate.return_3d_pct == null ? "—" : (candidate.return_3d_pct >= 0 ? "+" : "") + candidate.return_3d_pct.toFixed(2) + "%"}
                        </td>
                        <td className="py-3 pr-3 text-right tabular-nums">
                          {candidate.discount_to_vwap_pct == null ? "—" : (candidate.discount_to_vwap_pct > 0 ? "+" : "") + candidate.discount_to_vwap_pct.toFixed(1) + "%"}
                        </td>
                        <td className="py-3 pr-3 text-right tabular-nums">{candidate.rsi_14d == null ? "—" : candidate.rsi_14d.toFixed(0)}</td>
                        <td className="py-3 pr-3">
                          <div className={candidate.eligible ? "text-xs font-medium text-emerald-500" : "text-xs font-medium text-amber-500"}>{status}</div>
                          {reversal.length ? (
                            <div className="mt-1 max-w-[300px] text-[11px] text-[var(--color-text-muted)]">
                              {reversal.join(" · ")}
                            </div>
                          ) : null}
                          <div className="mt-1 text-[11px] text-[var(--color-text-muted)]">
                            {candidate.governor_approved ? "Governor allowlist match" : "Not on Governor allowlist"} · no capital authorization
                          </div>
                        </td>
                        <td className="py-3 text-right font-semibold tabular-nums">
                          {candidate.selection_score == null ? "—" : candidate.selection_score.toFixed(0) + "/100"}
                        </td>
                        <td className="py-3 pl-3 text-right tabular-nums">
                          {activeMarketList !== "mean_reversion_candidates" || candidate.selection_score == null
                            ? <span className="text-[var(--color-text-muted)]">—</span>
                            : (() => {
                                const previous = previousDailyCandidates.get(candidate.symbol);
                                if (!previous) return <span className="text-[var(--color-text-muted)]">NEW</span>;
                                const delta = candidate.selection_score - previous.selection_score;
                                return <span className={delta > 0 ? "text-emerald-500" : delta < 0 ? "text-rose-500" : "text-[var(--color-text-muted)]"}>
                                  {delta > 0 ? "+" : ""}{delta.toFixed(0)}
                                </span>;
                              })()}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {!activeMarketRows.length ? (
                <p className="py-6 text-center text-sm text-[var(--color-text-muted)]">
                  {activeMarketList === "recovery_watchlist"
                    ? "No losing markets currently show at least two independent reversal observations."
                    : activeMarketList === "mean_reversion_candidates"
                      ? "No markets currently pass every mean-reversion and entry-eligibility guardrail. Review the research and rejected lists for the exact reasons."
                      : "No markets currently match this research list."}
                </p>
              ) : null}
            </div>
            <p className="mt-3 text-xs leading-relaxed text-[var(--color-text-muted)]">
              {marketDiscovery.data.source}. Scans up to {marketDiscovery.data.criteria.top_by_24h_quote_volume} Kraken USD spot markets with a ${marketDiscovery.data.criteria.minimum_research_24h_volume_quote.toLocaleString()} research-volume floor. The existing ${marketDiscovery.data.criteria.minimum_24h_volume_quote.toLocaleString()} entry-volume floor, 90-day history, historical turnover, volatility, and downside guardrails remain in force for the eligible-only shortlist. Research lists never authorize capital or activate recovery modules. Last refreshed {new Date(marketDiscovery.data.as_of * 1000).toLocaleString()}.
            </p>
          </>
        ) : null}
      </section>

      <section>
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Advanced module readiness</h2>
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              Source-backed status. A listed module is not automatically active or authorized.
            </p>
          </div>
          {registry.isLoading ? <span className="text-xs text-[var(--color-text-muted)]">Refreshing…</span> : null}
        </div>
        {registry.isError ? (
          <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
            <CircleAlert className="mb-2 h-4 w-4 text-amber-500" />
            Module registry could not be loaded. The execution boundary remains independently locked unless verified.
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {(registry.data?.modules ?? []).map((module) => {
              const control = registry.data?.runtime_controls?.find((item) => item.module_id === module.id);
              const triggerable = module.id === "dttp" || module.id === "dtsl";
              return (
                <ModuleCard
                  key={module.id}
                  module={module}
                  control={triggerable ? control : undefined}
                  canOperate={registry.data?.operator_controls_available === true}
                  busy={triggerMutation.isPending && triggerMutation.variables?.moduleId === module.id}
                  onToggle={(enabled) => triggerMutation.mutate({ moduleId: module.id, enabled })}
                />
              );
            })}
          </div>
        )}
        {triggerMutation.isError ? (
          <p role="alert" className="mt-3 text-xs text-rose-500">{triggerMutation.error.message}</p>
        ) : null}
        {registry.data?.operator_controls_available === false ? (
          <p className="mt-3 text-xs text-[var(--color-text-muted)]">Trigger controls are read-only for this account. The server owner must change them.</p>
        ) : null}
        <p className="mt-3 text-xs text-[var(--color-text-muted)]">Enabling a trigger does not activate an unverified module or authorize capital. DTTP and DTSL remain blocked until their runtime implementations are verified.</p>
      </section>

      <p className="text-xs leading-relaxed text-[var(--color-text-muted)]">
        {registry.data?.status_note ?? "Status data is not available."} This overview is informational only; it does not place orders or change capital permissions.
      </p>
    </div>
  );
}
