import type { Backend } from "../lib/backend";
import type { EventsHandler, StateHandler } from "../lib/stream";
import type { AgentSummary, RunTrace, Span, StreamEvent } from "../lib/types";
import { rng, scriptRun, type ScriptedEvent } from "./script";

/**
 * An in-browser stand-in for `agentmesh serve` + the example fleet. It keeps
 * its own event log, replays a scripted planner -> coder -> reviewer fleet in
 * real time, and answers the same three questions the daemon does: the event
 * stream, `/api/agents` and `/api/runs/<run_id>`.
 *
 * Used by `npm run dev:mock`, by `?mock` on the built bundle, and by tests.
 */

export interface MockFleetOptions {
  seed?: number;
  /** Completed runs to seed before the live ones (default 3). */
  history?: number;
  /** Delay multiplier for live runs (default 1). */
  speed?: number;
  /** Pause between live runs in ms (default 3000). */
  gapMs?: number;
  /** Stop after this many live runs (default: never). */
  liveRuns?: number;
  now?: () => number;
  setTimeout?: (fn: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
}

export class MockFleet implements Backend {
  readonly mode = "mock" as const;
  readonly log: StreamEvent[] = [];
  private nextId = 1;
  private runIndex = 0;
  private listeners = new Set<EventsHandler>();
  private timers = new Set<unknown>();
  private started = false;
  private liveCount = 0;
  private readonly o: Required<MockFleetOptions>;
  private readonly idRng: () => number;

  constructor(options: MockFleetOptions = {}) {
    this.o = {
      seed: options.seed ?? 7,
      history: options.history ?? 3,
      speed: options.speed ?? 1,
      gapMs: options.gapMs ?? 3000,
      liveRuns: options.liveRuns ?? Number.POSITIVE_INFINITY,
      now: options.now ?? (() => Date.now()),
      setTimeout: options.setTimeout ?? ((fn, ms) => globalThis.setTimeout(fn, ms)),
      clearTimeout:
        options.clearTimeout ??
        ((h) => globalThis.clearTimeout(h as ReturnType<typeof globalThis.setTimeout>)),
    };
    this.idRng = rng(this.o.seed);
    this.seedHistory();
  }

  subscribe(onEvents: EventsHandler, onState: StateHandler): () => void {
    onState("mock");
    if (this.log.length) onEvents([...this.log]);
    this.listeners.add(onEvents);
    if (!this.started) {
      this.started = true;
      this.scheduleNextRun(400);
    }
    return () => {
      this.listeners.delete(onEvents);
      if (this.listeners.size === 0) this.stop();
    };
  }

  stop(): void {
    for (const t of this.timers) this.o.clearTimeout(t);
    this.timers.clear();
    this.started = false;
  }

  async agents(): Promise<AgentSummary[]> {
    return summarizeAgents(this.log);
  }

  async run(runId: string): Promise<RunTrace> {
    const trace = buildTrace(this.log, runId, this.o.now());
    if (!trace) throw new Error(`no events found for run_id '${runId}'`);
    return trace;
  }

  private newRunId(): string {
    this.runIndex += 1;
    return `fleet-${String(this.runIndex).padStart(4, "0")}`;
  }

  private record(runId: string, step: ScriptedEvent, ts: number): StreamEvent {
    const event: StreamEvent = {
      id: this.nextId++,
      event_id: `ev-${Math.floor(this.idRng() * 0xffffffff).toString(16).padStart(8, "0")}`,
      run_id: runId,
      agent: step.agent,
      type: step.type,
      ts: new Date(ts).toISOString(),
      payload: step.payload,
    };
    this.log.push(event);
    return event;
  }

  /** Completed runs that finished a few minutes ago, so no view starts empty. */
  private seedHistory(): void {
    const now = this.o.now();
    const runs = Array.from({ length: this.o.history }, (_, i) =>
      scriptRun({ index: i, seed: this.o.seed, speed: 1 }),
    );
    let start = now - 60_000 - runs.reduce((sum, r) => sum + r[r.length - 1].atMs + 20_000, 0);
    for (const steps of runs) {
      const runId = this.newRunId();
      for (const step of steps) this.record(runId, step, start + step.atMs);
      start += steps[steps.length - 1].atMs + 20_000;
    }
  }

  private scheduleNextRun(delay: number): void {
    if (this.liveCount >= this.o.liveRuns) return;
    const timer = this.o.setTimeout(() => {
      this.timers.delete(timer);
      this.playRun();
    }, delay);
    this.timers.add(timer);
  }

