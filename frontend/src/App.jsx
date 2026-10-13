import { useState } from "react";
import {
  Globe,
  Search,
  SlidersHorizontal,
  X,
  AlertTriangle,
} from "lucide-react";
import { useDashboard } from "./hooks/useDashboard.js";
import { useQuery } from "./hooks/useQuery.js";
import { api } from "./services/api.js";
import { Sidebar } from "./components/layout/Sidebar.jsx";
import { Topbar } from "./components/layout/Topbar.jsx";
import { ScanToolbar } from "./components/scan/ScanToolbar.jsx";
import { UrlEvidenceDialog } from "./components/inventory/UrlEvidenceDialog.jsx";
import { Empty } from "./components/common/Empty.jsx";
import { Overview } from "./pages/Overview.jsx";
import { WebsiteWorkspace } from "./pages/WebsiteWorkspace.jsx";
import { SummaryStrip } from "./components/coverage/SummaryStrip.jsx";
import { Inventory } from "./pages/Inventory.jsx";
import { Analysis } from "./pages/Analysis.jsx";
import { ActivityView } from "./pages/Activity.jsx";
import { ControlRoom } from "./pages/ControlRoom.jsx";
import { TYPES } from "./config/capture.js";
import { time } from "./utils/format.js";
import { NAVIGATION } from "./config/navigation.js";

