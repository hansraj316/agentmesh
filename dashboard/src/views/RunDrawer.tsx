import { useEffect, useRef, useState } from "react";
import type { Backend } from "../lib/backend";
import { elapsedSeconds, formatCost, formatDuration, formatTokens } from "../lib/format";
import type { Handoff, RunTrace, Span } from "../lib/types";
import { StatusPill } from "./StatusPill";

export interface RunDrawerProps {
  backend: Backend;
  runId: string;
  handoffs: Handoff[];
  /** Bumps when this run gets new events, so the trace refetches. */
  version: number;
  now: number;
  onClose: () => void;
}

function total(spans: Span[], pick: (s: Span) => number | null): number | null {
  let sum: number | null = null;
  const walk = (list: Span[]) => {
    for (const s of list) {
      const v = pick(s);
      if (v !== null) sum = (sum ?? 0) + v;
      walk(s.children);
    }
  };
  walk(spans);
  return sum;
}

function SpanNode({ span, depth }: { span: Span; depth: number }) {
  const tokens =
    span.input_tokens !== null || span.output_tokens !== null
      ? `${formatTokens(span.input_tokens)} in · ${formatTokens(span.output_tokens)} out`
      : null;
  return (
    <li>
      <div className={`span span--${span.status}`} style={{ paddingLeft: 12 + depth * 18 }}>
        <span className="span__dot" aria-hidden="true" />
        <span className="span__agent">{span.agent}</span>
        <span className="span__meta">
          {[tokens, span.cost_usd !== null ? formatCost(span.cost_usd) : null, span.model]
            .filter(Boolean)
            .join(" · ")}
        </span>
        <span className="span__time">
          {span.status === "running" ? "running" : formatDuration(span.duration_seconds)}
        </span>
      </div>
      {span.error ? (
        <div className="span__error" style={{ paddingLeft: 30 + depth * 18 }}>
          {span.error_type ? `${span.error_type}: ` : ""}
          {span.error}
        </div>
      ) : null}
      {span.children.length ? (
        <ul className="spans">
          {span.children.map((child, i) => (
            <SpanNode key={child.span_id ?? i} span={child} depth={depth + 1} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function RunDrawer({ backend, runId, handoffs, version, now, onClose }: RunDrawerProps) {
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    backend
      .run(runId)
      .then((data) => {
        if (cancelled) return;
        setTrace(data);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [backend, runId, version]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const runHandoffs = handoffs.filter((h) => h.run_id === runId);
  const cost = trace ? total(trace.roots, (s) => s.cost_usd) : null;
  const tokensIn = trace ? total(trace.roots, (s) => s.input_tokens) : null;
  const tokensOut = trace ? total(trace.roots, (s) => s.output_tokens) : null;

  return (
    <div className="drawer-layer">
      <div className="drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`Run ${runId}`}>
        <header className="drawer__head">
          <div>
            <p className="eyebrow">Run</p>
            <h2 className="drawer__title">{runId}</h2>
          </div>
          <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="Close run">
            ×
          </button>
        </header>

        {error && !trace ? <p className="empty empty--error">Could not load this run: {error}</p> : null}
        {!trace && !error ? <p className="empty">Loading run…</p> : null}

        {trace ? (
          <>
            <dl className="stats">
              <div>
                <dt>Spans</dt>
                <dd>{trace.span_count}</dd>
              </div>
              <div>
                <dt>Failed</dt>
                <dd className={trace.failed_count ? "bad" : undefined}>{trace.failed_count}</dd>
              </div>
              <div>
                <dt>Time</dt>
                <dd>{formatDuration(trace.total_seconds)}</dd>
              </div>
              <div>
                <dt>Cost</dt>
                <dd>{formatCost(cost)}</dd>
              </div>
              <div>
                <dt>Tokens</dt>
                <dd>
                  {tokensIn === null && tokensOut === null
                    ? "—"
                    : `${formatTokens(tokensIn)} / ${formatTokens(tokensOut)}`}
                </dd>
              </div>
            </dl>

            {runHandoffs.length ? (
              <section className="drawer__section">
                <h3>Handoffs</h3>
                <ul className="mini-handoffs">
                  {runHandoffs.map((h) => (
                    <li key={h.task_id}>
                      <span className="mini-handoffs__task">{h.task}</span>
                      <span className="muted">
                        {h.from} → {h.to}
                      </span>
                      <StatusPill status={h.status} />
                      <span className="row__timer">{formatDuration(elapsedSeconds(h, now))}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            <section className="drawer__section">
              <h3>Span tree</h3>
              {trace.roots.length ? (
                <ul className="spans">
                  {trace.roots.map((s, i) => (
                    <SpanNode key={s.span_id ?? i} span={s} depth={0} />
                  ))}
                </ul>
              ) : (
                <p className="muted">This run has no agent spans yet.</p>
              )}
            </section>

            {trace.annotations?.length ? (
              <section className="drawer__section">
                <h3>Annotations</h3>
                <ul className="notes">
                  {trace.annotations.map((n, i) => (
                    <li key={i}>
                      <span>{n.text}</span>
                      <span className="muted">
                        {n.author ? `${n.author} · ` : ""}
                        {new Date(n.ts).toLocaleString()}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </>
        ) : null}
      </aside>
    </div>
  );
}
