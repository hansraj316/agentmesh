import uuid

import pytest

from agentmesh.cli import main
from agentmesh.diff import align_spans, diff_traces, render_diff
from agentmesh.events import Event
from agentmesh.sdk import Mesh
from agentmesh.trace import build_trace


@pytest.fixture
def mesh(store):
    return Mesh(store=store)


def _event(run_id, agent, type, ts, payload):
    return Event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        agent=agent,
        type=type,
        ts=ts,
        payload=payload,
    )


def _ts(second, micro=0):
    return "2026-06-01T12:00:%02d.%06d+00:00" % (second, micro)


def _add_span(
    store,
    run_id,
    agent,
    span_id,
    parent=None,
    duration=None,
    failed=False,
    running=False,
    start_second=0,
):
    """Append start (+ optional terminal) events for one span with explicit ts."""
    payload = {"function": agent, "span_id": span_id}
    if parent is not None:
        payload["parent_span_id"] = parent
    store.append(_event(run_id, agent, "agent_start", _ts(start_second), payload))
    if running:
        return
    end_payload = {"span_id": span_id}
    if duration is not None:
        end_payload["duration_seconds"] = duration
    if failed:
        end_payload["error"] = "boom"
        end_payload["error_type"] = "RuntimeError"
        store.append(_event(run_id, agent, "agent_error", _ts(start_second + 1), end_payload))
    else:
        store.append(_event(run_id, agent, "agent_end", _ts(start_second + 1), end_payload))


def _sdk_run(mesh, run_id):
    """orchestrator -> researcher, researcher, writer (all ok), built via the SDK."""

    @mesh.agent(name="researcher")
    def researcher():
        return "facts"

    @mesh.agent(name="writer")
    def writer():
        return "draft"

    @mesh.agent(name="orchestrator")
    def orchestrator():
        researcher()
        researcher()
        writer()

    with mesh.run(run_id):
        orchestrator()
    return run_id


# --- align_spans ---------------------------------------------------------


def test_align_spans_matches_by_structural_path(mesh, store):
    _sdk_run(mesh, "run-a")
    _sdk_run(mesh, "run-b")
    alignment = align_spans(build_trace(store, "run-a"), build_trace(store, "run-b"))
    assert [pair.path for pair in alignment.pairs] == [
        "orchestrator",
        "orchestrator/researcher",
        "orchestrator/researcher",
        "orchestrator/writer",
    ]
    assert alignment.only_in_a == []
    assert alignment.only_in_b == []


def test_align_spans_repeated_agent_aligns_by_occurrence_order(store):
    # A: researcher #1 = 1s, researcher #2 = 9s; B reversed: 9s then 1s.
    _add_span(store, "run-a", "orchestrator", "a-root", duration=20.0)
    _add_span(store, "run-a", "researcher", "a-r1", parent="a-root", duration=1.0)
    _add_span(store, "run-a", "researcher", "a-r2", parent="a-root", duration=9.0)
    _add_span(store, "run-b", "orchestrator", "b-root", duration=20.0)
    _add_span(store, "run-b", "researcher", "b-r1", parent="b-root", duration=9.0)
    _add_span(store, "run-b", "researcher", "b-r2", parent="b-root", duration=1.0)
    alignment = align_spans(build_trace(store, "run-a"), build_trace(store, "run-b"))
    researchers = [p for p in alignment.pairs if p.path == "orchestrator/researcher"]
    assert [(p.span_a.duration_seconds, p.span_b.duration_seconds) for p in researchers] == [
        (1.0, 9.0),
        (9.0, 1.0),
    ]


def test_align_spans_distinguishes_same_path_under_repeated_parents(store):
    # Two orchestrator roots, each with a researcher child: alignment must
    # keep first-root children with first-root children.
    for run_id in ("run-a", "run-b"):
        _add_span(store, run_id, "orchestrator", run_id + "-o1", duration=5.0)
        _add_span(store, run_id, "researcher", run_id + "-c1", parent=run_id + "-o1", duration=1.0)
        _add_span(store, run_id, "orchestrator", run_id + "-o2", duration=5.0)
        _add_span(store, run_id, "researcher", run_id + "-c2", parent=run_id + "-o2", duration=2.0)
    alignment = align_spans(build_trace(store, "run-a"), build_trace(store, "run-b"))
    assert len(alignment.pairs) == 4
    children = [p for p in alignment.pairs if p.path == "orchestrator/researcher"]
    assert [(p.span_a.duration_seconds, p.span_b.duration_seconds) for p in children] == [
        (1.0, 1.0),
        (2.0, 2.0),
    ]


