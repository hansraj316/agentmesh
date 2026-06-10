import pytest

from agentmesh.events import EVENT_TYPES, Event


def test_new_builds_valid_event_with_uuid_and_utc_ts():
    event = Event.new("run-1", "researcher", "agent_start", {"function": "f"})
    assert event.run_id == "run-1"
    assert event.agent == "researcher"
    assert event.type == "agent_start"
    assert event.payload == {"function": "f"}
    assert len(event.event_id) == 36  # uuid4 canonical form
    assert event.ts.endswith("+00:00")


def test_event_types_match_spec():
    assert EVENT_TYPES == {"agent_start", "agent_end", "agent_error", "message", "tool_call"}


def test_invalid_type_rejected():
    with pytest.raises(ValueError, match="type"):
        Event.new("run-1", "a", "agent.started", {})


@pytest.mark.parametrize("field", ["event_id", "run_id", "agent"])
def test_empty_string_fields_rejected(field):
    kwargs = {
        "event_id": "e1",
        "run_id": "r1",
        "agent": "a1",
        "type": "message",
        "ts": "2026-06-10T00:00:00+00:00",
        "payload": {},
    }
    kwargs[field] = ""
    with pytest.raises(ValueError, match=field):
        Event(**kwargs)


@pytest.mark.parametrize(
    "ts",
    [
        "not-a-timestamp",
        "2026-06-10T00:00:00",  # naive, no timezone
        "2026-06-10T00:00:00+05:30",  # not UTC
    ],
)
def test_bad_ts_rejected(ts):
    with pytest.raises(ValueError, match="ts"):
        Event(event_id="e1", run_id="r1", agent="a1", type="message", ts=ts, payload={})


def test_zulu_suffix_ts_accepted():
    event = Event(
        event_id="e1",
        run_id="r1",
        agent="a1",
        type="message",
        ts="2026-06-10T00:00:00Z",
        payload={},
    )
    assert event.ts == "2026-06-10T00:00:00Z"


def test_non_dict_payload_rejected():
    with pytest.raises(ValueError, match="payload"):
        Event(
            event_id="e1",
            run_id="r1",
            agent="a1",
            type="message",
            ts="2026-06-10T00:00:00+00:00",
            payload="nope",
        )


def test_dict_round_trip():
    event = Event.new("run-9", "writer", "tool_call", {"tool": "web_search"})
    assert Event.from_dict(event.to_dict()) == event
