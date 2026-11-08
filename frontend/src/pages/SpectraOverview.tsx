import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Brain,
  CheckCircle2,
  CircleAlert,
  LockKeyhole,
  Server,
  ShieldCheck,
} from "lucide-react";

import { useServer } from "@/hooks/useServer";
import { api, type SpectraModuleStatus } from "@/lib/api";

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
    staleTime: 15_000,
    refetchInterval: 30_000,
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
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-[var(--color-text-muted)]">
            <Activity className="h-4 w-4" />
            Spectra Finance
          </div>
          <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">System Overview</h1>
          <p className="mt-2 max-w-2xl text-sm text-[var(--color-text-muted)]">
            One view of the analysis, capital authority, execution boundary and advanced modules.
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
