"""Cost & token rollups: aggregate AMP v0.3 usage payload fields from the store."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from agentmesh.events import USAGE_EVENT_TYPES, Event
from agentmesh.store import EventStore

GROUP_BY_CHOICES = ("run", "agent", "model")

UNKNOWN_MODEL = "(unknown)"

_USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "model")

_GROUP_LABEL = {"run": "Run", "agent": "Agent", "model": "Model"}


@dataclass
class UsageRow:
    """Aggregated usage of one group (run, agent, or model)."""

    name: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    events: int


@dataclass
class UsageSummary:
    """Per-group usage rows plus a totals row."""

    group_by: str
    rows: List[UsageRow]
    total: UsageRow


def usage_summary(
    store: EventStore,
    group_by: str = "run",
    since: Optional[Union[str, datetime]] = None,
) -> UsageSummary:
    """Aggregate usage payload fields from ``agent_end`` and ``tool_call`` events.

    Only events carrying at least one usage key (``input_tokens``,
    ``output_tokens``, ``cost_usd``, ``model``) are counted. ``group_by`` is
    one of ``"run"``, ``"agent"``, or ``"model"`` (events with usage but no
    model are grouped under ``"(unknown)"``). Rows are sorted by cost
    descending, then name. ``since`` is an ISO-8601 UTC string (or datetime)
    compared against the event ``ts``; only events at or after it count.
    """
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError("group_by must be one of %s, got %r" % (list(GROUP_BY_CHOICES), group_by))
    if isinstance(since, datetime):
        since = since.isoformat()
    groups: Dict[str, UsageRow] = {}
    for _, event in store.events_after(0):
        if event.type not in USAGE_EVENT_TYPES:
            continue
        if since is not None and event.ts < since:
            continue
        if not any(key in event.payload for key in _USAGE_KEYS):
            continue
        name = _group_name(event, group_by)
        row = groups.setdefault(name, UsageRow(name, 0, 0, 0.0, 0))
        row.input_tokens += _int_value(event.payload, "input_tokens")
        row.output_tokens += _int_value(event.payload, "output_tokens")
        row.cost_usd += _number_value(event.payload, "cost_usd")
        row.events += 1
    rows = sorted(groups.values(), key=lambda row: (-row.cost_usd, row.name))
    total = UsageRow(
        name="total",
        input_tokens=sum(row.input_tokens for row in rows),
        output_tokens=sum(row.output_tokens for row in rows),
        cost_usd=sum(row.cost_usd for row in rows),
        events=sum(row.events for row in rows),
    )
    return UsageSummary(group_by=group_by, rows=rows, total=total)


def _group_name(event: Event, group_by: str) -> str:
    if group_by == "run":
        return event.run_id
    if group_by == "agent":
        return event.agent
    model = event.payload.get("model")
    if isinstance(model, str) and model:
        return model
    return UNKNOWN_MODEL


def _int_value(payload: Dict[str, Any], key: str) -> int:
    """A stored token count, defensively: 0 unless a real non-negative int."""
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _number_value(payload: Dict[str, Any], key: str) -> float:
    """A stored cost, defensively: 0.0 unless a real non-negative number."""
    value = payload.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 0.0


def render_costs(summary: UsageSummary) -> str:
    """Render a usage summary as a markdown table (totals row last)."""
    if not summary.rows:
        return "No usage data recorded."
    label = _GROUP_LABEL[summary.group_by]
    lines = [
        "| %s | Input tokens | Output tokens | Cost (USD) | Events |" % label,
        "|%s|--------------|---------------|------------|--------|" % ("-" * (len(label) + 2)),
    ]
    for row in summary.rows:
        lines.append(_format_row(row.name, row))
    lines.append(_format_row("**total**", summary.total))
    return "\n".join(lines)


def _format_row(name: str, row: UsageRow) -> str:
    return "| %s | %s | %s | $%.4f | %d |" % (
        name,
        "{:,}".format(row.input_tokens),
        "{:,}".format(row.output_tokens),
        row.cost_usd,
        row.events,
    )
