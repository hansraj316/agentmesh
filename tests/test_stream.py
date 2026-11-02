"""Live event stream: ``GET /api/stream`` over Server-Sent Events."""

import http.client
import json
import threading
import time

import pytest

import agentmesh.server as server_mod
from agentmesh.events import Event
from agentmesh.server import create_server


@pytest.fixture(autouse=True)
def fast_stream(monkeypatch):
    """Poll quickly so tests do not wait on the 250 ms default."""
    monkeypatch.setattr(server_mod, "STREAM_POLL_SECONDS", 0.02)


@pytest.fixture
def daemon(db_path):
    """A running server on a free port; stopped in teardown."""
    server = create_server(db_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class SSEClient:
    """Minimal blocking SSE reader over http.client (stdlib only)."""

    def __init__(self, port, path="/api/stream", headers=None, timeout=5.0):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        self.conn.request("GET", path, headers=headers or {})
        self.response = self.conn.getresponse()

    def next_frame(self):
        """Read one SSE frame; returns a dict of field -> value, or None at EOF."""
        frame = {}
        while True:
            line = self.response.readline()
            if not line:
                return None
            line = line.decode("utf-8").rstrip("\n")
            if line == "":
                if frame:
                    return frame
                continue
            if line.startswith(":"):
                frame.setdefault("comment", line[1:].strip())
                continue
            name, _, value = line.partition(":")
            frame[name] = value[1:] if value.startswith(" ") else value

    def next_events(self, n):
        """Read until ``n`` data frames arrived; returns their decoded JSON."""
        events = []
        while len(events) < n:
            frame = self.next_frame()
            assert frame is not None, "stream closed after %d events" % len(events)
            if "data" in frame:
                data = json.loads(frame["data"])
                assert frame["id"] == str(data["id"])
                events.append(data)
        return events

    def close(self):
        # The response holds its own reference to the socket; close both.
        self.response.close()
        self.conn.close()


def _port(server):
    return server.server_address[1]


def _append(store, run_id, n, agent="planner"):
    events = [Event.new(run_id, agent, "message", {"n": i}) for i in range(n)]
    for event in events:
        store.append(event)
    return events


def test_stream_headers_and_retry_hint(daemon):
    client = SSEClient(_port(daemon))
    try:
        assert client.response.status == 200
        assert client.response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert client.response.getheader("Cache-Control") == "no-cache"
        assert client.response.getheader("Content-Length") is None
        assert client.next_frame() == {"retry": "1000"}
    finally:
        client.close()


def test_stream_replays_existing_events_in_order(daemon, store):
    stored = _append(store, "fleet-1", 3)
    client = SSEClient(_port(daemon))
    try:
        got = client.next_events(3)
    finally:
        client.close()
    assert [e["event_id"] for e in got] == [e.event_id for e in stored]
    assert [e["id"] for e in got] == sorted(e["id"] for e in got)
    first = got[0]
    assert set(first) == {"id", "event_id", "run_id", "agent", "type", "ts", "payload"}
    assert first["payload"] == {"n": 0}
    assert first["run_id"] == "fleet-1"


def test_stream_pushes_events_appended_after_connect(daemon, store):
    client = SSEClient(_port(daemon))
    try:
        assert "retry" in client.next_frame()
        handoff = Event.new(
            "fleet-0007",
            "planner",
            "message",
            {
                "kind": "handoff",
                "task_id": "t-3f2a",
                "task": "Add retry to GitHub sync",
                "from": "planner",
                "to": "coder",
                "status": "running",
            },
        )
        store.append(handoff)
        later = _append(store, "fleet-0007", 2, agent="coder")
        got = client.next_events(3)
    finally:
        client.close()
    assert [e["event_id"] for e in got] == [handoff.event_id] + [e.event_id for e in later]
    assert got[0]["payload"]["kind"] == "handoff"


def test_stream_resumes_after_query_id(daemon, store):
    _append(store, "run-a", 2)
    newer = _append(store, "run-b", 2)
    first_ids = [row_id for row_id, _ in store.events_after(0)]
    client = SSEClient(_port(daemon), path="/api/stream?after=%d" % first_ids[1])
    try:
        got = client.next_events(2)
    finally:
        client.close()
    assert [e["event_id"] for e in got] == [e.event_id for e in newer]
    assert [e["id"] for e in got] == first_ids[2:]


def test_last_event_id_header_wins_over_after(daemon, store):
    events = _append(store, "run-r", 4)
    ids = [row_id for row_id, _ in store.events_after(0)]
    client = SSEClient(
        _port(daemon), path="/api/stream?after=0", headers={"Last-Event-ID": str(ids[2])}
    )
    try:
        got = client.next_events(1)
    finally:
        client.close()
    assert got[0]["event_id"] == events[3].event_id


def test_stream_catches_up_across_batches(daemon, store, monkeypatch):
    monkeypatch.setattr(server_mod, "STREAM_BATCH", 3)
    stored = _append(store, "run-big", 8)
    client = SSEClient(_port(daemon))
    try:
        got = client.next_events(8)
    finally:
        client.close()
    assert [e["event_id"] for e in got] == [e.event_id for e in stored]


def test_stream_sends_ping_comments(daemon, monkeypatch):
    monkeypatch.setattr(server_mod, "STREAM_PING_SECONDS", 0.05)
    client = SSEClient(_port(daemon))
    try:
        frames = [client.next_frame() for _ in range(3)]
    finally:
        client.close()
    assert frames[0] == {"retry": "1000"}
    assert {"comment": "ping"} in frames[1:]


@pytest.mark.parametrize(
    "path, headers",
    [
        ("/api/stream?after=abc", {}),
        ("/api/stream?after=-1", {}),
        ("/api/stream", {"Last-Event-ID": "nope"}),
    ],
)
def test_stream_bad_resume_id_is_400_json(daemon, path, headers):
    conn = http.client.HTTPConnection("127.0.0.1", _port(daemon), timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        assert response.status == 400
        assert response.getheader("Content-Type") == "application/json"
        assert "must be" in json.loads(response.read())["error"]
    finally:
        conn.close()


def _stream_threads():
    return [t for t in threading.enumerate() if "process_request" in t.name and t.is_alive()]


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_stream_handler_exits_when_client_disconnects(daemon):
    client = SSEClient(_port(daemon))
    assert "retry" in client.next_frame()
    assert _wait_until(lambda: len(_stream_threads()) == 1)
    client.close()
    assert _wait_until(lambda: not _stream_threads()), "stream handler still running"


def test_stream_ends_when_server_shuts_down(db_path):
    server = create_server(db_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SSEClient(_port(server))
    try:
        assert "retry" in client.next_frame()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert client.next_frame() is None  # clean EOF, not a hang
    finally:
        client.close()
    assert _wait_until(lambda: not _stream_threads())


def test_stream_sends_cors_header_only_in_dev_mode(daemon, monkeypatch):
    client = SSEClient(_port(daemon))
    try:
        assert client.response.getheader("Access-Control-Allow-Origin") is None
    finally:
        client.close()
    monkeypatch.setenv("AGENTMESH_DEV", "1")
    client = SSEClient(_port(daemon))
    try:
        assert client.response.getheader("Access-Control-Allow-Origin") == ("http://localhost:5173")
    finally:
        client.close()
