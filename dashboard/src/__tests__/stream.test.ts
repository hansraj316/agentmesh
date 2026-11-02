import { describe, expect, it } from "vitest";
import { LiveStream, type EventSourceLike } from "../lib/stream";
import type { ConnectionState, StreamEvent } from "../lib/types";

class FakeSource implements EventSourceLike {
  onopen: ((ev: Event) => unknown) | null = null;
  onmessage: ((ev: MessageEvent) => unknown) | null = null;
  onerror: ((ev: Event) => unknown) | null = null;
  closed = false;
  constructor(public url: string) {}
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.(new Event("open"));
  }
  send(data: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: typeof data === "string" ? data : JSON.stringify(data) }));
  }
  fail() {
    this.onerror?.(new Event("error"));
  }
}

function event(id: number): StreamEvent {
  return {
    id,
    event_id: `e${id}`,
    run_id: "fleet-0001",
    agent: "planner",
    type: "message",
    ts: "2026-11-02T10:00:00.000Z",
    payload: {},
  };
}

function harness(pages: Array<StreamEvent[] | Error | 404>, pageSize = 2) {
  const sources: FakeSource[] = [];
  const fetched: string[] = [];
  const timers: Array<{ fn: () => void; ms: number }> = [];
  const received: number[] = [];
  const states: ConnectionState[] = [];
  const stream = new LiveStream({
    pageSize,
    fetch: async (url) => {
      fetched.push(url);
      const next = pages.shift() ?? [];
      if (next instanceof Error) throw next;
      if (next === 404) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => next };
    },
    eventSource: (url) => {
      const s = new FakeSource(url);
      sources.push(s);
      return s;
    },
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout: () => {},
  });
  stream.start(
    (events) => received.push(...events.map((e) => e.id)),
    (s) => states.push(s),
  );
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { stream, sources, fetched, timers, received, states, flush };
}

describe("LiveStream", () => {
  it("pages through /api/events, then streams after the last id", async () => {
    const h = harness([[event(1), event(2)], [event(3)]]);
    await h.flush();
    expect(h.fetched).toEqual(["/api/events?after=0&limit=2", "/api/events?after=2&limit=2"]);
    expect(h.received).toEqual([1, 2, 3]);
    expect(h.sources[0].url).toBe("/api/stream?after=3");
    h.sources[0].open();
    expect(h.states).toEqual(["connecting", "live"]);
  });

  it("delivers stream events once, in order, skipping duplicates and bad lines", async () => {
    const h = harness([[]]);
    await h.flush();
    const s = h.sources[0];
    s.open();
    s.send(event(1));
    s.send(event(1));
    s.send("not json");
    s.send(event(2));
    expect(h.received).toEqual([1, 2]);
    expect(h.stream.cursor).toBe(2);
  });

  it("reconnects from the last id it saw with growing backoff", async () => {
    const h = harness([[event(1)]]);
    await h.flush();
    h.sources[0].open();
    h.sources[0].send(event(2));
    h.sources[0].fail();
    expect(h.sources[0].closed).toBe(true);
    expect(h.states.at(-1)).toBe("reconnecting");
    expect(h.timers.map((t) => t.ms)).toEqual([1000]);

    h.timers[0].fn();
    expect(h.sources[1].url).toBe("/api/stream?after=2");
    h.sources[1].fail();
    h.timers[1].fn();
    h.sources[2].fail();
    expect(h.timers.map((t) => t.ms)).toEqual([1000, 2000, 4000]);

    // A successful open resets the backoff.
    h.timers[2].fn();
    h.sources[3].open();
    expect(h.states.at(-1)).toBe("live");
    h.sources[3].send(event(3));
    h.sources[3].fail();
    expect(h.timers.at(-1)?.ms).toBe(1000);
    expect(h.received).toEqual([1, 2, 3]);
  });

  it("caps the backoff at 15 seconds", async () => {
    const h = harness([[]]);
    await h.flush();
    for (let i = 0; i < 7; i++) {
      h.sources[i].fail();
      h.timers[i].fn();
    }
    expect(h.timers.map((t) => t.ms)).toEqual([1000, 2000, 4000, 8000, 15000, 15000, 15000]);
  });

  it("goes straight to the stream when the events page is unavailable", async () => {
    const h = harness([404]);
    await h.flush();
    expect(h.sources[0].url).toBe("/api/stream?after=0");
    const g = harness([new Error("offline")]);
    await g.flush();
    expect(g.sources[0].url).toBe("/api/stream?after=0");
  });

  it("stops cleanly and never reconnects after stop", async () => {
    const h = harness([[]]);
    await h.flush();
    h.stream.stop();
    expect(h.sources[0].closed).toBe(true);
    h.sources[0].fail();
    expect(h.timers).toHaveLength(0);
  });

  it("ignores messages from a source it already replaced", async () => {
    const h = harness([[]]);
    await h.flush();
    const old = h.sources[0];
    old.fail();
    h.timers[0].fn();
    old.send(event(9));
    expect(h.received).toEqual([]);
  });
});
