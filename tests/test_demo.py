from datetime import datetime, timedelta, timezone

import pytest

from agentmesh.alerts import evaluate, load_rules
from agentmesh.board import agent_summaries, render_markdown
from agentmesh.cli import main
from agentmesh.costs import usage_summary
from agentmesh.demo import (
    DEMO_MODEL,
    DEMO_RULES_FILENAME,
    GHA_AGENT,
    RUN_BASELINE,
    RUN_GHA,
    RUN_REGRESSED,
    RUN_SILENT,
    SILENT_AGENT,
    demo_events,
    demo_tour,
    seed_demo,
    write_demo_rules,
)
from agentmesh.diff import diff_traces, render_diff
from agentmesh.store import EventStore
from agentmesh.trace import build_trace, render_tree

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(store):
    summary = seed_demo(store, now=NOW)
    return store, summary


def _all_events(store):
    return [event for _, event in store.events_after(0)]


# ---------------------------------------------------------------- determinism


def test_seeding_is_fully_deterministic_including_event_ids(tmp_path):
    store_a = EventStore(tmp_path / "a.db")
    store_b = EventStore(tmp_path / "b.db")
    seed_demo(store_a, now=NOW)
    seed_demo(store_b, now=NOW)
    events_a = [event.to_dict() for event in _all_events(store_a)]
    events_b = [event.to_dict() for event in _all_events(store_b)]
    assert events_a == events_b


def test_all_timestamps_derive_from_injected_now():
    shift = timedelta(days=3, hours=7)
    by_id = {event.event_id: event.ts for event in demo_events(now=NOW)}
    for event in demo_events(now=NOW + shift):
        original = datetime.fromisoformat(by_id[event.event_id])
        assert datetime.fromisoformat(event.ts) == original + shift


def test_naive_now_is_treated_as_utc():
    aware = demo_events(now=NOW)
    naive = demo_events(now=NOW.replace(tzinfo=None))
    assert [event.to_dict() for event in aware] == [event.to_dict() for event in naive]


# ------------------------------------------------------------------ structure


def test_summary_counts_match_store(seeded):
    store, summary = seeded
    assert summary == {"runs": 4, "agents": 6, "events": 19}
    assert store.count_events() == 19
    assert store.count_distinct_runs() == 4
    assert store.count_distinct_agents() == 6


def test_baseline_run_is_all_ok_with_usage(seeded):
    store, _ = seeded
    events = [event for _, event in store.events_after(0, run_id=RUN_BASELINE)]
    assert {event.agent for event in events} == {
        "orchestrator",
        "researcher",
        "writer",
        "reviewer",
    }
    assert all(event.type != "agent_error" for event in events)
    ends = [event for event in events if event.type == "agent_end"]
    assert len(ends) == 4
    for event in ends:
        assert event.payload["model"] == DEMO_MODEL
        assert event.payload["input_tokens"] > 0
        assert event.payload["output_tokens"] > 0
        assert event.payload["cost_usd"] > 0


def test_regressed_run_writer_fails_and_is_40_percent_slower(seeded):
    store, _ = seeded
    errors = [
        event
        for _, event in store.events_after(0, run_id=RUN_REGRESSED)
        if event.type == "agent_error"
    ]
    assert [event.agent for event in errors] == ["writer"]
    assert errors[0].payload["error_type"] == "APITimeoutError"
    assert "timed out" in errors[0].payload["error"]
    baseline = _duration(store, RUN_BASELINE, "writer")
    regressed = errors[0].payload["duration_seconds"]
    assert regressed == pytest.approx(baseline * 1.4)


def _duration(store, run_id, agent):
    for _, event in store.events_after(0, run_id=run_id, agent=agent):
        if event.type in ("agent_end", "agent_error"):
            return event.payload["duration_seconds"]
    raise AssertionError("no terminal event for %s in %s" % (agent, run_id))


def test_gha_run_is_flat_with_conclusion_payload(seeded):
    store, _ = seeded
    events = [event for _, event in store.events_after(0, run_id=RUN_GHA)]
    assert [event.type for event in events] == ["agent_start", "agent_end"]
    assert all(event.agent == GHA_AGENT for event in events)
    assert events[1].payload["conclusion"] == "success"
    assert "html_url" in events[1].payload


def test_silent_agent_has_one_event_three_hours_old(seeded):
    store, _ = seeded
    events = [event for _, event in store.events_after(0, agent=SILENT_AGENT)]
    assert len(events) == 1
    assert events[0].run_id == RUN_SILENT
    assert datetime.fromisoformat(events[0].ts) == NOW - timedelta(hours=3)


# ----------------------------------------------------------- real consumers


def test_board_renders_both_pipeline_runs_and_the_mixed_fleet(seeded):
    store, _ = seeded
    summaries = agent_summaries(store)
    by_agent = {summary.agent: summary for summary in summaries}
    assert set(by_agent) == {
        "orchestrator",
        "researcher",
        "writer",
        "reviewer",
        GHA_AGENT,
        SILENT_AGENT,
    }
    assert by_agent["orchestrator"].runs == 2  # both pipeline runs
    assert by_agent["writer"].status == "failed"
    assert by_agent["orchestrator"].status == "ok"
    markdown = render_markdown(summaries, now=NOW)
    assert "| writer | 🔴 failed |" in markdown
    assert GHA_AGENT in markdown
    assert "| Cost |" in markdown  # usage data grows the cost column


