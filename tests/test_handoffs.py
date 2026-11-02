import argparse
import asyncio
from datetime import datetime, timezone

import pytest

from agentmesh import handoffs as handoffs_mod
from agentmesh.events import Event
from agentmesh.handoffs import (
    Handoff,
    add_cli,
    fold,
    format_change,
    is_handoff,
    list_handoffs,
    render_markdown,
)
from agentmesh.sdk import Mesh
from agentmesh.trace import build_trace


@pytest.fixture
def mesh(store):
    return Mesh(store=store)


def _ev(run_id, ts, payload, agent="planner", type_="message", event_id=None):
    return Event(
        event_id=event_id or "%s-%s-%s" % (run_id, ts, payload.get("status")),
        run_id=run_id,
        agent=agent,
        type=type_,
        ts=ts,
        payload=payload,
    )


def _hp(status, task_id="t-1", task="Add retry to GitHub sync", **extra):
    payload = {
        "kind": "handoff",
        "task_id": task_id,
        "task": task,
        "from": "planner",
        "to": "coder",
        "status": status,
    }
    payload.update(extra)
    return payload


def _handoff_events(store):
    return [e for e in store.query(limit=1000) if is_handoff(e)]


# --- SDK: mesh.handoff -------------------------------------------------------


def test_handoff_emits_running_then_done_with_contract_payload(mesh, store):
    with mesh.run("fleet-0007"):
        with mesh.handoff("Add retry to GitHub sync", to="coder", from_="planner") as task_id:
            pass

    events = store.query()
    assert [e.type for e in events] == ["message", "message"]
    opened, closed = (e.payload for e in events)
    assert all(e.agent == "planner" and e.run_id == "fleet-0007" for e in events)
    assert opened == {
        "kind": "handoff",
        "task_id": task_id,
        "task": "Add retry to GitHub sync",
        "from": "planner",
        "to": "coder",
        "status": "running",
    }
    assert closed["status"] == "done"
    assert closed["task_id"] == task_id
    assert closed["duration_ms"] >= 0
    assert {k: v for k, v in closed.items() if k not in ("status", "duration_ms")} == {
        k: v for k, v in opened.items() if k != "status"
    }
    assert task_id.startswith("t-")


def test_handoff_explicit_task_id_is_used(mesh, store):
    with mesh.handoff("Review PR #41", to="reviewer", from_="coder", task_id="t-3f2a") as tid:
        assert tid == "t-3f2a"
    assert {e.payload["task_id"] for e in store.query()} == {"t-3f2a"}


def test_handoff_failed_carries_first_line_of_error_and_reraises(mesh, store):
    with pytest.raises(RuntimeError, match="tests broke"):
        with mesh.handoff("Add retry", to="coder", from_="planner"):
            raise RuntimeError("tests broke\nTraceback details here")

    closed = store.query()[-1].payload
    assert closed["status"] == "failed"
    assert closed["error"] == "tests broke"
    assert closed["error_type"] == "RuntimeError"
    assert closed["duration_ms"] >= 0


def test_handoff_failed_with_empty_message_uses_exception_type(mesh, store):
    with pytest.raises(KeyError):
        with mesh.handoff("Add retry", to="coder", from_="planner"):
            raise KeyError()
    assert store.query()[-1].payload["error"] == "KeyError"


def test_handoff_inside_agent_defaults_sender_and_links_span(mesh, store):
    @mesh.agent(name="coder")
    def coder(task):
        return "patched: " + task

    @mesh.agent(name="planner")
    def planner():
        with mesh.handoff("Add retry to GitHub sync", to="coder"):
            return coder("Add retry to GitHub sync")

    with mesh.run("fleet-1"):
        assert planner() == "patched: Add retry to GitHub sync"

    events = store.query()
    planner_start = events[0]
    hand = [e for e in events if is_handoff(e)]
    assert [e.payload["status"] for e in hand] == ["running", "done"]
    assert all(e.agent == "planner" and e.payload["from"] == "planner" for e in hand)
    assert all(e.payload["span_id"] == planner_start.payload["span_id"] for e in hand)
    assert all("parent_span_id" not in e.payload for e in hand)

    # The receiver's span still nests under the sender's span: the trace is intact.
    trace = build_trace(store, "fleet-1")
    assert [s.agent for s in trace.roots] == ["planner"]
    assert [c.agent for c in trace.roots[0].children] == ["coder"]


