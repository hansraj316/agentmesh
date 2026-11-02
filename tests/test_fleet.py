"""Tests for the example fleet (build plan P4). Everything runs offline: the
scripted model with zero delay, and ClaudeLLM against a fake HTTP opener."""

import argparse
import io
import json
import threading
import time
import urllib.error

import pytest

from agentmesh import cli
from agentmesh.fleet import (
    DEFAULT_CLAUDE_MODEL,
    GOALS,
    ClaudeLLM,
    LLMError,
    ScriptedLLM,
    add_cli,
    run_fleet,
    run_forever,
    run_once,
)
from agentmesh.fleet.llm import CLAUDE_API_VERSION, CLAUDE_FALLBACK_BETA
from agentmesh.fleet.runner import _parse_tasks

HANDOFF_KEYS = {"kind", "task_id", "task", "from", "to", "status"}


def quiet_llm(seed=1, **overrides):
    """Scripted model with no delay, no random failures and no send-backs."""
    options = {"delay_s": (0, 0), "failure_rate": 0.0, "review_changes_rate": 0.0}
    options.update(overrides)
    return ScriptedLLM(seed=seed, **options)


def events_for(store, run_id):
    return store.query(run_id=run_id, limit=10_000)


def handoffs(events):
    return [e for e in events if e.type == "message" and e.payload.get("kind") == "handoff"]


def hops(events):
    """(from, to, status) for every handoff event, in order."""
    return [(e.payload["from"], e.payload["to"], e.payload["status"]) for e in handoffs(events)]


def pairs_by_task_id(events):
    pairs = {}
    for e in handoffs(events):
        pairs.setdefault(e.payload["task_id"], []).append(e)
    return pairs


def test_run_once_returns_run_id_with_planner_coder_reviewer_handoffs(store):
    run_id = run_once(store, llm=quiet_llm(), seed=1)

    events = events_for(store, run_id)
    assert run_id.startswith("fleet-")
    assert all(e.run_id == run_id for e in events)
    summary_tasks = {e.payload["task"] for e in handoffs(events)}
    assert 2 <= len(summary_tasks) <= 3

    # Each task: planner -> coder opens, coder -> reviewer opens and closes,
    # then planner -> coder closes.
    per_task = [("planner", "coder", "running"), ("coder", "reviewer", "running")]
    per_task += [("coder", "reviewer", "done"), ("planner", "coder", "done")]
    assert hops(events) == per_task * len(summary_tasks)


def test_handoff_events_follow_the_contract(store):
    run_id = run_once(store, llm=quiet_llm(), seed=2)
    events = events_for(store, run_id)

    pairs = pairs_by_task_id(events)
    assert len(pairs) == len(handoffs(events)) // 2
    for task_id, (opened, closed) in pairs.items():
        assert task_id.startswith("t-")
        assert HANDOFF_KEYS <= set(opened.payload)
        assert opened.payload["status"] == "running"
        assert closed.payload["status"] == "done"
        assert isinstance(closed.payload["duration_ms"], float)
        for key in ("task", "from", "to"):
            assert opened.payload[key] == closed.payload[key]
        # The event's agent is the sender.
        assert opened.agent == opened.payload["from"]
        assert closed.agent == closed.payload["from"]


def test_spans_nest_and_every_step_records_usage(store):
    run_id = run_once(store, llm=quiet_llm(), seed=3)
    events = events_for(store, run_id)

    starts = {e.payload["span_id"]: e for e in events if e.type == "agent_start"}
    ends = [e for e in events if e.type == "agent_end"]
    assert {e.agent for e in starts.values()} == {"planner", "coder", "reviewer"}
    assert len(ends) == len(starts)

    planner_span = next(s for s, e in starts.items() if e.agent == "planner")
    assert "parent_span_id" not in starts[planner_span].payload
    for span_id, start in starts.items():
        if start.agent == "coder":
            assert start.payload["parent_span_id"] == planner_span
        if start.agent == "reviewer":
            assert starts[start.payload["parent_span_id"]].agent == "coder"

    for end in ends:
        assert end.payload["model"] == "scripted-fleet-1"
        assert end.payload["input_tokens"] > 0
        assert end.payload["output_tokens"] > 0
        assert end.payload["cost_usd"] > 0


def test_same_seed_gives_the_same_run(store):
    first = run_fleet(store, llm=ScriptedLLM(seed=7, delay_s=(0, 0)), seed=7)
    second = run_fleet(store, llm=ScriptedLLM(seed=7, delay_s=(0, 0)), seed=7)

    assert first.run_id != second.run_id
    assert (first.goal, first.tasks, first.done, first.failed, first.sent_back) == (
        second.goal,
        second.tasks,
        second.done,
        second.failed,
        second.sent_back,
    )
    assert hops(events_for(store, first.run_id)) == hops(events_for(store, second.run_id))


