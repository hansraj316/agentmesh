import sqlite3

import pytest

from agentmesh.events import Event
from agentmesh.store import EventStore


def _event(run_id="run-1", agent="researcher", type="message", payload=None, ts=None):
    event = Event.new(run_id, agent, type, payload if payload is not None else {})
    if ts is not None:
        event = Event(
            event_id=event.event_id,
            run_id=run_id,
            agent=agent,
            type=type,
            ts=ts,
            payload=event.payload,
        )
    return event


def test_append_query_round_trip(store):
    event = _event(payload={"tool": "web_search", "n": 3})
    store.append(event)
    results = store.query()
    assert results == [event]
    assert results[0].payload == {"tool": "web_search", "n": 3}


def test_persistence_across_reopen(db_path, store):
    event = _event()
    store.append(event)
    reopened = EventStore(db_path)
    assert reopened.query() == [event]


def test_query_filters_by_run_id_and_agent(store):
    a = _event(run_id="run-a", agent="researcher")
    b = _event(run_id="run-b", agent="writer")
    c = _event(run_id="run-a", agent="writer")
    for event in (a, b, c):
        store.append(event)
    assert store.query(run_id="run-a") == [a, c]
    assert store.query(agent="writer") == [b, c]
    assert store.query(run_id="run-a", agent="writer") == [c]
    assert store.query(run_id="missing") == []


def test_query_since(store):
    old = _event(ts="2026-01-01T00:00:00+00:00")
    new = _event(ts="2026-06-01T00:00:00+00:00")
    store.append(old)
    store.append(new)
    assert store.query(since="2026-03-01T00:00:00+00:00") == [new]
    assert store.query(since="2025-01-01T00:00:00+00:00") == [old, new]


def test_query_limit_returns_most_recent_in_order(store):
    events = [_event(payload={"i": i}) for i in range(5)]
    for event in events:
        store.append(event)
    results = store.query(limit=2)
    assert results == events[-2:]  # newest two, chronological order


def test_append_rejects_invalid_event(store):
    bad = _event()
    object.__setattr__(bad, "type", "bogus")  # bypass frozen dataclass validation
    with pytest.raises(ValueError):
        store.append(bad)
    assert store.query() == []


def test_append_rejects_duplicate_event_id(store):
    event = _event()
    store.append(event)
    with pytest.raises(sqlite3.IntegrityError):
        store.append(event)


def test_events_after_and_max_id(store):
    first = _event(payload={"i": 1})
    store.append(first)
    marker = store.max_id()
    second = _event(run_id="run-2", payload={"i": 2})
    store.append(second)
    rows = store.events_after(marker)
    assert [event for _, event in rows] == [second]
    assert store.events_after(marker, run_id="run-1") == []
    assert store.max_id() == rows[0][0]
