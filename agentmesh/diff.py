"""Run comparison: align two traces span-by-span and diff durations and statuses.

Spans are aligned by *structural path* — the root-relative chain of agent
names (e.g. ``orchestrator/researcher``). When the same agent appears more
than once under one parent, occurrences are aligned in order (first with
first, second with second, …), so A's second ``researcher`` never matches
B's first.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from agentmesh.trace import (
    STATUS_FAILED,
    _format_duration,
    _STATUS_MARK,
    Span,
    Trace,
)

# (path, span) for spans that exist in only one of the two traces.
UnmatchedSpan = Tuple[str, Span]

# Alignment key: one (agent, occurrence-under-parent) entry per tree level.
_PathKey = Tuple[Tuple[str, int], ...]


@dataclass
class AlignedPair:
    """One span present in both traces at the same structural path."""

    path: str
    span_a: Span
    span_b: Span


@dataclass
class Alignment:
    """The result of matching two traces span-by-span."""

    pairs: List[AlignedPair]
    only_in_a: List[UnmatchedSpan]
    only_in_b: List[UnmatchedSpan]


@dataclass
class SpanDiff:
    """Duration and status comparison of one aligned span pair."""

    path: str
    duration_a: Optional[float]
    duration_b: Optional[float]
    delta_seconds: Optional[float]  # B - A; None when either duration is missing
    delta_pct: Optional[float]  # None when missing durations or A duration is 0
    status_a: str
    status_b: str

    @property
    def status_changed(self) -> bool:
        return self.status_a != self.status_b


@dataclass
class RunDiff:
    """Full comparison of two runs: per-span diffs plus run-level summary."""

    run_id_a: str
    run_id_b: str
    spans: List[SpanDiff]
    only_in_a: List[UnmatchedSpan]
    only_in_b: List[UnmatchedSpan]
    span_count_a: int
    span_count_b: int
    failed_count_a: int
    failed_count_b: int
    total_seconds_a: float
    total_seconds_b: float

    @property
    def total_delta_seconds(self) -> float:
        return self.total_seconds_b - self.total_seconds_a


def align_spans(trace_a: Trace, trace_b: Trace) -> Alignment:
    """Match spans of two traces by structural path and occurrence order."""
    flat_a = _flatten(trace_a.roots)
    flat_b = _flatten(trace_b.roots)
    b_by_key: Dict[_PathKey, Tuple[str, Span]] = {key: (path, span) for key, path, span in flat_b}
    pairs: List[AlignedPair] = []
    only_in_a: List[UnmatchedSpan] = []
    matched_keys = set()
    for key, path, span in flat_a:
        match = b_by_key.get(key)
        if match is not None:
            pairs.append(AlignedPair(path=path, span_a=span, span_b=match[1]))
            matched_keys.add(key)
        else:
            only_in_a.append((path, span))
    only_in_b = [(path, span) for key, path, span in flat_b if key not in matched_keys]
    return Alignment(pairs=pairs, only_in_a=only_in_a, only_in_b=only_in_b)


def _flatten(roots: List[Span]) -> List[Tuple[_PathKey, str, Span]]:
    """Depth-first list of (alignment key, display path, span) for a span tree."""
    out: List[Tuple[_PathKey, str, Span]] = []
    _flatten_into(roots, (), "", out)
    return out


def _flatten_into(
    spans: List[Span],
    parent_key: _PathKey,
    parent_path: str,
    out: List[Tuple[_PathKey, str, Span]],
) -> None:
    seen: Dict[str, int] = {}
    for span in spans:
        occurrence = seen.get(span.agent, 0)
        seen[span.agent] = occurrence + 1
        key = parent_key + ((span.agent, occurrence),)
        path = parent_path + "/" + span.agent if parent_path else span.agent
        out.append((key, path, span))
        _flatten_into(span.children, key, path, out)


def diff_traces(trace_a: Trace, trace_b: Trace) -> RunDiff:
    """Compare two traces of the same workflow, span by aligned span."""
    alignment = align_spans(trace_a, trace_b)
    spans = [
        SpanDiff(
            path=pair.path,
            duration_a=pair.span_a.duration_seconds,
            duration_b=pair.span_b.duration_seconds,
            delta_seconds=_delta(pair.span_a.duration_seconds, pair.span_b.duration_seconds),
            delta_pct=_delta_pct(pair.span_a.duration_seconds, pair.span_b.duration_seconds),
            status_a=pair.span_a.status,
            status_b=pair.span_b.status,
        )
        for pair in alignment.pairs
    ]
    return RunDiff(
        run_id_a=trace_a.run_id,
        run_id_b=trace_b.run_id,
        spans=spans,
        only_in_a=alignment.only_in_a,
        only_in_b=alignment.only_in_b,
        span_count_a=trace_a.span_count,
        span_count_b=trace_b.span_count,
        failed_count_a=trace_a.failed_count,
        failed_count_b=trace_b.failed_count,
        total_seconds_a=trace_a.total_seconds,
        total_seconds_b=trace_b.total_seconds,
    )


def _delta(duration_a: Optional[float], duration_b: Optional[float]) -> Optional[float]:
    if duration_a is None or duration_b is None:
        return None
    return duration_b - duration_a


def _delta_pct(duration_a: Optional[float], duration_b: Optional[float]) -> Optional[float]:
    if duration_a is None or duration_b is None or duration_a == 0:
        return None
    return (duration_b - duration_a) / duration_a * 100.0


def render_diff(diff: RunDiff, threshold_pct: float = 20.0) -> str:
    """Render a run diff as markdown; mark regressions ⚠️ and improvements ✅.

    A span is a regression when it got slower by more than ``threshold_pct``
    percent or its status changed to failed; an improvement when it got
    faster beyond the threshold or recovered from failed. When no span
    crosses the threshold, no status changed, and both runs have the same
    structure, a "no significant changes" line replaces the noise.
    """
    lines = ["# Run diff: %s → %s" % (diff.run_id_a, diff.run_id_b)]
    lines.append("")
    lines.append(
        "%s: %d spans, %d failed, total %s | %s: %d spans, %d failed, total %s | "
        "wall-clock Δ %s"
        % (
            diff.run_id_a,
            diff.span_count_a,
            diff.failed_count_a,
            _format_duration(diff.total_seconds_a),
            diff.run_id_b,
            diff.span_count_b,
            diff.failed_count_b,
            _format_duration(diff.total_seconds_b),
            _format_signed(diff.total_delta_seconds),
        )
    )

    marks = [_mark(span, threshold_pct) for span in diff.spans]
    if diff.spans:
        lines.append("")
        lines.append(
            "| Agent path | %s | %s | Δ | %%Δ | Status |  |" % (diff.run_id_a, diff.run_id_b)
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for span, mark in zip(diff.spans, marks):
            lines.append(
                "| %s | %s | %s | %s | %s | %s → %s | %s |"
                % (
                    span.path,
                    _format_optional(span.duration_a),
                    _format_optional(span.duration_b),
                    _format_signed(span.delta_seconds) if span.delta_seconds is not None else "—",
                    "%+.1f%%" % span.delta_pct if span.delta_pct is not None else "—",
                    span.status_a,
                    span.status_b,
                    mark,
                )
            )

    _render_unmatched(lines, "Only in %s" % diff.run_id_a, diff.only_in_a)
    _render_unmatched(lines, "Only in %s" % diff.run_id_b, diff.only_in_b)

    quiet = not any(marks) and not any(span.status_changed for span in diff.spans)
    if quiet and not diff.only_in_a and not diff.only_in_b:
        lines.append("")
        lines.append("No significant changes (threshold ±%.1f%%)." % threshold_pct)
    return "\n".join(lines)


def _mark(span: SpanDiff, threshold_pct: float) -> str:
    slower = span.delta_pct is not None and span.delta_pct > threshold_pct
    faster = span.delta_pct is not None and span.delta_pct < -threshold_pct
    if slower or (span.status_changed and span.status_b == STATUS_FAILED):
        return "⚠️"
    if faster or (span.status_changed and span.status_a == STATUS_FAILED):
        return "✅"
    return ""


def _render_unmatched(lines: List[str], title: str, unmatched: List[UnmatchedSpan]) -> None:
    if not unmatched:
        return
    lines.append("")
    lines.append("## %s" % title)
    lines.append("")
    for path, span in unmatched:
        lines.append(
            "- %s %s %s"
            % (path, _STATUS_MARK[span.status], _format_optional(span.duration_seconds))
        )


def _format_optional(seconds: Optional[float]) -> str:
    return _format_duration(seconds) if seconds is not None else "—"


def _format_signed(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    return sign + _format_duration(abs(seconds))
