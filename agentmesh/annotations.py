"""Run annotations: operator context attached to runs ("deploy happened here").

An annotation is not a new AMP event type. By convention (see the
"Annotations" note in docs/amp-spec.md) it is a regular ``message`` event
whose payload is::

    {"kind": "annotation", "text": str, "author": str}   # author optional

with the event ``agent`` set to ``"operator"`` by default. Every annotation
is a valid AMP v0.3 event, so stores and consumers that don't know the
convention simply treat it as a free-form message.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from agentmesh.events import Event, _parse_ts, utc_now_iso
from agentmesh.store import EventStore

ANNOTATION_KIND = "annotation"
DEFAULT_AGENT = "operator"


def add_annotation(
    store: EventStore,
    run_id: str,
    text: str,
    author: Optional[str] = None,
    agent: str = DEFAULT_AGENT,
    now: Optional[Union[str, datetime]] = None,
) -> Event:
    """Attach an annotation to an existing run; returns the stored event.

    ``now`` overrides the event timestamp (ISO-8601 UTC string or aware
    datetime, injectable for tests). Raises ValueError if ``text`` is empty
    or if the run has no stored events — annotations must attach to real
    runs.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("annotation text must be a non-empty string, got %r" % (text,))
    if author is not None and (not isinstance(author, str) or not author.strip()):
        raise ValueError("author must be a non-empty string when given, got %r" % (author,))
    if not store.events_after(0, run_id=run_id):
        raise ValueError(
            "no events found for run_id %r — annotations must attach to an existing run" % (run_id,)
        )
    payload: Dict[str, Any] = {"kind": ANNOTATION_KIND, "text": text}
    if author is not None:
        payload["author"] = author
    event = Event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        agent=agent,
        type="message",
        ts=_to_ts(now),
        payload=payload,
    )
    store.append(event)
    return event


def is_annotation(event: Event) -> bool:
    """True if ``event`` is a ``message`` event following the annotation convention."""
    if event.type != "message":
        return False
    text = event.payload.get("text")
    return event.payload.get("kind") == ANNOTATION_KIND and isinstance(text, str) and bool(text)


def annotations_for_run(store: EventStore, run_id: str) -> List[Dict[str, Any]]:
    """The annotations of one run, oldest first: ``{"ts", "text", "author"}`` dicts.

    ``author`` is None when the annotation was stored without one. Runs
    without annotations (and unknown runs) yield an empty list.
    """
    notes = [
        _note(event) for _, event in store.events_after(0, run_id=run_id) if is_annotation(event)
    ]
    notes.sort(key=lambda note: _parse_ts(note["ts"]))
    return notes


def annotations_summary(
    store: EventStore,
    since: Optional[Union[str, datetime]] = None,
) -> List[Dict[str, Any]]:
    """Recent annotations across all runs, newest first.

    Each entry is a ``{"run_id", "ts", "text", "author"}`` dict. ``since``
    (ISO-8601 UTC string or datetime) keeps only annotations at or after
    that time; an unparsable string raises ValueError.
    """
    cutoff = _parse_since(since)
    notes = []
    for _, event in store.events_after(0):
        if not is_annotation(event):
            continue
        if cutoff is not None and _parse_ts(event.ts) < cutoff:
            continue
        note: Dict[str, Any] = {"run_id": event.run_id}
        note.update(_note(event))
        notes.append(note)
    notes.sort(key=lambda note: _parse_ts(note["ts"]), reverse=True)
    return notes


def _note(event: Event) -> Dict[str, Any]:
    return {
        "ts": event.ts,
        "text": event.payload["text"],
        "author": event.payload.get("author"),
    }


def _to_ts(now: Optional[Union[str, datetime]]) -> str:
    if now is None:
        return utc_now_iso()
    if isinstance(now, datetime):
        return now.isoformat()
    return now


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
