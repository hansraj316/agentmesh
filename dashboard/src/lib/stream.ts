import type { ConnectionState, StreamEvent } from "./types";

/**
 * Client for the daemon's live event contract (BUILD_PLAN, P2):
 *
 * - `GET /api/events?after=<id>&limit=<n>` returns stored events as a JSON
 *   array; the client pages through it once for a fast first render;
 * - `GET /api/stream?after=<id>` is Server-Sent Events, one `data:` line per
 *   event, each JSON object carrying the store's row `id`;
 * - on any stream error the client closes the connection and reconnects with
 *   the last `id` it saw, backing off from 1 s up to 15 s, so no event is
 *   missed or shown twice.
 */

/** The parts of the browser EventSource the client uses (injectable in tests). */
export interface EventSourceLike {
  onopen: ((ev: Event) => unknown) | null;
  onmessage: ((ev: MessageEvent) => unknown) | null;
  onerror: ((ev: Event) => unknown) | null;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
type FetchLike = (url: string) => Promise<Pick<Response, "ok" | "status" | "json">>;

export interface StreamOptions {
  baseUrl?: string;
  pageSize?: number;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  fetch?: FetchLike;
  eventSource?: EventSourceFactory;
  setTimeout?: (fn: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
}

export type EventsHandler = (events: StreamEvent[]) => void;
export type StateHandler = (state: ConnectionState) => void;

export class LiveStream {
  private lastId = 0;
  private source: EventSourceLike | null = null;
  private timer: unknown = null;
  private backoffMs: number;
  private stopped = true;
  private readonly opts: Required<Omit<StreamOptions, "baseUrl">> & { baseUrl: string };

  constructor(options: StreamOptions = {}) {
    this.opts = {
      baseUrl: options.baseUrl ?? "",
      pageSize: options.pageSize ?? 1000,
      initialBackoffMs: options.initialBackoffMs ?? 1000,
      maxBackoffMs: options.maxBackoffMs ?? 15000,
      fetch: options.fetch ?? ((url) => globalThis.fetch(url)),
      eventSource: options.eventSource ?? ((url) => new EventSource(url)),
      setTimeout: options.setTimeout ?? ((fn, ms) => globalThis.setTimeout(fn, ms)),
      clearTimeout:
        options.clearTimeout ??
        ((h) => globalThis.clearTimeout(h as ReturnType<typeof globalThis.setTimeout>)),
    };
    this.backoffMs = this.opts.initialBackoffMs;
  }

  /** The last event id delivered; reconnects resume after it. */
  get cursor(): number {
    return this.lastId;
  }

  start(onEvents: EventsHandler, onState: StateHandler): void {
    this.stopped = false;
    this.onEvents = onEvents;
    this.onState = onState;
    onState("connecting");
    void this.backfill().then(() => this.open());
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) this.opts.clearTimeout(this.timer);
    this.timer = null;
    this.source?.close();
    this.source = null;
  }

  private onEvents: EventsHandler = () => {};
  private onState: StateHandler = () => {};

  /** Keep only events newer than the cursor, in id order, and advance it. */
  private accept(events: StreamEvent[]): StreamEvent[] {
    const fresh: StreamEvent[] = [];
    for (const event of [...events].sort((a, b) => a.id - b.id)) {
      if (typeof event.id !== "number" || event.id <= this.lastId) continue;
      this.lastId = event.id;
      fresh.push(event);
    }
    return fresh;
  }

  private async backfill(): Promise<void> {
    // Best effort: when the page endpoint fails, the stream itself replays
    // every stored event after the cursor.
    for (let pages = 0; pages < 1000 && !this.stopped; pages++) {
      let page: StreamEvent[];
      try {
        const res = await this.opts.fetch(
          `${this.opts.baseUrl}/api/events?after=${this.lastId}&limit=${this.opts.pageSize}`,
        );
        if (!res.ok) return;
        const body = (await res.json()) as unknown;
        if (!Array.isArray(body)) return;
        page = body as StreamEvent[];
      } catch {
        return;
      }
      const fresh = this.accept(page);
      if (fresh.length) this.onEvents(fresh);
      if (page.length < this.opts.pageSize || fresh.length === 0) return;
    }
  }

  private open(): void {
    if (this.stopped) return;
    const source = this.opts.eventSource(`${this.opts.baseUrl}/api/stream?after=${this.lastId}`);
    this.source = source;
    source.onopen = () => {
      if (this.source !== source) return;
      this.backoffMs = this.opts.initialBackoffMs;
      this.onState("live");
    };
    source.onmessage = (ev: MessageEvent) => {
      if (this.source !== source) return;
      let event: StreamEvent;
      try {
        event = JSON.parse(String(ev.data)) as StreamEvent;
      } catch {
        return; // a malformed line never stops the stream
      }
      const fresh = this.accept([event]);
      if (fresh.length) this.onEvents(fresh);
    };
    source.onerror = () => {
      if (this.source !== source) return;
      // Close so the browser does not retry with the stale `after` cursor;
      // reconnect ourselves from the last id we saw.
      source.close();
      this.source = null;
      if (this.stopped) return;
      this.onState("reconnecting");
      const delay = this.backoffMs;
      this.backoffMs = Math.min(this.backoffMs * 2, this.opts.maxBackoffMs);
      this.timer = this.opts.setTimeout(() => {
        this.timer = null;
        this.open();
      }, delay);
    };
  }
}
