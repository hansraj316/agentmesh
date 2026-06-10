"""Fleet status board: aggregate stored AMP events into a per-agent dashboard."""

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from agentmesh.events import Event, _parse_ts
from agentmesh.store import EventStore

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"
STATUS_IDLE = "idle"

_STATUS_EMOJI = {
    STATUS_OK: "🟢",
    STATUS_FAILED: "🔴",
    STATUS_RUNNING: "🔵",
    STATUS_IDLE: "⚪",
}

_TERMINAL_TYPES = frozenset({"agent_end", "agent_error"})

_MARKDOWN_HEADER = [
    "| Agent | Status | Last event | Age | Runs | Errors | Streak | Avg duration |",
    "|-------|--------|------------|-----|------|--------|--------|--------------|",
]

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AgentMesh board</title>
<style>
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       margin: 2rem; color: #1f2933; }
h1 { font-size: 1.3rem; }
table { border-collapse: collapse; }
th, td { padding: 0.4rem 0.8rem; border-bottom: 1px solid #d8dee4; text-align: left; }
th { background: #f1f5f9; }
tr.ok { background: #f0fdf4; }
tr.failed { background: #fef2f2; }
tr.running { background: #eff6ff; }
tr.idle { background: #fafafa; }
</style>
</head>
<body>
<h1>AgentMesh board</h1>
<table>
<tr><th>Agent</th><th>Status</th><th>Last event</th><th>Age</th>
<th>Runs</th><th>Errors</th><th>Streak</th><th>Avg duration</th></tr>
%s
</table>
</body>
</html>
"""


@dataclass
class AgentSummary:
    """Aggregated view of one agent's activity in the event store."""

    agent: str
    status: str
    last_event_type: str
    last_ts: str
    runs: int
    errors: int
    streak: int
    avg_duration_seconds: Optional[float]


def agent_summaries(
    store: EventStore,
    since: Optional[Union[str, datetime]] = None,
) -> List[AgentSummary]:
    """Aggregate stored events into one summary per agent, sorted by name.

    ``since`` is an ISO-8601 UTC string (or datetime) compared against the
    event ``ts``; only events at or after it are considered.
    """
    if isinstance(since, datetime):
        since = since.isoformat()
    per_agent: Dict[str, List[Event]] = {}
    for _, event in store.events_after(0):
        if since is not None and event.ts < since:
            continue
        per_agent.setdefault(event.agent, []).append(event)
    return [_summarize(agent, events) for agent, events in sorted(per_agent.items())]


def _summarize(agent: str, events: List[Event]) -> AgentSummary:
    """Build one summary from this agent's events, oldest first."""
    runs: Dict[str, Dict[str, Optional[Event]]] = {}
    terminals: List[Event] = []  # first terminal event of each run, in end order
    errors = 0
    for event in events:
        run = runs.setdefault(event.run_id, {"start": None, "end": None})
        if event.type == "agent_start" and run["start"] is None:
            run["start"] = event
        elif event.type in _TERMINAL_TYPES and run["end"] is None:
            run["end"] = event
            terminals.append(event)
        if event.type == "agent_error":
            errors += 1

    streak = 0
    for terminal in reversed(terminals):
        if terminal.type != "agent_error":
            break
        streak += 1

    durations: List[float] = []
    for run in runs.values():
        end = run["end"]
        if end is None or end.type != "agent_end":
            continue
        duration = _run_duration_seconds(run["start"], end)
        if duration is not None:
            durations.append(duration)

    last = events[-1]
    return AgentSummary(
        agent=agent,
        status=_status(last, runs[last.run_id]["end"], terminals),
        last_event_type=last.type,
        last_ts=last.ts,
        runs=len(runs),
        errors=errors,
        streak=streak,
        avg_duration_seconds=sum(durations) / len(durations) if durations else None,
    )


def _status(last: Event, last_run_end: Optional[Event], terminals: List[Event]) -> str:
    """Classify an agent from its latest event and latest terminal event."""
    if last.type == "agent_start" and last_run_end is None:
        return STATUS_RUNNING
    if not terminals:
        return STATUS_IDLE
    return STATUS_FAILED if terminals[-1].type == "agent_error" else STATUS_OK


def _run_duration_seconds(start: Optional[Event], end: Event) -> Optional[float]:
    """Duration of one successful run: payload first, start→end ts as fallback."""
    seconds = end.payload.get("duration_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return float(seconds)
    millis = end.payload.get("duration_ms")
    if isinstance(millis, (int, float)) and not isinstance(millis, bool):
        return float(millis) / 1000.0
    if start is not None:
        return (_parse_ts(end.ts) - _parse_ts(start.ts)).total_seconds()
    return None


def _humanize_age(ts: str, now: datetime) -> str:
    """Compact age of ``ts`` relative to ``now``: "42s", "3m", "2h", "5d"."""
    seconds = max(0, int((now - _parse_ts(ts)).total_seconds()))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm %ds" % (seconds // 60, seconds % 60)


def render_markdown(summaries: List[AgentSummary], now: Optional[datetime] = None) -> str:
    """Render summaries as a markdown table; ``now`` is injectable for tests."""
    if now is None:
        now = datetime.now(timezone.utc)
    lines = list(_MARKDOWN_HEADER)
    for summary in summaries:
        lines.append(
            "| %s | %s %s | %s | %s | %d | %d | %d | %s |"
            % (
                summary.agent,
                _STATUS_EMOJI[summary.status],
                summary.status,
                summary.last_event_type,
                _humanize_age(summary.last_ts, now),
                summary.runs,
                summary.errors,
                summary.streak,
                _format_duration(summary.avg_duration_seconds),
            )
        )
    return "\n".join(lines)


def render_html(summaries: List[AgentSummary], now: Optional[datetime] = None) -> str:
    """Render summaries as a minimal self-contained HTML page (no JS, no assets)."""
    if now is None:
        now = datetime.now(timezone.utc)
    rows = []
    for summary in summaries:
        rows.append(
            '<tr class="%s"><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td>'
            "<td>%d</td><td>%d</td><td>%d</td><td>%s</td></tr>"
            % (
                html.escape(summary.status, quote=True),
                html.escape(summary.agent),
                _STATUS_EMOJI[summary.status],
                html.escape(summary.status),
                html.escape(summary.last_event_type),
                html.escape(_humanize_age(summary.last_ts, now)),
                summary.runs,
                summary.errors,
                summary.streak,
                html.escape(_format_duration(summary.avg_duration_seconds)),
            )
        )
    return _HTML_TEMPLATE % "\n".join(rows)
