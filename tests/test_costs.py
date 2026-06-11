import asyncio
import uuid

import pytest

from agentmesh.board import agent_summaries, render_html, render_markdown
from agentmesh.cli import main
from agentmesh.costs import render_costs, usage_summary
from agentmesh.events import Event
from agentmesh.sdk import Mesh
from agentmesh.trace import build_trace, render_tree


@pytest.fixture
def mesh(store):
    return Mesh(store=store)


def _append(store, run_id, agent, type, payload=None, ts=None):
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type=type,
            ts=ts if ts is not None else "2026-06-10T11:00:00+00:00",
            payload=payload if payload is not None else {},
        )
    )


def _end_event(store, run_id):
    events = [e for _, e in store.events_after(0, run_id=run_id)]
    return [e for e in events if e.type == "agent_end"][0]


# --- mesh.record_usage ------------------------------------------------------


def test_record_usage_merges_into_agent_end_payload(mesh, store):
    @mesh.agent(name="researcher")
    def work():
        mesh.record_usage(input_tokens=100, output_tokens=40, cost_usd=0.01, model="m-1")
        return "ok"

    with mesh.run("run-usage"):
        work()

    end = _end_event(store, "run-usage")
    assert end.payload["input_tokens"] == 100
    assert end.payload["output_tokens"] == 40
    assert end.payload["cost_usd"] == pytest.approx(0.01)
    assert end.payload["model"] == "m-1"
    start = [e for _, e in store.events_after(0, run_id="run-usage")][0]
    assert "input_tokens" not in start.payload  # usage lands on agent_end only


def test_record_usage_sums_across_calls_and_model_last_wins(mesh, store):
    @mesh.agent(name="researcher")
    def work():
        mesh.record_usage(input_tokens=100, output_tokens=10, cost_usd=0.01, model="m-1")
        mesh.record_usage(input_tokens=50, cost_usd=0.005)
        mesh.record_usage(output_tokens=5, model="m-2")

    with mesh.run("run-sum"):
        work()

    end = _end_event(store, "run-sum")
    assert end.payload["input_tokens"] == 150
    assert end.payload["output_tokens"] == 15
    assert end.payload["cost_usd"] == pytest.approx(0.015)
    assert end.payload["model"] == "m-2"


def test_record_usage_only_passed_fields_appear(mesh, store):
    @mesh.agent(name="partial")
    def work():
        mesh.record_usage(cost_usd=0.02)

    with mesh.run("run-partial"):
        work()

    end = _end_event(store, "run-partial")
    assert end.payload["cost_usd"] == pytest.approx(0.02)
    assert "input_tokens" not in end.payload
    assert "output_tokens" not in end.payload
    assert "model" not in end.payload


def test_record_usage_in_async_agent(mesh, store):
    @mesh.agent(name="async-worker")
    async def work():
        mesh.record_usage(input_tokens=10, model="m-async")
        await asyncio.sleep(0)
        mesh.record_usage(input_tokens=20, cost_usd=0.003)

    async def run():
        with mesh.run("run-async-usage"):
            await work()

    asyncio.run(run())
    end = _end_event(store, "run-async-usage")
    assert end.payload["input_tokens"] == 30
    assert end.payload["cost_usd"] == pytest.approx(0.003)
    assert end.payload["model"] == "m-async"


def test_record_usage_nested_spans_stay_separate(mesh, store):
    @mesh.agent(name="inner")
    def inner():
        mesh.record_usage(input_tokens=5)

    @mesh.agent(name="outer")
    def outer():
        mesh.record_usage(input_tokens=100)
        inner()

    with mesh.run("run-nested-usage"):
        outer()

    ends = {
        e.agent: e
        for _, e in store.events_after(0, run_id="run-nested-usage")
        if e.type == "agent_end"
    }
    assert ends["outer"].payload["input_tokens"] == 100
    assert ends["inner"].payload["input_tokens"] == 5


def test_record_usage_outside_span_warns_and_does_not_raise(mesh, store):
    with pytest.warns(UserWarning, match="outside any"):
        mesh.record_usage(input_tokens=10, cost_usd=0.01)
    assert store.query() == []


# --- AMP v0.3 validation ----------------------------------------------------


def test_v02_events_without_usage_keys_still_validate():
    event = Event.new("run-1", "a", "agent_end", {"duration_ms": 5.0, "span_id": "sp-1"})
    assert "input_tokens" not in event.payload  # absent usage keys are fine


def test_valid_usage_fields_accepted_on_agent_end_and_tool_call():
    payload = {"input_tokens": 0, "output_tokens": 12, "cost_usd": 0.0, "model": "m-1"}
    for type in ("agent_end", "tool_call"):
        event = Event.new("run-1", "a", type, dict(payload))
        assert event.payload["output_tokens"] == 12


