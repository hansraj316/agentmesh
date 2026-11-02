"""Handoffs: one agent passing a task to another (AMP v0.4).

A handoff is not a new AMP event type. By convention (see the "Handoffs"
section in docs/amp-spec.md) it is a pair of regular ``message`` events that
share a ``task_id``, emitted by the sender::

    {"kind": "handoff", "task_id": "t-3f2a", "task": "Add retry to GitHub sync",
     "from": "planner", "to": "coder", "status": "running"}
    {"kind": "handoff", "task_id": "t-3f2a", "task": "Add retry to GitHub sync",
     "from": "planner", "to": "coder", "status": "done", "duration_ms": 23140}

``status`` is ``running`` on open and ``done`` or ``failed`` on close. Every
handoff event is a valid AMP v0.3 ``message`` event, so consumers that don't
know the convention treat it as a free-form message.

This module folds those events into :class:`Handoff` records (``fold``),
reads them from a store (``list_handoffs``), renders them as a markdown
table (``render_markdown``), and registers the ``agentmesh handoffs`` CLI
command (``add_cli``). Events are produced by ``mesh.handoff(...)`` in
:mod:`agentmesh.sdk`.
"""

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from agentmesh.events import Event, _parse_ts
from agentmesh.store import EventStore

HANDOFF_KIND = "handoff"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED})
STATUSES = frozenset({STATUS_RUNNING}) | TERMINAL_STATUSES

_STATUS_LABEL = {
    STATUS_RUNNING: "🔵 Running",
    STATUS_DONE: "🟢 Done",
    STATUS_FAILED: "🔴 Failed",
}

_FOLLOW_POLL_SECONDS = 0.5

EventLike = Union[Event, Mapping[str, Any]]


@dataclass
class Handoff:
    """One folded handoff: an open event plus its close, if it has arrived.

    ``from_`` is the sender (``from`` in :meth:`to_dict`, since ``from`` is a
    Python keyword). Still-open handoffs have ``status == "running"`` and
    ``ended_at``/``duration_seconds`` set to None. ``error`` is the first
    line of the failure message on ``failed`` handoffs, else None.
    """

    task_id: str
    run_id: str
    task: str
    from_: str
    to: str
    status: str
    started_at: Optional[str]
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """The folded handoff as the build plan's JSON shape (plus ``error``)."""
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "task": self.task,
            "from": self.from_,
            "to": self.to,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