def test_handoff_in_nested_agent_carries_parent_span_id(mesh, store):
    @mesh.agent(name="coder")
    def coder():
        with mesh.handoff("Review the patch", to="reviewer"):
            pass

    @mesh.agent(name="planner")
    def planner():
        coder()

    planner()
    events = store.query()
    planner_span = events[0].payload["span_id"]
    coder_span = events[1].payload["span_id"]
    hand = [e for e in events if is_handoff(e)]
    assert all(e.agent == "coder" for e in hand)
    assert all(e.payload["span_id"] == coder_span for e in hand)
    assert all(e.payload["parent_span_id"] == planner_span for e in hand)


def test_explicit_from_overrides_current_agent(mesh, store):
    @mesh.agent(name="router")
    def router():
        with mesh.handoff("Triage", to="coder", from_="planner"):
            pass

    router()
    hand = _handoff_events(store)
    assert all(e.agent == "planner" and e.payload["from"] == "planner" for e in hand)


def test_handoff_outside_agent_without_from_raises_and_emits_nothing(mesh, store):
    with pytest.raises(ValueError, match="from_"):
        with mesh.handoff("Add retry", to="coder"):
            pass
    assert store.query() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task": "", "to": "coder"},
        {"task": "x", "to": ""},
        {"task": "x", "to": "coder", "task_id": ""},
        {"task": "x", "to": "coder", "from_": ""},
    ],
)
def test_handoff_rejects_empty_fields(mesh, kwargs):
    with pytest.raises(ValueError):
        mesh.handoff(**kwargs)


def test_handoff_context_cannot_be_reused(mesh, store):
    handoff = mesh.handoff("Add retry", to="coder", from_="planner")
    with handoff:
        pass
    with pytest.raises(RuntimeError):
        with handoff:
            pass


def test_handoff_outside_run_shares_one_fresh_run_id(mesh, store):
    with mesh.handoff("Add retry", to="coder", from_="planner"):
        pass
    events = store.query()
    assert len({e.run_id for e in events}) == 1


def test_async_handoff_done_and_failed(mesh, store):
    @mesh.agent(name="planner")
    async def planner():
        async with mesh.handoff("Add retry", to="coder", task_id="t-a") as tid:
            await asyncio.sleep(0)
            assert tid == "t-a"
        with pytest.raises(ValueError):
            async with mesh.handoff("Review", to="reviewer", task_id="t-b"):
                await asyncio.sleep(0)
                raise ValueError("nope")

    with mesh.run("async-run"):
        asyncio.run(planner())

    folded = {h.task_id: h for h in fold(store.query(limit=100))}
    assert folded["t-a"].status == "done"
    assert folded["t-b"].status == "failed"
    assert folded["t-b"].error == "nope"
    assert folded["t-a"].from_ == "planner"


def test_concurrent_async_agents_keep_their_own_sender(mesh, store):
    async def work(name):
        @mesh.agent(name=name)
        async def agent():
            async with mesh.handoff("task from " + name, to="reviewer"):
                await asyncio.sleep(0)

        await agent()

    async def main():
        await asyncio.gather(work("alpha"), work("beta"))

    asyncio.run(main())
    for h in fold(store.query(limit=100)):
        assert h.task == "task from " + h.from_
        assert h.status == "done"


def test_handoff_events_are_valid_v03_message_events(mesh, store):
    with mesh.handoff("Add retry", to="coder", from_="planner"):
        pass
    for event in store.query():
        Event.from_dict(event.to_dict()).validate()
        assert event.type == "message"


# --- fold --------------------------------------------------------------------


def test_fold_pairs_open_and_close():
    events = [
        _ev("r1", "2026-11-02T10:00:00+00:00", _hp("running")),
        _ev("r1", "2026-11-02T10:00:23.140000+00:00", _hp("done", duration_ms=23140)),
    ]
    (h,) = fold(events)
    assert h.to_dict() == {
        "task_id": "t-1",
        "run_id": "r1",
        "task": "Add retry to GitHub sync",
        "from": "planner",
        "to": "coder",
        "status": "done",
        "started_at": "2026-11-02T10:00:00+00:00",
        "ended_at": "2026-11-02T10:00:23.140000+00:00",
        "duration_seconds": 23.14,
        "error": None,
    }


