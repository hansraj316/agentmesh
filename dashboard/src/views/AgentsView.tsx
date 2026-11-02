import { useEffect, useRef, useState } from "react";
import type { Backend } from "../lib/backend";
import { formatAgo, formatCost, formatDuration } from "../lib/format";
import type { AgentSummary } from "../lib/types";
import { Avatar } from "./Avatar";
import { StatusPill } from "./StatusPill";

export interface AgentsViewProps {
  backend: Backend;
  /** Changes whenever new events arrive; the board refetches, at most once a second. */
  version: number;
  now: number;
}

export function AgentsView({ backend, version, now }: AgentsViewProps) {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastFetch = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const wait = Math.max(0, 1000 - (Date.now() - lastFetch.current));
    const timer = setTimeout(() => {
      lastFetch.current = Date.now();
      backend
        .agents()
        .then((data) => {
          if (cancelled) return;
          setAgents(data);
          setError(null);
        })
        .catch((err: unknown) => {
          if (!cancelled) setError(err instanceof Error ? err.message : String(err));
        });
    }, wait);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [backend, version]);

  if (error && !agents) return <div className="empty empty--error">Could not load agents: {error}</div>;
  if (!agents) return <div className="empty">Loading agents…</div>;
  if (agents.length === 0) return <div className="empty">No agents have reported yet.</div>;

  return (
    <div className="table-wrap">
      <table className="agents">
        <thead>
          <tr>
            <th scope="col">Agent</th>
            <th scope="col">Status</th>
            <th scope="col" className="num">Runs</th>
            <th scope="col" className="num">Errors</th>
            <th scope="col" className="num">Streak</th>
            <th scope="col" className="num">Avg</th>
            <th scope="col" className="num">p95</th>
            <th scope="col" className="num">Cost</th>
            <th scope="col" className="num">Last seen</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.agent}>
              <th scope="row">
                <span className="agent-name">
                  <Avatar name={a.agent} active={a.status === "running"} />
                  {a.agent}
                </span>
              </th>
              <td>
                <StatusPill status={a.status} />
              </td>
              <td className="num">{a.runs}</td>
              <td className={"num" + (a.errors ? " num--bad" : "")}>{a.errors}</td>
              <td className="num">{a.streak}</td>
              <td className="num">{formatDuration(a.avg_duration_seconds)}</td>
              <td className="num">{formatDuration(a.p95_duration_seconds)}</td>
              <td className="num">{formatCost(a.cost_usd)}</td>
              <td className="num muted">{formatAgo(a.last_ts, now)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {error ? <p className="stale">Showing the last good data: {error}</p> : null}
    </div>
  );
}
