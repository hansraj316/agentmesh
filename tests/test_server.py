import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict

import pytest

from agentmesh.board import agent_summaries
from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.server import create_server
from agentmesh.store import EventStore
from agentmesh.trace import build_trace

RUN_ID = "run-7"


def _get(url):
    """GET ``url``; returns (status, content_type, body) even for HTTP errors."""
    try:
        with urllib.request.urlopen(url) as response:
            return response.getcode(), response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def _get_json(url, expected_status=200):
    status, content_type, body = _get(url)
    assert status == expected_status
    assert content_type == "application/json"
    return json.loads(body.decode("utf-8"))


@pytest.fixture
def seeded(store):
    """One run: orchestrator (ok) with a nested writer span that failed."""
    store.append(Event.new(RUN_ID, "orchestrator", "agent_start", {"span_id": "sp-root"}))
    store.append(
        Event.new(RUN_ID, "writer", "agent_start", {"span_id": "sp-w", "parent_span_id": "sp-root"})
    )
    store.append(
        Event.new(
            RUN_ID,
            "writer",
            "agent_error",
            {"span_id": "sp-w", "error": "kaput", "error_type": "ValueError"},
        )
    )
    store.append(
        Event.new(
            RUN_ID, "orchestrator", "agent_end", {"span_id": "sp-root", "duration_seconds": 2.5}
        )
    )
    return store


@pytest.fixture
def serve_url(db_path, seeded):
    """Factory starting a server on a free port; all servers stop in teardown."""
    started = []

    def start(rules_path=None):
        server = create_server(db_path, 0, rules_path=rules_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return "http://127.0.0.1:%d" % server.server_address[1]

    yield start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_index_serves_board_with_auto_refresh(serve_url):
    status, content_type, body = _get(serve_url() + "/")
    page = body.decode("utf-8")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert '<meta http-equiv="refresh" content="5">' in page
    assert "orchestrator" in page
    assert "writer" in page


def test_api_agents_matches_board_summaries(serve_url, db_path):
    data = _get_json(serve_url() + "/api/agents")
    expected = [asdict(summary) for summary in agent_summaries(EventStore(db_path))]
    assert data == expected
    assert [agent["agent"] for agent in data] == ["orchestrator", "writer"]
    assert data[1]["status"] == "failed"
    assert data[1]["streak"] == 1


def test_api_runs_returns_span_tree(serve_url, db_path):
    data = _get_json(serve_url() + "/api/runs/" + RUN_ID)
    assert data == asdict(build_trace(EventStore(db_path), RUN_ID))
    assert data["run_id"] == RUN_ID
    assert data["span_count"] == 2
    assert data["failed_count"] == 1
    root = data["roots"][0]
    assert root["agent"] == "orchestrator"
    assert root["status"] == "ok"
    assert root["children"][0]["agent"] == "writer"
    assert root["children"][0]["error_type"] == "ValueError"


def test_api_runs_unknown_run_is_404_json(serve_url):
    data = _get_json(serve_url() + "/api/runs/run-missing", expected_status=404)
    assert "run-missing" in data["error"]


def test_api_alerts_fires_with_rules_file(serve_url, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps([{"name": "broken", "type": "failure_streak", "agent": "*", "min_streak": 1}]),
        encoding="utf-8",
    )
    data = _get_json(serve_url(rules_path=rules_path) + "/api/alerts")
    assert len(data) == 1
    assert data[0]["source"] == "agentmesh"
    assert data[0]["severity"] == "critical"
    assert data[0]["title"] == "broken: writer"


def test_api_alerts_empty_without_rules(serve_url, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ALERTS", str(tmp_path / "no-rules.json"))
    assert _get_json(serve_url() + "/api/alerts") == []


def test_unknown_path_is_404_json(serve_url):
    data = _get_json(serve_url() + "/nope", expected_status=404)
    assert "/nope" in data["error"]


def test_cli_serve_prints_url_and_exits_zero_on_interrupt(monkeypatch, capsys):
    calls = []

    def fake_serve(db_path, port, rules_path=None):
        calls.append((db_path, port, rules_path))
        raise KeyboardInterrupt

    monkeypatch.setattr("agentmesh.cli.serve", fake_serve)
    assert main(["serve", "--port", "7878"]) == 0
    assert "agentmesh board at http://127.0.0.1:7878/" in capsys.readouterr().out
    assert calls == [(None, 7878, None)]
