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


def _post(url, body, content_type="application/json"):
    """POST raw ``body`` bytes; returns (status, decoded JSON) even for errors."""
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_json(url, data, content_type="application/json"):
    return _post(url, json.dumps(data).encode("utf-8"), content_type=content_type)


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

    def start(rules_path=None, static_dir=None):
        server = create_server(db_path, 0, rules_path=rules_path, static_dir=static_dir)
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


def test_post_single_event_accepted_and_stored(serve_url, db_path):
    event = Event.new("run-post", "ingestor", "message", {"text": "hi"})
    status, data = _post_json(serve_url() + "/api/events", event.to_dict())
    assert status == 202
    assert data == {"accepted": 1, "skipped": 0}
    stored = EventStore(db_path).query(run_id="run-post")
    assert [e.event_id for e in stored] == [event.event_id]
    assert stored[0].payload == {"text": "hi"}


def test_post_array_accepts_all_events(serve_url, db_path):
    events = [Event.new("run-batch", "a-%d" % i, "agent_start", {}) for i in range(3)]
    status, data = _post_json(serve_url() + "/api/events", [e.to_dict() for e in events])
    assert status == 202
    assert data == {"accepted": 3, "skipped": 0}
    stored = EventStore(db_path).query(run_id="run-batch")
    assert [e.event_id for e in stored] == [e.event_id for e in events]


def test_post_duplicate_events_are_skipped(serve_url, db_path):
    url = serve_url() + "/api/events"
    events = [Event.new("run-dup", "a", "agent_start", {}) for _ in range(2)]
    body = [e.to_dict() for e in events]
    assert _post_json(url, body) == (202, {"accepted": 2, "skipped": 0})
    assert _post_json(url, body) == (202, {"accepted": 0, "skipped": 2})
    assert len(EventStore(db_path).query(run_id="run-dup")) == 2


def test_post_invalid_event_in_array_rejects_whole_batch(serve_url, db_path):
    before = EventStore(db_path).count_events()
    good = Event.new("run-bad", "a", "agent_start", {}).to_dict()
    bad = dict(good, event_id="e-bad", type="not-a-type")
    status, data = _post_json(serve_url() + "/api/events", [good, bad])
    assert status == 400
    assert "index 1" in data["error"]
    assert "type" in data["error"]
    # all-or-nothing: the valid event at index 0 was not written either
    assert EventStore(db_path).count_events() == before


def test_post_malformed_json_is_400(serve_url):
    status, data = _post(serve_url() + "/api/events", b"{not json")
    assert status == 400
    assert "JSON" in data["error"]


def test_post_wrong_content_type_is_415(serve_url):
    event = Event.new("run-ct", "a", "message", {}).to_dict()
    status, data = _post_json(serve_url() + "/api/events", event, content_type="text/plain")
    assert status == 415
    assert "application/json" in data["error"]


def test_post_more_than_1000_events_is_413(serve_url, db_path):
    before = EventStore(db_path).count_events()
    events = [Event.new("run-big", "a", "message", {}).to_dict() for _ in range(1001)]
    status, data = _post_json(serve_url() + "/api/events", events)
    assert status == 413
    assert "1001" in data["error"]
    assert "1000" in data["error"]
    assert EventStore(db_path).count_events() == before


def test_post_unknown_path_is_404_json(serve_url):
    status, data = _post_json(serve_url() + "/api/unknown", {})
    assert status == 404
    assert "/api/unknown" in data["error"]


def test_get_routes_unaffected_after_post(serve_url, db_path):
    url = serve_url()
    event = Event.new("run-mix", "poster", "agent_start", {"span_id": "sp-p"})
    assert _post_json(url + "/api/events", event.to_dict())[0] == 202
    agents = _get_json(url + "/api/agents")
    assert "poster" in [agent["agent"] for agent in agents]
    status, content_type, body = _get(url + "/")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert _get_json(url + "/api/runs/" + RUN_ID)["run_id"] == RUN_ID