def test_trace_of_regressed_run_shows_failed_writer(seeded):
    store, _ = seeded
    trace = build_trace(store, RUN_REGRESSED)
    assert trace.span_count == 4
    assert trace.failed_count == 1
    assert [root.agent for root in trace.roots] == ["orchestrator"]
    assert [child.agent for child in trace.roots[0].children] == [
        "researcher",
        "writer",
        "reviewer",
    ]
    rendered = render_tree(trace, now=NOW)
    assert "writer ✗" in rendered
    assert "APITimeoutError" in rendered


def test_diff_flags_writer_as_regression_and_status_change(seeded):
    store, _ = seeded
    diff = diff_traces(build_trace(store, RUN_BASELINE), build_trace(store, RUN_REGRESSED))
    writer = next(span for span in diff.spans if span.path == "orchestrator/writer")
    assert writer.delta_pct == pytest.approx(40.0)
    assert writer.status_changed
    assert (writer.status_a, writer.status_b) == ("ok", "failed")
    rendered = render_diff(diff, threshold_pct=20.0)
    writer_row = next(line for line in rendered.splitlines() if "orchestrator/writer" in line)
    assert "⚠️" in writer_row
    assert "ok → failed" in writer_row
    assert not diff.only_in_a and not diff.only_in_b


def test_costs_rollup_sees_demo_usage(seeded):
    store, _ = seeded
    by_agent = usage_summary(store, group_by="agent")
    names = {row.name for row in by_agent.rows}
    assert {"orchestrator", "researcher", "writer", "reviewer"} <= names
    assert by_agent.total.cost_usd > 0
    by_model = usage_summary(store, group_by="model")
    model_row = next(row for row in by_model.rows if row.name == DEMO_MODEL)
    assert model_row.events == 7  # 4 baseline ends + 3 regressed ends


def test_demo_alert_rules_fire_both_rules(seeded, tmp_path):
    store, _ = seeded
    rules = load_rules(write_demo_rules(tmp_path / DEMO_RULES_FILENAME))
    alerts = evaluate(store, rules, now=NOW)
    fired = {(alert.rule_name, alert.agent) for alert in alerts}
    assert fired == {
        ("demo-fleet-quiet", SILENT_AGENT),
        ("demo-writer-broken", "writer"),
    }


# ------------------------------------------------------------------- the CLI


def test_demo_command_seeds_writes_rules_and_prints_tour(tmp_path, capsys):
    db = tmp_path / "demo.db"
    assert main(["demo", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Seeded 19 events across 4 runs and 6 agents into %s" % db in out
    assert "Guided tour" in out
    assert (tmp_path / DEMO_RULES_FILENAME).exists()
    assert EventStore(db).count_events() == 19


def test_demo_refuses_to_reseed_nonempty_db_without_fresh(tmp_path, capsys):
    db = tmp_path / "demo.db"
    assert main(["demo", "--db", str(db)]) == 0
    assert main(["demo", "--db", str(db)]) == 1
    captured = capsys.readouterr()
    assert "--fresh" in captured.err
    assert EventStore(db).count_events() == 19  # unchanged


def test_demo_fresh_deletes_and_reseeds(tmp_path, capsys):
    db = tmp_path / "demo.db"
    assert main(["demo", "--db", str(db)]) == 0
    assert main(["demo", "--db", str(db), "--fresh"]) == 0
    assert EventStore(db).count_events() == 19


def test_demo_seeds_into_existing_empty_db(tmp_path, capsys):
    db = tmp_path / "demo.db"
    EventStore(db)  # creates an empty database file
    assert main(["demo", "--db", str(db)]) == 0
    assert EventStore(db).count_events() == 19


# ------------------------------------------------------------------ the tour


def test_tour_lists_every_command_with_the_db_flag(tmp_path):
    db = tmp_path / "demo.db"
    tour = demo_tour(db)
    db_flag = "--db %s" % db
    assert tour.count(db_flag) == 7
    assert "agentmesh board %s" % db_flag in tour
    assert "agentmesh tail --run %s -n 20 %s" % (RUN_REGRESSED, db_flag) in tour
    assert "agentmesh trace %s %s" % (RUN_REGRESSED, db_flag) in tour
    assert "agentmesh diff %s %s %s" % (RUN_BASELINE, RUN_REGRESSED, db_flag) in tour
    assert "agentmesh costs --by agent %s" % db_flag in tour
    assert "agentmesh alerts --rules %s %s" % (tmp_path / DEMO_RULES_FILENAME, db_flag) in tour
    assert "agentmesh serve %s" % db_flag in tour


def test_tour_commands_run_against_the_seeded_db(tmp_path, capsys):
    """End to end: the exact tour commands (minus serve/follow) all exit 0."""
    db = tmp_path / "demo.db"
    assert main(["demo", "--db", str(db)]) == 0
    rules = tmp_path / DEMO_RULES_FILENAME
    commands = [
        ["board", "--db", str(db)],
        ["tail", "--run", RUN_REGRESSED, "-n", "20", "--db", str(db)],
        ["trace", RUN_REGRESSED, "--db", str(db)],
        ["diff", RUN_BASELINE, RUN_REGRESSED, "--db", str(db)],
        ["costs", "--by", "agent", "--db", str(db)],
        ["alerts", "--rules", str(rules), "--db", str(db)],
    ]
    capsys.readouterr()
    for argv in commands:
        assert main(argv) == 0, argv
    out = capsys.readouterr().out
    assert "writer ✗" in out  # trace
    assert "ok → failed" in out  # diff
    assert "demo-writer-broken" in out  # alerts
