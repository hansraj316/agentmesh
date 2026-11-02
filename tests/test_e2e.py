"""End to end: `agentmesh up` runs the daemon and the fleet against one store,
and every surface (live stream, events page, run API, dashboard, CLI) sees the
same handoffs."""

import http.client
import json
import socket
import time
import urllib.request

import pytest

from agentmesh import up
from agentmesh.cli import main
from agentmesh.fleet import ScriptedLLM

TIMEOUT_S = 20.0


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


def _first_done_handoff_from_stream(port):
    """Read /api/stream until a handoff closes as done; return that event."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT_S)
    conn.request("GET", "/api/stream?after=0")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")
    deadline = time.monotonic() + TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            line = resp.fp.readline().decode("utf-8")
            if not line.startswith("data:"):
                continue
            event = json.loads(line[len("data:") :].strip())
            payload = event.get("payload") or {}
            if payload.get("kind") == "handoff" and payload.get("status") == "done":
                return event
    finally:
        conn.close()
    raise AssertionError("no handoff finished within %.0fs" % TIMEOUT_S)


@pytest.fixture
def session(store):
    llm = ScriptedLLM(seed=7, delay_s=(0, 0), failure_rate=0.0)
    sess = up.start(store, 0, llm, interval_s=0.05)
    yield sess
    sess.stop()


def test_up_serves_live_handoffs_end_to_end(session, store, db_path, capsys):
    done = _first_done_handoff_from_stream(session.port)
    run_id = done["run_id"]
    task = done["payload"]["task"]
    assert done["payload"]["from"] and done["payload"]["to"]
    assert isinstance(done["payload"].get("duration_ms"), (int, float))

    page = _get_json(session.base_url + "/api/events?after=0&limit=1000")
    assert any(e["event_id"] == done["event_id"] for e in page)
    assert all("id" in e for e in page)

    runs = _get_json(session.base_url + "/api/runs")
    assert run_id in {r["run_id"] for r in runs}

    run = _get_json(session.base_url + "/api/runs/" + run_id)
    assert run

    agents = {a["agent"] for a in _get_json(session.base_url + "/api/agents")}
    assert {"planner", "coder"} <= agents

    with urllib.request.urlopen(session.app_url, timeout=5) as resp:
        assert resp.status == 200
        assert "<title>AgentMesh</title>" in resp.read().decode("utf-8")

    assert main(["handoffs", "--db", str(db_path), "--run", run_id]) == 0
    assert task in capsys.readouterr().out


def test_up_stops_fleet_and_daemon(store):
    llm = ScriptedLLM(seed=1, delay_s=(0, 0))
    sess = up.start(store, 0, llm, interval_s=0.05)
    port = sess.port
    sess.stop()
    sess.stop()  # idempotent
    assert not sess.fleet_thread.is_alive()
    assert not sess.server_thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_up_reports_busy_port(store, capsys, monkeypatch):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    try:
        code = main(
            ["up", "--no-browser", "--port", str(port), "--db", str(store.path), "--seed", "1"]
        )
    finally:
        busy.close()
    assert code == 1
    assert "could not listen" in capsys.readouterr().err


def test_up_cli_prints_urls_and_stops_on_ctrl_c(store, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(up.webbrowser, "open", lambda url: opened.append(url))
    sessions = []
    real_start = up.start

    def start_and_keep(*args, **kwargs):
        sessions.append(real_start(*args, **kwargs))
        return sessions[-1]

    def interrupt(session):
        raise KeyboardInterrupt

    monkeypatch.setattr(up, "start", start_and_keep)
    monkeypatch.setattr(up, "_wait", interrupt)
    code = main(["up", "--port", "0", "--db", str(store.path), "--seed", "1", "--interval", "0"])
    out = capsys.readouterr().out
    assert code == 0
    assert "AgentMesh is up" in out
    assert "/app/" in out
    assert "stopping..." in out
    assert len(opened) == 1 and opened[0].endswith("/app/")
    assert not sessions[0].fleet_thread.is_alive()
    assert not sessions[0].server_thread.is_alive()


def test_up_claude_without_key_is_a_clear_error(store, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = main(["up", "--no-browser", "--llm", "claude", "--db", str(store.path)])
    assert code == 1
    assert "error:" in capsys.readouterr().err