  private playRun(): void {
    this.liveCount += 1;
    const runId = this.newRunId();
    const steps = scriptRun({ index: this.runIndex - 1, seed: this.o.seed, speed: this.o.speed });
    const start = this.o.now();
    steps.forEach((step, i) => {
      const timer = this.o.setTimeout(() => {
        this.timers.delete(timer);
        const event = this.record(runId, step, start + step.atMs);
        for (const listener of this.listeners) listener([event]);
        if (i === steps.length - 1) this.scheduleNextRun(this.o.gapMs);
      }, step.atMs);
      this.timers.add(timer);
    });
  }
}

// --- the daemon's /api/agents and /api/runs/<id>, recomputed in TypeScript ---

interface PairedSpan extends Span {
  run_id: string;
  ended_ms: number | null;
}

function pairSpans(events: StreamEvent[]): PairedSpan[] {
  const spans: PairedSpan[] = [];
  const byId = new Map<string, PairedSpan>();
  for (const e of events) {
    const spanId = typeof e.payload.span_id === "string" ? e.payload.span_id : null;
    if (e.type === "agent_start") {
      const span: PairedSpan = {
        run_id: e.run_id,
        agent: e.agent,
        status: "running",
        started_ts: e.ts,
        span_id: spanId,
        parent_span_id:
          typeof e.payload.parent_span_id === "string" ? e.payload.parent_span_id : null,
        duration_seconds: null,
        error: null,
        error_type: null,
        input_tokens: null,
        output_tokens: null,
        cost_usd: null,
        model: null,
        children: [],
        ended_ms: null,
      };
      spans.push(span);
      if (spanId) byId.set(spanId, span);
    } else if (e.type === "agent_end" || e.type === "agent_error") {
      const span = spanId ? byId.get(spanId) : undefined;
      if (!span) continue;
      const end = Date.parse(e.ts);
      span.ended_ms = end;
      span.duration_seconds = Math.max(0, (end - Date.parse(span.started_ts)) / 1000);
      span.status = e.type === "agent_end" ? "ok" : "failed";
      const p = e.payload;
      if (e.type === "agent_error") {
        span.error = typeof p.error === "string" ? p.error : null;
        span.error_type = typeof p.error_type === "string" ? p.error_type : null;
      }
      span.input_tokens = typeof p.input_tokens === "number" ? p.input_tokens : null;
      span.output_tokens = typeof p.output_tokens === "number" ? p.output_tokens : null;
      span.cost_usd = typeof p.cost_usd === "number" ? p.cost_usd : null;
      span.model = typeof p.model === "string" ? p.model : null;
    }
  }
  return spans;
}

function strip(span: PairedSpan): Span {
  const { run_id: _r, ended_ms: _e, children, ...rest } = span;
  return { ...rest, children: (children as PairedSpan[]).map(strip) };
}

export function buildTrace(events: StreamEvent[], runId: string, now: number): RunTrace | null {
  const runEvents = events.filter((e) => e.run_id === runId);
  if (runEvents.length === 0) return null;
  const spans = pairSpans(runEvents);
  const byId = new Map(spans.filter((s) => s.span_id).map((s) => [s.span_id as string, s]));
  const roots: PairedSpan[] = [];
  for (const span of spans) {
    const parent = span.parent_span_id ? byId.get(span.parent_span_id) : undefined;
    if (parent) parent.children.push(span);
    else roots.push(span);
  }
  const first = Math.min(...runEvents.map((e) => Date.parse(e.ts)));
  const last = Math.max(...runEvents.map((e) => Date.parse(e.ts)));
  const open = spans.some((s) => s.status === "running");
  return {
    run_id: runId,
    roots: roots.map(strip),
    span_count: spans.length,
    failed_count: spans.filter((s) => s.status === "failed").length,
    total_seconds: Math.max(0, ((open ? now : last) - first) / 1000),
    annotations:
      runId === "fleet-0001"
        ? [{ ts: runEvents[0].ts, text: "Seeded by the mock fleet", author: "operator" }]
        : undefined,
  };
}

function p95(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(0.95 * sorted.length) - 1)];
}

export function summarizeAgents(events: StreamEvent[]): AgentSummary[] {
  const spans = pairSpans(events);
  const names = Array.from(new Set(events.map((e) => e.agent))).sort();
  return names.map((agent) => {
    const own = events.filter((e) => e.agent === agent);
    const last = own[own.length - 1];
    const mine = spans.filter((s) => s.agent === agent);
    const terminal = mine.filter((s) => s.status !== "running");
    let streak = 0;
    for (let i = terminal.length - 1; i >= 0 && terminal[i].status === "ok"; i--) streak++;
    const ok = terminal.filter((s) => s.status === "ok").map((s) => s.duration_seconds ?? 0);
    const costs = mine.map((s) => s.cost_usd).filter((c): c is number => c !== null);
    const latest = mine[mine.length - 1];
    const status = latest ? latest.status : "idle";
    return {
      agent,
      status,
      last_event_type: last.type,
      last_ts: last.ts,
      runs: new Set(mine.map((s) => s.run_id)).size,
      errors: mine.filter((s) => s.status === "failed").length,
      streak,
      avg_duration_seconds: ok.length ? ok.reduce((a, b) => a + b, 0) / ok.length : null,
      cost_usd: costs.length ? Math.round(costs.reduce((a, b) => a + b, 0) * 1e6) / 1e6 : null,
      p95_duration_seconds: p95(ok),
    };
  });
}
