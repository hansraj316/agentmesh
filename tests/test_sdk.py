import asyncio

import pytest

from agentmesh.sdk import Mesh


@pytest.fixture
def mesh(store):
    return Mesh(store=store)


def test_sync_decorator_emits_start_and_end(mesh, store):
    @mesh.agent(name="researcher")
    def work(x):
        return x * 2

    assert work(21) == 42

    events = store.query()
    assert [e.type for e in events] == ["agent_start", "agent_end"]
    assert all(e.agent == "researcher" for e in events)
    assert events[0].run_id == events[1].run_id
    assert events[1].payload["duration_ms"] >= 0
    assert events[0].payload["function"].endswith("work")


def test_sync_decorator_emits_error_and_reraises(mesh, store):
    @mesh.agent(name="flaky")
    def boom():
        raise RuntimeError("kaput")

    with pytest.raises(RuntimeError, match="kaput"):
        boom()

    events = store.query()
    assert [e.type for e in events] == ["agent_start", "agent_error"]
    error = events[1]
    assert error.payload["error"] == "kaput"
    assert error.payload["error_type"] == "RuntimeError"
    assert error.payload["duration_ms"] >= 0


def test_async_decorator_emits_start_and_end(mesh, store):
    @mesh.agent(name="async-researcher")
    async def work(x):
        await asyncio.sleep(0)
        return x + 1

    assert asyncio.run(work(1)) == 2

    events = store.query()
    assert [e.type for e in events] == ["agent_start", "agent_end"]
    assert all(e.agent == "async-researcher" for e in events)
    assert events[0].run_id == events[1].run_id


def test_async_decorator_emits_error_and_reraises(mesh, store):
    @mesh.agent(name="async-flaky")
    async def boom():
        await asyncio.sleep(0)
        raise ValueError("async kaput")

    with pytest.raises(ValueError, match="async kaput"):
        asyncio.run(boom())

    events = store.query()
    assert [e.type for e in events] == ["agent_start", "agent_error"]
    assert events[1].payload["error_type"] == "ValueError"


def test_run_context_groups_calls_under_one_run_id(mesh, store):
    @mesh.agent(name="a")
    def agent_a():
        return "a"

    @mesh.agent(name="b")
    def agent_b():
        return "b"

    with mesh.run("run-xyz") as rid:
        assert rid == "run-xyz"
        agent_a()
        agent_b()

    events = store.query()
    assert len(events) == 4
    assert {e.run_id for e in events} == {"run-xyz"}
    assert [(e.agent, e.type) for e in events] == [
        ("a", "agent_start"),
        ("a", "agent_end"),
        ("b", "agent_start"),
        ("b", "agent_end"),
    ]


def test_run_context_applies_to_async_calls(mesh, store):
    @mesh.agent(name="async-a")
    async def agent_a():
        return "a"

    async def main():
        with mesh.run("run-async"):
            await agent_a()

    asyncio.run(main())
    events = store.query()
    assert {e.run_id for e in events} == {"run-async"}


def test_calls_outside_run_get_distinct_run_ids(mesh, store):
    @mesh.agent(name="solo")
    def solo():
        return None

    solo()
    solo()

    events = store.query()
    run_ids = {e.run_id for e in events}
    assert len(events) == 4
    assert len(run_ids) == 2  # each top-level call is its own run


def test_run_id_resets_after_context(mesh, store):
    with mesh.run("run-1"):
        assert mesh.current_run_id() == "run-1"
    assert mesh.current_run_id() is None


def test_run_generates_run_id_when_omitted(mesh):
    with mesh.run() as rid:
        assert isinstance(rid, str) and rid


def test_default_mesh_uses_agentmesh_db_env(db_path):
    from agentmesh.sdk import mesh as global_mesh
    from agentmesh.store import EventStore

    @global_mesh.agent(name="env-agent")
    def work():
        return 1

    work()
    events = EventStore(db_path).query()
    assert [e.type for e in events] == ["agent_start", "agent_end"]
    assert events[0].agent == "env-agent"
