/**
 * Event sinks: where emitted AMP events go.
 *
 * The v1 bridge to the Python store is a JSONL file — emit events with
 * JsonlFileSink, then import them with `agentmesh import-jsonl PATH`.
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

/** Collects events in memory; for tests. */
export class MemorySink implements Sink {
  readonly events: AmpEvent[] = [];

  emit(event: AmpEvent): void {
    this.events.push(event);
  }
}
