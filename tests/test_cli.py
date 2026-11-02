import json

import pytest

from agentmesh.cli import main
from agentmesh.events import Event


@pytest.fixture
def populated_store(store):
    store.append(Event.new("run-1", "researcher", "agent_start", {"function": "r"}))
    store.append(Event.new("run-1", "researcher", "agent_end", {"duration_ms": 5.0}))
    store.append(Event.new("run-2", "writer", "agent_start", {"function": "w"}))
    store.append(Event.new("run-2", "writer", "agent_error", {"error": "kaput"}))
    return store


def _lines(capsys):
    out = capsys.readouterr().out
    return [line for line in out.splitlines() if line]


def test_tail_prints_one_event_per_line(populated_store, capsys):
    assert main(["tail"]) == 0
    lines = _lines(capsys)
    assert len(lines) == 4
    assert "agent=researcher" in lines[0]
    assert "type=agent_start" in lines[0]
    assert "run=run-1" in lines[0]
    assert "agent=writer" in lines[3]
    assert "type=agent_error" in lines[3]
    assert json.dumps({"error": "kaput"}) in lines[3]


def test_tail_filters_by_run(populated_store, capsys):
    assert main(["tail", "--run", "run-2"]) == 0
    lines = _lines(capsys)
    assert len(lines) == 2
    assert all("run=run-2" in line for line in lines)


def test_tail_filters_by_agent(populated_store, capsys):
    assert main(["tail", "--agent", "researcher"]) == 0
    lines = _lines(capsys)
    assert len(lines) == 2
    assert all("agent=researcher" in line for line in lines)


def test_tail_limit_keeps_most_recent(populated_store, capsys):
    assert main(["tail", "-n", "1"]) == 0
    lines = _lines(capsys)
    assert len(lines) == 1
    assert "type=agent_error" in lines[0]  # the newest event


def test_tail_empty_db_prints_nothing(store, capsys):
    assert main(["tail"]) == 0
    assert _lines(capsys) == []


def test_unknown_command_exits_nonzero(db_path):
    with pytest.raises(SystemExit) as excinfo:
        main(["bogus"])
    assert excinfo.value.code != 0


@pytest.mark.parametrize("command", ["handoffs", "fleet", "up"])
def test_end_to_end_commands_are_registered(command, capsys):
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    assert "usage: agentmesh %s" % command in capsys.readouterr().out


def test_fleet_once_then_handoffs(db_path, capsys, monkeypatch):
    from agentmesh.fleet import llm as fleet_llm

    real = fleet_llm.make_llm

    def fast(kind="scripted", seed=None):
        model = real(kind, seed=seed)
        model.delay_s = (0, 0)
        return model

    monkeypatch.setattr("agentmesh.fleet.cli.make_llm", fast)
    assert main(["fleet", "--once", "--seed", "3", "--db", str(db_path)]) == 0
    assert "fleet-" in capsys.readouterr().out
    assert main(["handoffs", "--db", str(db_path)]) == 0
    assert "planner" in capsys.readouterr().out
