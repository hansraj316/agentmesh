import { useCallback, useEffect, useMemo, useState } from "react";
import type { Backend } from "./lib/backend";
import { useFleet, useNow } from "./lib/useFleet";
import type { ConnectionState } from "./lib/types";
import { AgentsView } from "./views/AgentsView";
import { HandoffsView } from "./views/HandoffsView";
import { RunDrawer } from "./views/RunDrawer";

type Tab = "handoffs" | "agents";

function readHash(): { tab: Tab; run: string | null } {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = hash.split("?");
  const run = new URLSearchParams(query).get("run");
  return { tab: path === "agents" ? "agents" : "handoffs", run };
}

function writeHash(tab: Tab, run: string | null) {
  const next = `#/${tab}${run ? `?run=${encodeURIComponent(run)}` : ""}`;
  if (window.location.hash !== next) window.history.replaceState(null, "", next);
}

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
  mock: "Mock fleet",
};

export function App({ backend }: { backend: Backend }) {
  const fleet = useFleet(backend);
  const now = useNow(true);
  const [{ tab, run }, setRoute] = useState(readHash);
  const [runFilter, setRunFilter] = useState<string>("");

  useEffect(() => writeHash(tab, run), [tab, run]);
  useEffect(() => {
    const onHash = () => setRoute(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const openRun = useCallback((id: string) => setRoute((r) => ({ ...r, run: id })), []);
  const closeRun = useCallback(() => setRoute((r) => ({ ...r, run: null })), []);
  const shown = useMemo(
    () => (runFilter ? fleet.handoffs.filter((h) => h.run_id === runFilter) : fleet.handoffs),
    [fleet.handoffs, runFilter],
  );
  const running = fleet.handoffs.filter((h) => h.status === "running").length;

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">
          <span className="brand__mark" aria-hidden="true" />
          AgentMesh
        </span>
        <nav className="tabs" aria-label="Views">
          {(["handoffs", "agents"] as Tab[]).map((t) => (
            <button
              key={t}
              type="button"
              className={"tab" + (tab === t ? " tab--on" : "")}
              aria-current={tab === t ? "page" : undefined}
              onClick={() => setRoute((r) => ({ ...r, tab: t }))}
            >
              {t === "handoffs" ? "Handoffs" : "Agents"}
            </button>
          ))}
        </nav>
        <span className={`conn conn--${fleet.connection}`} role="status">
          <span className="conn__dot" aria-hidden="true" />
          {CONNECTION_LABEL[fleet.connection]}
        </span>
      </header>

      <main className="card">
        <div className="card__head">
          <h1 className="card__title">{tab === "handoffs" ? "Handoffs" : "Agents"}</h1>
          {tab === "handoffs" ? (
            <>
              <label className="chip">
                <span className="chip__dot" aria-hidden="true" />
                <span className="sr-only">Run</span>
                <select value={runFilter} onChange={(e) => setRunFilter(e.target.value)}>
                  <option value="">All runs</option>
                  {fleet.runs.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              <span className="card__meta">
                {running ? `${running} running` : `${fleet.handoffs.length} handoffs`}
              </span>
            </>
          ) : null}
        </div>
        {tab === "handoffs" ? (
          <HandoffsView
            handoffs={shown}
            now={now}
            onOpenRun={openRun}
            ready={fleet.connection !== "connecting" || fleet.version > 0}
          />
        ) : (
          <AgentsView backend={backend} version={fleet.version} now={now} />
        )}
      </main>

      {run ? (
        <RunDrawer
          backend={backend}
          runId={run}
          handoffs={fleet.handoffs}
          version={fleet.runVersions.get(run) ?? 0}
          now={now}
          onClose={closeRun}
        />
      ) : null}
    </div>
  );
}