def test_cli_serve_prints_url_and_exits_zero_on_interrupt(monkeypatch, capsys):
    calls = []

    def fake_serve(db_path, port, rules_path=None):
        calls.append((db_path, port, rules_path))
        raise KeyboardInterrupt

    monkeypatch.setattr("agentmesh.cli.serve", fake_serve)
    assert main(["serve", "--port", "7878"]) == 0
    assert "agentmesh board at http://127.0.0.1:7878/" in capsys.readouterr().out
    assert calls == [(None, 7878, None)]


# --- GET /api/events (paged read of the stream contract) -------------------


def test_get_events_returns_stored_events_with_row_ids(serve_url, db_path):
    data = _get_json(serve_url() + "/api/events")
    stored = EventStore(db_path).events_after(0)
    assert [item["id"] for item in data] == [row_id for row_id, _ in stored]
    assert [item["event_id"] for item in data] == [event.event_id for _, event in stored]
    first = data[0]
    assert set(first) == {"id", "event_id", "run_id", "agent", "type", "ts", "payload"}
    assert first["payload"] == {"span_id": "sp-root"}


def test_get_events_pages_with_after_and_limit(serve_url, db_path):
    ids = [row_id for row_id, _ in EventStore(db_path).events_after(0)]
    url = serve_url() + "/api/events"
    page = _get_json(url + "?after=%d&limit=2" % ids[0])
    assert [item["id"] for item in page] == ids[1:3]
    assert _get_json(url + "?after=%d" % ids[-1]) == []


def test_get_events_default_and_max_limit(serve_url, store):
    for i in range(1205):
        store.append(Event.new("run-many", "bulk", "message", {"i": i}))
    url = serve_url() + "/api/events"
    assert len(_get_json(url)) == 200
    assert len(_get_json(url + "?limit=5000")) == 1000


@pytest.mark.parametrize("query", ["after=x", "after=-1", "limit=0", "limit=ten"])
def test_get_events_bad_params_are_400(serve_url, query):
    data = _get_json(serve_url() + "/api/events?" + query, expected_status=400)
    assert "must be" in data["error"]


# --- GET /api/runs ----------------------------------------------------------


def test_api_runs_lists_recent_runs_newest_first(serve_url, store):
    store.append(Event.new("run-live", "coder", "agent_start", {"span_id": "sp-c"}))
    store.append(Event.new("run-msgs", "planner", "message", {"text": "hi"}))
    data = _get_json(serve_url() + "/api/runs")
    assert [run["run_id"] for run in data] == ["run-msgs", "run-live", RUN_ID]
    by_id = {run["run_id"]: run for run in data}

    seeded = by_id[RUN_ID]
    assert set(seeded) == {
        "run_id",
        "started_at",
        "ended_at",
        "status",
        "span_count",
        "failed_count",
        "event_count",
    }
    assert seeded["status"] == "failed"
    assert seeded["span_count"] == 2
    assert seeded["failed_count"] == 1
    assert seeded["event_count"] == 4
    assert seeded["started_at"] <= seeded["ended_at"]

    live = by_id["run-live"]
    assert live["status"] == "running"
    assert live["ended_at"] is None
    assert live["span_count"] == 1

    msgs = by_id["run-msgs"]
    assert msgs["status"] == "ok"
    assert msgs["span_count"] == 0
    assert msgs["ended_at"] == msgs["started_at"]


def test_api_runs_limit(serve_url, store):
    for i in range(3):
        store.append(Event.new("run-%d" % i, "a", "message", {}))
    url = serve_url() + "/api/runs"
    assert [run["run_id"] for run in _get_json(url + "?limit=2")] == ["run-2", "run-1"]
    assert "must be" in _get_json(url + "?limit=0", expected_status=400)["error"]


