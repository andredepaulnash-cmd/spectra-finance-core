import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Bug, Eye, Moon, Sun } from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import spectraLogo from "@/assets/spectra-finance-logo.svg";
import { ChatBubble } from "@/components/chat/ChatBubble";
import { api } from "@/lib/api";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { RelaunchBanner } from "@/components/RelaunchBanner";
import { ReportIssueDialog } from "@/components/ReportIssueDialog";
import { TelemetryConsentBanner } from "@/components/TelemetryConsentBanner";
import { ChatProvider } from "@/hooks/useChat";
import { usePrefetchData } from "@/hooks/usePrefetchData";
import { useServer } from "@/hooks/useServer";
import { useTheme } from "@/hooks/useTheme";
import { NAV_ITEMS, FULL_BLEED_ROUTES } from "@/lib/nav";
import { routeFacts } from "@/lib/pageFacts";
import { useViewFacts } from "@/lib/viewFacts";
import { CurrencySelector } from "./CurrencySelector";
import { ServerSelector } from "./ServerSelector";
import { NotificationBell } from "./NotificationBell";

const FULL_BLEED_PATTERNS = [/^\/agents\/[^/]+$/];

export function AppShell() {
  return (
    <ChatProvider>
      <AppShellBody />
    </ChatProvider>
  );
}

