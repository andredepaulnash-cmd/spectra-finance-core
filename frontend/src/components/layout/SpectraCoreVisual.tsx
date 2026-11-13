import { Activity, ArrowDownRight, ArrowUpRight, BrainCircuit, Orbit, ShieldCheck, Sparkles } from "lucide-react";

export function SpectraCoreVisual() {
  return (
    <section className="spectra-core-stage" aria-label="Spectra Intelligence command visualization">
      <div className="spectra-core-copy">
        <div className="spectra-core-kicker"><span className="spectra-core-live-dot" /> SPECTRA INTELLIGENCE <span className="spectra-core-version">ALPHA / PAPER</span></div>
        <h1>Think clearly.<br /><span>Move deliberately.</span></h1>
        <p>The intelligence layer for a governed trading system. Research, orchestration and execution visibility—connected, but never confused.</p>
        <div className="spectra-core-pill-row">
          <span><BrainCircuit size={14} /> Condor · analysis</span>
          <span><Activity size={14} /> Hummingbot · paper execution</span>
          <span><ShieldCheck size={14} /> Governor · authority</span>
        </div>
      </div>
      <div className="spectra-neural-wrap" aria-hidden="true">
        <div className="spectra-neural-halo spectra-neural-halo-a" />
        <div className="spectra-neural-halo spectra-neural-halo-b" />
        <div className="spectra-neural-orbit spectra-neural-orbit-a"><i /><i /><i /></div>
        <div className="spectra-neural-orbit spectra-neural-orbit-b"><i /><i /></div>
        <div className="spectra-neural-orbit spectra-neural-orbit-c"><i /></div>
        <div className="spectra-neural-core"><div className="spectra-neural-core-inner"><Orbit size={42} strokeWidth={1.15} /><span>S</span></div></div>
        <div className="spectra-neural-scan" />
        <div className="spectra-neural-caption"><Sparkles size={13} /> GOVERNED INTELLIGENCE</div>
      </div>
      <div className="spectra-core-footer">
        <div><span className="spectra-core-label">SYSTEM ROLE</span><strong>Orchestrator</strong></div>
        <div><span className="spectra-core-label">CAPITAL AUTHORITY</span><strong>Architect retained</strong></div>
        <div><span className="spectra-core-label">EXECUTION MODE</span><strong className="spectra-core-paper"><span /> Paper only</strong></div>
        <div className="spectra-core-mark"><ArrowUpRight size={15} /> <ArrowDownRight size={15} /></div>
      </div>
    </section>
  );
}
