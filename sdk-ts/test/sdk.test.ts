import { beforeEach, describe, expect, it } from "vitest";

import { validateEvent } from "../src/events.js";
import { Mesh } from "../src/sdk.js";
import { MemorySink } from "../src/sink.js";

let sink: MemorySink;
let mesh: Mesh;

beforeEach(() => {
  sink = new MemorySink();
  mesh = new Mesh(sink);
});

describe("mesh.agent — sync", () => {
  it("emits start and end with duration_seconds", () => {
    const work = mesh.agent("researcher", (x: number) => x * 2);

    expect(work(21)).toBe(42);

    expect(sink.events.map((e) => e.type)).toEqual(["agent_start", "agent_end"]);
    expect(sink.events.every((e) => e.agent === "researcher")).toBe(true);
    expect(sink.events[0]!.run_id).toBe(sink.events[1]!.run_id);
    expect(sink.events[1]!.payload["duration_seconds"]).toBeGreaterThanOrEqual(0);
    expect(sink.events[0]!.payload["function"]).toBeTruthy();
  });

  it("emits error and re-throws", () => {
    const boom = mesh.agent("flaky", () => {
      throw new RangeError("kaput");
    });

    expect(() => boom()).toThrow("kaput");

    expect(sink.events.map((e) => e.type)).toEqual(["agent_start", "agent_error"]);
    const error = sink.events[1]!;
    expect(error.payload["error"]).toBe("kaput");
    expect(error.payload["error_type"]).toBe("RangeError");
    expect(error.payload["duration_seconds"]).toBeGreaterThanOrEqual(0);
  });

  it("emits AMP-valid events", () => {
    mesh.agent("checker", () => "ok")();
    for (const event of sink.events) {
      expect(() => validateEvent(event)).not.toThrow();
    }
  });
});

describe("mesh.agent — async", () => {
  it("emits start and end when the promise resolves", async () => {
    const work = mesh.agent("async-researcher", async (x: number) => {
      await Promise.resolve();
      return x + 1;
    });

    await expect(work(1)).resolves.toBe(2);

    expect(sink.events.map((e) => e.type)).toEqual(["agent_start", "agent_end"]);
    expect(sink.events.every((e) => e.agent === "async-researcher")).toBe(true);
    expect(sink.events[0]!.run_id).toBe(sink.events[1]!.run_id);
  });

  it("emits error and rejects when the promise rejects", async () => {
    const boom = mesh.agent("async-flaky", async () => {
      await Promise.resolve();
      throw new TypeError("async kaput");
    });

    await expect(boom()).rejects.toThrow("async kaput");

    expect(sink.events.map((e) => e.type)).toEqual(["agent_start", "agent_error"]);
    expect(sink.events[1]!.payload["error_type"]).toBe("TypeError");
  });
});

describe("mesh.run", () => {
  it("groups sync calls under one run_id", () => {
    const a = mesh.agent("a", () => "a");
    const b = mesh.agent("b", () => "b");

    mesh.run("run-xyz", (rid) => {
      expect(rid).toBe("run-xyz");
      a();
      b();
    });

    expect(sink.events).toHaveLength(4);
    expect(new Set(sink.events.map((e) => e.run_id))).toEqual(new Set(["run-xyz"]));
    expect(sink.events.map((e) => [e.agent, e.type])).toEqual([
      ["a", "agent_start"],
      ["a", "agent_end"],
      ["b", "agent_start"],
      ["b", "agent_end"],
    ]);
  });

  it("applies to async calls across awaits", async () => {
    const a = mesh.agent("async-a", async () => {
      await Promise.resolve();
      return "a";
    });

    await mesh.run("run-async", async () => {
      await a();
      await a();
    });

    expect(sink.events).toHaveLength(4);
    expect(new Set(sink.events.map((e) => e.run_id))).toEqual(new Set(["run-async"]));
  });

  it("generates a run_id when omitted", () => {
    const rid = mesh.run(undefined, (runId) => runId);
    expect(typeof rid).toBe("string");
    expect(rid.length).toBeGreaterThan(0);
  });

  it("calls outside a run get distinct run_ids", () => {
    const solo = mesh.agent("solo", () => null);
    solo();
    solo();

    expect(sink.events).toHaveLength(4);
    expect(new Set(sink.events.map((e) => e.run_id)).size).toBe(2);
  });

  it("currentRunId resets after the run", () => {
    mesh.run("run-1", () => {
      expect(mesh.currentRunId()).toBe("run-1");
    });
    expect(mesh.currentRunId()).toBeUndefined();
  });
});

describe("span nesting", () => {
  it("links nested sync calls parent → child", () => {
    const inner = mesh.agent("inner", () => "i");
    const outer = mesh.agent("outer", () => inner());

    mesh.run("run-spans", () => outer());

    const [outerStart, innerStart, innerEnd, outerEnd] = sink.events;
    expect(sink.events.map((e) => [e.agent, e.type])).toEqual([
      ["outer", "agent_start"],
      ["inner", "agent_start"],
      ["inner", "agent_end"],
      ["outer", "agent_end"],
    ]);
    expect(outerStart!.payload["span_id"]).toBeTruthy();
    expect(outerStart!.payload["parent_span_id"]).toBeUndefined(); // root span
    expect(innerStart!.payload["parent_span_id"]).toBe(outerStart!.payload["span_id"]);
    expect(innerEnd!.payload["span_id"]).toBe(innerStart!.payload["span_id"]);
    expect(outerEnd!.payload["span_id"]).toBe(outerStart!.payload["span_id"]);
  });

  it("links nested async calls parent → child", async () => {
    const inner = mesh.agent("inner", async () => {
      await Promise.resolve();
      return "i";
    });
    const outer = mesh.agent("outer", async () => {
      await Promise.resolve();
      return inner();
    });

    await mesh.run("run-async-spans", () => outer());

    const starts = sink.events.filter((e) => e.type === "agent_start");
    const outerStart = starts.find((e) => e.agent === "outer")!;
    const innerStart = starts.find((e) => e.agent === "inner")!;
    expect(innerStart.payload["parent_span_id"]).toBe(outerStart.payload["span_id"]);
  });

  it("error events carry the span_id of their start", () => {
    const boom = mesh.agent("flaky", () => {
      throw new Error("kaput");
    });
    expect(() => boom()).toThrow("kaput");
    expect(sink.events[1]!.payload["span_id"]).toBe(sink.events[0]!.payload["span_id"]);
  });
});