def test_fold_failed_with_error_first_line():
    events = [
        _ev("r1", "2026-11-02T10:00:00+00:00", _hp("running")),
        _ev("r1", "2026-11-02T10:00:05+00:00", _hp("failed", error="boom\nmore detail")),
    ]
    (h,) = fold(events)
    assert h.status == "failed"
    assert h.error == "boom"
    assert h.duration_seconds == 5.0  # falls back to ts difference without duration_ms


def test_fold_unmatched_open_shows_running():
    (h,) = fold([_ev("r1", "2026-11-02T10:00:00+00:00", _hp("running"))])
    assert h.status == "running"
    assert h.ended_at is None
    assert h.duration_seconds is None
    assert h.started_at == "2026-11-02T10:00:00+00:00"


def test_fold_close_without_open_back_computes_start():
    (h,) = fold([_ev("r1", "2026-11-02T10:00:10+00:00", _hp("done", duration_ms=4000))])
    assert h.status == "done"
    assert h.started_at == "2026-11-02T10:00:06+00:00"
    assert h.duration_seconds == 4.0


def test_fold_close_before_open_prefers_real_open_ts():
    events = [
        _ev("r1", "2026-11-02T10:00:10+00:00", _hp("done")),
        _ev("r1", "2026-11-02T10:00:02+00:00", _hp("running")),
    ]
    (h,) = fold(events)
    assert h.status == "done"
    assert h.started_at == "2026-11-02T10:00:02+00:00"
    assert h.duration_seconds == 8.0


def test_fold_first_close_wins():
    events = [
        _ev("r1", "2026-11-02T10:00:00+00:00", _hp("running")),
        _ev("r1", "2026-11-02T10:00:01+00:00", _hp("done", duration_ms=1000)),
        _ev("r1", "2026-11-02T10:00:02+00:00", _hp("failed", error="late")),
    ]
    (h,) = fold(events)
    assert h.status == "done"
    assert h.error is None
    assert h.ended_at == "2026-11-02T10:00:01+00:00"


def test_fold_keys_by_run_and_task_id_in_first_seen_order():
    events = [
        _ev("r1", "2026-11-02T10:00:00+00:00", _hp("running", task_id="t-1", task="A")),
        _ev("r2", "2026-11-02T10:00:01+00:00", _hp("running", task_id="t-1", task="B")),
        _ev("r1", "2026-11-02T10:00:02+00:00", _hp("running", task_id="t-2", task="C")),
        _ev("r1", "2026-11-02T10:00:03+00:00", _hp("done", task_id="t-1", task="A")),
    ]
    folded = fold(events)
    assert [(h.run_id, h.task_id, h.task, h.status) for h in folded] == [
        ("r1", "t-1", "A", "done"),
        ("r2", "t-1", "B", "running"),
        ("r1", "t-2", "C", "running"),
    ]


def test_fold_ignores_v03_events_and_malformed_handoffs_without_mutating():
    plain = [
        _ev("r1", "2026-11-02T10:00:00+00:00", {"function": "f"}, type_="agent_start"),
        _ev("r1", "2026-11-02T10:00:01+00:00", {"text": "hi"}),
        _ev("r1", "2026-11-02T10:00:02+00:00", {"kind": "annotation", "text": "deploy"}),
        _ev("r1", "2026-11-02T10:00:03+00:00", _hp("paused")),
        _ev("r1", "2026-11-02T10:00:04+00:00", {"kind": "handoff", "status": "running"}),
        _ev("r1", "2026-11-02T10:00:05+00:00", _hp("running"), type_="tool_call"),
    ]
    before = [e.to_dict() for e in plain]
    assert fold(plain) == []
    assert [e.to_dict() for e in plain] == before
    assert not any(is_handoff(e) for e in plain)


