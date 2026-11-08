import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Brain,
  CircleAlert,
  LockKeyhole,
  Server,
  ShieldCheck,
  Wallet,
} from "lucide-react";

import { useServer } from "@/hooks/useServer";
import { api, type SpectraModuleStatus } from "@/lib/api";

function formatUsd(value: number) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value);
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

function ModuleCard({ module }: { module: SpectraModuleStatus }) {
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
    </article>
  );
}

export function SpectraOverview() {
  const { server } = useServer();
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

      <section className="grid gap-3 md:grid-cols-3">
        {(registry.data?.system_flow ?? [
          { id: "condor", name: "Condor", role: "Analyze and propose", authority: "No independent capital authority" },
          { id: "spectra-core", name: "Spectra Core", role: "Validate, authorize and account", authority: "Sole capital and policy authority" },
          { id: "hummingbot", name: "Hummingbot", role: "Execute approved paper orders", authority: "Execution adapter only" },
        ]).map((step, index) => (
          <div key={step.id} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">Layer {index + 1}</span>
              {index === 1 ? <ShieldCheck className="h-4 w-4 text-emerald-500" /> : <Brain className="h-4 w-4 text-[var(--color-text-muted)]" />}
            </div>
            <h2 className="font-semibold">{step.name}</h2>
            <p className="mt-1 text-sm">{step.role}</p>
            <p className="mt-2 text-xs text-[var(--color-text-muted)]">{step.authority}</p>
          </div>
        ))}
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
            <p className="text-xs text-[var(--color-text-muted)]">Paper connectors</p>
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
            Connected paper adapters: {runtime.data.paper_connectors.join(", ")}
          </p>
        ) : null}
      </section>

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-4 md:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold">Core fill reconciliation</h2>
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              Durable accounting-health watermark from Spectra Core
            </p>
          </div>
          <span className={"rounded-full border px-2.5 py-1 text-[11px] font-medium " + (accountingHealthy ? statusTone("implemented") : statusTone("integration-in-progress"))}>
            {accountingHealth?.state?.replaceAll("_", " ") ?? "Not verified"}
          </span>
        </div>
        {!accountingHealth ? (
          <p className="text-sm text-[var(--color-text-muted)]">
            Core accounting health is unavailable. The dashboard will not assume reconciliation is healthy.
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
              Read-only account and executor data from the selected Hummingbot API
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
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px] text-left text-sm">
                    <thead className="text-xs text-[var(--color-text-muted)]">
                      <tr>
                        <th className="pb-2 pr-3 font-medium">Pair</th>
                        <th className="pb-2 pr-3 font-medium">Side</th>
                        <th className="pb-2 pr-3 font-medium">Status</th>
                        <th className="pb-2 pr-3 text-right font-medium">PnL (quote)</th>
                        <th className="pb-2 text-right font-medium">Fees (quote)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {krakenPaperActivity.slice(0, 8).map((executor) => (
                        <tr key={executor.id} className="border-t border-[var(--color-border)]">
                          <td className="py-2 pr-3 font-medium">{executor.trading_pair}</td>
                          <td className="py-2 pr-3">{executor.side}</td>
                          <td className="py-2 pr-3">{executor.status}</td>
                          <td className="py-2 pr-3 text-right tabular-nums">{executor.pnl.toFixed(2)}</td>
                          <td className="py-2 text-right tabular-nums">{executor.cum_fees_quote.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-sm text-[var(--color-text-muted)]">
                  No Kraken paper executors have been reported yet.
                </p>
              )}
            </div>
            <p className="mt-3 text-xs text-[var(--color-text-muted)]">
              These are Hummingbot-reported paper account and executor values. Core fill reconciliation health is not represented by this panel.
            </p>
          </>
        )}
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
            {(registry.data?.modules ?? []).map((module) => (
              <ModuleCard key={module.id} module={module} />
            ))}
          </div>
        )}
      </section>

      <p className="text-xs leading-relaxed text-[var(--color-text-muted)]">
        {registry.data?.status_note ?? "Status data is not available."} This overview is informational only; it does not place orders or change capital permissions.
      </p>
    </div>
  );
}
