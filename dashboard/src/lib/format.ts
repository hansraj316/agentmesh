/** Elapsed time the way the stageshow shows it: 0s, 23s, 1m 05s, 2h 03m. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

/** Relative age of a timestamp: Just now, 12s ago, 4m ago, 3h ago, 2d ago. */
export function formatAgo(ts: string, now: number): string {
  const then = Date.parse(ts);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.floor((now - then) / 1000));
  if (s < 5) return "Just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function formatCost(usd: number | null | undefined): string {
  if (usd == null) return "—";
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Live seconds for a running handoff, or its settled duration. */
export function elapsedSeconds(
  h: { started_at: string; duration_seconds: number | null; ended_at: string | null },
  now: number,
): number | null {
  if (h.ended_at !== null) return h.duration_seconds;
  const started = Date.parse(h.started_at);
  return Number.isNaN(started) ? null : Math.max(0, (now - started) / 1000);
}