def test_api_runs_empty_store_is_empty_list(tmp_path):
    server = create_server(tmp_path / "empty.db", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d/api/runs" % server.server_address[1]
        assert _get_json(url) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_run_detail_route_unchanged_next_to_runs_list(serve_url):
    url = serve_url()
    assert isinstance(_get_json(url + "/api/runs"), list)
    assert _get_json(url + "/api/runs/" + RUN_ID)["run_id"] == RUN_ID


# --- /app/ static dashboard hosting -----------------------------------------


@pytest.fixture
def bundle(tmp_path):
    """A fake built dashboard: index.html, a hashed asset and a favicon."""
    root = tmp_path / "app"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><div id="root"></div><script src="/app/assets/index-abc.js"></script>',
        encoding="utf-8",
    )
    (root / "assets" / "index-abc.js").write_text("console.log('mesh')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the bundle", encoding="utf-8")
    return root


def _get_headers(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.getcode(), response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_app_serves_index_html(serve_url, bundle):
    status, headers, body = _get_headers(serve_url(static_dir=bundle) + "/app/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache"
    assert b'<div id="root">' in body


def test_app_without_slash_redirects_to_slash(serve_url, bundle):
    status, headers, body = _get_headers(serve_url(static_dir=bundle) + "/app")
    # urllib follows the 301, so we land on the index page
    assert status == 200
    assert b'<div id="root">' in body


def test_app_serves_assets_with_types_and_caching(serve_url, bundle):
    url = serve_url(static_dir=bundle)
    status, headers, body = _get_headers(url + "/app/assets/index-abc.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert "immutable" in headers["Cache-Control"]
    assert body == b"console.log('mesh')"
    status, headers, _ = _get_headers(url + "/app/favicon.svg")
    assert status == 200
    assert headers["Content-Type"] == "image/svg+xml"
    assert headers["Cache-Control"] == "no-cache"


def test_app_client_routes_fall_back_to_index(serve_url, bundle):
    url = serve_url(static_dir=bundle)
    for route in ("/app/handoffs", "/app/runs/fleet-0007"):
        status, headers, body = _get_headers(url + route)
        assert status == 200, route
        assert b'<div id="root">' in body


def test_app_missing_asset_is_404_not_index(serve_url, bundle):
    data = _get_json(serve_url(static_dir=bundle) + "/app/assets/gone.js", expected_status=404)
    assert "gone.js" in data["error"]


def test_app_blocks_path_traversal(serve_url, bundle):
    url = serve_url(static_dir=bundle)
    for path in (
        "/app/../secret.txt",
        "/app/%2e%2e/secret.txt",
        "/app/assets/%2e%2e/%2e%2e/secret.txt",
    ):
        status, _, body = _get_headers(url + path)
        assert b"outside the bundle" not in body, path
        assert status in (200, 404), path


def test_app_missing_bundle_explains_how_to_build(serve_url, tmp_path):
    status, headers, body = _get_headers(serve_url(static_dir=tmp_path / "nope") + "/app/")
    assert status == 404
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert b"npm run build" in body
    assert b"dashboard bundle not found" in body


def test_app_does_not_shadow_api_or_board(serve_url, bundle):
    url = serve_url(static_dir=bundle)
    assert _get(url + "/")[0] == 200
    assert isinstance(_get_json(url + "/api/agents"), list)
    assert "/apples" in _get_json(url + "/apples", expected_status=404)["error"]


# --- dev-mode CORS ----------------------------------------------------------


def test_no_cors_header_by_default(serve_url):
    status, headers, _ = _get_headers(serve_url() + "/api/agents")
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] is None


def test_cors_header_for_vite_in_dev_mode(serve_url, monkeypatch):
    monkeypatch.setenv("AGENTMESH_DEV", "1")
    url = serve_url()
    for path in ("/api/agents", "/api/events", "/api/runs", "/nope"):
        _, headers, _ = _get_headers(url + path)
        assert headers["Access-Control-Allow-Origin"] == "http://localhost:5173", path


def test_cors_only_for_exact_dev_flag(serve_url, monkeypatch):
    monkeypatch.setenv("AGENTMESH_DEV", "true")
    _, headers, _ = _get_headers(serve_url() + "/api/agents")
    assert headers["Access-Control-Allow-Origin"] is None
