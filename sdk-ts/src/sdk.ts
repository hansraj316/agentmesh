/**
 * AgentMesh TypeScript SDK — mirror of the Python `mesh.agent` decorator,
 * `mesh.run` grouping, and `mesh.record_usage` token/cost accounting
 * (agentmesh/sdk.py).
 *
 * Wrapped agents emit `agent_start` before the call, `agent_end` (with
 * `duration_seconds`) on success, and `agent_error` (with exception info,
 * re-thrown unchanged) on failure. Each invocation gets a fresh `span_id`;
 * nested wrapped calls record the enclosing invocation's span as
 * `parent_span_id` (AMP v0.2). Usage recorded via `mesh.recordUsage` inside
 * the call is merged into the `agent_end` payload (AMP v0.3). Context
 * propagates through sync and async calls via AsyncLocalStorage.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";

import { newEvent, type EventType } from "./events.js";
import type { Sink } from "./sink.js";

interface MeshContext {
  runId: string | undefined;
  spanId: string | undefined;
  /** Per-span usage accumulator; present only inside a `mesh.agent` span. */
  usage: Record<string, unknown> | undefined;
}

/** Token/cost usage for `mesh.recordUsage` (camelCase in the API; emitted
 * snake_case on the wire, mirroring Python's keyword arguments). */
export interface UsageRecord {
  inputTokens?: number;
  outputTokens?: number;
  costUsd?: number;
  model?: string;
}

const context = new AsyncLocalStorage<MeshContext>();

function isPromiseLike(value: unknown): value is PromiseLike<unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { then?: unknown }).then === "function"
  );
}

function errorPayload(exc: unknown): { error: string; error_type: string } {
  if (exc instanceof Error) {
    return { error: exc.message, error_type: exc.constructor.name };
  }
  return { error: String(exc), error_type: typeof exc };
}

/** Entry point for instrumenting agents. Events go to the given sink. */
export class Mesh {
  private readonly sink: Sink;

  constructor(sink: Sink) {
    this.sink = sink;
  }

  private emit(
    runId: string,
    agent: string,
    type: EventType,
    payload: Record<string, unknown>,
  ): void {
    this.sink.emit(newEvent(runId, agent, type, payload));
  }

  /** Group all wrapped calls inside `fn` under one run_id (mirrors `mesh.run`). */
  run<T>(runId: string | undefined, fn: (runId: string) => T): T {
    const rid = runId ?? randomUUID();
    const parent = context.getStore();
    return context.run({ runId: rid, spanId: parent?.spanId, usage: parent?.usage }, () =>
      fn(rid),
    );
  }

  /** The run_id of the enclosing `mesh.run`, if any. */
  currentRunId(): string | undefined {
    return context.getStore()?.runId;
  }

  /**
   * Accumulate token/cost usage onto the current `mesh.agent` span
   * (mirrors Python's `mesh.record_usage`).
   *
   * The accumulated fields are merged into the span's `agent_end` payload
   * as `input_tokens` / `output_tokens` / `cost_usd` / `model` (AMP v0.3).
   * Multiple calls within one span sum the token and cost numbers; for
   * `model` the last non-null value wins. Nested spans are isolated — a
   * child's usage never leaks onto its parent. Outside any span this is a
   * no-op that emits a `console.warn` per call.
   */
  recordUsage(usage: UsageRecord): void {
    const current = context.getStore()?.usage;
    if (current === undefined) {
      console.warn(
        "mesh.recordUsage() called outside any mesh.agent span; usage dropped",
      );
      return;
    }
    if (usage.inputTokens != null) {
      current["input_tokens"] = ((current["input_tokens"] as number) ?? 0) + usage.inputTokens;
    }
    if (usage.outputTokens != null) {
      current["output_tokens"] =
        ((current["output_tokens"] as number) ?? 0) + usage.outputTokens;
    }
    if (usage.costUsd != null) {
      current["cost_usd"] = ((current["cost_usd"] as number) ?? 0) + usage.costUsd;
    }
    if (usage.model != null) {
      current["model"] = usage.model;
    }
  }

  /**
   * Instrument a sync or async function as a named agent.
   *
   * Calls outside `mesh.run` each get their own fresh run_id, like the
   * Python SDK. For async functions the terminal event is emitted when the
   * returned promise settles.
   */
  agent<Args extends unknown[], R>(name: string, fn: (...args: Args) => R): (...args: Args) => R {
    const functionName = fn.name !== "" ? fn.name : name;
    const mesh = this;
    return function wrapped(...args: Args): R {
      const enclosing = context.getStore();
      const runId = enclosing?.runId ?? randomUUID();
      const spanId = randomUUID();
      const span: Record<string, string> = { span_id: spanId };
      if (enclosing?.spanId !== undefined) {
        span["parent_span_id"] = enclosing.spanId;
      }
      const usage: Record<string, unknown> = {};
      const started = Date.now();
      const end = (): void => {
        mesh.emit(runId, name, "agent_end", {
          function: functionName,
          duration_seconds: (Date.now() - started) / 1000,
          ...usage,
          ...span,
        });
      };
      const error = (exc: unknown): void => {
        mesh.emit(runId, name, "agent_error", {
          function: functionName,
          duration_seconds: (Date.now() - started) / 1000,
          ...errorPayload(exc),
          ...span,
        });
      };

      mesh.emit(runId, name, "agent_start", { function: functionName, ...span });
      let result: R;
      try {
        result = context.run({ runId, spanId, usage }, () => fn(...args));
      } catch (exc) {
        error(exc);
        throw exc;
      }
      if (isPromiseLike(result)) {
        return (result as PromiseLike<unknown>).then(
          (value) => {
            end();
            return value;
          },
          (exc: unknown) => {
            error(exc);
            throw exc;
          },
        ) as R;
      }
      end();
      return result;
    };
  }
}
