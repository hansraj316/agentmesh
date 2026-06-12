import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { validateEvent } from "../src/events.js";
import { Mesh } from "../src/sdk.js";
import { HttpSink, JsonlFileSink, MemorySink } from "../src/sink.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "agentmesh-sdk-ts-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("JsonlFileSink", () => {
  it("writes one parseable, AMP-valid JSON line per event", () => {
    const path = join(dir, "events.jsonl");
    const mesh = new Mesh(new JsonlFileSink(path));
    const work = mesh.agent("researcher", (x: number) => x * 2);

    mesh.run("run-jsonl", () => {
      work(2);
      work(3);
    });

    const lines = readFileSync(path, "utf-8").split("\n").filter(Boolean);
    expect(lines).toHaveLength(4);
    const events = lines.map((line) => validateEvent(JSON.parse(line)));
    expect(events.map((e) => e.type)).toEqual([
      "agent_start",
      "agent_end",
      "agent_start",
      "agent_end",
    ]);
    expect(new Set(events.map((e) => e.run_id))).toEqual(new Set(["run-jsonl"]));
  });

  it("writes AMP v0.3 usage fields that validate from the wire", () => {
    const path = join(dir, "usage.jsonl");
    const mesh = new Mesh(new JsonlFileSink(path));
    const work = mesh.agent("researcher", () => {
      mesh.recordUsage({ inputTokens: 1000, outputTokens: 200, costUsd: 0.0034, model: "m-1" });
      return "ok";
    });

    mesh.run("run-usage-jsonl", () => work());

    const lines = readFileSync(path, "utf-8").split("\n").filter(Boolean);
    const events = lines.map((line) => validateEvent(JSON.parse(line)));
    const end = events.find((e) => e.type === "agent_end")!;
    expect(end.payload["input_tokens"]).toBe(1000);
    expect(end.payload["output_tokens"]).toBe(200);
    expect(end.payload["cost_usd"]).toBeCloseTo(0.0034);
    expect(end.payload["model"]).toBe("m-1");
    // snake_case on the wire, exactly as the Python importer expects
    const raw = JSON.parse(lines[lines.length - 1]!) as { payload: Record<string, unknown> };
    expect(Object.keys(raw.payload)).toEqual(
      expect.arrayContaining(["input_tokens", "output_tokens", "cost_usd", "model"]),
    );
  });

  it("creates missing parent directories", () => {
    const path = join(dir, "nested", "deeper", "events.jsonl");
    const sink = new JsonlFileSink(path);
    sink.emit({
      event_id: "e-1",
      run_id: "r-1",
      agent: "a",
      type: "message",
      ts: "2026-06-10T00:00:00+00:00",
      payload: {},
    });
    expect(JSON.parse(readFileSync(path, "utf-8").trim()).event_id).toBe("e-1");
  });

  it("appends across sink instances", () => {
    const path = join(dir, "events.jsonl");
    const event = {
      event_id: "e-1",
      run_id: "r-1",
      agent: "a",
      type: "message" as const,
      ts: "2026-06-10T00:00:00+00:00",
      payload: {},
    };
    new JsonlFileSink(path).emit(event);
    new JsonlFileSink(path).emit({ ...event, event_id: "e-2" });
    const lines = readFileSync(path, "utf-8").split("\n").filter(Boolean);
    expect(lines.map((line) => JSON.parse(line).event_id)).toEqual(["e-1", "e-2"]);
  });
});

describe("HttpSink", () => {
  const event = {
    event_id: "e-http-1",
    run_id: "r-1",
    agent: "a",
    type: "message" as const,
    ts: "2026-06-10T00:00:00+00:00",
    payload: {},
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs the event as JSON and surfaces non-2xx as thrown errors", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 202, text: async () => "" }));
    vi.stubGlobal("fetch", fetchMock);

    const sink = new HttpSink("http://127.0.0.1:7777/api/events");
    await sink.emit(event);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      { method: string; headers: Record<string, string>; body: string },
    ];
    expect(url).toBe("http://127.0.0.1:7777/api/events");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body)).toEqual(event);

    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 400,
      text: async () => '{"error":"invalid event"}',
    });
    await expect(sink.emit(event)).rejects.toThrow(
      'POST http://127.0.0.1:7777/api/events failed with status 400: {"error":"invalid event"}',
    );
  });
});

describe("MemorySink", () => {
  it("collects emitted events in order", () => {
    const sink = new MemorySink();
    const mesh = new Mesh(sink);
    mesh.agent("a", () => 1)();
    expect(sink.events.map((e) => e.type)).toEqual(["agent_start", "agent_end"]);
  });
});
