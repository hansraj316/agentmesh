import type { Handoff, HandoffStatus, StreamEvent } from "./types";

/**
 * Fold AMP v0.4 handoff events into handoffs, using the same rules as the
 * Python `agentmesh.handoffs.fold`:
 *
 * - a handoff is a `message` event whose payload has `kind: "handoff"`;
 * - the open (`status: "running"`) and close (`done` / `failed`) events share
 *   a `task_id`, unique within a run, so handoffs are keyed by run and task;
 * - a close sets `ended_at` and `duration_seconds` (from `duration_ms` when
 *   present, otherwise the gap between the two timestamps) and keeps the
 *   first line of `error` on failure;
 * - opens with no close stay `running` with `ended_at: null`;
 * - every other event, including v0.3 messages, is ignored.
 */

export type HandoffMap = ReadonlyMap<string, Handoff>;

const CLOSED: ReadonlySet<string> = new Set(["done", "failed"]);

function key(runId: string, taskId: string): string {
  return runId + "\u0000" + taskId;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

function firstLine(value: unknown): string | undefined {
  const text = str(value);
  return text === undefined ? undefined : text.split(/\r?\n/, 1)[0];
}

function secondsBetween(start: string, end: string): number | null {
  const a = Date.parse(start);
  const b = Date.parse(end);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 1000);
}

/** True when `event` follows the AMP v0.4 handoff convention. */
export function isHandoffEvent(event: Pick<StreamEvent, "type" | "payload">): boolean {
  const payload = event.payload;
  return (
    event.type === "message" &&
    payload != null &&
    payload.kind === "handoff" &&
    str(payload.task_id) !== undefined
  );
}

/** Apply one event; returns the same map when the event changes nothing. */
export function applyEvent(handoffs: HandoffMap, event: StreamEvent): HandoffMap {
  if (!isHandoffEvent(event)) return handoffs;
  const p = event.payload;
  const taskId = p.task_id as string;
  const status = str(p.status);
  if (status !== "running" && !CLOSED.has(status ?? "")) return handoffs;

  const k = key(event.run_id, taskId);
  const existing = handoffs.get(k);
  const next = new Map(handoffs);

  if (status === "running") {
    // A repeated open, or an open arriving after its close, changes nothing.
    if (existing) return handoffs;
    next.set(k, {
      task_id: taskId,
      run_id: event.run_id,
      task: str(p.task) ?? taskId,
      from: str(p.from) ?? event.agent,
      to: str(p.to) ?? "",
      status: "running",
      started_at: event.ts,
      ended_at: null,
      duration_seconds: null,
      ...(str(p.span_id) ? { span_id: str(p.span_id) } : {}),
      ...(str(p.parent_span_id) ? { parent_span_id: str(p.parent_span_id) } : {}),
    });
    return next;
  }

  // A second close for an already closed handoff changes nothing.
  if (existing && existing.status !== "running") return handoffs;

  const durationMs = typeof p.duration_ms === "number" && p.duration_ms >= 0 ? p.duration_ms : null;
  const startedAt =
    existing?.started_at ??
    (durationMs !== null ? new Date(Date.parse(event.ts) - durationMs).toISOString() : event.ts);
  const duration = durationMs !== null ? durationMs / 1000 : secondsBetween(startedAt, event.ts);
  const error = status === "failed" ? firstLine(p.error) : undefined;

  next.set(k, {
    task_id: taskId,
    run_id: event.run_id,
    task: existing?.task ?? str(p.task) ?? taskId,
    from: existing?.from ?? str(p.from) ?? event.agent,
    to: existing?.to || str(p.to) || "",
    status: status as HandoffStatus,
    started_at: startedAt,
    ended_at: event.ts,
    duration_seconds: duration,
    ...(error ? { error } : {}),
    ...(existing?.span_id || str(p.span_id) ? { span_id: existing?.span_id ?? str(p.span_id) } : {}),
    ...(existing?.parent_span_id || str(p.parent_span_id)
      ? { parent_span_id: existing?.parent_span_id ?? str(p.parent_span_id) }
      : {}),
  });
  return next;
}

/** Fold a batch of events, oldest first, into a handoff map. */
export function foldInto(handoffs: HandoffMap, events: Iterable<StreamEvent>): HandoffMap {
  let state = handoffs;
  for (const event of events) state = applyEvent(state, event);
  return state;
}

/** Newest first, the order the Handoffs view and `agentmesh handoffs` show. */
export function sortHandoffs(handoffs: Iterable<Handoff>): Handoff[] {
  return Array.from(handoffs).sort((a, b) => {
    const diff = Date.parse(b.started_at) - Date.parse(a.started_at);
    if (diff !== 0 && !Number.isNaN(diff)) return diff;
    return b.task_id < a.task_id ? -1 : b.task_id > a.task_id ? 1 : 0;
  });
}

/** Fold events into handoffs, newest first. */
export function fold(events: Iterable<StreamEvent>): Handoff[] {
  return sortHandoffs(foldInto(new Map(), events).values());
}