def test_fold_accepts_stream_dicts():
    stream = [
        dict(_ev("r1", "2026-11-02T10:00:00+00:00", _hp("running")).to_dict(), id=1),
        dict(_ev("r1", "2026-11-02T10:00:02+00:00", _hp("done")).to_dict(), id=2),
        {"id": 3, "type": "agent_end", "payload": {}},
    ]
    (h,) = fold(stream)
    assert h.status == "done"
    assert h.duration_seconds == 2.0


def test_fold_missing_task_text_folds_as_empty_then_fills():
    open_payload = _hp("running")
    del open_payload["task"]
    events = [
        _ev("r1", "2026-11-02T10:00:00+00:00", open_payload),
        _ev("r1", "2026-11-02T10:00:01+00:00", _hp("done", task="Filled in")),
    ]
    (h,) = fold(events)
    assert h.task == "Filled in"


def test_fold_sdk_round_trip(mesh, store):
    @mesh.agent(name="reviewer")
    def reviewer():
        raise RuntimeError("needs changes")

    @mesh.agent(name="coder")
    def coder():
        with mesh.handoff("Review PR #41", to="reviewer", task_id="t-review"):
            reviewer()

    @mesh.agent(name="planner")
    def planner():
        with mesh.handoff("Add retry to GitHub sync", to="coder", task_id="t-code"):
            coder()

    with mesh.run("fleet-0007"):
        with pytest.raises(RuntimeError):
            planner()

    folded = {h.task_id: h for h in fold(store.query(limit=100))}
    assert folded["t-code"].from_ == "planner" and folded["t-code"].to == "coder"
    assert folded["t-review"].from_ == "coder" and folded["t-review"].to == "reviewer"
    assert folded["t-code"].status == "failed"
    assert folded["t-review"].error == "needs changes"
    assert all(h.run_id == "fleet-0007" for h in folded.values())


# --- list_handoffs -----------------------------------------------------------


def _seed(store):
    for event in [
        _ev("r1", "2026-11-02T10:00:00+00:00", _hp("running", task_id="t-1", task="first")),
        _ev("r1", "2026-11-02T10:00:05+00:00", _hp("done", task_id="t-1", task="first")),
        _ev("r1", "2026-11-02T10:01:00+00:00", _hp("running", task_id="t-2", task="second")),
        _ev("r2", "2026-11-02T10:02:00+00:00", _hp("running", task_id="t-1", task="third")),
        _ev("r2", "2026-11-02T10:02:01+00:00", {"function": "f"}, type_="agent_start"),
    ]:
        store.append(event)


def test_list_handoffs_newest_first_with_run_and_limit(store):
    _seed(store)
    assert [h.task for h in list_handoffs(store)] == ["third", "second", "first"]
    assert [h.task for h in list_handoffs(store, run_id="r1")] == ["second", "first"]
    assert [h.task for h in list_handoffs(store, limit=1)] == ["third"]


def test_list_handoffs_since_keeps_still_active(store):
    _seed(store)
    tasks = [h.task for h in list_handoffs(store, since="2026-11-02T10:00:30+00:00")]
    assert tasks == ["third", "second"]  # "first" ended before the cutoff
    aware = datetime(2026, 11, 2, 10, 0, 5, tzinfo=timezone.utc)
    assert "first" in [h.task for h in list_handoffs(store, since=aware)]


def test_list_handoffs_bad_since_raises(store):
    with pytest.raises(ValueError, match="since"):
        list_handoffs(store, since="not a date")


def test_list_handoffs_empty_store(store):
    assert list_handoffs(store) == []


# --- rendering ---------------------------------------------------------------