def test_reviewer_sends_work_back_once_then_approves(store):
    summary = run_fleet(store, llm=quiet_llm(review_changes_rate=1.0), goal=GOALS[0])
    events = events_for(store, summary.run_id)

    first_task = [e for e in handoffs(events) if e.payload["task"] == summary.tasks[0]]
    assert hops(first_task) == [
        ("planner", "coder", "running"),
        ("coder", "reviewer", "running"),
        ("coder", "reviewer", "done"),
        ("reviewer", "coder", "running"),
        ("reviewer", "coder", "done"),
        ("coder", "reviewer", "running"),
        ("coder", "reviewer", "done"),
        ("planner", "coder", "done"),
    ]
    assert summary.sent_back == summary.tasks
    assert summary.done == summary.tasks


def test_a_failed_step_produces_a_failed_handoff_and_the_run_continues(store):
    goal = "Make GitHub ingestion reliable"
    llm = quiet_llm(seed=4, fail_on="Add retry to GitHub sync")
    summary = run_fleet(store, llm=llm, goal=goal)
    events = events_for(store, summary.run_id)

    failed = [e for e in handoffs(events) if e.payload["status"] == "failed"]
    assert len(failed) == 1
    closed = failed[0]
    assert closed.payload["task"] == "Add retry to GitHub sync"
    assert (closed.payload["from"], closed.payload["to"]) == ("planner", "coder")
    assert closed.payload["error"].startswith("RateLimitError")
    assert "\n" not in closed.payload["error"]
    opened = pairs_by_task_id(events)[closed.payload["task_id"]][0]
    assert opened.payload["status"] == "running"

    errors = [e for e in events if e.type == "agent_error"]
    assert [e.agent for e in errors] == ["coder"]
    assert summary.failed == {"Add retry to GitHub sync": closed.payload["error"]}
    assert summary.done == summary.tasks[1:]


def test_random_failures_happen_for_some_seeds(store):
    failures = 0
    for seed in range(20):
        summary = run_fleet(store, llm=ScriptedLLM(seed=seed, delay_s=(0, 0)), seed=seed)
        failures += len(summary.failed)
    assert failures > 0


def test_run_forever_runs_until_stopped(store):
    stop = threading.Event()
    seen = []
    worker = threading.Thread(
        target=run_forever,
        args=(store, stop),
        kwargs={"interval_s": 0.0, "llm": quiet_llm(), "on_run": seen.append},
    )
    worker.start()
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(seen) >= 2
    assert len({s.run_id for s in seen}) == len(seen)


