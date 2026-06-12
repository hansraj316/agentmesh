import { describe, expect, it } from "vitest";

import { newEvent, utcNowIso, validateEvent } from "../src/events.js";

function valid(): Record<string, unknown> {
  return {
    event_id: "e-1",
    run_id: "run-1",
    agent: "researcher",
    type: "agent_start",
    ts: "2026-06-10T08:15:30.123456+00:00",
    payload: {},
  };
}

describe("validateEvent", () => {
  it("accepts a minimal valid event", () => {
    expect(validateEvent(valid())).toEqual(valid());
  });

  it("accepts a trailing-Z UTC timestamp", () => {
    expect(() => validateEvent({ ...valid(), ts: "2026-06-10T08:15:30.123Z" })).not.toThrow();
  });

  it("accepts all known event types", () => {
    for (const type of ["agent_start", "agent_end", "agent_error", "message", "tool_call"]) {
      expect(() => validateEvent({ ...valid(), type })).not.toThrow();
    }
  });

  it("accepts span fields as non-empty strings", () => {
    const event = { ...valid(), payload: { span_id: "s-1", parent_span_id: "s-0" } };
    expect(() => validateEvent(event)).not.toThrow();
  });

  it("accepts a non-empty payload object", () => {
    const event = { ...valid(), payload: { function: "work", duration_seconds: 1.5 } };
    expect(() => validateEvent(event)).not.toThrow();
  });

  it.each(["event_id", "run_id", "agent", "type", "ts", "payload"])(
    "rejects a missing %s",
    (field) => {
      const event = valid();
      delete event[field];
      expect(() => validateEvent(event)).toThrow(field);
    },
  );

  it.each(["event_id", "run_id", "agent"])("rejects an empty %s", (field) => {
    expect(() => validateEvent({ ...valid(), [field]: "" })).toThrow(field);
  });

  it("rejects an unknown type", () => {
    expect(() => validateEvent({ ...valid(), type: "agent_paused" })).toThrow(/type/);
  });

  it("rejects a non-object event", () => {
    expect(() => validateEvent("nope")).toThrow(/object/);
    expect(() => validateEvent(null)).toThrow(/object/);
  });

  it.each([
    "not-a-date",
    "2026-06-10T08:15:30", // naive — no timezone
    "2026-06-10T08:15:30+02:00", // non-zero UTC offset
    "2026-06-10", // date only
    12345,
  ])("rejects invalid ts %s", (ts) => {
    expect(() => validateEvent({ ...valid(), ts })).toThrow(/ts/);
  });

  it.each([[["a"]], ["text"], [null], [42]])("rejects non-object payload %s", (payload) => {
    expect(() => validateEvent({ ...valid(), payload })).toThrow(/payload/);
  });

  it("rejects empty or non-string span fields", () => {
    expect(() => validateEvent({ ...valid(), payload: { span_id: "" } })).toThrow(/span_id/);
    expect(() => validateEvent({ ...valid(), payload: { parent_span_id: 7 } })).toThrow(
      /parent_span_id/,
    );
  });
});

describe("validateEvent — AMP v0.3 usage fields", () => {
  it.each(["agent_end", "tool_call"])("accepts valid usage fields on %s", (type) => {
    const payload = { input_tokens: 0, output_tokens: 12, cost_usd: 0.0, model: "m-1" };
    expect(() => validateEvent({ ...valid(), type, payload })).not.toThrow();
  });

  it("absent usage keys are fine — v0.2 events validate unchanged", () => {
    const event = {
      ...valid(),
      type: "agent_end",
      payload: { duration_seconds: 0.5, span_id: "sp-1" },
    };
    expect(() => validateEvent(event)).not.toThrow();
  });

  it.each([
    [{ input_tokens: "100" }, /input_tokens/],
    [{ input_tokens: -1 }, /input_tokens/],
    [{ input_tokens: true }, /input_tokens/], // bool rejected where an int is expected
    [{ input_tokens: 1.5 }, /input_tokens/],
    [{ output_tokens: 1.5 }, /output_tokens/],
    [{ output_tokens: false }, /output_tokens/],
    [{ cost_usd: "0.01" }, /cost_usd/],
    [{ cost_usd: -0.01 }, /cost_usd/],
    [{ cost_usd: true }, /cost_usd/],
    [{ cost_usd: Number.NaN }, /cost_usd/],
    [{ cost_usd: Number.POSITIVE_INFINITY }, /cost_usd/],
    [{ model: "" }, /model/],
    [{ model: 42 }, /model/],
  ])("rejects invalid usage payload %j when present", (payload, match) => {
    for (const type of ["agent_end", "tool_call"]) {
      expect(() => validateEvent({ ...valid(), type, payload: { ...payload } })).toThrow(match);
    }
  });

  it("valid partial combos are accepted", () => {
    const combos = [
      { cost_usd: 0.02 },
      { input_tokens: 5, model: "m-1" },
      { output_tokens: 3, cost_usd: 1 }, // an int cost is a fine number
    ];
    for (const payload of combos) {
      expect(() => validateEvent({ ...valid(), type: "agent_end", payload })).not.toThrow();
    }
  });

  it("usage keys on other event types stay free-form", () => {
    // The spec defines usage fields on agent_end/tool_call only.
    const event = { ...valid(), type: "message", payload: { input_tokens: "free-form" } };
    expect(() => validateEvent(event)).not.toThrow();
  });
});

describe("newEvent", () => {
  it("builds a valid event with fresh id and current UTC ts", () => {
    const event = newEvent("run-9", "writer", "message", { text: "hi" });
    expect(() => validateEvent(event)).not.toThrow();
    expect(event.run_id).toBe("run-9");
    expect(event.agent).toBe("writer");
    expect(event.payload).toEqual({ text: "hi" });
    expect(event.event_id).not.toBe(newEvent("run-9", "writer", "message", {}).event_id);
  });

  it("utcNowIso ends with Z", () => {
    expect(utcNowIso().endsWith("Z")).toBe(true);
  });
});
