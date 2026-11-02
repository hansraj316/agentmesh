export type PillStatus = "running" | "done" | "failed" | "ok" | "idle" | string;

const LABELS: Record<string, string> = {
  running: "Running",
  done: "Done",
  ok: "OK",
  failed: "Failed",
  idle: "Idle",
};

/** The stageshow status pill: spinner while running, green done, red failed. */
export function StatusPill({ status }: { status: PillStatus }) {
  const tone =
    status === "done" || status === "ok"
      ? "ok"
      : status === "failed"
        ? "failed"
        : status === "running"
          ? "running"
          : "idle";
  return (
    <span className={`pill pill--${tone}`} key={status}>
      {tone === "running" ? (
        <svg className="spin" width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
          <circle cx="4" cy="4" r="3.2" fill="none" stroke="currentColor" strokeOpacity=".22" strokeWidth="1.4" />
          <path d="M4 .8 A3.2 3.2 0 0 1 7.2 4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      ) : (
        <span className="pill__dot" aria-hidden="true" />
      )}
      {LABELS[status] ?? status}
    </span>
  );
}
