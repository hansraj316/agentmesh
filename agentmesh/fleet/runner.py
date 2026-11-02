"""The example fleet: Planner, Coder and Reviewer handing work to each other.

One run picks a goal from the backlog. The Planner splits it into tasks and
hands each one to the Coder; the Coder hands its patch to the Reviewer; the
Reviewer approves it or sends it back once, and the Coder revises. Every hop
is an AMP v0.4 handoff (``mesh.handoff``) and every model call records usage,
so the Handoffs, Agents, cost and latency views all have live data.
"""

import random
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agentmesh.fleet.backlog import GOALS
from agentmesh.fleet.llm import LLMError, ScriptedLLM
from agentmesh.sdk import Mesh
from agentmesh.store import EventStore

MAX_TASKS = 4


@dataclass
class RunSummary:
    """What one fleet run did, for the CLI and tests."""

    run_id: str
    goal: str
    tasks: List[str] = field(default_factory=list)
    done: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    sent_back: List[str] = field(default_factory=list)
    stopped_early: bool = False


def _first_line(text: str) -> str:
    lines = str(text).strip().splitlines()
    return lines[0] if lines else ""


def _handoff(mesh: Mesh, task: str, to: str, from_: str) -> Any:
    """Open an AMP v0.4 handoff from ``from_`` to ``to`` (sync context manager)."""
    return mesh.handoff(task, to, from_=from_)


def _parse_tasks(text: str) -> List[str]:
    tasks = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-*•").strip()
        # Drop "1." or "1)" numbering a model may add despite the prompt.
        head, _, rest = cleaned.partition(" ")
        if rest and head.rstrip(".)").isdigit():
            cleaned = rest.strip()
        if cleaned:
            tasks.append(cleaned)
    return tasks[:MAX_TASKS]


def _approved(verdict: str) -> bool:
    return _first_line(verdict).strip().upper().startswith("APPROVE")


class Fleet:
    """Three decorated agents bound to one store and one model."""

    def __init__(self, store: EventStore, llm: Any, stop: Optional[threading.Event] = None):
        self.mesh = Mesh(store)
        self.llm = llm
        self.stop = stop
        agent = self.mesh.agent
        self.planner = agent("planner")(self._plan)
        self.coder = agent("coder")(self._code)
        self.revise = agent("coder")(self._revise)
        self.reviewer = agent("reviewer")(self._review)
        self._summary: Optional[RunSummary] = None

    def _stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def _ask(self, role: str, prompt: str) -> str:
        result = self.llm.complete(role, prompt)
        self.mesh.record_usage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            model=result.model,
        )
        return result.text

    def run(self, goal: str) -> RunSummary:
        run_id = "fleet-" + uuid.uuid4().hex[:8]
        self._summary = RunSummary(run_id=run_id, goal=goal)
        with self.mesh.run(run_id):
            self.planner(goal)
        summary, self._summary = self._summary, None
        return summary

    def _plan(self, goal: str) -> int:
        summary = self._summary
        assert summary is not None
        summary.tasks = _parse_tasks(self._ask("planner", goal))
        for task in summary.tasks:
            if self._stopped():
                summary.stopped_early = True
                break
            try:
                with _handoff(self.mesh, task, to="coder", from_="planner"):
                    self.coder(task)
            except LLMError as exc:
                summary.failed[task] = _first_line(exc)
                continue
            summary.done.append(task)
        return len(summary.done)

    def _code(self, task: str) -> str:
        patch = self._ask("coder", task)
        for attempt in range(2):
            final = attempt == 1
            with _handoff(self.mesh, task, to="reviewer", from_="coder"):
                verdict = self.reviewer(task, patch, final)
            if final or _approved(verdict):
                return patch
            assert self._summary is not None
            self._summary.sent_back.append(task)
            with _handoff(self.mesh, task, to="coder", from_="reviewer"):
                patch = self.revise(task, verdict)
        return patch

    def _revise(self, task: str, verdict: str) -> str:
        prompt = "%s\n\nThe reviewer asked for changes:\n%s\n\nDescribe the revised patch." % (
            task,
            verdict,
        )
        return self._ask("coder", prompt)

    def _review(self, task: str, patch: str, final: bool) -> str:
        prompt = "Task: %s\n\nProposed patch:\n%s" % (task, patch)
        if final:
            prompt += (
                "\n\nFINAL REVIEW: the coder already revised this once. Approve it unless "
                "there is a serious defect."
            )
        return self._ask("reviewer", prompt)


def _seeded(llm: Any, seed: Optional[int]) -> Any:
    if llm is None:
        return ScriptedLLM(seed=seed)
    if seed is not None and hasattr(llm, "reseed"):
        llm.reseed(seed)
    return llm


def run_fleet(
    store: EventStore,
    llm: Any = None,
    seed: Optional[int] = None,
    goal: Optional[str] = None,
    stop: Optional[threading.Event] = None,
) -> RunSummary:
    """Run the fleet once and return a :class:`RunSummary`."""
    llm = _seeded(llm, seed)
    if goal is None:
        goal = random.Random(seed).choice(GOALS)
    return Fleet(store, llm, stop=stop).run(goal)


def run_once(
    store: EventStore,
    llm: Any = None,
    seed: Optional[int] = None,
    *,
    goal: Optional[str] = None,
    stop: Optional[threading.Event] = None,
) -> str:
    """Run the fleet once against ``store`` and return the run_id.

    ``llm`` defaults to a :class:`ScriptedLLM` seeded with ``seed``. A task
    whose model call fails is recorded as a ``failed`` handoff and the run
    moves on to the next task.
    """
    return run_fleet(store, llm=llm, seed=seed, goal=goal, stop=stop).run_id


def run_forever(
    store: EventStore,
    stop: threading.Event,
    interval_s: float = 3.0,
    llm: Any = None,
    on_run: Optional[Callable[[RunSummary], None]] = None,
) -> int:
    """Run the fleet back to back until ``stop`` is set; return the run count.

    A scripted model sleeping mid-call wakes as soon as ``stop`` is set, so
    shutdown is prompt. An unexpected error in one run is reported on stderr
    and the loop carries on.
    """
    llm = llm if llm is not None else ScriptedLLM()
    if hasattr(llm, "stop"):
        llm.stop = stop
    runs = 0
    rng = random.Random()
    while not stop.is_set():
        try:
            summary = run_fleet(store, llm=llm, goal=rng.choice(GOALS), stop=stop)
        except Exception as exc:  # noqa: BLE001 - keep the demo fleet alive
            print("agentmesh fleet: run failed: %s" % _first_line(exc), file=sys.stderr)
        else:
            runs += 1
            if on_run is not None:
                on_run(summary)
        stop.wait(interval_s)
    return runs
