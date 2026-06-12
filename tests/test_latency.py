import uuid

import pytest

from agentmesh.board import agent_summaries, render_html, render_markdown
from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.latency import latency_stats, percentile, render_latency, span_durations


def _ts(minute, second=0):
    return "2026-06-11T11:%02d:%02d+00:00" % (minute, second)


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


@pytest.fixture
def spans(store):
    """Three alpha spans (one per duration source), a failed beta, a running gamma."""
    _append(store, "r1", "alpha", "agent_start", _ts(0))
    _append(
        store, "r1", "alpha", "agent_end", _ts(0, 20), {"duration_seconds": 10.0, "model": "m-big"}
    )
    _append(store, "r2", "alpha", "agent_start", _ts(1))
    _append(store, "r2", "alpha", "agent_end", _ts(1, 10), {"duration_ms": 5000})
    _append(store, "r3", "alpha", "agent_start", _ts(30))
    _append(store, "r3", "alpha", "agent_end", _ts(30, 30))
    _append(store, "r4", "beta", "agent_start", _ts(2))
    _append(store, "r4", "beta", "agent_error", _ts(2, 2), {"error": "boom"})
    _append(store, "r5", "gamma", "agent_start", _ts(3))
    return store


# ---------------------------------------------------------------- durations


def test_span_durations_uses_all_three_sources(spans):
    samples = span_durations(spans)
    # payload duration_seconds (10.0, not the 20s ts diff), duration_ms, ts diff.
    assert sorted(samples.by_agent["alpha"]) == pytest.approx([5.0, 10.0, 30.0])


def test_span_durations_includes_failed_spans(spans):
    assert span_durations(spans).by_agent["beta"] == pytest.approx([2.0])


def test_span_durations_excludes_running_spans(spans):
    assert "gamma" not in span_durations(spans).by_agent


def test_span_durations_since_keeps_spans_started_in_window(spans):
    samples = span_durations(spans, since=_ts(30))
    assert samples.by_agent == {"alpha": [30.0]}
    assert samples.by_model == {}


def test_span_durations_by_model_from_agent_end_usage(spans):
    samples = span_durations(spans)
    # Only the r1 span carried a model; the others have no usage field.
    assert samples.by_model == {"m-big": [10.0]}


def test_span_durations_by_model_from_tool_call_usage(spans):
    _append(spans, "r6", "delta", "agent_start", _ts(5), {"span_id": "sp6"})
    _append(spans, "r6", "delta", "tool_call", _ts(5, 1), {"span_id": "sp6", "model": "m-small"})
    _append(
        spans, "r6", "delta", "agent_end", _ts(5, 2), {"span_id": "sp6", "duration_seconds": 1.5}
    )
    samples = span_durations(spans)
    assert samples.by_model["m-small"] == [1.5]
    assert samples.by_agent["delta"] == [1.5]


def test_span_durations_empty_store(store):
    samples = span_durations(store)
    assert samples.by_agent == {}
    assert samples.by_model == {}


# --------------------------------------------------------------- percentile


def test_percentile_single_element_for_any_p():
    for p in (0, 50, 99, 100):
        assert percentile([7.0], p) == 7.0


def test_percentile_exact_known_values():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 50) == 2.5
    assert percentile(values, 100) == 4.0


def test_percentile_linear_interpolation():
    assert percentile([10.0, 20.0], 90) == pytest.approx(19.0)
    values = [float(n) for n in range(1, 11)]  # 1..10
    assert percentile(values, 90) == pytest.approx(9.1)
    assert percentile(values, 99) == pytest.approx(9.91)


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], -1)
    with pytest.raises(ValueError):
        percentile([1.0], 100.1)


# -------------------------------------------------------------------- stats


def test_latency_stats_values():
    (stat,) = latency_stats({"a": [float(n) for n in range(1, 11)]})
    assert stat.key == "a"
    assert stat.count == 10
    assert stat.min_seconds == 1.0
    assert stat.p50_seconds == pytest.approx(5.5)
    assert stat.p90_seconds == pytest.approx(9.1)
    assert stat.p99_seconds == pytest.approx(9.91)
    assert stat.max_seconds == 10.0
    assert stat.mean_seconds == pytest.approx(5.5)


def test_latency_stats_ordering_count_desc_then_key():
    stats = latency_stats({"b": [5.0], "c": [1.0, 2.0, 3.0], "a": [4.0, 5.0, 6.0]})
    assert [stat.key for stat in stats] == ["a", "c", "b"]


def test_latency_stats_drops_empty_lists():
    assert latency_stats({"a": []}) == []


# ------------------------------------------------------------------- render


