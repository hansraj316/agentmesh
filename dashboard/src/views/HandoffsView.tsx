import { useRef } from "react";
import { elapsedSeconds, formatAgo, formatDuration } from "../lib/format";
import type { Handoff } from "../lib/types";
import { AvatarPair } from "./Avatar";
import { StatusPill } from "./StatusPill";

export interface HandoffsViewProps {
  handoffs: Handoff[];
  now: number;
  onOpenRun?: (runId: string) => void;
  /** Set once the first batch has rendered; rows that arrive later animate in. */
  ready?: boolean;
}

const keyOf = (h: Handoff) => `${h.run_id}/${h.task_id}`;

export function HandoffsView({ handoffs, now, onOpenRun, ready = true }: HandoffsViewProps) {
  // Rows present on the first render settle in place; later ones slide in.
  const seen = useRef<Set<string> | null>(null);
  if (seen.current === null && ready) seen.current = new Set(handoffs.map(keyOf));

  if (handoffs.length === 0) {
    return (
      <div className="empty">
        <p className="empty__title">No handoffs yet</p>
        <p className="empty__hint">
          Wrap a delegation in <code>mesh.handoff(task, to=…)</code> and it shows up here live.
        </p>
      </div>
    );
  }

  return (
    <ol className="rows" aria-label="Handoffs, newest first">
      {handoffs.map((h) => {
        const k = keyOf(h);
        // The class stays on; CSS plays the entrance once, when the row mounts.
        const fresh = seen.current !== null && !seen.current.has(k);
        const running = h.status === "running";
        const elapsed = elapsedSeconds(h, now);
        return (
          <li key={k} className={"row-wrap" + (fresh ? " row-wrap--enter" : "")}>
            <button
              type="button"
              className={`row row--${h.status}`}
              onClick={() => onOpenRun?.(h.run_id)}
              aria-label={`${h.task}, ${h.from} to ${h.to}, ${h.status}, ${formatDuration(elapsed)}. Open run ${h.run_id}`}
            >
              <AvatarPair from={h.from} to={h.to} active={running} />
              <span className="row__main">
                <span className="row__title">{h.task}</span>
                <span className="row__sub">
                  {h.from}
                  <span className="row__arrow" aria-hidden="true">
                    →
                  </span>
                  {h.to}
                  <span className="row__ago">{formatAgo(h.started_at, now)}</span>
                  <span className="row__run">{h.run_id}</span>
                </span>
                {h.error ? <span className="row__error">{h.error}</span> : null}
              </span>
              <StatusPill status={h.status} />
              <span className="row__timer" data-testid="timer">
                {formatDuration(elapsed)}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
