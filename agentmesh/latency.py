"""Latency percentiles: the SLO view of completed-span durations.

The board answers "how is each agent doing right now?" and the flakiness
report "which agents can I not trust?"; this module answers "how slow is
slow?" by distributing completed-span durations into percentiles:

* ``span_durations`` collects, per agent (and per model when a span carries
  the AMP v0.3 ``model`` usage field), the durations of completed spans.
* ``percentile`` computes a percentile via linear interpolation.
* ``latency_stats`` computes count/min/p50/p90/p99/max/mean per key.
* ``render_latency`` renders the stats as a markdown table, busiest first.

Spans are rebuilt with ``trace.build_trace`` so span pairing and duration
derivation (payload ``duration_seconds``, then ``duration_ms``, then the
start->end timestamp difference) match the trace and board views exactly —
the event semantics live in one place.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Union

from agentmesh.store import EventStore
from agentmesh.trace import STATUS_RUNNING, Span, build_trace

GROUP_BY_CHOICES = ("agent", "model")

_GROUP_LABEL = {"agent": "Agent", "model": "Model"}


@dataclass
class DurationSamples:
    """Completed-span durations (seconds), grouped by agent and by model."""

    by_agent: Dict[str, List[float]]
    by_model: Dict[str, List[float]]


@dataclass
class LatencyStats:
    """Latency distribution of one key (an agent or a model)."""

    key: str
    count: int
    min_seconds: float
    p50_seconds: float
    p90_seconds: float
    p99_seconds: float
    max_seconds: float
    mean_seconds: float


def span_durations(
    store: EventStore,
    since: Optional[Union[str, datetime]] = None,
) -> DurationSamples:
    """Collect completed-span durations from every stored run.

    Each run's spans are rebuilt with ``trace.build_trace``, so pairing
    (AMP v0.2 span ids with the v0.1 per-agent fallback) and duration
    sources (payload ``duration_seconds`` -> ``duration_ms`` -> start/end
    ts difference) are exactly the trace/board semantics. Still-running
    spans are skipped; both ok and failed spans count — a failure's
    duration is still latency the caller waited. A span also counts toward
    its model when its usage payload carried one. ``since`` is an ISO-8601
    UTC string (or datetime) compared against the span's start ``ts``;
    only spans started at or after it are kept.
    """
    if isinstance(since, datetime):
        since = since.isoformat()
    run_ids: List[str] = []
    seen = set()
    for _, event in store.events_after(0):
        if event.run_id not in seen:
            seen.add(event.run_id)
            run_ids.append(event.run_id)
    samples = DurationSamples(by_agent={}, by_model={})
    for run_id in run_ids:
        spans: List[Span] = []
        _collect_completed(build_trace(store, run_id).roots, spans)
        for span in spans:
            if since is not None and span.started_ts < since:
                continue
            if span.duration_seconds is None:
                continue
            samples.by_agent.setdefault(span.agent, []).append(span.duration_seconds)
            if span.model is not None:
                samples.by_model.setdefault(span.model, []).append(span.duration_seconds)
    return samples


def _collect_completed(spans: List[Span], out: List[Span]) -> None:
    """Flatten a span tree into ``out``, keeping only completed spans."""
    for span in spans:
        if span.status != STATUS_RUNNING:
            out.append(span)
        _collect_completed(span.children, out)


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """The ``p``-th percentile of ``sorted_values`` via linear interpolation.

    Uses the "linear" method (NumPy's default; type 7 in Hyndman & Fan):
    the percentile sits at fractional rank ``(n - 1) * p / 100`` and values
    between two adjacent ranks are linearly interpolated. A 1-element list
    returns that element for every ``p``. ``sorted_values`` must already be
    sorted ascending and non-empty; ``p`` is a number in [0, 100].
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sequence is undefined")
    if not 0.0 <= p <= 100.0:
        raise ValueError("p must be between 0 and 100, got %r" % (p,))
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (p / 100.0)
    lower = int(math.floor(rank))
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    low, high = float(sorted_values[lower]), float(sorted_values[upper])
    return low + (high - low) * fraction


def latency_stats(durations_by_key: Dict[str, List[float]]) -> List[LatencyStats]:
    """Latency distribution per key, busiest first (count desc, then key).

    ``durations_by_key`` is one grouping from ``span_durations`` (by agent
    or by model); keys with no durations are dropped.
    """
    stats = []
    for key, durations in durations_by_key.items():
        if not durations:
            continue
        values = sorted(durations)
        stats.append(
            LatencyStats(
                key=key,
                count=len(values),
                min_seconds=values[0],
                p50_seconds=percentile(values, 50),
                p90_seconds=percentile(values, 90),
                p99_seconds=percentile(values, 99),
                max_seconds=values[-1],
                mean_seconds=sum(values) / len(values),
            )
        )
    stats.sort(key=lambda stat: (-stat.count, stat.key))
    return stats


def render_latency(
    stats: List[LatencyStats],
    by: str = "agent",
    since: Optional[Union[str, datetime]] = None,
) -> str:
    """Render latency stats as a markdown table; the header notes the window."""
    if by not in GROUP_BY_CHOICES:
        raise ValueError("by must be one of %s, got %r" % (list(GROUP_BY_CHOICES), by))
    if not stats:
        return "No completed spans."
    # Local import: board imports ``percentile`` from this module at the top,
    # so importing its formatter lazily avoids a circular module import.
    from agentmesh.board import _format_duration

    if isinstance(since, datetime):
        since = since.isoformat()
    window = "all time" if since is None else "since %s" % since
    label = _GROUP_LABEL[by]
    lines = [
        "Latency percentiles by %s (%s)" % (by, window),
        "",
        "| %s | Spans | Min | p50 | p90 | p99 | Max | Mean |" % label,
        "|%s|-------|-----|-----|-----|-----|-----|------|" % ("-" * (len(label) + 2)),
    ]
    for stat in stats:
        lines.append(
            "| %s | %d | %s | %s | %s | %s | %s | %s |"
            % (
                stat.key,
                stat.count,
                _format_duration(stat.min_seconds),
                _format_duration(stat.p50_seconds),
                _format_duration(stat.p90_seconds),
                _format_duration(stat.p99_seconds),
                _format_duration(stat.max_seconds),
                _format_duration(stat.mean_seconds),
            )
        )
    return "\n".join(lines)