function AppShellBody() {
  const { server } = useServer();
  const { pathname, search } = useLocation();
  const {
    data: spectraRuntime,
    isLoading: spectraRuntimeLoading,
    isError: spectraRuntimeError,
  } = useQuery({
    queryKey: ["spectra-runtime", server],
    queryFn: () => api.getSpectraRuntime(server!),
    enabled: Boolean(server),
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 1,
  });
  const paperModeConfirmed = Boolean(
    !spectraRuntimeError &&
      spectraRuntime?.mode === "PAPER_ONLY" &&
      spectraRuntime.paper_only === true &&
      spectraRuntime.live_dispatch_available === false,
  );
  const paperConnectorReady = Boolean(
    paperModeConfirmed && (spectraRuntime?.paper_connector_count ?? 0) > 0,
  );
  const visibleNavItems = paperModeConfirmed
    ? NAV_ITEMS.filter(
        ({ to }) =>
          to === "/" ||
          to === "/overview" ||
          to === "/settings" ||
          (to === "/portfolio" && paperConnectorReady),
      )
    : NAV_ITEMS.filter(({ to }) => to === "/");
  const isSpectraSafeRoute =
    pathname === "/" ||
    pathname.startsWith("/agents/") ||
    (paperModeConfirmed && pathname.startsWith("/overview")) ||
    (paperModeConfirmed && pathname.startsWith("/settings")) ||
    (paperConnectorReady && pathname.startsWith("/portfolio"));
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const [reportOpen, setReportOpen] = useState(false);
  const isFullBleed =
    FULL_BLEED_ROUTES.includes(pathname) ||
    FULL_BLEED_PATTERNS.some((re) => re.test(pathname));

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        navigate("/");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate]);

  usePrefetchData();
  const queryClient = useQueryClient();
  useViewFacts(() => routeFacts(pathname, search, queryClient));

  return (
    <div className="spectra-app-frame flex h-screen min-h-0">
      <aside className="spectra-sidebar flex w-64 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="flex h-16 shrink-0 items-center border-b border-[var(--color-border)] px-5">
          <NavLink to="/" aria-label="Spectra Finance home" className="spectra-brand flex items-center">
            <img src={spectraLogo} alt="Spectra Finance" className="h-9 w-auto max-w-[190px]" />
          </NavLink>
        </div>
        <div className="px-3 pt-6">
          <p className="px-3 pb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-[var(--color-text-muted)]">Workspace</p>
          <nav className="flex flex-col gap-1">
            {visibleNavItems.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                title={label}
                className={({ isActive }) =>
                  `spectra-sidebar-link group flex items-center gap-3 rounded-xl px-3 py-3 text-sm font-medium transition-colors ${
                    isActive
                      ? "is-active border border-[var(--color-primary)]/25 bg-[var(--color-primary)]/10 text-[var(--color-primary)] shadow-[0_0_24px_rgba(39,217,242,0.07)]"
                      : "border border-transparent text-[var(--color-text-muted)] hover:border-[var(--color-border)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)]"
                  }`
                }
              >
                <Icon className="h-[18px] w-[18px] shrink-0" />
                <span className="spectra-sidebar-label">{label}</span>
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="mt-auto p-3">
          <div className="spectra-sidebar-governance rounded-xl border border-emerald-500/20 bg-emerald-500/[0.06] p-3">
            <div className="flex items-center gap-2 text-xs font-semibold text-emerald-400">
              <span className={`spectra-runtime-dot ${paperConnectorReady ? "is-ready" : paperModeConfirmed ? "is-pending" : "is-locked"}`} />
              {paperConnectorReady ? "Paper runtime ready" : paperModeConfirmed ? "Paper setup pending" : "Execution locked"}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-[var(--color-text-muted)]">
              Governor authority retained. Live execution is unavailable.
            </p>
          </div>
          <p className="px-2 pt-3 text-[9px] font-semibold uppercase tracking-[0.18em] text-[var(--color-text-muted)]">Spectra Finance · Alpha</p>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="spectra-topbar flex h-16 shrink-0 items-center gap-3 border-b border-[var(--color-border)] px-5">
          <div className="min-w-0">
            <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-[var(--color-text-muted)]">Spectra Finance</p>
            <p className="truncate text-sm font-semibold text-[var(--color-text)]">
              {pathname === "/" ? "Intelligence" : pathname.startsWith("/overview") ? "Command Center" : pathname.startsWith("/portfolio") ? "Portfolio" : pathname.startsWith("/settings") ? "Settings" : "Workspace"}
            </p>
          </div>
          <span
            aria-live="polite"
            title={
              paperConnectorReady
                ? "Spectra paper-only runtime confirmed; only simulated connectors are available."
                : paperModeConfirmed
                  ? "Paper-only mode is confirmed, but no paper connector is initialized yet."
                  : "Execution locked: this server has not confirmed Spectra paper-only mode."
            }
            className="spectra-runtime-pill ml-2 rounded-full border border-[var(--color-border)] px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)]"
          >
            <span aria-hidden="true" className={`spectra-runtime-dot ${paperConnectorReady ? "is-ready" : paperModeConfirmed ? "is-pending" : "is-locked"}`} />
            {paperConnectorReady
              ? "Paper only"
              : paperModeConfirmed
                ? "Paper setup"
                : spectraRuntimeLoading
                  ? "Checking"
                  : "Execution locked"}
          </span>
          <div className="ml-auto flex items-center gap-3">
            <ServerSelector />
            <CurrencySelector />
            <div className="flex items-center gap-1">
              <NotificationBell />
              <button
                onClick={() => setReportOpen(true)}
                className="rounded-lg p-2 text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-accent)]"
                title="Report an issue"
              >
                <Bug className="h-4 w-4" />
              </button>
              <button
                onClick={toggleTheme}
                className="rounded-lg p-2 text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-accent)]"
                title={
                  theme === "dark" ? "Switch to light mode" :
                  theme === "light" ? "Switch to color-blind mode" :
                  "Switch to dark mode"
                }
              >
                {theme === "dark" ? <Sun className="h-4 w-4" /> :
                 theme === "light" ? <Eye className="h-4 w-4" /> :
                 <Moon className="h-4 w-4" />}
              </button>
            </div>
          </div>
        </header>
        {server && (
          <div className="spectra-safety-banner shrink-0 border-b border-[var(--color-border)] px-5 py-2 text-xs text-[var(--color-text-muted)]">
            {paperConnectorReady
              ? "Spectra paper-only runtime verified. Orders must use the isolated simulated connector; live trading controls remain hidden."
              : paperModeConfirmed
                ? "Paper-only runtime verified. Add a dedicated *_paper_trade connector to enable paper portfolio views."
                : "Execution locked. This server has not verified Spectra paper-only mode, so trading, DEX, bot deployment, and routine controls are hidden."}
          </div>
        )}
        <RelaunchBanner />
        <TelemetryConsentBanner />
        <main
          className={`spectra-main relative min-h-0 flex-1 overflow-x-hidden overflow-y-auto ${
            isFullBleed && isSpectraSafeRoute ? "" : "p-6"
          }`}
        >
          <ErrorBoundary resetKey={pathname + server}>
            {isSpectraSafeRoute ? (
              <Outlet key={server} />
            ) : (
              <section
                role="alert"
                className="mx-auto flex min-h-[50vh] max-w-2xl flex-col items-center justify-center gap-4 text-center"
              >
                <div className="rounded-full border border-[var(--color-border)] p-4 text-[var(--color-text-muted)]">
                  <img src={spectraLogo} alt="Spectra Finance" className="h-10 w-auto" />
                </div>
                <h1 className="text-xl font-semibold">Spectra execution locked</h1>
                <p className="max-w-xl text-sm leading-6 text-[var(--color-text-muted)]">
                  {!paperModeConfirmed
                    ? "This server has not confirmed Spectra paper-only mode. Trading and account controls stay unavailable until the authenticated runtime reports PAPER_ONLY."
                    : pathname.startsWith("/portfolio") && !paperConnectorReady
                      ? "Paper-only mode is confirmed, but no paper connector is initialized. Set up a paper-trading connection in Spectra Settings first."
                      : "This control remains unavailable until Spectra authorizes it. Direct exchange access, wallet-gateway controls, bot deployment, and routine execution are not exposed."}
                </p>
                <button
                  onClick={() => navigate(paperModeConfirmed ? "/settings" : "/")}
                  className="rounded-md border border-[var(--color-border)] px-4 py-2 text-sm font-medium hover:bg-[var(--color-surface-hover)]"
                >
                  {paperModeConfirmed ? "Open Spectra settings" : "Return to Spectra"}
                </button>
              </section>
            )}
          </ErrorBoundary>
        </main>
      </div>
      <ReportIssueDialog open={reportOpen} onClose={() => setReportOpen(false)} />
      <ChatBubble />
    </div>
  );
}