@pytest.mark.parametrize(
    "payload, match",
    [
        ({"input_tokens": "100"}, "input_tokens"),
        ({"input_tokens": -1}, "input_tokens"),
        ({"input_tokens": True}, "input_tokens"),
        ({"output_tokens": 1.5}, "output_tokens"),
        ({"cost_usd": "0.01"}, "cost_usd"),
        ({"cost_usd": -0.01}, "cost_usd"),
        ({"model": ""}, "model"),
        ({"model": 42}, "model"),
    ],
)
def test_invalid_usage_fields_rejected_when_present(payload, match):
    for type in ("agent_end", "tool_call"):
        with pytest.raises(ValueError, match=match):
            Event.new("run-1", "a", type, dict(payload))


def test_usage_keys_on_other_event_types_stay_free_form():
    # The spec defines usage fields on agent_end/tool_call only.
    event = Event.new("run-1", "a", "message", {"input_tokens": "free-form"})
    assert event.payload["input_tokens"] == "free-form"


# --- usage_summary ----------------------------------------------------------


@pytest.fixture
def usage_store(store):
    """Two runs, two agents, two models, plus a usage-free event."""
    _append(store, "r1", "researcher", "agent_start")
    _append(
        store,
        "r1",
        "researcher",
        "agent_end",
        {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.01, "model": "m-big"},
    )
    _append(
        store,
        "r1",
        "writer",
        "tool_call",
        {"tool": "search", "input_tokens": 500, "cost_usd": 0.002, "model": "m-small"},
    )
    _append(
        store,
        "r2",
        "writer",
        "agent_end",
        {"input_tokens": 2000, "output_tokens": 400, "cost_usd": 0.05, "model": "m-big"},
        ts="2026-06-10T12:00:00+00:00",
    )
    _append(store, "r3", "critic", "agent_end", {"duration_ms": 5.0})  # no usage keys
    return store


def test_usage_summary_groups_by_run_with_totals(usage_store):
    summary = usage_summary(usage_store, group_by="run")
    assert summary.group_by == "run"
    assert [row.name for row in summary.rows] == ["r2", "r1"]  # cost desc
    r1 = summary.rows[1]
    assert (r1.input_tokens, r1.output_tokens) == (1500, 200)
    assert r1.cost_usd == pytest.approx(0.012)
    assert r1.events == 2
    assert summary.total.name == "total"
    assert summary.total.input_tokens == 3500
    assert summary.total.output_tokens == 600
    assert summary.total.cost_usd == pytest.approx(0.062)
    assert summary.total.events == 3


def test_usage_summary_groups_by_agent(usage_store):
    summary = usage_summary(usage_store, group_by="agent")
    assert [row.name for row in summary.rows] == ["writer", "researcher"]
    writer = summary.rows[0]
    assert writer.input_tokens == 2500
    assert writer.cost_usd == pytest.approx(0.052)
    assert writer.events == 2


def test_usage_summary_groups_by_model(usage_store):
    _append(usage_store, "r4", "critic", "agent_end", {"input_tokens": 7})  # no model key
    summary = usage_summary(usage_store, group_by="model")
    assert [row.name for row in summary.rows] == ["m-big", "m-small", "(unknown)"]
    assert summary.rows[0].cost_usd == pytest.approx(0.06)
    assert summary.rows[2].input_tokens == 7


def test_usage_summary_equal_costs_sort_by_name(store):
    _append(store, "r-b", "a", "agent_end", {"cost_usd": 0.01})
    _append(store, "r-a", "a", "agent_end", {"cost_usd": 0.01})
    summary = usage_summary(store, group_by="run")
    assert [row.name for row in summary.rows] == ["r-a", "r-b"]


def test_usage_summary_since_filters_out_older_events(usage_store):
    summary = usage_summary(usage_store, group_by="run", since="2026-06-10T11:30:00+00:00")
    assert [row.name for row in summary.rows] == ["r2"]


def test_usage_summary_ignores_non_usage_event_types(store):
    _append(store, "r1", "a", "message", {"input_tokens": 99, "cost_usd": 1.0})
    assert usage_summary(store, group_by="run").rows == []


def test_usage_summary_rejects_unknown_group_by(store):
    with pytest.raises(ValueError, match="group_by"):
        usage_summary(store, group_by="bogus")


# --- render_costs -----------------------------------------------------------


def test_render_costs_markdown_format(usage_store):
    output = render_costs(usage_summary(usage_store, group_by="run"))
    lines = output.splitlines()
    assert lines[0] == "| Run | Input tokens | Output tokens | Cost (USD) | Events |"
    assert "| r2 | 2,000 | 400 | $0.0500 | 1 |" in lines
    assert "| r1 | 1,500 | 200 | $0.0120 | 2 |" in lines
    assert lines[-1] == "| **total** | 3,500 | 600 | $0.0620 | 3 |"


def test_render_costs_by_agent_header(usage_store):
    output = render_costs(usage_summary(usage_store, group_by="agent"))
    assert output.splitlines()[0].startswith("| Agent |")


def test_render_costs_without_usage_data(store):
    assert render_costs(usage_summary(store)) == "No usage data recorded."


