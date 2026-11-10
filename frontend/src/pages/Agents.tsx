import { AgentChatTab } from "@/pages/tabs/AgentChatTab";
import { SpectraCoreVisual } from "@/components/layout/SpectraCoreVisual";

/**
 * The home is the Intelligence workspace. Keep the existing conversation and
 * its connected panels fully functional; give it a distinctive Spectra identity
 * and a lightweight animated neural centerpiece above the working interface.
 */
export function Agents() {
  return (
    <div className="spectra-intelligence-home h-full min-h-0">
      <SpectraCoreVisual />
      <div className="spectra-intelligence-console">
        <div className="spectra-intelligence-console-heading">
          <div>
            <p className="spectra-console-eyebrow">COMMAND INTERFACE</p>
            <h2>Intelligence workspace</h2>
          </div>
          <span className="spectra-console-status"><i /> READY FOR INPUT</span>
        </div>
        <div className="spectra-intelligence-chat h-full min-h-0">
          <AgentChatTab />
        </div>
      </div>
    </div>
  );
}
