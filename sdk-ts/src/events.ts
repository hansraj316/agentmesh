/**
 * AMP v0.2 event model — TypeScript mirror of agentmesh/events.py.
 * See docs/amp-spec.md in the repository root.
 */

import { randomUUID } from "node:crypto";

export const EVENT_TYPES = [
  "agent_start",
  "agent_end",
  "agent_error",
  "message",
  "tool_call",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/** A single AMP v0.2 event (see docs/amp-spec.md). */
export interface AmpEvent {
  event_id: string;
  run_id: string;
  agent: string;
  type: EventType;
  ts: string;
  payload: Record<string, unknown>;
}

/** Current time as an ISO-8601 UTC string (trailing "Z", valid per AMP). */
export function utcNowIso(): string {
  return new Date().toISOString();
}

/** Create a validated event with a fresh event_id and current UTC ts. */
export function newEvent(
  runId: string,
  agent: string,
  type: EventType,
  payload: Record<string, unknown>,
): AmpEvent {
  return validateEvent({
    event_id: randomUUID(),
    run_id: runId,
    agent,
    type,
    ts: utcNowIso(),
    payload,
  });
}

// ISO-8601 timestamp with an explicit timezone, e.g.
// 2026-06-10T08:15:30.123456+00:00 or 2026-06-10T08:15:30.123Z.
const ISO_8601_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function isUtcIso8601(ts: string): boolean {
  const match = ISO_8601_RE.exec(ts);
  if (match === null) {
    return false;
  }
  if (Number.isNaN(Date.parse(ts.replace(" ", "T")))) {
    return false;
  }
  // Mirror Python: timezone-aware with a UTC offset of zero.
  return ts.endsWith("Z") || ts.endsWith("+00:00") || ts.endsWith("-00:00");
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Validate one AMP v0.2 event, mirroring Event.validate() in
 * agentmesh/events.py. Throws an Error describing the first violation;
 * returns the event (narrowed to AmpEvent) when valid.
 *
 * Rules: all six fields present; event_id/run_id/agent non-empty strings;
 * type one of EVENT_TYPES; ts ISO-8601 with a UTC offset of zero; payload
 * a JSON object. v0.2 span fields (payload.span_id / payload.parent_span_id)
 * are optional but must be non-empty strings when present.
 */
export function validateEvent(event: unknown): AmpEvent {
  if (!isPlainObject(event)) {
    throw new Error(`event must be an object, got ${JSON.stringify(event)}`);
  }
  for (const name of ["event_id", "run_id", "agent"] as const) {
    const value = event[name];
    if (typeof value !== "string" || value === "") {
      throw new Error(`${name} must be a non-empty string, got ${JSON.stringify(value)}`);
    }
  }
  const type = event["type"];
  if (typeof type !== "string" || !(EVENT_TYPES as readonly string[]).includes(type)) {
    throw new Error(
      `type must be one of ${JSON.stringify(EVENT_TYPES)}, got ${JSON.stringify(type)}`,
    );
  }
  const ts = event["ts"];
  if (typeof ts !== "string" || !isUtcIso8601(ts)) {
    throw new Error(`ts must be an ISO-8601 UTC string, got ${JSON.stringify(ts)}`);
  }
  const payload = event["payload"];
  if (!isPlainObject(payload)) {
    throw new Error(`payload must be an object, got ${JSON.stringify(payload)}`);
  }
  for (const name of ["span_id", "parent_span_id"] as const) {
    if (name in payload) {
      const value = payload[name];
      if (typeof value !== "string" || value === "") {
        throw new Error(
          `payload.${name} must be a non-empty string when present, got ${JSON.stringify(value)}`,
        );
      }
    }
  }
  return event as unknown as AmpEvent;
}
