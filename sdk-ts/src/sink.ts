/**
 * Event sinks: where emitted AMP events go.
 *
 * The v1 bridge to the Python store is a JSONL file — emit events with
 * JsonlFileSink, then import them with `agentmesh import-jsonl PATH`.
 * HttpSink instead POSTs each event to a running `agentmesh serve` daemon
 * (POST /api/events).
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

import type { AmpEvent } from "./events.js";

/** Destination for emitted events. */
export interface Sink {
  emit(event: AmpEvent): void;
}

/**
 * Appends one JSON line per event to a file (created on first emit;
 * parent directories are created eagerly). Synchronous v1 — one
 * appendFileSync call per event, so lines are never interleaved within
 * a process.
 */
export class JsonlFileSink implements Sink {
  readonly path: string;

  constructor(path: string) {
    this.path = path;
    const parent = dirname(path);
    if (parent !== "" && parent !== ".") {
      mkdirSync(parent, { recursive: true });
    }
  }

  emit(event: AmpEvent): void {
    appendFileSync(this.path, JSON.stringify(event) + "\n");
  }
}

/**
 * POSTs each event as JSON to a webhook URL — typically the agentmesh serve
 * daemon's `POST /api/events` route. One fetch per event (no batching in
 * v1); a non-2xx response or network failure surfaces as a thrown error
 * (an async rejection from `emit`).
 */
export class HttpSink implements Sink {
  readonly url: string;

  constructor(url: string) {
    this.url = url;
  }

  async emit(event: AmpEvent): Promise<void> {
    const response = await fetch(this.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST ${this.url} failed with status ${response.status}${detail ? `: ${detail}` : ""}`,
      );
    }
  }
}

/** Collects events in memory; for tests. */
export class MemorySink implements Sink {
  readonly events: AmpEvent[] = [];

  emit(event: AmpEvent): void {
    this.events.push(event);
  }
}