def test_render_latency_table_and_window(spans):
    text = render_latency(latency_stats(span_durations(spans).by_agent))
    lines = text.splitlines()
    assert lines[0] == "Latency percentiles by agent (all time)"
    assert "| Agent | Spans | Min | p50 | p90 | p99 | Max | Mean |" in lines
    assert "| alpha | 3 | 5.0s | 10.0s | 26.0s | 29.6s | 30.0s | 15.0s |" in lines
    assert "| beta | 1 | 2.0s | 2.0s | 2.0s | 2.0s | 2.0s | 2.0s |" in lines


def test_render_latency_header_notes_since(spans):
    text = render_latency(latency_stats(span_durations(spans).by_agent), since=_ts(30))
    assert text.splitlines()[0] == "Latency percentiles by agent (since %s)" % _ts(30)


def test_render_latency_by_model_label(spans):
    text = render_latency(latency_stats(span_durations(spans).by_model), by="model")
    assert "| Model | Spans |" in text
    assert "| m-big | 1 |" in text


def test_render_latency_humanizes_long_durations():
    text = render_latency(latency_stats({"slow": [90.0]}))
    assert "1m 30s" in text


def test_render_latency_empty_state():
    assert render_latency([]) == "No completed spans."


def test_render_latency_rejects_unknown_grouping():
    with pytest.raises(ValueError):
        render_latency([], by="run")


# ---------------------------------------------------------------------- CLI


def test_cli_latency_by_agent(spans, capsys):
    assert main(["latency"]) == 0
    out = capsys.readouterr().out
    assert "Latency percentiles by agent (all time)" in out
    assert "| alpha | 3 |" in out


def test_cli_latency_by_model(spans, capsys):
    assert main(["latency", "--by", "model"]) == 0
    out = capsys.readouterr().out
    assert "Latency percentiles by model (all time)" in out
    assert "| m-big | 1 |" in out


def test_cli_latency_since(spans, capsys):
    assert main(["latency", "--since", _ts(30)]) == 0
    out = capsys.readouterr().out
    assert "since %s" % _ts(30) in out
    assert "| alpha | 1 |" in out
    assert "beta" not in out


def test_cli_latency_explicit_db(spans, capsys):
    assert main(["latency", "--db", str(spans.path)]) == 0
    assert "| alpha | 3 |" in capsys.readouterr().out


def test_cli_latency_empty_store(store, capsys):
    assert main(["latency"]) == 0
    assert capsys.readouterr().out.strip() == "No completed spans."


# -------------------------------------------------------------- board p95


def test_agent_summary_p95(spans):
    by_name = {summary.agent: summary for summary in agent_summaries(spans)}
    # alpha successful-run durations sorted: [5.0, 10.0, 30.0] -> p95 = 28.0.
    assert by_name["alpha"].p95_duration_seconds == pytest.approx(28.0)
    # beta has no successful runs, gamma is still running.
    assert by_name["beta"].p95_duration_seconds is None
    assert by_name["gamma"].p95_duration_seconds is None


def test_render_markdown_default_has_no_p95_column(spans):
    markdown = render_markdown(agent_summaries(spans))
    assert "p95" not in markdown


def test_render_markdown_include_p95_adds_column(spans):
    markdown = render_markdown(agent_summaries(spans), include_p95=True)
    lines = markdown.splitlines()
    assert lines[0] == (
        "| Agent | Status | Last event | Age | Runs | Errors | Streak | Avg duration | p95 |"
    )
    assert lines[1] == (
        "|-------|--------|------------|-----|------|--------|--------|--------------|-----|"
    )
    alpha = next(line for line in lines if line.startswith("| alpha |"))
    assert alpha.endswith("| 15.0s | 28.0s |")
    beta = next(line for line in lines if line.startswith("| beta |"))
    assert beta.endswith("| — | — |")


def test_render_markdown_p95_before_cost_column(spans):
    _append(spans, "r7", "alpha", "agent_start", _ts(40))
    _append(
        spans, "r7", "alpha", "agent_end", _ts(40, 1), {"duration_seconds": 1.0, "cost_usd": 0.5}
    )
    markdown = render_markdown(agent_summaries(spans), include_p95=True)
    assert markdown.splitlines()[0].endswith("| Avg duration | p95 | Cost |")


def test_render_html_p95_column_only_with_flag(spans):
    summaries = agent_summaries(spans)
    assert "<th>p95</th>" not in render_html(summaries)
    page = render_html(summaries, include_p95=True)
    assert "<th>p95</th>" in page
    assert "<td>28.0s</td>" in page


def test_cli_board_p95_flag(spans, capsys):
    assert main(["board", "--p95"]) == 0
    out = capsys.readouterr().out
    assert "| p95 |" in out.splitlines()[0]
    assert "28.0s" in out


def test_cli_board_without_flag_unchanged(spans, capsys):
    assert main(["board"]) == 0
    out = capsys.readouterr().out
    assert "p95" not in out
