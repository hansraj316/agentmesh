import asyncio
from datetime import datetime, timezone

import pytest

from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.sdk import Mesh
from agentmesh.trace import build_trace, render_tree


@pytest.fixture
def mesh(store):
    return Mesh(store=store)


def _starts_by_agent(store, run_id):
    events = [e for _, e in store.events_after(0, run_id=run_id)]
    return {e.agent: e for e in events if e.type == "agent_start"}


def _nested_run(mesh, run_id="run-nested"):
    """orchestrator -> researcher (ok) + writer (raises, caught)."""

    @mesh.agent(name="researcher")
    def researcher():
        return "facts"

    @mesh.agent(name="writer")
    def writer():
        raise ValueError("bad draft\nstack detail")

    @mesh.agent(name="orchestrator")
    def orchestrator():
        researcher()
        try:
            writer()
        except ValueError:
            pass

    with mesh.run(run_id):
        orchestrator()
    return run_id


def test_nested_sync_calls_link_parent_and_child_spans(mesh, store):
    run_id = _nested_run(mesh)
    starts = _starts_by_agent(store, run_id)
    root_span = starts["orchestrator"].payload["span_id"]
    assert "parent_span_id" not in starts["orchestrator"].payload
    assert starts["researcher"].payload["parent_span_id"] == root_span
    assert starts["writer"].payload["parent_span_id"] == root_span


def test_terminal_events_share_the_start_span_id(mesh, store):
    run_id = _nested_run(mesh)
    events = [e for _, e in store.events_after(0, run_id=run_id)]
    by_type = {}
    for event in events:
        if event.agent == "writer":
            by_type[event.type] = event
    assert by_type["agent_error"].payload["span_id"] == by_type["agent_start"].payload["span_id"]


def test_span_context_restored_after_call(mesh, store):
    @mesh.agent(name="inner")
    def inner():
        return None

    @mesh.agent(name="outer")
    def outer():
        inner()

    with mesh.run("run-restore"):
        outer()
        inner()  # called after outer() returns: must be a root span again

    starts = [
        e
        for _, e in store.events_after(0, run_id="run-restore")
        if e.type == "agent_start" and e.agent == "inner"
    ]
    assert "parent_span_id" in starts[0].payload
    assert "parent_span_id" not in starts[1].payload


def test_nested_async_calls_link_parent_and_child_spans(mesh, store):
    @mesh.agent(name="async-child")
    async def child():
        await asyncio.sleep(0)
        return "done"

    @mesh.agent(name="async-parent")
    async def parent():
        return await child()

    async def run():
        with mesh.run("run-async-nested"):
            await parent()

    asyncio.run(run())
    starts = _starts_by_agent(store, "run-async-nested")
    assert "parent_span_id" not in starts["async-parent"].payload
    assert (
        starts["async-child"].payload["parent_span_id"] == starts["async-parent"].payload["span_id"]
    )


def test_build_trace_pairs_spans_with_duration_and_status(mesh, store):
    run_id = _nested_run(mesh)
    trace = build_trace(store, run_id)
    assert trace.run_id == run_id
    assert trace.span_count == 3
    assert trace.failed_count == 1
    assert len(trace.roots) == 1
    root = trace.roots[0]
    assert root.agent == "orchestrator"
    assert root.status == "ok"
    assert root.duration_seconds >= 0
    assert [child.agent for child in root.children] == ["researcher", "writer"]
    writer = root.children[1]
    assert writer.status == "failed"
    assert writer.error_type == "ValueError"
    assert writer.error.splitlines()[0] == "bad draft"


def test_build_trace_marks_unfinished_spans_running(store):
    store.append(
        Event.new("run-open", "slowpoke", "agent_start", {"function": "s", "span_id": "sp-1"})
    )
    trace = build_trace(store, "run-open")
    assert trace.span_count == 1
    assert trace.roots[0].status == "running"
    assert trace.roots[0].duration_seconds is None


def test_build_trace_v01_events_without_span_ids_build_a_flat_tree(store):
    store.append(Event.new("run-v01", "researcher", "agent_start", {"function": "r"}))
    store.append(Event.new("run-v01", "researcher", "agent_end", {"duration_ms": 2000.0}))
    store.append(Event.new("run-v01", "writer", "agent_start", {"function": "w"}))
    store.append(Event.new("run-v01", "writer", "agent_error", {"error": "kaput"}))
    trace = build_trace(store, "run-v01")
    assert trace.span_count == 2
    assert trace.failed_count == 1
    assert [root.agent for root in trace.roots] == ["researcher", "writer"]
    assert all(root.children == [] for root in trace.roots)
    assert trace.roots[0].status == "ok"
    assert trace.roots[0].duration_seconds == pytest.approx(2.0)
    assert trace.roots[1].status == "failed"


def test_build_trace_unknown_run_raises(store):
    with pytest.raises(ValueError, match="run-missing"):
        build_trace(store, "run-missing")


def test_render_tree_structure_marks_and_errors(mesh, store):
    run_id = _nested_run(mesh)
    output = render_tree(build_trace(store, run_id), now=datetime.now(timezone.utc))
    lines = output.splitlines()
    assert lines[0].startswith("run %s — 3 spans, 1 failed, total " % run_id)
    assert lines[1].startswith("└─ orchestrator ✓ ")
    assert lines[2].startswith("   ├─ researcher ✓ ")
    assert lines[3].startswith("   └─ writer ✗ ")
    assert lines[3].endswith("— ValueError: bad draft")  # first line of the error only
    assert "…" not in output


def test_render_tree_running_mark(store):
    store.append(
        Event.new("run-open", "slowpoke", "agent_start", {"function": "s", "span_id": "sp-1"})
    )
    output = render_tree(build_trace(store, "run-open"))
    assert "└─ slowpoke …" in output


def test_cli_trace_prints_tree(mesh, store, db_path, capsys):
    run_id = _nested_run(mesh)
    assert main(["trace", run_id, "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "run %s — 3 spans, 1 failed" % run_id in out
    assert "└─ orchestrator ✓" in out
    assert "└─ writer ✗" in out


def test_cli_trace_uses_agentmesh_db_env_without_db_flag(mesh, store, capsys):
    run_id = _nested_run(mesh)
    assert main(["trace", run_id]) == 0
    assert "└─ orchestrator ✓" in capsys.readouterr().out


def test_cli_trace_unknown_run_exits_nonzero_with_message(db_path, capsys):
    assert main(["trace", "run-missing", "--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run-missing" in captured.err