def handoff_payload(event: EventLike) -> Optional[Dict[str, Any]]:
    """The handoff payload of ``event``, or None if it isn't a well-formed handoff.

    Well-formed means: a ``message`` event whose payload has ``kind ==
    "handoff"``, non-empty string ``task_id``, ``from`` and ``to``, and a
    ``status`` of ``running``, ``done`` or ``failed``. ``task`` should be a
    string; a missing one folds as ``""``.
    """
    _, _, type_, _, payload = _fields(event)
    if type_ != "message" or not isinstance(payload, dict):
        return None
    if payload.get("kind") != HANDOFF_KIND:
        return None
    for key in ("task_id", "from", "to"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            return None
    if payload.get("status") not in STATUSES:
        return None
    return payload


def is_handoff(event: EventLike) -> bool:
    """True if ``event`` is a well-formed handoff ``message`` event."""
    return handoff_payload(event) is not None


def fold(events: Iterable[EventLike]) -> List[Handoff]:
    """Fold handoff events into one :class:`Handoff` per ``(run_id, task_id)``.

    ``events`` may be :class:`~agentmesh.events.Event` objects or AMP event
    dicts (such as ``/api/stream`` objects); non-handoff events are ignored
    and nothing is mutated. Rules:

    - The open (``running``) event sets ``started_at`` to its ``ts``.
    - The first close (``done``/``failed``) sets ``status``, ``ended_at`` and
      ``error``; later closes for the same task are ignored.
    - ``duration_seconds`` comes from the close's ``duration_ms`` when
      present, else from ``ended_at - started_at``.
    - A close with no open still folds: ``started_at`` is back-computed from
      ``duration_ms`` (or equals ``ended_at`` without it).
    - An open with no close folds as ``running`` with ``ended_at`` None.

    Handoffs are returned in the order their first event appears.
    """
    folded: Dict[Tuple[str, str], Handoff] = {}
    opened = set()  # keys whose started_at came from a real open event
    close_ms: Dict[Tuple[str, str], Optional[float]] = {}
    for event in events:
        payload = handoff_payload(event)
        if payload is None:
            continue
        run_id, _, _, ts, _ = _fields(event)
        key = (run_id, payload["task_id"])
        handoff = folded.get(key)
        if handoff is None:
            handoff = Handoff(
                task_id=payload["task_id"],
                run_id=run_id,
                task=_str_or(payload.get("task"), ""),
                from_=payload["from"],
                to=payload["to"],
                status=STATUS_RUNNING,
                started_at=None,
            )
            folded[key] = handoff
        elif not handoff.task:
            handoff.task = _str_or(payload.get("task"), "")
        status = payload["status"]
        if status == STATUS_RUNNING:
            if key not in opened:
                opened.add(key)
                handoff.started_at = ts
        elif handoff.ended_at is None:
            handoff.status = status
            handoff.ended_at = ts
            if status == STATUS_FAILED:
                handoff.error = _first_line(payload.get("error"))
            close_ms[key] = _number_or_none(payload.get("duration_ms"))
            if key not in opened:
                handoff.started_at = _back_compute_start(ts, close_ms[key])
        if handoff.ended_at is not None:
            handoff.duration_seconds = _duration(handoff, close_ms.get(key))
    return list(folded.values())


def list_handoffs(
    store: EventStore,
    run_id: Optional[str] = None,
    since: Optional[Union[str, datetime]] = None,
    limit: int = 50,
) -> List[Handoff]:
    """Handoffs from ``store``, newest first (by ``started_at``), at most ``limit``.

    ``run_id`` restricts to one run. ``since`` (ISO-8601 UTC string or
    datetime) keeps handoffs still active at or after that time: those that
    are still running or that ended at or after it. An unparsable ``since``
    raises ValueError.
    """
    cutoff = _parse_since(since)
    events = (event for _, event in store.events_after(0, run_id=run_id))
    handoffs = fold(events)
    if cutoff is not None:
        handoffs = [h for h in handoffs if h.ended_at is None or _parse_ts(h.ended_at) >= cutoff]
    handoffs.sort(key=_sort_key, reverse=True)
    if limit is not None and limit >= 0:
        handoffs = handoffs[:limit]
    return handoffs


def render_markdown(handoffs: List[Handoff], now: Optional[datetime] = None) -> str:
    """Render handoffs as a markdown table, in the order given.

    Running rows show elapsed time so far (``now`` is injectable for tests);
    failed rows append the error to the status.
    """
    if not handoffs:
        return "No handoffs found."
    if now is None:
        now = datetime.now(timezone.utc)
    lines = [
        "| Task | From | To | Status | Started | Duration | Run |",
        "|---|---|---|---|---|---|---|",
    ]
    for handoff in handoffs:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                _cell(handoff.task or handoff.task_id),
                _cell(handoff.from_),
                _cell(handoff.to),
                _cell(_status_text(handoff)),
                _cell(handoff.started_at or "-"),
                _duration_text(handoff, now),
                _cell(handoff.run_id),
            )
        )
    return "\n".join(lines)


def format_change(event: EventLike) -> Optional[str]:
    """One line describing a single handoff event, for ``--follow`` output."""
    payload = handoff_payload(event)
    if payload is None:
        return None
    run_id, _, _, ts, _ = _fields(event)
    status = payload["status"]
    line = "%s %s %s -> %s %s: %s" % (
        ts,
        _STATUS_LABEL[status],
        payload["from"],
        payload["to"],
        payload["task_id"],
        _str_or(payload.get("task"), ""),
    )
    duration_ms = _number_or_none(payload.get("duration_ms"))
    if status != STATUS_RUNNING and duration_ms is not None:
        line += " (%s)" % _format_seconds(duration_ms / 1000.0)
    if status == STATUS_FAILED:
        error = _first_line(payload.get("error"))
        if error:
            line += " error: %s" % error
    return line + " run=%s" % run_id