def test_align_spans_reports_only_in_a_and_only_in_b(store):
    _add_span(store, "run-a", "orchestrator", "a-root", duration=5.0)
    _add_span(store, "run-a", "critic", "a-critic", parent="a-root", duration=1.0)
    _add_span(store, "run-b", "orchestrator", "b-root", duration=5.0)
    _add_span(store, "run-b", "editor", "b-editor", parent="b-root", duration=2.0)
    alignment = align_spans(build_trace(store, "run-a"), build_trace(store, "run-b"))
    assert [pair.path for pair in alignment.pairs] == ["orchestrator"]
    assert [(path, span.agent) for path, span in alignment.only_in_a] == [
        ("orchestrator/critic", "critic")
    ]
    assert [(path, span.agent) for path, span in alignment.only_in_b] == [
        ("orchestrator/editor", "editor")
    ]


# --- diff_traces ---------------------------------------------------------


def test_diff_traces_duration_delta_absolute_and_percent(store):
    _add_span(store, "run-a", "researcher", "a-1", duration=2.0)
    _add_span(store, "run-b", "researcher", "b-1", duration=3.0)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    (span,) = diff.spans
    assert span.path == "researcher"
    assert span.duration_a == pytest.approx(2.0)
    assert span.duration_b == pytest.approx(3.0)
    assert span.delta_seconds == pytest.approx(1.0)
    assert span.delta_pct == pytest.approx(50.0)


def test_diff_traces_missing_duration_yields_none_deltas(store):
    _add_span(store, "run-a", "researcher", "a-1", duration=2.0)
    _add_span(store, "run-b", "researcher", "b-1", running=True)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    (span,) = diff.spans
    assert span.duration_b is None
    assert span.delta_seconds is None
    assert span.delta_pct is None


def test_diff_traces_zero_baseline_duration_has_no_percent(store):
    _add_span(store, "run-a", "researcher", "a-1", duration=0.0)
    _add_span(store, "run-b", "researcher", "b-1", duration=1.0)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    (span,) = diff.spans
    assert span.delta_seconds == pytest.approx(1.0)
    assert span.delta_pct is None


def test_diff_traces_detects_status_changes_both_directions(store):
    _add_span(store, "run-a", "writer", "a-w", duration=1.0, failed=False)
    _add_span(store, "run-a", "critic", "a-c", duration=1.0, failed=True, start_second=2)
    _add_span(store, "run-b", "writer", "b-w", duration=1.0, failed=True)
    _add_span(store, "run-b", "critic", "b-c", duration=1.0, failed=False, start_second=2)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    by_path = {span.path: span for span in diff.spans}
    assert (by_path["writer"].status_a, by_path["writer"].status_b) == ("ok", "failed")
    assert by_path["writer"].status_changed
    assert (by_path["critic"].status_a, by_path["critic"].status_b) == ("failed", "ok")
    assert by_path["critic"].status_changed


def test_diff_traces_run_level_summary(store):
    # run-a spans 0s..3s (wall clock 3s); run-b spans 0s..2s (wall clock 2s).
    _add_span(store, "run-a", "writer", "a-w", duration=1.0, failed=True)
    _add_span(store, "run-a", "critic", "a-c", duration=1.0, start_second=2)
    _add_span(store, "run-b", "writer", "b-w", duration=1.0, start_second=0)
    _add_span(store, "run-b", "critic", "b-c", duration=1.0, start_second=1)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    assert (diff.span_count_a, diff.span_count_b) == (2, 2)
    assert (diff.failed_count_a, diff.failed_count_b) == (1, 0)
    assert diff.total_seconds_a == pytest.approx(3.0)
    assert diff.total_seconds_b == pytest.approx(2.0)
    assert diff.total_delta_seconds == pytest.approx(-1.0)


# --- render_diff ---------------------------------------------------------


def test_render_diff_marks_regressions_and_improvements(store):
    _add_span(store, "run-a", "slower", "a-s", duration=1.0)
    _add_span(store, "run-a", "faster", "a-f", duration=2.0, start_second=2)
    _add_span(store, "run-a", "steady", "a-st", duration=1.0, start_second=4)
    _add_span(store, "run-b", "slower", "b-s", duration=2.0)
    _add_span(store, "run-b", "faster", "b-f", duration=1.0, start_second=2)
    _add_span(store, "run-b", "steady", "b-st", duration=1.05, start_second=4)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    output = render_diff(diff, threshold_pct=20.0)
    rows = {
        line.split(" | ")[0].lstrip("| "): line for line in output.splitlines() if " | " in line
    }
    assert "⚠️" in rows["slower"] and "+100.0%" in rows["slower"]
    assert "✅" in rows["faster"] and "-50.0%" in rows["faster"]
    assert "⚠️" not in rows["steady"] and "✅" not in rows["steady"]
    assert "# Run diff: run-a → run-b" in output
    assert "No significant changes" not in output


