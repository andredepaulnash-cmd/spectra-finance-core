import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowUpRight, Bot, BrainCircuit, ChartNoAxesCombined, CircleDollarSign, LockKeyhole, Radar, ShieldCheck, Sparkles, Wallet } from "lucide-react";
import { Link } from "react-router-dom";

import { useServer } from "@/hooks/useServer";
import { api } from "@/lib/api";
import { AgentChatTab } from "@/pages/tabs/AgentChatTab";

function Metric({ label, value, detail, icon: Icon }: { label: string; value: string; detail: string; icon: typeof Activity }) {
  return (
    <article className="spectra-metric-card">
      <div className="flex items-center justify-between gap-3">
        <span className="spectra-kicker">{label}</span>
        <Icon className="h-4 w-4 text-cyan-300" />
      </div>
      <p className="mt-3 text-2xl font-semibold tracking-tight text-white tabular-nums">{value}</p>
      <p className="mt-1 text-xs text-slate-400">{detail}</p>
    </article>
  );
}

export function SpectraCommandCenter() {
  const { server } = useServer();
  const runtime = useQuery({
    queryKey: ["spectra-runtime", server],
    queryFn: () => api.getSpectraRuntime(server!),
    enabled: Boolean(server),
    staleTime: 10_000,
    refetchInterval: 15_000,
    retry: 1,
  });
  const paperConfirmed = runtime.data?.mode === "PAPER_ONLY" && runtime.data.paper_only === true && runtime.data.live_dispatch_available === false;
  const paperReady = paperConfirmed && (runtime.data?.paper_connector_count ?? 0) > 0;

  return (
    <div className="spectra-command-page">
      <header className="spectra-command-header">
        <div className="spectra-command-brand">
          <span className="spectra-command-mark" aria-hidden="true"><span>S</span></span>
          <div><p className="spectra-command-wordmark">SPECTRA</p><p className="spectra-command-submark">FINANCE</p></div>
        </div>
        <div className="spectra-command-title">
          <div className="spectra-command-orbit" aria-hidden="true"><BrainCircuit /></div>
          <div><p className="spectra-kicker">AI-powered · secure · paper trading</p><h1>Spectra Command Center</h1></div>
        </div>
        <div className="spectra-command-safety">
          <span className={paperConfirmed ? "spectra-status-light is-ready" : "spectra-status-light"} />
          <div><strong>{paperConfirmed ? "PAPER ONLY" : "EXECUTION LOCKED"}</strong><small>{paperReady ? "Simulated connector ready" : "Live trading disabled"}</small></div>
        </div>
      </header>

      <div className="spectra-command-grid">
        <section className="spectra-command-main">
          <section className="spectra-welcome-panel">
            <div className="spectra-neural-visual" aria-hidden="true">
              <div className="spectra-neural-core"><BrainCircuit /></div>
              <span className="spectra-neural-ring ring-one" /><span className="spectra-neural-ring ring-two" /><span className="spectra-neural-ring ring-three" />
              <span className="spectra-neural-particle particle-one" /><span className="spectra-neural-particle particle-two" /><span className="spectra-neural-particle particle-three" />
            </div>
            <div className="spectra-welcome-copy">
              <p className="spectra-kicker">Your private financial operating system</p>
              <h2>Welcome to <span>Spectra Intelligence</span></h2>
              <p>One coordinated workspace for market research, strategy analysis, portfolio visibility, and governed paper execution.</p>
              <div className="spectra-capability-row"><span><BrainCircuit /> Intelligence</span><span><Radar /> Market research</span><span><ShieldCheck /> Capital governance</span></div>
            </div>
          </section>

          <div className="spectra-section-heading"><div><p className="spectra-kicker">Your workspace</p><h2>Intelligence Console</h2></div><span className="spectra-live-indicator"><i /> Conversation workspace</span></div>
          <section className="spectra-chat-stage"><AgentChatTab /></section>
        </section>

        <aside className="spectra-command-rail">
          <section className="spectra-rail-panel">
            <div className="spectra-rail-heading"><span><Wallet /> Runtime overview</span><span className="spectra-mini-status">{runtime.isLoading ? "Checking" : paperConfirmed ? "Verified" : "Locked"}</span></div>
            <Metric label="Paper connectors" value={String(runtime.data?.paper_connector_count ?? "—")} detail={paperReady ? "Isolated simulated venue available" : "Waiting for verified paper connector"} icon={CircleDollarSign} />
            <div className="spectra-rail-stat"><span>Live dispatch</span><strong>Unavailable</strong></div>
            <div className="spectra-rail-stat"><span>Selected server</span><strong>{server ? "Connected" : "Not selected"}</strong></div>
            <Link className="spectra-rail-link" to="/portfolio">Open portfolio <ArrowUpRight /></Link>
          </section>

          <section className="spectra-rail-panel">
            <div className="spectra-rail-heading"><span><ChartNoAxesCombined /> Explore the system</span></div>
            <Link className="spectra-action-link" to="/overview"><Activity /><span><strong>System overview</strong><small>Runtime, safeguards, and module health</small></span><ArrowUpRight /></Link>
            <Link className="spectra-action-link" to="/bots"><Bot /><span><strong>Strategies & agents</strong><small>Inspect configured agents and strategies</small></span><ArrowUpRight /></Link>
            <Link className="spectra-action-link" to="/settings"><LockKeyhole /><span><strong>Connections & settings</strong><small>Manage approved configuration</small></span><ArrowUpRight /></Link>
          </section>

          <section className="spectra-integrity-panel">
            <div className="spectra-integrity-orb"><ShieldCheck /></div>
            <div><p className="spectra-kicker">Capital protection</p><strong>{paperConfirmed ? "Paper boundary confirmed" : "Execution remains locked"}</strong><p>{paperConfirmed ? "The runtime reports paper-only mode and no live dispatch." : "The interface will not assume a safe runtime without server confirmation."}</p></div>
          </section>
        </aside>
      </div>
      <footer className="spectra-command-footer"><span><i /> Spectra Finance Alpha</span><span><ShieldCheck /> Paper-trading boundary enforced</span><span><Sparkles /> Built to think clearly. Operate carefully.</span></footer>
    </div>
  );
}