export function App() {
  const {
    state,
    setState,
    runId,
    setRunId,
    snapshot,
    setSnapshot,
    error,
    setError,
    working,
    connected,
    setRefresh,
    action,
  } = useDashboard();
  const [view, setView] = useState("overview");
  const [target, setTarget] = useState("");
  const [detail, setDetail] = useState(null);
  const [preset, setPreset] = useState(null);
  const [menu, setMenu] = useState(false);
  const { data: workspace, error: workspaceError } = useQuery(
    "/sites",
    state.active ? 2000 : 5000,
  );
  function selectRun(id) {
    setRunId(id);
    setSnapshot(null);
    setDetail(null);
    setPreset(null);
  }
  function inspect(row) {
    action(async () =>
      setDetail(await api(`/runs/${runId}/urls/${row.id}`, undefined, "GET")),
    );
  }
  const run = snapshot?.run;
  const compatible = state.scan_workflow === "serial-automatic";
  const savedWebsites = (
    <>
      {workspaceError && (
        <p className="error-banner" role="alert">
          {workspaceError}
        </p>
      )}
      <WebsiteWorkspace
        sites={workspace?.items || []}
        selected={runId}
        onOpen={(id, page) => {
          if (id !== runId) selectRun(id);
          else setPreset(null);
          setView(page);
        }}
      />
    </>
  );
  return (
    <div className="app-shell">
      <Sidebar
        view={view}
        setView={setView}
        snapshot={snapshot}
        connected={connected}
        menu={menu}
        setMenu={setMenu}
      />
      <main>
        <Topbar view={view} menu={menu} setMenu={setMenu} />
        <div className="workspace">
          {view !== "control" && <SummaryStrip snapshot={snapshot} />}
          <div className="page-heading">
            <div>
              <div className="eyebrow">WEBSITE CRAWL & CORPUS COVERAGE</div>
              <h1>{NAVIGATION.find((item) => item[0] === view)?.[1]}</h1>
              <p>
                Independent discovery. Confirmed production crawler coverage.
              </p>
            </div>
            {view !== "control" && (
              <button className="secondary" onClick={() => setView("control")}>
                <SlidersHorizontal size={17} />
                Capture settings
                <span className="count-pill">
                  {state.settings.capture_types.length}
                </span>
              </button>
            )}
          </div>
          {error && (
            <div className="error-banner" role="alert">
              <AlertTriangle size={18} />
              <span>{error}</span>
              <button
                className="icon-button"
                aria-label="Dismiss message"
                onClick={() => setError("")}
              >
                <X size={17} />
              </button>
            </div>
          )}
          {connected && !compatible && (
            <div className="error-banner" role="alert">
              Backend restart required. Stop the old server with Ctrl+C in its
              terminal, then run npm.cmd --prefix frontend start again to enable
              automatic scanning and text checks.
            </div>
          )}
          {connected && compatible && state.backend_update_available && (
            <p className="info-box" role="status">
              Backend improvements are ready.{" "}
              {state.active
                ? "Pause this scan and wait for it to save before restarting. "
                : "Restart the backend to apply them. "}
              Stop the server with Ctrl+C, then run npm.cmd --prefix frontend
              start. Saved URLs and completed checks are retained.
            </p>
          )}
          {view === "control" ? (
            connected && state.settings.discovery_sources ? (
              <ControlRoom
                value={state.settings}
                active={state.active}
                corpus={state.corpus_api}
                target={snapshot?.run.target_url}
                sessionAvailable={snapshot?.public_session_available}
                save={async (settings) => {
                  const saved = await api("/settings", settings, "PUT");
                  setState((previous) => ({ ...previous, settings: saved }));
                  setRefresh((value) => value + 1);
                }}
              />
            ) : (
              <Empty title="Loading saved settings…" />
            )
          ) : (
            <>
              {view === "overview" && (
                <section className="scan-console">
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      action(async () => {
                        const result = await api("/discover", {
                          target_url: target,
                        });
                        selectRun(result.run_id);
                        setView("overview");
                      });
                    }}
                  >
                    <label className="website-input">
                      <Globe size={19} />
                      <input
                        type="url"
                        aria-label="Website to explore"
                        placeholder="https://your-website.com"
                        required
                        value={target}
                        onChange={(event) => setTarget(event.target.value)}
                        disabled={working || !!state.active}
                      />
                    </label>
                    <button
                      className="primary"
                      disabled={
                        working || !!state.active || !connected || !compatible
                      }
                    >
                      <Search size={17} />
                      Start website scan
                    </button>
                  </form>
                  <div className="scan-console-bottom">
                    <span>
                      Next scan:{" "}
                      {state.settings.capture_types
                        .map((kind) => TYPES[kind])
                        .join(" · ")}
                    </span>
                    <span>
                      Automatic Corpus and HTML text checks · one URL at a time.
                    </span>
                  </div>
                </section>
              )}
              {view === "inventory" && savedWebsites}
              <ScanToolbar
                showActions={view !== "inventory"}
                runs={state.runs}
                runId={runId}
                run={run}
                active={state.active}
                working={working}
                connected={connected && compatible}
                onSelect={selectRun}
                onStop={() => action(() => api(`/runs/${runId}/stop`, {}))}
                onResume={() => action(() => api(`/runs/${runId}/resume`, {}))}
              />
              {state.active && state.active.run_id !== runId && (
                <div className="info-box">
                  An audit is running for {state.active.target}.
                  <button
                    className="text-button"
                    onClick={() => selectRun(state.active.run_id)}
                  >
                    View active scan
                  </button>
                </div>
              )}
              {snapshot && (
                <div className="run-meta">
                  <span>
                    <Globe size={14} />
                    {run.target_url}
                  </span>
                  <span>
                    Inventory #{run.id} · Started {time(run.started_at)}
                  </span>
                  <span>
                    Maximum navigation depth{" "}
                    {snapshot.metrics.max_depth ?? "Unknown"}
                  </span>
                </div>
              )}
              {!snapshot ? (
                <Empty title={runId ? "Loading saved inventory…" : undefined} />
              ) : view === "overview" ? (
                <Overview key={runId} snapshot={snapshot} onDetail={inspect} />
              ) : view === "inventory" ? (
                <Inventory
                  key={runId}
                  snapshot={snapshot}
                  preset={preset}
                  onDetail={inspect}
                  methods={state.discovery_methods}
                />
              ) : view === "analysis" ? (
                <Analysis key={runId} snapshot={snapshot} />
              ) : (
                <ActivityView key={runId} snapshot={snapshot} />
              )}
              {view === "overview" && savedWebsites}
            </>
          )}
          <footer className="workspace-footer">
            <span>CrawlScope · Public website coverage analysis</span>
            <span>Local evidence · Paced automatic verification</span>
          </footer>
        </div>
      </main>
      {detail && (
        <UrlEvidenceDialog detail={detail} onClose={() => setDetail(null)} />
      )}
    </div>
  );
}
