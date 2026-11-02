/**
 * A scripted "indian-avengers" fleet: Planner breaks a goal into tasks and
 * hands each to Coder, Coder hands its change to Reviewer, and Reviewer
 * approves (or now and then the Coder fails). Everything is deterministic for
 * a seed, so tests and screenshots are repeatable.
 *
 * The output is plain AMP events with offsets in milliseconds; the mock fleet
 * turns offsets into timestamps and plays them in real time.
 */

export interface ScriptedEvent {
  atMs: number;
  agent: string;
  type: "agent_start" | "agent_end" | "agent_error" | "message";
  payload: Record<string, unknown>;
}

export const BACKLOG: readonly string[] = [
  "Add retry to GitHub sync",
  "Fix flaky webhook test",
  "Cache board query results",
  "Paginate /api/runs",
  "Handle Slack rate limits",
  "Add OTLP batch size flag",
  "Trim cost rollup to 30 days",
  "Document AMP v0.4 handoffs",
];

const MODEL = "claude-sonnet-scripted";

/** mulberry32: a tiny seeded PRNG. */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function between(r: () => number, lo: number, hi: number): number {
  return Math.round(lo + r() * (hi - lo));
}

function hex(r: () => number, n = 4): string {
  let s = "";
  for (let i = 0; i < n; i++) s += Math.floor(r() * 16).toString(16);
  return s;
}

function usage(r: () => number, scale: number) {
  const input_tokens = between(r, 600, 2400) * scale;
  const output_tokens = between(r, 120, 900) * scale;
  const cost_usd = Math.round((input_tokens * 3e-6 + output_tokens * 15e-6) * 1e6) / 1e6;
  return { input_tokens, output_tokens, cost_usd, model: MODEL };
}

export interface ScriptOptions {
  /** Which run this is; picks the tasks from the backlog. */
  index: number;
  seed: number;
  /** Multiply every delay; 0 makes the whole run instantaneous. */
  speed?: number;
  /** Chance that a Coder step fails (default 0.15). */
  failureRate?: number;
  tasksPerRun?: number;
}

/** One fleet run as AMP events with offsets from the run's start. */
export function scriptRun(opts: ScriptOptions): ScriptedEvent[] {
  const r = rng(opts.seed * 7919 + opts.index);
  const speed = opts.speed ?? 1;
  const failureRate = opts.failureRate ?? 0.15;
  const tasksPerRun = opts.tasksPerRun ?? 2;
  const out: ScriptedEvent[] = [];
  let t = 0;
  const wait = (lo: number, hi: number) => {
    t += between(r, lo, hi) * speed;
  };
  const emit = (agent: string, type: ScriptedEvent["type"], payload: Record<string, unknown>) =>
    out.push({ atMs: t, agent, type, payload });

  const plannerSpan = `sp-${hex(r, 8)}`;
  emit("planner", "agent_start", { span_id: plannerSpan });
  wait(400, 900);

  for (let i = 0; i < tasksPerRun; i++) {
    const task = BACKLOG[(opts.index * tasksPerRun + i) % BACKLOG.length];
    const taskId = `t-${hex(r)}`;
    const opened = t;
    const handoff = (status: string, extra: Record<string, unknown> = {}) => ({
      kind: "handoff",
      task_id: taskId,
      task,
      from: "planner",
      to: "coder",
      status,
      span_id: plannerSpan,
      ...extra,
    });
    emit("planner", "message", handoff("running"));
    wait(150, 400);

    const coderSpan = `sp-${hex(r, 8)}`;
    emit("coder", "agent_start", { span_id: coderSpan, parent_span_id: plannerSpan });
    wait(4000, 9000);

    if (r() < failureRate) {
      emit("coder", "agent_error", {
        span_id: coderSpan,
        parent_span_id: plannerSpan,
        error: "GitHub API timed out after 30s",
        error_type: "TimeoutError",
      });
      wait(100, 250);
      emit(
        "planner",
        "message",
        handoff("failed", {
          duration_ms: t - opened,
          error: "TimeoutError: GitHub API timed out after 30s",
        }),
      );
      wait(600, 1200);
      continue;
    }

    // Coder hands its change to Reviewer.
    const reviewId = `t-${hex(r)}`;
    const reviewTask = `Review: ${task}`;
    const reviewOpened = t;
    const review = (status: string, extra: Record<string, unknown> = {}) => ({
      kind: "handoff",
      task_id: reviewId,
      task: reviewTask,
      from: "coder",
      to: "reviewer",
      status,
      span_id: coderSpan,
      parent_span_id: plannerSpan,
      ...extra,
    });
    emit("coder", "message", review("running"));
    wait(150, 400);
    const reviewerSpan = `sp-${hex(r, 8)}`;
    emit("reviewer", "agent_start", { span_id: reviewerSpan, parent_span_id: coderSpan });
    wait(2500, 6000);
    emit("reviewer", "agent_end", {
      span_id: reviewerSpan,
      parent_span_id: coderSpan,
      ...usage(r, 1),
    });
    wait(100, 250);
    emit("coder", "message", review("done", { duration_ms: t - reviewOpened }));
    wait(300, 800);
    emit("coder", "agent_end", { span_id: coderSpan, parent_span_id: plannerSpan, ...usage(r, 2) });
    wait(100, 250);
    emit("planner", "message", handoff("done", { duration_ms: t - opened }));
    wait(600, 1200);
  }

  emit("planner", "agent_end", { span_id: plannerSpan, ...usage(r, 1) });
  return out;
}
