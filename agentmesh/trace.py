"""Run-level tracing: rebuild a span tree from stored AMP events and render it."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from agentmesh.events import Event, _parse_ts
from agentmesh.store import EventStore

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"

_STATUS_MARK = {
    STATUS_OK: "✓",
    STATUS_FAILED: "✗",
    STATUS_RUNNING: "…",
}

_TERMINAL_TYPES = frozenset({"agent_end", "agent_error"})


@dataclass
class Span:
    """One agent invocation inside a run, with its child spans."""

    agent: str
    status: str
    started_ts: str
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    model: Optional[str] = None
    children: List["Span"] = field(default_factory=list)


@dataclass
class Trace:
    """The spans of one run, arranged as a tree."""

    run_id: str
    roots: List[Span]
    span_count: int
    failed_count: int
    total_seconds: float


def build_trace(store: EventStore, run_id: str) -> Trace:
    """Reconstruct the span tree of one run from its stored events.

    Start and terminal events are paired by ``span_id`` when present
    (AMP v0.2); v0.1 events without span ids fall back to per-agent pairing
    in event order. Spans without a terminal event stay ``running``. Roots
    are spans with no (or unknown) ``parent_span_id``.

    Raises ValueError if the store has no events for ``run_id``.
    """
    events = [event for _, event in store.events_after(0, run_id=run_id)]
    if not events:
        raise ValueError("no events found for run_id %r" % run_id)

    spans: List[Span] = []
    by_span_id: Dict[str, Span] = {}
    open_v01: Dict[str, List[Span]] = {}  # agent -> open spans without span ids
    for event in events:
        if event.type == "agent_start":
            span = Span(
                agent=event.agent,
                status=STATUS_RUNNING,
                started_ts=event.ts,
                span_id=_payload_str(event, "span_id"),
                parent_span_id=_payload_str(event, "parent_span_id"),
            )
            spans.append(span)
            if span.span_id is not None:
                by_span_id[span.span_id] = span
            else:
                open_v01.setdefault(event.agent, []).append(span)
        elif event.type in _TERMINAL_TYPES:
            span = _match_open_span(event, by_span_id, open_v01)
            if span is not None:
                _close_span(span, event)
        elif event.type == "tool_call":
            span_id = _payload_str(event, "span_id")
            span = by_span_id.get(span_id) if span_id is not None else None
            if span is not None:
                _add_usage(span, event)

    roots: List[Span] = []
    for span in spans:
        parent = by_span_id.get(span.parent_span_id) if span.parent_span_id else None
        if parent is not None and parent is not span:
            parent.children.append(span)
        else:
            roots.append(span)

    return Trace(
        run_id=run_id,
        roots=roots,
        span_count=len(spans),
        failed_count=sum(1 for span in spans if span.status == STATUS_FAILED),
        total_seconds=(_parse_ts(events[-1].ts) - _parse_ts(events[0].ts)).total_seconds(),
    )


def _match_open_span(
    event: Event,
    by_span_id: Dict[str, Span],
    open_v01: Dict[str, List[Span]],
) -> Optional[Span]:
    """Find the open span this terminal event closes, or None if unmatched."""
    span_id = _payload_str(event, "span_id")
    if span_id is not None:
        span = by_span_id.get(span_id)
        if span is not None and span.status == STATUS_RUNNING:
            return span
        return None
    stack = open_v01.get(event.agent)
    return stack.pop() if stack else None


def _close_span(span: Span, event: Event) -> None:
    span.status = STATUS_FAILED if event.type == "agent_error" else STATUS_OK
    span.duration_seconds = _duration_seconds(span, event)
    if event.type == "agent_error":
        span.error = str(event.payload.get("error", "")) or None
        span.error_type = str(event.payload.get("error_type", "")) or None
    else:
        _add_usage(span, event)


def _add_usage(span: Span, event: Event) -> None:
    """Accumulate the event's AMP v0.3 usage payload fields onto the span."""
    input_tokens = _payload_int(event, "input_tokens")
    if input_tokens is not None:
        span.input_tokens = (span.input_tokens or 0) + input_tokens
    output_tokens = _payload_int(event, "output_tokens")
    if output_tokens is not None:
        span.output_tokens = (span.output_tokens or 0) + output_tokens
    cost = event.payload.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        span.cost_usd = (span.cost_usd or 0.0) + float(cost)
    model = event.payload.get("model")
    if isinstance(model, str) and model:
        span.model = model


def _payload_int(event: Event, key: str) -> Optional[int]:
    value = event.payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _duration_seconds(span: Span, end: Event) -> float:
    """Span duration: payload first, start→end ts as fallback (like board)."""
    seconds = end.payload.get("duration_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return float(seconds)
    millis = end.payload.get("duration_ms")
    if isinstance(millis, (int, float)) and not isinstance(millis, bool):
        return float(millis) / 1000.0
    return (_parse_ts(end.ts) - _parse_ts(span.started_ts)).total_seconds()


def _payload_str(event: Event, key: str) -> Optional[str]:
    value = event.payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def render_tree(trace: Trace, now: Optional[datetime] = None) -> str:
    """Render a trace as an ASCII tree; ``now`` sizes running spans (injectable for tests)."""
    if now is None:
        now = datetime.now(timezone.utc)
    lines = [
        "run %s — %d spans, %d failed, total %s"
        % (
            trace.run_id,
            trace.span_count,
            trace.failed_count,
            _format_duration(trace.total_seconds),
        )
    ]
    _render_spans(trace.roots, "", lines, now)
    return "\n".join(lines)


def _render_spans(spans: List[Span], prefix: str, lines: List[str], now: datetime) -> None:
    for index, span in enumerate(spans):
        last = index == len(spans) - 1
        lines.append(prefix + ("└─ " if last else "├─ ") + _format_span(span, now))
        _render_spans(span.children, prefix + ("   " if last else "│  "), lines, now)


def _format_span(span: Span, now: datetime) -> str:
    text = "%s %s %s" % (span.agent, _STATUS_MARK[span.status], _span_duration_text(span, now))
    usage = _span_usage_text(span)
    if usage:
        text += " " + usage
    if span.status == STATUS_FAILED and (span.error_type or span.error):
        first_line = span.error.splitlines()[0] if span.error else ""
        if span.error_type:
            detail = "%s: %s" % (span.error_type, first_line) if first_line else span.error_type
        else:
            detail = first_line
        text += " — " + detail
    return text


def _span_usage_text(span: Span) -> str:
    """Compact usage suffix like "[1.2k tok, $0.0034]"; "" without usage."""
    parts = []
    if span.input_tokens is not None or span.output_tokens is not None:
        parts.append(
            "%s tok" % _format_tokens((span.input_tokens or 0) + (span.output_tokens or 0))
        )
    if span.cost_usd is not None:
        parts.append("$%.4f" % span.cost_usd)
    return "[%s]" % ", ".join(parts) if parts else ""


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return "%.1fM" % (tokens / 1_000_000.0)
    if tokens >= 1000:
        return "%.1fk" % (tokens / 1000.0)
    return "%d" % tokens


def _span_duration_text(span: Span, now: datetime) -> str:
    if span.duration_seconds is not None:
        return _format_duration(span.duration_seconds)
    if span.status == STATUS_RUNNING:
        return _format_duration(max(0.0, (now - _parse_ts(span.started_ts)).total_seconds()))
    return "—"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm %ds" % (seconds // 60, seconds % 60)