def test_run_forever_wakes_a_sleeping_model_on_stop(store):
    stop = threading.Event()
    slow = ScriptedLLM(seed=1, delay_s=(30, 30), failure_rate=0.0)
    worker = threading.Thread(target=run_forever, args=(store, stop), kwargs={"llm": slow})
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    stop.set()
    worker.join(5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 5


def test_parse_tasks_strips_bullets_and_numbering():
    text = "1. Add retry\n- Write tests\n\n* Update docs\n2) Ship it\nfive\nsix"
    assert _parse_tasks(text) == ["Add retry", "Write tests", "Update docs", "Ship it"]


# ClaudeLLM, against a fake opener (no network).


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(json.dumps(reply).encode("utf-8"))


def message(text, model=DEFAULT_CLAUDE_MODEL, input_tokens=120, output_tokens=40, **extra):
    data = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    data.update(extra)
    return data


def test_claude_llm_needs_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        ClaudeLLM()


def test_claude_llm_sends_a_messages_request(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("AGENTMESH_FLEET_MODEL", raising=False)
    opener = FakeOpener([message("APPROVE\nLooks good.")])
    llm = ClaudeLLM(opener=opener)

    result = llm.complete("reviewer", "Task: Add retry")

    request = opener.requests[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_method() == "POST"
    assert headers["x-api-key"] == "test-key-not-real"
    assert headers["anthropic-version"] == CLAUDE_API_VERSION
    assert headers["anthropic-beta"] == CLAUDE_FALLBACK_BETA
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == DEFAULT_CLAUDE_MODEL
    assert body["messages"] == [{"role": "user", "content": "Task: Add retry"}]
    assert "Reviewer" in body["system"]
    assert body["fallbacks"] == "default"
    for removed in ("temperature", "top_p", "top_k"):
        assert removed not in body

    assert result.text == "APPROVE\nLooks good."
    assert (result.input_tokens, result.output_tokens) == (120, 40)
    assert result.model == DEFAULT_CLAUDE_MODEL
    assert result.cost_usd == pytest.approx((120 * 2.0 + 40 * 10.0) / 1_000_000)
    assert "test-key-not-real" not in repr(llm)


def test_claude_llm_model_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("AGENTMESH_FLEET_MODEL", "claude-haiku-5-5")
    opener = FakeOpener([message("ok", model="claude-haiku-5-5")])

    result = ClaudeLLM(opener=opener).complete("coder", "Add retry")

    assert json.loads(opener.requests[0].data)["model"] == "claude-haiku-5-5"
    assert result.model == "claude-haiku-5-5"


def test_claude_llm_unknown_model_records_tokens_without_cost():
    opener = FakeOpener([message("ok", model="some-future-model")])
    result = ClaudeLLM(api_key="k", model="some-future-model", opener=opener).complete("coder", "x")
    assert result.cost_usd is None
    assert result.input_tokens == 120


def test_claude_llm_refusal_raises():
    reply = message("", stop_reason="refusal", stop_details={"category": "cyber"})
    reply["content"] = []
    llm = ClaudeLLM(api_key="k", opener=FakeOpener([reply]))
    with pytest.raises(LLMError, match="refusal, category cyber"):
        llm.complete("coder", "x")


def test_claude_llm_http_error_raises():
    error = urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages",
        429,
        "Too Many Requests",
        {},
        io.BytesIO(b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'),
    )
    llm = ClaudeLLM(api_key="k", opener=FakeOpener([error]))
    with pytest.raises(LLMError, match="429: slow down"):
        llm.complete("coder", "x")


def test_claude_llm_network_error_raises():
    llm = ClaudeLLM(api_key="k", opener=FakeOpener([urllib.error.URLError("no route")]))
    with pytest.raises(LLMError, match="unreachable"):
        llm.complete("coder", "x")


def test_fleet_runs_on_claude_llm(store):
    class ByRole:
        def __init__(self):
            self.requests = []

        def __call__(self, request, timeout=None):
            self.requests.append(request)
            system = json.loads(request.data)["system"]
            if "Planner" in system:
                text = "1. Add retry to GitHub sync\n2. Add tests for the retry path"
            elif "Reviewer" in system:
                text = "APPROVE\nFine."
            else:
                text = "Touches agentmesh/ingest_gha.py: bounded retry."
            return FakeResponse(json.dumps(message(text)).encode("utf-8"))

    opener = ByRole()
    summary = run_fleet(store, llm=ClaudeLLM(api_key="k", opener=opener), goal=GOALS[0])

    assert summary.done == ["Add retry to GitHub sync", "Add tests for the retry path"]
    assert len(opener.requests) == 5  # one plan, then code + review per task
    ends = [e for e in events_for(store, summary.run_id) if e.type == "agent_end"]
    assert {e.payload["model"] for e in ends} == {DEFAULT_CLAUDE_MODEL}


# CLI and existing consumers.


def fleet_parser():
    parser = argparse.ArgumentParser(prog="agentmesh")
    add_cli(parser.add_subparsers(dest="command", required=True))
    return parser


def test_fleet_cli_once(db_path, monkeypatch):
    import agentmesh.fleet.cli as fleet_cli

    # Keep the CLI test fast: a seeded scripted model with zero delay.
    monkeypatch.setattr(
        fleet_cli, "make_llm", lambda kind, seed=None: ScriptedLLM(seed=seed, delay_s=(0, 0))
    )
    args = fleet_parser().parse_args(["fleet", "--once", "--seed", "3", "--db", str(db_path)])
    out = io.StringIO()

    assert args.handler(args, out=out) == 0

    line = out.getvalue().strip()
    assert line.startswith("fleet-")
    assert "done" in line


def test_fleet_cli_claude_without_key_fails(db_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = fleet_parser().parse_args(["fleet", "--once", "--llm", "claude", "--db", str(db_path)])
    assert args.handler(args) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_existing_commands_read_fleet_data(db_path, store, capsys):
    run_id = run_once(store, llm=quiet_llm(), seed=5)
    capsys.readouterr()

    assert cli.main(["trace", run_id, "--db", str(db_path)]) == 0
    assert "planner" in capsys.readouterr().out
    assert cli.main(["costs", "--by", "agent", "--db", str(db_path)]) == 0
    assert "reviewer" in capsys.readouterr().out
    assert cli.main(["board", "--db", str(db_path)]) == 0
    assert "coder" in capsys.readouterr().out


def test_fleet_handoffs_fold_with_the_sdk_reader(store):
    from agentmesh.handoffs import fold, list_handoffs

    llm = quiet_llm(seed=6, fail_on="Handle 429 rate limits in ingest-gha")
    summary = run_fleet(store, llm=llm, goal="Make GitHub ingestion reliable")
    events = events_for(store, summary.run_id)

    folded = fold(events)
    assert len(folded) == len(pairs_by_task_id(events))
    assert all(h.run_id == summary.run_id for h in folded)
    assert all(h.ended_at is not None for h in folded)
    failed = [h for h in folded if h.status == "failed"]
    assert [(h.task, h.from_, h.to) for h in failed] == [
        ("Handle 429 rate limits in ingest-gha", "planner", "coder")
    ]
    assert failed[0].error.startswith("RateLimitError")
    closed = [e for e in handoffs(events) if e.payload["status"] == "failed"][0]
    assert closed.payload["error_type"] == "LLMError"

    listed = list_handoffs(store, run_id=summary.run_id, limit=100)
    assert {h.task_id for h in listed} == {h.task_id for h in folded}