# --- board cost column ------------------------------------------------------


def test_board_summaries_carry_total_cost(usage_store):
    by_name = {summary.agent: summary for summary in agent_summaries(usage_store)}
    assert by_name["writer"].cost_usd == pytest.approx(0.052)
    assert by_name["researcher"].cost_usd == pytest.approx(0.01)
    assert by_name["critic"].cost_usd is None


def test_render_markdown_cost_column_only_with_usage(usage_store):
    markdown = render_markdown(agent_summaries(usage_store))
    lines = markdown.splitlines()
    assert lines[0].endswith("| Avg duration | Cost |")
    assert any(line.endswith("| $0.0520 |") for line in lines)
    assert any(line.endswith("| — |") for line in lines)  # critic has no usage


def test_render_markdown_without_usage_has_no_cost_column(store):
    _append(store, "r1", "a", "agent_start")
    _append(store, "r1", "a", "agent_end", {"duration_ms": 5.0})
    markdown = render_markdown(agent_summaries(store))
    assert "Cost" not in markdown
    assert markdown.splitlines()[0].endswith("| Avg duration |")


def test_render_html_cost_column_only_with_usage(usage_store):
    page = render_html(agent_summaries(usage_store))
    assert "<th>Cost</th>" in page
    assert "<td>$0.0520</td>" in page


def test_render_html_without_usage_has_no_cost_column(store):
    _append(store, "r1", "a", "agent_start")
    page = render_html(agent_summaries(store))
    assert "Cost" not in page


# --- trace usage suffix -----------------------------------------------------


def test_trace_spans_carry_usage_and_render_suffix(mesh, store):
    @mesh.agent(name="researcher")
    def work():
        mesh.record_usage(input_tokens=1000, output_tokens=200, cost_usd=0.0034)

    with mesh.run("run-trace-usage"):
        work()

    trace = build_trace(store, "run-trace-usage")
    span = trace.roots[0]
    assert (span.input_tokens, span.output_tokens) == (1000, 200)
    assert span.cost_usd == pytest.approx(0.0034)
    output = render_tree(trace)
    assert output.splitlines()[1].endswith("[1.2k tok, $0.0034]")


def test_trace_tool_call_usage_accumulates_onto_span(store):
    _append(store, "r-t", "a", "agent_start", {"span_id": "sp-1"})
    _append(store, "r-t", "a", "tool_call", {"span_id": "sp-1", "input_tokens": 300})
    _append(
        store, "r-t", "a", "agent_end", {"span_id": "sp-1", "duration_ms": 5.0, "input_tokens": 100}
    )
    span = build_trace(store, "r-t").roots[0]
    assert span.input_tokens == 400
    assert span.output_tokens is None
    assert span.cost_usd is None
    assert "[400 tok]" in render_tree(build_trace(store, "r-t"))


def test_trace_spans_without_usage_render_without_suffix(store):
    _append(store, "r-p", "a", "agent_start", {"span_id": "sp-1"})
    _append(store, "r-p", "a", "agent_end", {"span_id": "sp-1", "duration_ms": 5.0})
    output = render_tree(build_trace(store, "r-p"))
    assert "[" not in output


def test_trace_cost_only_span_renders_cost_only(store):
    _append(store, "r-c", "a", "agent_start", {"span_id": "sp-1"})
    _append(store, "r-c", "a", "agent_end", {"span_id": "sp-1", "cost_usd": 0.5})
    assert "[$0.5000]" in render_tree(build_trace(store, "r-c"))


# --- CLI --------------------------------------------------------------------


def test_cli_costs_defaults_to_by_run(usage_store, db_path, capsys):
    assert main(["costs", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "| Run |" in out
    assert "| **total** | 3,500 | 600 | $0.0620 | 3 |" in out


def test_cli_costs_by_agent_and_model(usage_store, db_path, capsys):
    assert main(["costs", "--by", "agent", "--db", str(db_path)]) == 0
    assert "| writer | 2,500 |" in capsys.readouterr().out
    assert main(["costs", "--by", "model", "--db", str(db_path)]) == 0
    assert "| m-big |" in capsys.readouterr().out


def test_cli_costs_since_filters(usage_store, db_path, capsys):
    assert main(["costs", "--since", "2026-06-10T11:30:00+00:00", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "| r2 |" in out
    assert "| r1 |" not in out


def test_cli_costs_uses_agentmesh_db_env_without_db_flag(usage_store, capsys):
    assert main(["costs"]) == 0
    assert "| **total** |" in capsys.readouterr().out


def test_cli_costs_rejects_unknown_by(db_path):
    with pytest.raises(SystemExit) as excinfo:
        main(["costs", "--by", "bogus", "--db", str(db_path)])
    assert excinfo.value.code != 0


def test_cli_costs_empty_db(db_path, capsys):
    assert main(["costs", "--db", str(db_path)]) == 0
    assert "No usage data recorded." in capsys.readouterr().out
