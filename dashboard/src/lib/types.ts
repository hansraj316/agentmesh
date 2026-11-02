/** An AMP event as the daemon streams it: the stored event plus its row id. */
export interface StreamEvent {
  id: number;
  event_id: string;
  run_id: string;
  agent: string;
  type: "agent_start" | "agent_end" | "agent_error" | "message" | "tool_call" | string;
  ts: string;
  payload: Record<string, unknown>;
}

export type HandoffStatus = "running" | "done" | "failed";

/** A folded handoff, the shape every AgentMesh consumer shows (AMP v0.4). */
export interface Handoff {
  task_id: string;
  run_id: string;
  task: string;
  from: string;
  to: string;
  status: HandoffStatus;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  error?: string;
  span_id?: string;
  parent_span_id?: string;
}

/** One row of GET /api/agents (board.AgentSummary). */
export interface AgentSummary {
  agent: string;
  status: string;
  last_event_type: string;
  last_ts: string;
  runs: number;
  errors: number;
  streak: number;
  avg_duration_seconds: number | null;
  cost_usd: number | null;
  p95_duration_seconds: number | null;
}

/** One span of GET /api/runs/<run_id> (trace.Span). */
export interface Span {
  agent: string;
  status: "ok" | "failed" | "running" | string;
  started_ts: string;
  span_id: string | null;
  parent_span_id: string | null;
  duration_seconds: number | null;
  error: string | null;
  error_type: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  model: string | null;
  children: Span[];
}

export interface Annotation {
  ts: string;
  text: string;
  author: string | null;
}

/** GET /api/runs/<run_id> (trace.Trace plus optional annotations). */
export interface RunTrace {
  run_id: string;
  roots: Span[];
  span_count: number;
  failed_count: number;
  total_seconds: number;
  annotations?: Annotation[];
}

export type ConnectionState = "connecting" | "live" | "reconnecting" | "mock";
