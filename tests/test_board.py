import uuid
from datetime import datetime, timezone

import pytest

from agentmesh.board import agent_summaries, render_html, render_markdown
from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.sdk import Mesh

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minute, second=0):
    """A UTC timestamp within the hour before NOW."""
    return "2026-06-10T11:%02d:%02d+00:00" % (minute, second)


def _append(store, run_id, agent, type, ts, payload=None):
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type=type,
            ts=ts,
            payload=payload if payload is not None else {},
        )
    )


def _summary(store, agent, since=None):
    by_name = {summary.agent: summary for summary in agent_summaries(store, since=since)}
    return by_name[agent]


@pytest.fixture
def fleet(store):
    """Three agents: one ok, one mid-run, one on a failure streak."""
    _append(store, "r1", "researcher", "agent_start", _ts(0))
    _append(store, "r1", "researcher", "agent_end", _ts(0, 10), {"duration_seconds": 10.0})
    _append(store, "r2", "researcher", "agent_start", _ts(1))
    _append(store, "r2", "researcher", "agent_end", _ts(1, 5), {"duration_ms": 5000})
    _append(store, "w1", "writer", "agent_start", _ts(57))
    _append(store, "c1", "critic", "agent_start", _ts(10))
    _append(store, "c1", "critic", "agent_error", _ts(10, 1), {"error": "boom"})
    _append(store, "c2", "critic", "agent_start", _ts(11))
    _append(store, "c2", "critic", "agent_error", _ts(11, 1), {"error": "boom"})
    _append(store, "c3", "critic", "agent_start", _ts(12))
    _append(store, "c3", "critic", "agent_error", _ts(12, 1), {"error": "boom"})
    return store


def test_status_ok_when_latest_terminal_is_end(fleet):
    summary = _summary(fleet, "researcher")
    assert summary.status == "ok"
    assert summary.last_event_type == "agent_end"
    assert summary.last_ts == _ts(1, 5)


def test_status_failed_when_latest_terminal_is_error(fleet):
    assert _summary(fleet, "critic").status == "failed"


def test_status_running_when_latest_start_has_no_terminal(fleet):
    assert _summary(fleet, "writer").status == "running"


def test_status_idle_when_no_start_or_terminal_events(store):
    _append(store, "m1", "broker", "message", _ts(5), {"text": "hi"})
    assert _summary(store, "broker").status == "idle"


def test_run_and_error_counts(fleet):
    researcher = _summary(fleet, "researcher")
    critic = _summary(fleet, "critic")
    assert (researcher.runs, researcher.errors) == (2, 0)
    assert (critic.runs, critic.errors) == (3, 3)


def test_streak_counts_consecutive_failures(fleet):
    assert _summary(fleet, "critic").streak == 3


def test_streak_resets_after_a_success(fleet):
    _append(fleet, "c4", "critic", "agent_start", _ts(20))
    _append(fleet, "c4", "critic", "agent_end", _ts(20, 2), {"duration_seconds": 2.0})
    summary = _summary(fleet, "critic")
    assert summary.streak == 0
    assert summary.status == "ok"
    assert summary.errors == 3


def test_avg_duration_prefers_payload_values(fleet):
    # r1 carries duration_seconds=10.0, r2 carries duration_ms=5000 -> 5.0s.
    assert _summary(fleet, "researcher").avg_duration_seconds == pytest.approx(7.5)


def test_avg_duration_computed_from_start_and_end_ts(store):
    _append(store, "p1", "planner", "agent_start", _ts(30))
    _append(store, "p1", "planner", "agent_end", _ts(30, 30))
    assert _summary(store, "planner").avg_duration_seconds == pytest.approx(30.0)


def test_avg_duration_none_without_successful_runs(fleet):
    assert _summary(fleet, "critic").avg_duration_seconds is None
    assert _summary(fleet, "writer").avg_duration_seconds is None


def test_since_filters_out_older_events(fleet):
    summaries = agent_summaries(fleet, since=_ts(50))
    assert [summary.agent for summary in summaries] == ["writer"]


def test_sdk_emitted_events_are_classified(store):
    mesh = Mesh(store)

    @mesh.agent(name="solver")
    def solve(fail):
        if fail:
            raise RuntimeError("kaput")
        return "done"

    assert solve(False) == "done"
    with pytest.raises(RuntimeError):
        solve(True)
    summary = _summary(store, "solver")
    assert summary.status == "failed"
    assert summary.runs == 2
    assert summary.errors == 1
    assert summary.streak == 1
    assert summary.avg_duration_seconds is not None


def test_render_markdown_rows_and_emoji(fleet):
    markdown = render_markdown(agent_summaries(fleet), now=NOW)
    lines = markdown.splitlines()
    header = "| Agent | Status | Last event | Age | Runs | Errors | Streak | Avg duration |"
    assert lines[0] == header
    assert "| critic | 🔴 failed | agent_error | 47m | 3 | 3 | 3 | — |" in lines
    assert "| researcher | 🟢 ok | agent_end | 58m | 2 | 0 | 0 | 7.5s |" in lines
    assert "| writer | 🔵 running | agent_start | 3m | 1 | 0 | 0 | — |" in lines


def test_render_markdown_humanizes_hours_and_days(store):
    _append(store, "h1", "hourly", "agent_start", "2026-06-10T10:00:00+00:00")
    _append(store, "d1", "daily", "agent_start", "2026-06-05T12:00:00+00:00")
    markdown = render_markdown(agent_summaries(store), now=NOW)
    assert "| hourly | 🔵 running | agent_start | 2h |" in markdown
    assert "| daily | 🔵 running | agent_start | 5d |" in markdown


def test_render_html_contains_agents_and_status_classes(fleet):
    page = render_html(agent_summaries(fleet), now=NOW)
    assert page.startswith("<!DOCTYPE html>")
    assert "researcher" in page
    assert '<tr class="failed"><td>critic</td><td>🔴 failed</td>' in page
    assert "<script" not in page  # no JS, ever


def test_render_html_escapes_agent_names(store):
    _append(store, "x1", "evil<script>", "agent_start", _ts(40))
    page = render_html(agent_summaries(store), now=NOW)
    assert "evil&lt;script&gt;" in page
    assert "evil<script>" not in page


def test_cli_board_prints_markdown(fleet, capsys):
    assert main(["board"]) == 0
    out = capsys.readouterr().out
    assert "| Agent | Status |" in out
    assert "researcher" in out
    assert "🔴 failed" in out


def test_cli_board_since_filters(fleet, capsys):
    assert main(["board", "--since", _ts(50)]) == 0
    out = capsys.readouterr().out
    assert "writer" in out
    assert "critic" not in out


def test_cli_board_writes_html_file(fleet, tmp_path, capsys):
    path = tmp_path / "board.html"
    assert main(["board", "--html", str(path)]) == 0
    out = capsys.readouterr().out
    assert "wrote %s" % path in out
    page = path.read_text(encoding="utf-8")
    assert "<table>" in page
    assert "critic" in page


def test_cli_board_writes_markdown_file(fleet, tmp_path, capsys):
    path = tmp_path / "board.md"
    assert main(["board", "--out", str(path)]) == 0
    out = capsys.readouterr().out
    assert "wrote %s" % path in out
    assert "| Agent | Status |" not in out  # markdown went to the file, not stdout
    assert "| researcher |" in path.read_text(encoding="utf-8")
