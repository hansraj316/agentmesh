import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict

import pytest

from agentmesh.annotations import add_annotation, annotations_for_run, annotations_summary
from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.server import create_server
from agentmesh.store import EventStore
from agentmesh.trace import build_trace, render_tree

RUN_ID = "run-anno"


def _seed_run(store, run_id=RUN_ID, agent="orchestrator"):
    store.append(
        Event(
            event_id="e-start-%s" % run_id,
            run_id=run_id,
            agent=agent,
            type="agent_start",
            ts="2026-06-12T08:00:00+00:00",
            payload={"span_id": "sp-%s" % run_id},
        )
    )
    store.append(
        Event(
            event_id="e-end-%s" % run_id,
            run_id=run_id,
            agent=agent,
            type="agent_end",
            ts="2026-06-12T08:00:05+00:00",
            payload={"span_id": "sp-%s" % run_id, "duration_seconds": 5.0},
        )
    )
    return store


# -------------------------------------------------------------- add_annotation


def test_add_annotation_event_matches_the_convention(store):
    _seed_run(store)
    event = add_annotation(store, RUN_ID, "deploy happened here", author="alice")
    assert event.type == "message"
    assert event.agent == "operator"
    assert event.run_id == RUN_ID
    assert event.payload == {
        "kind": "annotation",
        "text": "deploy happened here",
        "author": "alice",
    }


def test_add_annotation_without_author_omits_the_key(store):
    _seed_run(store)
    event = add_annotation(store, RUN_ID, "rolled back")
    assert event.payload == {"kind": "annotation", "text": "rolled back"}


def test_add_annotation_persists_the_event(store):
    _seed_run(store)
    event = add_annotation(store, RUN_ID, "investigating")
    stored = [e for _, e in store.events_after(0, run_id=RUN_ID)]
    assert [e.event_id for e in stored][-1] == event.event_id
    assert stored[-1].payload["text"] == "investigating"


def test_add_annotation_custom_agent_and_now(store):
    _seed_run(store)
    event = add_annotation(
        store, RUN_ID, "canary", agent="release-bot", now="2026-06-12T09:00:00+00:00"
    )
    assert event.agent == "release-bot"
    assert event.ts == "2026-06-12T09:00:00+00:00"


def test_add_annotation_unknown_run_raises(store):
    with pytest.raises(ValueError, match="run-missing"):
        add_annotation(store, "run-missing", "deploy happened here")
    assert store.count_events() == 0


@pytest.mark.parametrize("text", ["", "   ", None, 42])
def test_add_annotation_rejects_empty_or_non_string_text(store, text):
    _seed_run(store)
    with pytest.raises(ValueError, match="text"):
        add_annotation(store, RUN_ID, text)


def test_add_annotation_rejects_empty_author(store):
    _seed_run(store)
    with pytest.raises(ValueError, match="author"):
        add_annotation(store, RUN_ID, "deploy", author="  ")


# --------------------------------------------------------- annotations_for_run


def test_annotations_for_run_is_chronological(store):
    _seed_run(store)
    add_annotation(store, RUN_ID, "second", now="2026-06-12T10:00:00+00:00")
    add_annotation(store, RUN_ID, "first", author="bob", now="2026-06-12T09:00:00+00:00")
    notes = annotations_for_run(store, RUN_ID)
    assert notes == [
        {"ts": "2026-06-12T09:00:00+00:00", "text": "first", "author": "bob"},
        {"ts": "2026-06-12T10:00:00+00:00", "text": "second", "author": None},
    ]


def test_annotations_for_run_ignores_plain_message_events(store):
    _seed_run(store)
    store.append(Event.new(RUN_ID, "writer", "message", {"text": "not an annotation"}))
    store.append(Event.new(RUN_ID, "writer", "message", {"kind": "other", "text": "nope"}))
    assert annotations_for_run(store, RUN_ID) == []


def test_annotations_for_run_empty_for_unknown_run(store):
    assert annotations_for_run(store, "run-missing") == []


# --------------------------------------------------------- annotations_summary


def test_summary_is_newest_first_across_runs(store):
    _seed_run(store, "run-a")
    _seed_run(store, "run-b")
    add_annotation(store, "run-a", "older", now="2026-06-12T09:00:00+00:00")
    add_annotation(store, "run-b", "newer", author="alice", now="2026-06-12T11:00:00+00:00")
    assert annotations_summary(store) == [
        {"run_id": "run-b", "ts": "2026-06-12T11:00:00+00:00", "text": "newer", "author": "alice"},
        {"run_id": "run-a", "ts": "2026-06-12T09:00:00+00:00", "text": "older", "author": None},
    ]


def test_summary_since_keeps_annotations_at_or_after_cutoff(store):
    _seed_run(store)
    add_annotation(store, RUN_ID, "old", now="2026-06-12T09:00:00+00:00")
    add_annotation(store, RUN_ID, "boundary", now="2026-06-12T10:00:00+00:00")
    add_annotation(store, RUN_ID, "new", now="2026-06-12T11:00:00+00:00")
    notes = annotations_summary(store, since="2026-06-12T10:00:00+00:00")
    assert [note["text"] for note in notes] == ["new", "boundary"]


def test_summary_invalid_since_raises(store):
    with pytest.raises(ValueError, match="ISO-8601"):
        annotations_summary(store, since="not-a-timestamp")


# ---------------------------------------------------------------------- trace