def test_render_markdown_table():
    now = datetime(2026, 11, 2, 10, 0, 12, tzinfo=timezone.utc)
    rows = [
        Handoff(
            "t-3", "r1", "Fix | pipes", "coder", "reviewer", "running", "2026-11-02T10:00:10+00:00"
        ),
        Handoff(
            "t-2",
            "r1",
            "Review PR #41",
            "coder",
            "reviewer",
            "failed",
            "2026-11-02T10:00:00+00:00",
            "2026-11-02T10:00:03+00:00",
            3.0,
            "needs changes",
        ),
        Handoff(
            "t-1",
            "r1",
            "Add retry to GitHub sync",
            "planner",
            "coder",
            "done",
            "2026-11-02T09:58:00+00:00",
            "2026-11-02T09:59:23+00:00",
            83.0,
        ),
    ]
    assert render_markdown(rows, now=now).splitlines() == [
        "| Task | From | To | Status | Started | Duration | Run |",
        "|---|---|---|---|---|---|---|",
        "| Fix \\| pipes | coder | reviewer | 🔵 Running | 2026-11-02T10:00:10+00:00 "
        "| 2.0s so far | r1 |",
        "| Review PR #41 | coder | reviewer | 🔴 Failed: needs changes "
        "| 2026-11-02T10:00:00+00:00 | 3.0s | r1 |",
        "| Add retry to GitHub sync | planner | coder | 🟢 Done "
        "| 2026-11-02T09:58:00+00:00 | 1m 23s | r1 |",
    ]


def test_render_markdown_empty():
    assert render_markdown([]) == "No handoffs found."


def test_format_change_lines():
    opened = _ev("r1", "2026-11-02T10:00:00+00:00", _hp("running"))
    failed = _ev("r1", "2026-11-02T10:00:02+00:00", _hp("failed", duration_ms=2000, error="x\ny"))
    assert format_change(opened) == (
        "2026-11-02T10:00:00+00:00 🔵 Running planner -> coder t-1: "
        "Add retry to GitHub sync run=r1"
    )
    assert format_change(failed).endswith("(2.0s) error: x run=r1")
    assert format_change(_ev("r1", "2026-11-02T10:00:00+00:00", {"text": "hi"})) is None


# --- CLI ---------------------------------------------------------------------


def _parse(argv):
    parser = argparse.ArgumentParser(prog="agentmesh")
    add_cli(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(argv)


def test_cli_registers_handoffs_command_with_defaults():
    args = _parse(["handoffs"])
    assert args.command == "handoffs"
    assert (args.run, args.follow, args.db, args.limit) == (None, False, None, 50)
    assert args.func is handoffs_mod.run_cli
    args = _parse(["handoffs", "--run", "r1", "--follow", "--db", "x.db", "-n", "5"])
    assert (args.run, args.follow, args.db, args.limit) == ("r1", True, "x.db", 5)


def test_cli_prints_table(store, db_path, capsys):
    _seed(store)
    args = _parse(["handoffs", "--db", str(db_path), "--run", "r1"])
    assert args.func(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("| Task |")
    assert [line.split("|")[1].strip() for line in out[2:]] == ["second", "first"]


def test_cli_uses_agentmesh_db_env_when_no_db_flag(store, capsys):
    _seed(store)
    args = _parse(["handoffs"])
    assert args.func(args) == 0
    assert "third" in capsys.readouterr().out


def test_cli_empty_store(db_path, capsys):
    args = _parse(["handoffs", "--db", str(db_path)])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "No handoffs found."


def test_cli_follow_prints_changes_until_ctrl_c(store, db_path, capsys, monkeypatch):
    _seed(store)
    new_events = iter(
        [
            [_ev("r3", "2026-11-02T11:00:00+00:00", _hp("running", task_id="t-9", task="live"))],
            [
                _ev("r3", "2026-11-02T11:00:01+00:00", {"function": "f"}, type_="agent_start"),
                _ev(
                    "r3", "2026-11-02T11:00:04+00:00", _hp("done", "t-9", "live", duration_ms=4000)
                ),
            ],
        ]
    )

    def fake_sleep(_seconds):
        batch = next(new_events, None)
        if batch is None:
            raise KeyboardInterrupt
        for event in batch:
            store.append(event)

    monkeypatch.setattr(handoffs_mod.time, "sleep", fake_sleep)
    args = _parse(["handoffs", "--db", str(db_path), "--follow"])
    assert args.func(args) == 0
    out = capsys.readouterr().out.splitlines()
    follow_lines = [line for line in out if not line.startswith("|")]
    assert follow_lines == [
        "2026-11-02T11:00:00+00:00 🔵 Running planner -> coder t-9: live run=r3",
        "2026-11-02T11:00:04+00:00 🟢 Done planner -> coder t-9: live (4.0s) run=r3",
    ]
