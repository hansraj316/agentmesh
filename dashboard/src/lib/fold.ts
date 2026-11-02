import type { Handoff, HandoffStatus, StreamEvent } from "./types";

/**
 * Fold AMP v0.4 handoff events into handoffs, with the same rules as the
 * Python `agentmesh.handoffs.fold`:
 *
 * - a handoff is a `message` event whose payload has `kind: "handoff"`,
 *   non-empty string `task_id`, `from` and `to`, and a `status` of
 *   `running`, `done` or `failed`; a missing `task` folds as "";
 * - handoffs are keyed by run and `task_id`;
 * - the open (`running`) sets `started_at` to its `ts`;
 * - the first close (`done` / `failed`) sets `status`, `ended_at` and, on
 *   failure, the first line of `error`; later closes are ignored;
 * - `duration_seconds` comes from the close's `duration_ms` when present,
 *   else from `ended_at - started_at`;
 * - a close with no open still folds, `started_at` back-computed from
 *   `duration_ms` (or equal to `ended_at` without it);
 * - an open with no close folds as `running` with `ended_at: null`;
 * - every other event, including v0.3 messages, is ignored.
 */

interface Entry {
  readonly handoff: Handoff;
  /** True once a real open event set `started_at`. */
  readonly opened: boolean;
  /** The close's `duration_ms`, when the close carried one. */
  readonly closeMs: number | null;
}

export type HandoffMap = ReadonlyMap<string, Entry>;

const STATUSES: ReadonlySet<string> = new Set(["running", "done", "failed"]);

function key(runId: string, taskId: string): string {
  return runId + "\u0000" + taskId;
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value !== "";
}

function firstLine(value: unknown): string | undefined {
  if (!nonEmpty(value)) return undefined;
  const line = value.split(/\r?\n/, 1)[0].trim();
  return line || undefined;
}

function durationOf(h: Handoff, closeMs: number | null): number | null {
  if (closeMs !== null) return closeMs / 1000;
  if (h.ended_at === null) return null;
  const a = Date.parse(h.started_at);
  const b = Date.parse(h.ended_at);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 1000);
}

/** True when `event` is a well-formed AMP v0.4 handoff. */
export function isHandoffEvent(event: Pick<StreamEvent, "type" | "payload">): boolean {
  const p = event.payload;
  return (
    event.type === "message" &&
    p != null &&
    typeof p === "object" &&
    p.kind === "handoff" &&
    nonEmpty(p.task_id) &&
    nonEmpty(p.from) &&
    nonEmpty(p.to) &&
    typeof p.status === "string" &&
    STATUSES.has(p.status)
  );
}

/** Apply one event; returns the same map when the event changes nothing. */
export function applyEvent(state: HandoffMap, event: StreamEvent): HandoffMap {
  if (!isHandoffEvent(event)) return state;
  const p = event.payload;
  const taskId = p.task_id as string;
  const status = p.status as HandoffStatus;
  const k = key(event.run_id, taskId);
  const prev = state.get(k);

  let h: Handoff =
    prev?.handoff ??
    ({
      task_id: taskId,
      run_id: event.run_id,
      task: typeof p.task === "string" ? p.task : "",
      from: p.from as string,
      to: p.to as string,
      status: "running",
      started_at: event.ts,
      ended_at: null,
      duration_seconds: null,
      ...(nonEmpty(p.span_id) ? { span_id: p.span_id } : {}),
      ...(nonEmpty(p.parent_span_id) ? { parent_span_id: p.parent_span_id } : {}),
    } satisfies Handoff);
  let opened = prev?.opened ?? false;
  let closeMs = prev?.closeMs ?? null;
  let changed = prev === undefined;

  if (!h.task && typeof p.task === "string" && p.task) {
    h = { ...h, task: p.task };
    changed = true;
  }

  if (status === "running") {
    if (!opened) {
      opened = true;
      h = { ...h, started_at: event.ts };
      changed = true;
    }
  } else if (h.ended_at === null) {
    closeMs = typeof p.duration_ms === "number" && p.duration_ms >= 0 ? p.duration_ms : null;
    const error = status === "failed" ? firstLine(p.error) : undefined;
    h = { ...h, status, ended_at: event.ts, ...(error ? { error } : {}) };
    if (!opened) {
      const end = Date.parse(event.ts);
      h = {
        ...h,
        started_at:
          closeMs !== null && !Number.isNaN(end) ? new Date(end - closeMs).toISOString() : event.ts,
      };
    }
    changed = true;
  }

  if (!changed) return state;
  if (h.ended_at !== null) h = { ...h, duration_seconds: durationOf(h, closeMs) };
  const next = new Map(state);
  next.set(k, { handoff: h, opened, closeMs });
  return next;
}

/** Fold a batch of events, oldest first, into a handoff map. */
export function foldInto(state: HandoffMap, events: Iterable<StreamEvent>): HandoffMap {
  let s = state;
  for (const event of events) s = applyEvent(s, event);
  return s;
}

/** Newest first, the order the Handoffs view shows. */
export function sortHandoffs(state: HandoffMap): Handoff[] {
  return Array.from(state.values(), (e) => e.handoff).sort((a, b) => {
    const diff = Date.parse(b.started_at) - Date.parse(a.started_at);
    if (diff !== 0 && !Number.isNaN(diff)) return diff;
    return b.task_id < a.task_id ? -1 : b.task_id > a.task_id ? 1 : 0;
  });
}

/** Fold events into handoffs, newest first. */
export function fold(events: Iterable<StreamEvent>): Handoff[] {
  return sortHandoffs(foldInto(new Map(), events));
}