def add_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``agentmesh handoffs`` on an argparse subparsers object.

    The parser's default ``func`` is :func:`run_cli`, so a dispatcher can call
    ``args.func(args)``. Returns the new subparser.
    """
    parser = subparsers.add_parser(
        "handoffs",
        help="Show agent-to-agent handoffs, newest first.",
        description="Show agent-to-agent handoffs (AMP v0.4), newest first.",
    )
    parser.add_argument("--run", default=None, metavar="ID", help="Only handoffs in this run.")
    parser.add_argument(
        "-n",
        type=int,
        default=50,
        dest="limit",
        help="Number of recent handoffs to show (default: 50).",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Keep polling and print each handoff change as it arrives (Ctrl-C to stop).",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    parser.set_defaults(func=run_cli)
    return parser


def run_cli(args: argparse.Namespace) -> int:
    """Run ``agentmesh handoffs``: print the table, then follow if asked."""
    store = EventStore(args.db) if getattr(args, "db", None) else EventStore()
    run_id = getattr(args, "run", None)
    limit = getattr(args, "limit", 50)
    print(render_markdown(list_handoffs(store, run_id=run_id, limit=limit)), flush=True)
    if not getattr(args, "follow", False):
        return 0
    last_id = store.max_id()
    try:
        while True:
            time.sleep(_FOLLOW_POLL_SECONDS)
            for row_id, event in store.events_after(last_id, run_id=run_id):
                last_id = max(last_id, row_id)
                line = format_change(event)
                if line is not None:
                    print(line, flush=True)
    except KeyboardInterrupt:
        return 0


# --- helpers -----------------------------------------------------------------


def _fields(event: EventLike) -> Tuple[Any, Any, Any, Any, Any]:
    """(run_id, agent, type, ts, payload) of an Event or AMP dict."""
    if isinstance(event, Event):
        return event.run_id, event.agent, event.type, event.ts, event.payload
    if isinstance(event, Mapping):
        return (
            event.get("run_id"),
            event.get("agent"),
            event.get("type"),
            event.get("ts"),
            event.get("payload"),
        )
    return None, None, None, None, None


def _str_or(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _first_line(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    lines = value.strip().splitlines()
    return lines[0] if lines else None


def _safe_parse(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return _parse_ts(ts)
    except ValueError:
        return None


def _duration(handoff: Handoff, duration_ms: Optional[float]) -> Optional[float]:
    """Close's duration_ms when present, else ended_at - started_at."""
    if duration_ms is not None:
        return duration_ms / 1000.0
    start = _safe_parse(handoff.started_at)
    end = _safe_parse(handoff.ended_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _back_compute_start(ended_at: str, duration_ms: Optional[float]) -> str:
    end = _safe_parse(ended_at)
    if end is None or duration_ms is None:
        return ended_at
    return (end - timedelta(milliseconds=duration_ms)).isoformat()


def _sort_key(handoff: Handoff) -> datetime:
    parsed = _safe_parse(handoff.started_at)
    return parsed if parsed is not None else datetime.min.replace(tzinfo=timezone.utc)


def _parse_since(since: Optional[Union[str, datetime]]) -> Optional[datetime]:
    if since is None:
        return None
    if isinstance(since, datetime):
        cutoff = since
    else:
        try:
            cutoff = _parse_ts(since)
        except ValueError:
            raise ValueError("since is not valid ISO-8601: %r" % (since,))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff


def _status_text(handoff: Handoff) -> str:
    label = _STATUS_LABEL.get(handoff.status, handoff.status)
    if handoff.status == STATUS_FAILED and handoff.error:
        return "%s: %s" % (label, handoff.error)
    return label


def _duration_text(handoff: Handoff, now: datetime) -> str:
    if handoff.duration_seconds is not None:
        return _format_seconds(handoff.duration_seconds)
    if handoff.status == STATUS_RUNNING:
        start = _safe_parse(handoff.started_at)
        if start is not None:
            return "%s so far" % _format_seconds(max(0.0, (now - start).total_seconds()))
    return "-"


def _format_seconds(seconds: float) -> str:
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm %ds" % (seconds // 60, seconds % 60)


def _cell(text: str) -> str:
    """Make text safe inside a markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
