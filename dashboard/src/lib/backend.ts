import { LiveStream, type EventsHandler, type StateHandler } from "./stream";
import type { AgentSummary, RunTrace } from "./types";

/** Everything the views need from the daemon (or from the mock fleet). */
export interface Backend {
  readonly mode: "live" | "mock";
  subscribe(onEvents: EventsHandler, onState: StateHandler): () => void;
  agents(): Promise<AgentSummary[]>;
  run(runId: string): Promise<RunTrace>;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  const body = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) {
    const message =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : `HTTP ${res.status}`;
    throw new Error(message);
  }
  return body as T;
}

/** Talks to `agentmesh serve` on the same origin (or via the Vite proxy). */
export function httpBackend(baseUrl = ""): Backend {
  return {
    mode: "live",
    subscribe(onEvents, onState) {
      const stream = new LiveStream({ baseUrl });
      stream.start(onEvents, onState);
      return () => stream.stop();
    },
    agents: () => getJson<AgentSummary[]>(`${baseUrl}/api/agents`),
    run: (runId) => getJson<RunTrace>(`${baseUrl}/api/runs/${encodeURIComponent(runId)}`),
  };
}
