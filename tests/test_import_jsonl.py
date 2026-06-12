import json

import pytest

from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.jsonl import import_jsonl


def _event_line(event_id, type="message", payload=None):
    return json.dumps(
        {
            "event_id": event_id,
            "run_id": "run-ts",
            "agent": "ts-agent",
            "type": type,
            "ts": "2026-06-10T08:15:30.123Z",
            "payload": payload if payload is not None else {},
        }
    )


@pytest.fixture
def jsonl_file(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                _event_line("e-1", type="agent_start", payload={"span_id": "s-1"}),
                _event_line(
                    "e-2", type="agent_end", payload={"span_id": "s-1", "duration_seconds": 0.5}
                ),
                "",  # blank lines are skipped
                _event_line("e-3"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_import_jsonl_imports_valid_events(jsonl_file, store):
    assert import_jsonl(jsonl_file, store) == (3, 0)
    events = store.query()
    assert [e.event_id for e in events] == ["e-1", "e-2", "e-3"]
    assert events[1].payload["duration_seconds"] == 0.5


def test_reimport_is_idempotent(jsonl_file, store):
    assert import_jsonl(jsonl_file, store) == (3, 0)
    assert import_jsonl(jsonl_file, store) == (0, 3)
    assert len(store.query()) == 3


def test_malformed_json_names_line_number(tmp_path, store):
    path = tmp_path / "bad.jsonl"
    path.write_text(_event_line("e-1") + "\n{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        import_jsonl(path, store)
    assert store.query() == []  # nothing imported from a malformed file


def test_invalid_event_names_line_number(tmp_path, store):
    path = tmp_path / "bad.jsonl"
    path.write_text(_event_line("e-1") + "\n" + _event_line("e-2", type="nope") + "\n")
    with pytest.raises(ValueError, match="line 2"):
        import_jsonl(path, store)


def test_missing_field_names_line_number(tmp_path, store):
    path = tmp_path / "bad.jsonl"
    data = json.loads(_event_line("e-1"))
    del data["agent"]
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1.*agent"):
        import_jsonl(path, store)


def test_cli_imports_and_prints_counts(jsonl_file, store, capsys):
    assert main(["import-jsonl", str(jsonl_file)]) == 0
    assert "imported 3 event(s), skipped 0 duplicate(s)" in capsys.readouterr().out

    assert main(["import-jsonl", str(jsonl_file)]) == 0
    assert "imported 0 event(s), skipped 3 duplicate(s)" in capsys.readouterr().out

    assert len(store.query()) == 3


def test_cli_respects_db_flag(jsonl_file, tmp_path, capsys):
    from agentmesh.store import EventStore

    db = tmp_path / "other.db"
    assert main(["import-jsonl", str(jsonl_file), "--db", str(db)]) == 0
    assert len(EventStore(db).query()) == 3


def test_cli_malformed_line_errors_with_line_number(tmp_path, store, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text(_event_line("e-1") + "\nnot json at all\n", encoding="utf-8")
    assert main(["import-jsonl", str(path)]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "line 2" in err


def test_cli_missing_file_exits_1(tmp_path, store, capsys):
    missing = tmp_path / "nope.jsonl"
    assert main(["import-jsonl", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "nope.jsonl" in err


def test_imported_events_round_trip_into_event_objects(jsonl_file, store):
    import_jsonl(jsonl_file, store)
    for event in store.query():
        assert isinstance(event, Event)
        event.validate()


def test_usage_fields_import_and_show_up_in_costs(tmp_path, store, capsys):
    path = tmp_path / "usage.jsonl"
    payload = {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0034, "model": "m-1"}
    path.write_text(_event_line("e-usage", type="agent_end", payload=payload) + "\n")

    assert import_jsonl(path, store) == (1, 0)
    assert store.query()[0].payload["cost_usd"] == 0.0034

    assert main(["costs"]) == 0
    out = capsys.readouterr().out
    assert "run-ts" in out
    assert "$0.0034" in out