def test_render_tree_lists_annotations_after_the_tree(store):
    _seed_run(store)
    add_annotation(store, RUN_ID, "deploy happened here", author="alice")
    add_annotation(store, RUN_ID, "rolled back")
    output = render_tree(build_trace(store, RUN_ID), annotations=annotations_for_run(store, RUN_ID))
    lines = output.splitlines()
    assert lines[1] == "└─ orchestrator ✓ 5.0s"
    assert lines[2] == ""
    assert lines[3] == "Annotations:"
    assert lines[4].endswith("— deploy happened here (alice)")
    assert lines[5].endswith("— rolled back")


def test_render_tree_without_annotations_is_unchanged(store):
    _seed_run(store)
    trace = build_trace(store, RUN_ID)
    assert render_tree(trace, annotations=[]) == render_tree(trace)
    assert "Annotations:" not in render_tree(trace)


# --------------------------------------------------------------------- server


def _get(url):
    """GET ``url``; returns (status, decoded JSON) even for HTTP errors."""
    try:
        with urllib.request.urlopen(url) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def serve_url(db_path, store):
    server = create_server(db_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_api_runs_includes_annotations_when_present(serve_url, store, db_path):
    _seed_run(store)
    add_annotation(store, RUN_ID, "deploy happened here", author="alice")
    status, data = _get(serve_url + "/api/runs/" + RUN_ID)
    assert status == 200
    assert data["annotations"] == annotations_for_run(EventStore(db_path), RUN_ID)
    assert data["annotations"][0]["text"] == "deploy happened here"
    assert data["span_count"] == 1


def test_api_runs_without_annotations_matches_plain_trace(serve_url, store, db_path):
    _seed_run(store)
    status, data = _get(serve_url + "/api/runs/" + RUN_ID)
    assert status == 200
    assert "annotations" not in data
    assert data == asdict(build_trace(EventStore(db_path), RUN_ID))


def test_api_runs_unknown_run_is_404_json(serve_url):
    status, data = _get(serve_url + "/api/runs/run-missing")
    assert status == 404
    assert "run-missing" in data["error"]


def test_api_annotations_lists_newest_first(serve_url, store):
    _seed_run(store, "run-a")
    _seed_run(store, "run-b")
    add_annotation(store, "run-a", "older", now="2026-06-12T09:00:00+00:00")
    add_annotation(store, "run-b", "newer", now="2026-06-12T11:00:00+00:00")
    status, data = _get(serve_url + "/api/annotations")
    assert status == 200
    assert [(note["run_id"], note["text"]) for note in data] == [
        ("run-b", "newer"),
        ("run-a", "older"),
    ]


def test_api_annotations_since_filters(serve_url, store):
    _seed_run(store)
    add_annotation(store, RUN_ID, "old", now="2026-06-12T09:00:00+00:00")
    add_annotation(store, RUN_ID, "new", now="2026-06-12T11:00:00+00:00")
    status, data = _get(serve_url + "/api/annotations?since=2026-06-12T10:00:00Z")
    assert status == 200
    assert [note["text"] for note in data] == ["new"]


def test_api_annotations_empty_store_is_empty_list(serve_url):
    assert _get(serve_url + "/api/annotations") == (200, [])


def test_api_annotations_bad_since_is_400_json(serve_url):
    status, data = _get(serve_url + "/api/annotations?since=banana")
    assert status == 400
    assert "ISO-8601" in data["error"]


# ------------------------------------------------------------------------ CLI


def test_cli_annotate_happy_path(store, db_path, capsys):
    _seed_run(store)
    code = main(
        ["annotate", RUN_ID, "deploy happened here", "--author", "alice", "--db", str(db_path)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert 'annotated run %s: "deploy happened here" (alice)' % RUN_ID in out
    notes = annotations_for_run(EventStore(db_path), RUN_ID)
    assert [(note["text"], note["author"]) for note in notes] == [("deploy happened here", "alice")]


def test_cli_annotate_unknown_run_exits_1(store, db_path, capsys):
    assert main(["annotate", "run-missing", "some text", "--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run-missing" in captured.err


def test_cli_annotate_empty_text_exits_1(store, db_path, capsys):
    _seed_run(store)
    assert main(["annotate", RUN_ID, "   ", "--db", str(db_path)]) == 1
    assert "text" in capsys.readouterr().err


def test_cli_trace_shows_annotations_section(store, db_path, capsys):
    _seed_run(store)
    add_annotation(store, RUN_ID, "deploy happened here", author="alice")
    assert main(["trace", RUN_ID, "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "└─ orchestrator ✓" in out
    assert "Annotations:" in out
    assert "deploy happened here (alice)" in out


def test_cli_trace_without_annotations_has_no_section(store, db_path, capsys):
    _seed_run(store)
    assert main(["trace", RUN_ID, "--db", str(db_path)]) == 0
    assert "Annotations:" not in capsys.readouterr().out


def test_cli_annotate_honors_config_file_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTMESH_DB", raising=False)
    db_file = tmp_path / "configured.db"
    _seed_run(EventStore(db_file))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"db": str(db_file)}), encoding="utf-8")
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config_path))
    assert main(["annotate", RUN_ID, "from config db"]) == 0
    assert "annotated run %s" % RUN_ID in capsys.readouterr().out
    assert [note["text"] for note in annotations_for_run(EventStore(db_file), RUN_ID)] == [
        "from config db"
    ]