def test_render_diff_marks_status_change_to_failed_as_regression(store):
    # Duration unchanged: the ok→failed flip alone must mark the row.
    _add_span(store, "run-a", "writer", "a-w", duration=1.0)
    _add_span(store, "run-b", "writer", "b-w", duration=1.0, failed=True)
    output = render_diff(diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b")))
    assert "ok → failed" in output
    assert "⚠️" in output
    assert "No significant changes" not in output


def test_render_diff_marks_recovery_as_improvement(store):
    _add_span(store, "run-a", "writer", "a-w", duration=1.0, failed=True)
    _add_span(store, "run-b", "writer", "b-w", duration=1.0)
    output = render_diff(diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b")))
    assert "failed → ok" in output
    assert "✅" in output


def test_render_diff_respects_threshold(store):
    _add_span(store, "run-a", "researcher", "a-1", duration=1.0)
    _add_span(store, "run-b", "researcher", "b-1", duration=1.3)
    diff = diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b"))
    assert "⚠️" in render_diff(diff, threshold_pct=20.0)
    assert "⚠️" not in render_diff(diff, threshold_pct=50.0)
    assert "No significant changes (threshold ±50.0%)." in render_diff(diff, threshold_pct=50.0)


def test_render_diff_no_significant_changes_message(store):
    _add_span(store, "run-a", "researcher", "a-1", duration=2.0)
    _add_span(store, "run-b", "researcher", "b-1", duration=2.1)
    output = render_diff(diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b")))
    assert "No significant changes (threshold ±20.0%)." in output


def test_render_diff_lists_unmatched_spans_in_sections(store):
    _add_span(store, "run-a", "orchestrator", "a-root", duration=5.0)
    _add_span(store, "run-a", "critic", "a-critic", parent="a-root", duration=1.0, failed=True)
    _add_span(store, "run-b", "orchestrator", "b-root", duration=5.0)
    _add_span(store, "run-b", "editor", "b-editor", parent="b-root", duration=2.0)
    output = render_diff(diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b")))
    lines = output.splitlines()
    assert "## Only in run-a" in lines
    assert "- orchestrator/critic ✗ 1.0s" in lines
    assert "## Only in run-b" in lines
    assert "- orchestrator/editor ✓ 2.0s" in lines
    assert "No significant changes" not in output


def test_render_diff_shows_missing_durations_as_dashes(store):
    _add_span(store, "run-a", "researcher", "a-1", duration=2.0)
    _add_span(store, "run-b", "researcher", "b-1", running=True)
    output = render_diff(diff_traces(build_trace(store, "run-a"), build_trace(store, "run-b")))
    assert "| researcher | 2.0s | — | — | — | ok → running |" in output


# --- CLI -----------------------------------------------------------------


def test_cli_diff_happy_path(mesh, store, db_path, capsys):
    _sdk_run(mesh, "run-a")
    _sdk_run(mesh, "run-b")
    assert main(["diff", "run-a", "run-b", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "# Run diff: run-a → run-b" in out
    assert "| orchestrator |" in out


def test_cli_diff_passes_threshold_through(store, db_path, capsys):
    _add_span(store, "run-a", "researcher", "a-1", duration=1.0)
    _add_span(store, "run-b", "researcher", "b-1", duration=1.3)
    assert main(["diff", "run-a", "run-b", "--db", str(db_path), "--threshold", "50"]) == 0
    out = capsys.readouterr().out
    assert "⚠️" not in out
    assert "No significant changes (threshold ±50.0%)." in out


def test_cli_diff_unknown_first_run_names_it(store, db_path, capsys):
    _add_span(store, "run-b", "researcher", "b-1", duration=1.0)
    assert main(["diff", "run-missing", "run-b", "--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run-missing" in captured.err
    assert "run-b" not in captured.err


def test_cli_diff_unknown_second_run_names_it(store, db_path, capsys):
    _add_span(store, "run-a", "researcher", "a-1", duration=1.0)
    assert main(["diff", "run-a", "run-also-missing", "--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run-also-missing" in captured.err
