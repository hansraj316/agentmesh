import { describe, expect, it } from "vitest";
import { applyEvent, fold, isHandoffEvent } from "../lib/fold";
import type { StreamEvent } from "../lib/types";

let nextId = 1;
function ev(partial: Partial<StreamEvent> & { payload: Record<string, unknown> }): StreamEvent {
  return {
    id: nextId++,
    event_id: `e${nextId}`,
    run_id: "fleet-0007",
    agent: "planner",
    type: "message",
    ts: "2026-11-02T10:00:00.000Z",
    ...partial,
  };
}

function handoff(status: string, extra: Record<string, unknown> = {}, ts?: string, run_id?: string) {
  return ev({
    ...(ts ? { ts } : {}),
    ...(run_id ? { run_id } : {}),
    payload: {
      kind: "handoff",
      task_id: "t-3f2a",
      task: "Add retry to GitHub sync",
      from: "planner",
      to: "coder",
      status,
      ...extra,
    },
  });
}

describe("fold", () => {
  it("pairs an open and a close into one done handoff", () => {
    const [h, ...rest] = fold([
      handoff("running", {}, "2026-11-02T10:00:00.000Z"),
      handoff("done", { duration_ms: 23140 }, "2026-11-02T10:00:23.140Z"),
    ]);
    expect(rest).toHaveLength(0);
    expect(h).toEqual({
      task_id: "t-3f2a",
      run_id: "fleet-0007",
      task: "Add retry to GitHub sync",
      from: "planner",
      to: "coder",
      status: "done",
      started_at: "2026-11-02T10:00:00.000Z",
      ended_at: "2026-11-02T10:00:23.140Z",
      duration_seconds: 23.14,
    });
  });

  it("falls back to the timestamp gap when duration_ms is missing", () => {
    const [h] = fold([
      handoff("running", {}, "2026-11-02T10:00:00.000Z"),
      handoff("done", {}, "2026-11-02T10:00:05.500Z"),
    ]);
    expect(h.duration_seconds).toBeCloseTo(5.5);
  });

  it("marks failed handoffs and keeps only the first line of the error", () => {
    const [h] = fold([
      handoff("running"),
      handoff("failed", { error: "TimeoutError: GitHub API timed out\nTraceback (most recent call last):" }),
    ]);
    expect(h.status).toBe("failed");
    expect(h.error).toBe("TimeoutError: GitHub API timed out");
  });

  it("leaves unmatched opens running with ended_at null", () => {
    const [h] = fold([handoff("running")]);
    expect(h.status).toBe("running");
    expect(h.ended_at).toBeNull();
    expect(h.duration_seconds).toBeNull();
  });

  it("carries span ids when the handoff happens inside an agent", () => {
    const [h] = fold([
      handoff("running", { span_id: "sp-1", parent_span_id: "sp-0" }),
      handoff("done", { duration_ms: 10 }),
    ]);
    expect(h.span_id).toBe("sp-1");
    expect(h.parent_span_id).toBe("sp-0");
  });

  it("ignores v0.3 events, annotations and malformed handoffs", () => {
    const events = [
      ev({ type: "agent_start", payload: { span_id: "sp-1" } }),
      ev({ type: "message", payload: { text: "hello" } }),
      ev({ type: "message", agent: "operator", payload: { kind: "annotation", text: "deploy" } }),
      ev({ type: "message", payload: { kind: "handoff", status: "running" } }), // no task_id
      ev({ type: "tool_call", payload: { kind: "handoff", task_id: "x", status: "running" } }),
      handoff("paused"),
    ];
    expect(fold(events)).toEqual([]);
    expect(isHandoffEvent(events[1])).toBe(false);
  });

  it("returns the same map for events that change nothing", () => {
    const start = applyEvent(new Map(), handoff("running"));
    expect(applyEvent(start, ev({ type: "agent_end", payload: {} }))).toBe(start);
    expect(applyEvent(start, handoff("running"))).toBe(start);
    const closed = applyEvent(start, handoff("done"));
    expect(applyEvent(closed, handoff("failed"))).toBe(closed);
    expect(applyEvent(closed, handoff("running"))).toBe(closed);
  });

  it("keeps the same task_id in different runs apart", () => {
    const out = fold([
      handoff("running", {}, "2026-11-02T10:00:00.000Z", "fleet-0001"),
      handoff("running", {}, "2026-11-02T10:01:00.000Z", "fleet-0002"),
      handoff("done", {}, "2026-11-02T10:00:09.000Z", "fleet-0001"),
    ]);
    expect(out.map((h) => [h.run_id, h.status])).toEqual([
      ["fleet-0002", "running"],
      ["fleet-0001", "done"],
    ]);
  });

  it("shows a close that arrives without its open", () => {
    const [h] = fold([handoff("done", { duration_ms: 4000 }, "2026-11-02T10:00:10.000Z")]);
    expect(h.status).toBe("done");
    expect(h.started_at).toBe("2026-11-02T10:00:06.000Z");
    expect(h.duration_seconds).toBe(4);
  });

  it("needs from and to, and folds a missing task as an empty string", () => {
    const noFrom = ev({ payload: { kind: "handoff", task_id: "t-9", to: "reviewer", status: "running" } });
    expect(fold([noFrom])).toEqual([]);
    const [h] = fold([
      ev({ payload: { kind: "handoff", task_id: "t-9", from: "coder", to: "reviewer", status: "running" } }),
    ]);
    expect(h.task).toBe("");
  });

  it("takes started_at from an open that arrives after its close", () => {
    const [h] = fold([
      handoff("done", {}, "2026-11-02T10:00:10.000Z"),
      handoff("running", {}, "2026-11-02T10:00:04.000Z"),
    ]);
    expect(h.status).toBe("done");
    expect(h.started_at).toBe("2026-11-02T10:00:04.000Z");
    expect(h.duration_seconds).toBe(6);
  });

  it("orders handoffs newest first", () => {
    const mk = (task_id: string, ts: string) =>
      ev({ ts, payload: { kind: "handoff", task_id, task: task_id, from: "planner", to: "coder", status: "running" } });
    const out = fold([
      mk("a", "2026-11-02T10:00:00.000Z"),
      mk("c", "2026-11-02T10:02:00.000Z"),
      mk("b", "2026-11-02T10:01:00.000Z"),
    ]);
    expect(out.map((h) => h.task_id)).toEqual(["c", "b", "a"]);
  });
});
